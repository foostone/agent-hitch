"""Preparation preserves native Compose image selection and pull policies.

A loopback-only registry serves a tiny synthetic image; no credentials,
external registry, runnable image or daemon configuration change is needed.
"""
import asyncio
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from hitch_harbor_environment import HitchHarborDockerEnvironment
from hitch_image_cache import docker


def checksum(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def registry_documents(architecture):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        entry = tarfile.TarInfo("value")
        entry.size, entry.mode = len(b"remote"), 0o644
        archive.addfile(entry, io.BytesIO(b"remote"))
    config = json.dumps({"architecture": architecture, "os": "linux", "config": {"Cmd": ["/unused"]},
                         "rootfs": {"type": "layers", "diff_ids": [checksum(layer.getvalue())]},
                         "history": [{"created_by": "Hitch loopback registry fixture"}]}).encode()
    compressed = gzip.compress(layer.getvalue(), mtime=0)
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": len(config), "digest": checksum(config)},
        "layers": [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip", "size": len(compressed), "digest": checksum(compressed)}]}).encode()
    return manifest, {checksum(config): config, checksum(compressed): compressed}


async def main(root):
    root = Path(root).resolve()
    process_env = dict(os.environ)
    info = json.loads(await docker(["info", "--format", "{{json .}}"], env=process_env))
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(info["Architecture"], info["Architecture"])
    manifest, blobs = registry_documents(architecture)
    requests = []

    class Registry(BaseHTTPRequestHandler):
        # Docker probes HTTPS first. A malformed TLS request must receive an
        # HTTP status line, not Python's HTTP/0.9 bare error body, so Docker
        # recognizes an HTTP registry and retries insecure loopback correctly.
        default_request_version = "HTTP/1.1"

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def respond(self, body):
            requests.append(self.path)
            if self.path.rstrip("/") == "/v2":
                data, mime = b"{}", "application/json"
            elif "/manifests/" in self.path:
                data, mime = manifest, "application/vnd.docker.distribution.manifest.v2+json"
            elif "/blobs/" in self.path and self.path.rsplit("/", 1)[-1] in blobs:
                data, mime = blobs[self.path.rsplit("/", 1)[-1]], "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Docker-Content-Digest", checksum(data))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = root / "context"
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text('FROM scratch\nCOPY value /value\nCMD ["/unused"]\n')
    (context / "value").write_text("local")
    prefix = "hitch-refresh-" + uuid.uuid4().hex[:12]
    tag = f"127.0.0.1:{server.server_port}/{prefix}:latest"
    cache = root / "cache"
    results, failures = [], []
    try:
        for policy in (None, "missing", "if_not_present", "always"):
            service = {"build": {"context": "."}, "image": tag}
            if policy is not None:
                service["pull_policy"] = policy
            (context / "docker-compose.yaml").write_text(json.dumps({"services": {"main": service}}))
            control = None
            for attempt, cached in enumerate((False, True, True)):
                name = prefix + f"-{policy or 'default'}-{attempt}"
                paths = TrialPaths(root / name)
                paths.mkdir()
                environment = HitchHarborDockerEnvironment(
                    environment_dir=context, environment_name=name, session_id=name, trial_paths=paths,
                    task_env_config=EnvironmentConfig(cpus=1, memory_mb=512), mounts=[],
                    hitch_image_cache_dir=str(cache) if cached else None,
                )
                try:
                    await environment._run_docker_compose_command(["build"])
                    requests.clear()
                    # Only image selection is under test: the synthetic image
                    # has no shell or task agent to start.
                    up = await environment._run_docker_compose_command(["up", "--no-start"])
                    container = (await environment._run_docker_compose_command(["ps", "--all", "--quiet", "main"])).stdout.strip()
                    output = root / f"{name}-value"
                    await docker(["cp", f"{container}:/value", str(output)], env=process_env)
                    observed = output.read_text()
                    pulled = any("/manifests/" in request for request in requests)
                    row = {"policy": policy, "cached": cached, "attempt": attempt, "value": observed, "pulled": pulled}
                    results.append(row)
                    if not cached:
                        control = (observed, pulled)
                    # Compose versions differ in whether up refreshes an
                    # already-built latest tag under missing. Preserve the
                    # actual native selection, not an assumed documentation
                    # interpretation. Always proves the registry is usable.
                    assert (observed, pulled) == control, row
                    if policy == "always":
                        assert observed == "remote" and pulled, (row, up.stdout)
                    if cached and policy is not None:
                        assert environment._hitch_preparation.receipts == [{"event": "fallback", "reason": "refreshable-build-image"}]
                        assert environment._hitch_preparation.path is None
                    elif cached:
                        # Harbor defaults builds to pull_policy: build.
                        assert environment._hitch_preparation.receipts[0]["event"] == "image"
                        if attempt == 2:
                            assert environment._hitch_preparation.receipts[0]["cache_hit"]
                except Exception as error:
                    failures.append({"case": name, "error": str(error)})
                finally:
                    await environment._run_docker_compose_command(["down", "--volumes", "--remove-orphans"])
                    prep = environment._hitch_preparation
                    if prep is not None and prep.temporary is not None:
                        prep.temporary.cleanup()
        evidence = {"results": results, "failures": failures}
        (root / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        assert not failures, json.dumps(evidence, indent=2)
        print("Compose pull policy: native, cold and repeated image selection, registry refresh and build-cache reuse OK", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        await docker(["image", "rm", tag], env=process_env, check=False)
        for record in cache.glob("image-*.json"):
            await docker(["image", "rm", json.loads(record.read_text())["tag"]], env=process_env, check=False)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
