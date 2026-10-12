"""Host-local, cross-process preparation cache for resolved Compose builds."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from hitch_build_inputs import UnsupportedBuild, build_spec, digest, pinned_dockerfile, remove_snapshot, snapshot_context

_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")
_LOCK_FDS = ContextVar("hitch_build_lock_fds", default=())


async def docker(args, *, env, timeout=600, check=True):
    """Keep command lifetime and cache locks tied to the worker's liveness."""
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-I", "-B", str(Path(__file__).with_name("hitch_build_process.py")), "docker", *args,
        env=env, start_new_session=True, pass_fds=_LOCK_FDS.get(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    process = communication = None
    try:
        # Do not lose ownership if cancellation arrives during process creation.
        process = await asyncio.shield(spawning)
        communication = asyncio.create_task(communicate(process))
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except BaseException:
        async def stop():
            owned = process if process is not None else await spawning
            owned.stdin.close()
            await (communication if communication is not None else communicate(owned))

        cleanup = asyncio.create_task(stop())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Repeated cancellation cannot release image/slot locks early.
                pass
        cleanup.result()
        raise
    finally:
        if process is not None:
            process.stdin.close()
    if check and process.returncode:
        # Do not surface build args, proxy credentials or registry stderr.
        raise RuntimeError(f"Hitch image preparation Docker {args[0]} failed ({process.returncode})")
    return stdout.decode("utf-8", errors="strict") if process.returncode == 0 else None


async def communicate(process):
    stdout, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
    try:
        # Unlike Process.communicate(), keep stdin open while Docker runs. The
        # supervisor also waits for descendants holding either output pipe.
        process.stdin.write(b"1")
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    await process.wait()
    return stdout, stderr


@asynccontextmanager
async def lock(paths):
    handles = []
    acquired = None
    token = None
    try:
        for path in paths:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handles.append(os.fdopen(fd, "a"))
        while acquired is None:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = handle
                    break
                except BlockingIOError:
                    pass
            if acquired is None:
                await asyncio.sleep(0.05)
        token = _LOCK_FDS.set((*_LOCK_FDS.get(), acquired.fileno()))
        yield
    finally:
        if token is not None:
            _LOCK_FDS.reset(token)
        for handle in handles:
            handle.close()


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".record-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class ImagePreparationCache:
    def __init__(self, directory, env, slots=2):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or self.directory.resolve() != self.directory:
            raise ValueError("Hitch image cache requires an absolute non-symlink directory")
        if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 64:
            raise ValueError("Hitch image build slots must be between 1 and 64")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_uid != os.getuid() or self.directory.stat().st_mode & 0o022:
            raise ValueError("Hitch image cache must be owned by this user and not writable by others")
        self.env, self.slots, self.daemon = env, slots, None

    async def identity(self):
        # Source policies can deny or remap even digest-pinned base inputs.
        # Their files and external policy dependencies are outside this key.
        if self.env.get("EXPERIMENTAL_BUILDKIT_SOURCE_POLICY") or self.env.get("BUILDX_DEFAULT_POLICY"):
            raise UnsupportedBuild("source-policy")
        if self.daemon is None:
            raw = await docker(["info", "--format", "{{json .}}"], env=self.env, timeout=30)
            data = json.loads(raw)
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(data.get("Architecture"), data.get("Architecture"))
            if not data.get("ID") or data.get("OSType") != "linux":
                raise UnsupportedBuild("daemon-platform")
            name = self.env.get("BUILDX_BUILDER")
            if not name:
                inspected = await docker(["buildx", "inspect"], env=self.env, timeout=30)
                match = re.search(r"(?m)^Name:\s+(\S+)", inspected)
                name = match[1] if match else None
            # Human inspect output iterates driver-option maps in random order.
            # Use structured data and discard activity, status and GC counters.
            listing = await docker(["buildx", "ls", "--format", "{{json .}}"], env=self.env, timeout=30)
            builders = [json.loads(line) for line in listing.splitlines() if line.strip()]
            selected = next((entry for entry in builders if entry.get("Name") == name), None)
            if not selected or not selected.get("Driver") or not selected.get("Nodes"):
                raise UnsupportedBuild("builder-identity")
            nodes = []
            for node in selected["Nodes"]:
                normalized = {key: node.get(key) for key in ("Name", "Endpoint", "DriverOpts", "Flags", "Version", "ProxyConfig", "Labels")}
                normalized.update({key: sorted(node.get(key) or []) for key in ("Platforms", "IDs")})
                nodes.append(normalized)
            builder = {"name": name, "driver": selected["Driver"], "nodes": sorted(nodes, key=lambda n: str(n["Name"]))}
            config = Path(self.env.get("DOCKER_CONFIG") or str(Path(self.env.get("HOME") or Path.home()) / ".docker")) / "config.json"
            try:
                proxies = json.loads(config.read_text()).get("proxies", {})
            except FileNotFoundError:
                proxies = {}
            except (OSError, ValueError, AttributeError) as error:
                raise UnsupportedBuild("docker-client-config") from error
            self.daemon = {"id": data["ID"], "platform": f"linux/{architecture}", "builder": digest(builder), "proxy_config": digest(proxies)}
        return self.daemon

    async def inspect(self, image):
        raw = await docker(["image", "inspect", "--format", "{{json .}}", image], env=self.env, timeout=30, check=False)
        return json.loads(raw) if raw else None

    async def check_bases(self, bases, platform):
        # Digest-pinned base definitions can contain inherited ONBUILD triggers.
        # Inspect the immutable config once; a registry error falls back to Harbor.
        for base in bases:
            key = digest(["base-v1", base, platform])
            path = self.directory / f"base-{key}.json"
            async with lock([self.directory / f"base-{key}.lock"]):
                cached = read_json(path)
                if cached == {"base": base, "platform": platform, "no_onbuild": True}:
                    continue
                info = await self.inspect(base)
                if info is None or f"{info.get('Os')}/{info.get('Architecture')}" != platform:
                    try:
                        raw = await docker(["buildx", "imagetools", "inspect", base, "--format", "{{json .Image}}"], env=self.env, timeout=30)
                        info = json.loads(raw)
                        info = info.get(platform, info)
                        config = info.get("config")
                        if not isinstance(config, dict) or info.get("os") != "linux" or f"linux/{info.get('architecture')}" != platform:
                            raise UnsupportedBuild("base-platform")
                    except (RuntimeError, asyncio.TimeoutError, ValueError, AttributeError) as error:
                        raise UnsupportedBuild("base-config-unavailable") from error
                else:
                    config = info.get("Config", {})
                if config.get("OnBuild") or config.get("onbuild"):
                    raise UnsupportedBuild("inherited-onbuild")
                atomic_json(path, {"base": base, "platform": platform, "no_onbuild": True})

    async def prepare(self, service):
        started = time.monotonic()
        phases = {}
        daemon = await self.identity()
        source, spec = build_spec(service, self.env.get("DOCKER_DEFAULT_PLATFORM") or daemon["platform"])
        if self.directory.is_relative_to(source):
            raise UnsupportedBuild("cache-inside-context")
        temporary = Path(tempfile.mkdtemp(dir=self.directory, prefix=".context-"))
        try:
            context = temporary / "context"
            phase = time.monotonic()
            context_digest = await snapshot_context(source, context)
            phases["snapshot_seconds"] = time.monotonic() - phase
            bases = pinned_dockerfile(context / spec["dockerfile"])
            phase = time.monotonic()
            await self.check_bases(bases, spec["platform"])
            phases["base_check_seconds"] = time.monotonic() - phase
            implicit = {name: self.env.get(name) for name in (
                "BUILDX_BUILDER", "BUILDKIT_HOST", "SOURCE_DATE_EPOCH",
                *_PROXY_NAMES, *(name.lower() for name in _PROXY_NAMES),
            )}
            implicit.update({name: value for name, value in self.env.items() if name.startswith(("BUILDX_", "BUILDKIT_"))})
            key = digest({"schema": 1, "context": context_digest, "build": spec, "daemon": daemon,
                          "implicit": implicit})
            record = self.directory / f"image-{key}.json"
            tag = f"hitch-prepared:{digest(str(self.directory))[:16]}-{key}"
            phase = time.monotonic()
            async with lock([self.directory / f"image-{key}.lock"]):
                phases["key_lock_wait_seconds"] = time.monotonic() - phase
                cached = read_json(record)
                if isinstance(cached, dict) and cached.get("key") == key and _IMAGE_ID.fullmatch(str(cached.get("image"))):
                    phase = time.monotonic()
                    info = await self.inspect(cached["image"])
                    phases["probe_seconds"] = time.monotonic() - phase
                    if self.valid_image(info, key, spec["platform"]):
                        return {"image": info["Id"], "cache_key": key, "cache_hit": True, "seconds": time.monotonic() - started, "phases": phases}
                slots = [self.directory / f"slot-{digest(daemon['id'])}-{index}.lock" for index in range(self.slots)]
                phase = time.monotonic()
                async with lock(slots):
                    phases["build_slot_wait_seconds"] = time.monotonic() - phase
                    args = ["buildx", "build", "--load", "--progress", "plain", "--platform", spec["platform"],
                            "--file", str(context / spec["dockerfile"]), "--tag", tag]
                    if spec["target"]:
                        args.extend(["--target", spec["target"]])
                    for key_name, value in sorted(spec["args"].items()):
                        args.extend(["--build-arg", f"{key_name}={value}"])
                    for key_name, value in sorted(spec["labels"].items()):
                        args.extend(["--label", f"{key_name}={value}"])
                    args.extend(["--label", f"io.hitch.prepared-key={key}", str(context)])
                    phase = time.monotonic()
                    await docker(args, env=self.env, timeout=None)
                    phases["build_seconds"] = time.monotonic() - phase
                    info = await self.inspect(tag)
                    if not self.valid_image(info, key, spec["platform"]):
                        raise RuntimeError("Hitch prepared image identity/platform mismatch")
                    atomic_json(record, {"key": key, "image": info["Id"], "platform": spec["platform"], "tag": tag})
                    return {"image": info["Id"], "cache_key": key, "cache_hit": False, "seconds": time.monotonic() - started, "phases": phases}
        finally:
            remove_snapshot(temporary)

    @staticmethod
    def valid_image(info, key, platform):
        return (isinstance(info, dict) and _IMAGE_ID.fullmatch(str(info.get("Id")))
                and f"{info.get('Os')}/{info.get('Architecture')}" == platform
                and (info.get("Config", {}).get("Labels") or {}).get("io.hitch.prepared-key") == key)
