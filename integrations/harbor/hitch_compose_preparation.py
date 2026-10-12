"""Prepare immutable images and Harbor's own keepalive without pooling tasks."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import yaml

from hitch_build_inputs import UnsupportedBuild, build_spec, digest, pinned_dockerfile
from hitch_image_cache import ImagePreparationCache, atomic_json, docker

_DEFAULT_COMMAND = ["sh", "-c", "sleep infinity"]
_MANAGED_COMMAND = ["sh", "-c", 'trap \'kill "$child"; wait "$child"; exit 0\' TERM INT; sleep infinity & child=$!; wait "$child"']
_LIFECYCLE_FIELDS = ("init", "stop_signal", "stop_grace_period", "pre_stop")


def compose_document(output):
    # Harbor merges stderr into stdout. Benign Compose interpolation warnings
    # may precede the document; only accept a complete trailing JSON object.
    lines = output.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                document = json.loads("".join(lines[index:]))
                if isinstance(document, dict):
                    return document
            except ValueError:
                pass
    raise UnsupportedBuild("compose-config-output")


def compose_services(output):
    services = compose_document(output).get("services")
    if not isinstance(services, dict):
        raise UnsupportedBuild("compose-config-output")
    return services


def resolved_build_platforms(builds, output):
    # The project .env participates in build defaults without appearing in
    # service config or the parent's process environment. Ask Compose itself.
    targets = compose_document(output).get("target")
    if not isinstance(targets, dict) or set(targets) != set(builds):
        raise UnsupportedBuild("compose-build-plan")
    resolved = {}
    for name, service in builds.items():
        target = targets[name]
        if not isinstance(target, dict):
            raise UnsupportedBuild("compose-build-plan")
        platforms = target.get("platforms", [])
        if not isinstance(platforms, list) or len(platforms) > 1 or any(p not in ("linux/amd64", "linux/arm64") for p in platforms):
            raise UnsupportedBuild("compose-build-platform")
        resolved[name] = {**service, "platform": platforms[0]} if platforms else service
    return resolved


def image_tag_key(reference):
    # Compare Compose-resolved names using Docker's familiar-name defaults.
    # Digest references are not mutable build tags. Keep their identity intact.
    if "@" in reference:
        return reference
    registry, separator, repository = reference.partition("/")
    if not separator or (registry != "localhost" and "." not in registry
                         and ":" not in registry and registry == registry.lower()):
        registry, repository = "docker.io", reference
    if registry == "index.docker.io":
        registry = "docker.io"
    if registry == "docker.io" and "/" not in repository:
        repository = "library/" + repository
    if ":" not in repository.rsplit("/", 1)[-1]:
        repository += ":latest"
    return f"{registry}/{repository}"


def require_independent_build_images(services):
    references = {}
    unnamed_build = False
    image_only = False
    for service in services.values():
        builds = bool(service.get("build"))
        reference = service.get("image")
        if reference:
            references.setdefault(image_tag_key(reference), []).append(builds)
        elif builds:
            unnamed_build = True
        for mount in service.get("volumes") or []:
            if isinstance(mount, dict) and mount.get("type") == "image":
                source = mount.get("source")
                if not isinstance(source, str) or not source:
                    raise UnsupportedBuild("image-mount-source")
                references.setdefault(image_tag_key(source), []).append(False)
        if not builds:
            image_only = True
    # Cached builds publish private tags, not Compose's shared mutable names.
    # Leave tag publication, pull policy and competing producers to Compose.
    if any(len(uses) > 1 and any(uses) for uses in references.values()):
        raise UnsupportedBuild("shared-build-image")
    # Any explicitly named service can consume or overwrite a generated tag,
    # including another builder. Do not guess Compose's project-name rules.
    # All-unnamed builders remain independent: service names are unique.
    if unnamed_build and (references or image_only):
        raise UnsupportedBuild("implicit-build-image")


class ComposePreparation:
    def __init__(self, environment, cache_directory=None, build_slots=2, managed_keepalive=False):
        self.environment = environment
        self.cache_directory, self.build_slots = cache_directory, build_slots
        self.managed_keepalive = managed_keepalive
        self.images = {}
        self.overlay = {}
        self.path = None
        self.temporary = None
        self.receipts = []
        self.force_build = False
        self.runtime_prepared = False

    def reset(self, force_build):
        if self.temporary is not None:
            self.temporary.cleanup()
        self.images, self.overlay, self.receipts = {}, {}, []
        self.path, self.temporary = None, None
        self.runtime_prepared, self.force_build = False, force_build
        if force_build and self.cache_directory:
            self.record({"event": "bypass", "reason": "force-build"})

    async def build(self, run):
        if not self.cache_directory or self.force_build:
            return None
        env = self.environment
        cache = ImagePreparationCache(self.cache_directory, env._compose_env_vars(), self.build_slots)
        result = await run(["config", "--format", "json"])
        try:
            services = compose_services(result.stdout)
            builds = {name: value for name, value in services.items() if value.get("build")}
            if not builds:
                return result.model_copy(update={"stdout": "", "stderr": ""})
            # A composition with unsupported inputs uses its original build path
            # as a whole, including inter-service dependencies and extra contexts.
            require_independent_build_images(services)
            plan = await run(["build", "--print"], check=False)
            if plan.return_code != 0:
                # Older Compose versions and conflicting defaults retain their
                # native build behavior, including the original error handling.
                raise UnsupportedBuild("compose-build-plan")
            builds = resolved_build_platforms(builds, plan.stdout)
            daemon = await cache.identity()
            for service in builds.values():
                context, spec = build_spec(service, cache.env.get("DOCKER_DEFAULT_PLATFORM") or daemon["platform"])
                pinned_dockerfile(context / spec["dockerfile"])
            prepared = {name: await cache.prepare(value) for name, value in builds.items()}
        except UnsupportedBuild as error:
            self.record({"event": "fallback", "reason": str(error)})
            return None
        for name, receipt in prepared.items():
            self.images[name] = receipt["image"]
            self.overlay[name] = {"image": receipt["image"], "build": "__RESET__", "pull_policy": "never"}
            self.record({"event": "image", "service": name, **receipt})
        if prepared:
            self.write_overlay()
        return result.model_copy(update={"stdout": "", "stderr": ""})

    async def runtime(self, run):
        if self.runtime_prepared or not self.managed_keepalive:
            return
        self.runtime_prepared = True
        env = self.environment
        if getattr(env, "_is_windows_container", False) or self.custom_keepalive():
            return
        result = await run(["config", "--format", "json"])
        try:
            service = compose_services(result.stdout)["main"]
        except UnsupportedBuild:
            return
        if (service.get("command") != _DEFAULT_COMMAND or service.get("entrypoint")
                or any(key in service for key in _LIFECYCLE_FIELDS)):
            return
        reference = service.get("image") or env._main_image_name
        policy = service.get("pull_policy")
        # A successful local inspection says nothing about a replacement image
        # pulled or rebuilt by up. Compose also refreshes latest under missing.
        if (policy not in (None, "never", "missing", "if_not_present")
                or (policy != "never" and image_tag_key(reference).endswith(":latest"))):
            return
        raw = await docker(["image", "inspect", "--format", "{{json .}}", reference],
                           env=env._compose_env_vars(), timeout=30, check=False)
        if not raw:
            return
        image = json.loads(raw)
        config = image.get("Config", {})
        stop_signal = service.get("stop_signal", config.get("StopSignal") or "SIGTERM")
        if image.get("Os") != "linux" or config.get("Entrypoint") or stop_signal not in ("SIGTERM", "SIGINT", "15", "2"):
            return
        self.overlay.setdefault("main", {})["command"] = _MANAGED_COMMAND
        self.write_overlay()
        self.record({"event": "managed-keepalive", "service": "main"})

    def custom_keepalive(self):
        env = self.environment
        for source in [env._environment_docker_compose_path, *env.extra_docker_compose_paths]:
            if source.exists():
                try:
                    document = yaml.safe_load(source.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    # Compose supports tags such as !reset and !override that
                    # PyYAML does not. Leave their interpretation to Compose.
                    return True
                if document is None:
                    continue
                if not isinstance(document, dict) or "include" in document:
                    return True
                services = document.get("services") or {}
                if not isinstance(services, dict):
                    return True
                main = services.get("main") or {}
                # An extended/included definition may explicitly choose even
                # the default command; we cannot attribute it to Harbor.
                if not isinstance(main, dict) or any(key in main for key in ("extends", "command", "entrypoint", *_LIFECYCLE_FIELDS)):
                    return True
        return False

    def write_overlay(self):
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="hitch-compose-prepared-")
            self.path = Path(self.temporary.name) / "compose.yaml"
        # Compose interpolates '$' even in JSON/YAML list-form commands.
        content = json.dumps({"services": self.overlay}, sort_keys=True).replace("$", "$$")
        self.path.write_text(content.replace('"build": "__RESET__"', '"build": !reset null'))

    def record(self, value):
        self.receipts.append(value)
        env = self.environment
        env.logger.info("Hitch image preparation: %s", json.dumps(value, sort_keys=True))
        # Host-only evidence; do not expose context paths/args/credentials or
        # mount this cache into candidates, sidecars, or verifier containers.
        directory = env.trial_paths.trial_dir
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / f"hitch-preparation-{digest(env.session_id)[:16]}.json", {
            "schema_version": "1", "session_id": env.session_id, "events": self.receipts,
        })
