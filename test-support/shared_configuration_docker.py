"""Real Compose interpolation and inheritance checks before shared mounts."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from hitch_harbor_environment import HitchHarborDockerEnvironment
from hitch_image_cache import docker


async def main(root, image):
    root = Path(root).resolve()
    (root / "runtime/payload").mkdir(parents=True)
    (root / "artifact").mkdir()
    for name in ("HITCH_SHARED_FIXTURE_PRIVILEGED", "HITCH_SHARED_FIXTURE_CAPABILITY"):
        os.environ.pop(name, None)
    prefix = "hitch-shared-config-" + uuid.uuid4().hex[:12]
    environments, results = [], []
    try:
        # Sharing checks the fully resolved task configuration before mounting
        # host installations, including interpolated and inherited settings.
        shared_cases = [
            ({"privileged": "false"}, True),
            ({"privileged": "${HITCH_SHARED_FIXTURE_PRIVILEGED:-false}"}, True),
            ({"privileged": "false", "inherited": True}, True),
            ({"privileged": "${HITCH_SHARED_FIXTURE_PRIVILEGED:-true}"}, False),
            ({"cap_add": ["${HITCH_SHARED_FIXTURE_CAPABILITY:-SYS_ADMIN}"]}, False),
            ({"volumes": ["/unused-task-volume:/opt/hitch"], "inherited": True}, False),
            ({"sidecars": {"worker": {"volumes_from": ["main"], "privileged": True}}}, False),
            ({"sidecars": {"worker": {"volumes_from": ["main:ro"]}}}, False),
            ({"sidecars": {"relay": {"volumes_from": ["main"]},
                           "worker": {"volumes_from": ["relay"], "cap_add": ["SYS_ADMIN"]}}}, False),
            ({"sidecars": {"worker": {"privileged": True}}}, False),
            ({"sidecars": {"worker": {"pid": "service:main", "cap_add": ["SYS_ADMIN"]}}}, False),
            ({"sidecars": {"worker": {"cap_add": ["NET_ADMIN"]}}}, True),
        ]
        for index, (definition, allowed) in enumerate(shared_cases):
            context = root / f"shared-{index}"; context.mkdir()
            sidecars = definition.pop("sidecars", {})
            if definition.pop("inherited", False):
                (context / "template.yaml").write_text(json.dumps({"services": {"base": definition}}))
                definition = {"extends": {"file": "template.yaml", "service": "base"}}
            services = {"main": definition}
            for name, config in sidecars.items():
                services[name] = {"image": image, "command": ["sh", "-c", "sleep infinity"], **config}
            (context / "docker-compose.yaml").write_text(json.dumps({"services": services}))
            trial = prefix + f"-shared-{index}"
            paths = TrialPaths(root / trial); paths.mkdir()
            shared = HitchHarborDockerEnvironment(
                environment_dir=context, environment_name=trial, session_id=trial + "__env", trial_paths=paths,
                task_env_config=EnvironmentConfig(docker_image=image, cpus=1, memory_mb=512), mounts=[],
                hitch_shared_runtime={"runtime_directory": str(root / "runtime"), "artifact_directory": str(root / "artifact")},
            )
            environments.append(shared)
            if allowed:
                await shared.start(False)
                assert await shared.hitch_verify_shared_runtime(root / "runtime/payload", root / "artifact")
                assert (await shared.exec("touch /opt/hitch/forbidden")).return_code != 0
                await shared._run_docker_compose_command(["down", "--timeout", "1", "--volumes"])
            else:
                try:
                    await shared.start(False)
                except ValueError:
                    pass
                else:
                    raise AssertionError("unsafe resolved configuration reached container startup")
                assert not ((await shared._run_docker_compose_command(["ps", "--all", "--quiet"])).stdout or "").strip()
            await shared.stop(False); environments.remove(shared)
            assert await docker(["image", "inspect", "--format", "{{.Id}}", image], env=dict(os.environ)), "prebuilt image was removed"
            results.append({"case": index, "allowed": allowed, "status": "passed"})
        (root / "result.json").write_text(json.dumps(results, indent=2))
        print("shared configuration: resolved false values, inherited settings, unsafe privileges and mounts OK", flush=True)
        print("volume inheritance: direct, readonly and transitive sidecar sharing refused before startup OK", flush=True)
        print("sidecar privileges: privileged and SYS_ADMIN refused, unrelated capability allowed OK", flush=True)
    finally:
        for environment in environments:
            try:
                await environment._run_docker_compose_command(["down", "--timeout", "1", "--volumes"])
                await environment.stop(False)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
