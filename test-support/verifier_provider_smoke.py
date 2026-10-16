"""Load a provider-only repair without adding modules to the frozen payload."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

source, root = map(Path, sys.argv[1:])


class DockerEnvironment:
    def __init__(self, *, environment_dir, task_env_config, **kwargs):
        self._environment_docker_compose_path = environment_dir / "docker-compose.yaml"
        self.extra_docker_compose_paths = []
        self._enable_egress_control = False
        self.task_env_config = task_env_config

    async def _run_docker_compose_command(self, command, *args, **kwargs):
        return command


for name in ("harbor", "harbor.constants", "harbor.environments", "harbor.environments.docker", "harbor.environments.docker.docker"):
    sys.modules[name] = ModuleType(name)
sys.modules["harbor.constants"].MAIN_SERVICE_NAME = "main"
sys.modules["harbor.environments.docker.docker"].DockerEnvironment = DockerEnvironment
spec = importlib.util.spec_from_file_location("hitch_harbor_environment", source)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class TaskEnvironment:
    docker_image = None

    def model_copy(self, update):
        return SimpleNamespace(**update)


for session, expected in (("trial__env", "sha256:" + "a" * 64), ("trial__verifier__env", None)):
    environment = provider.HitchHarborDockerEnvironment(
        environment_dir=root, task_env_config=TaskEnvironment(), session_id=session,
        trial_paths=SimpleNamespace(trial_dir=root / "trial"), hitch_prebuilt_task_image="sha256:" + "a" * 64,
    )
    assert environment.task_env_config.docker_image == expected
    assert environment._hitch_shared_runtime is None
    assert environment._hitch_preparation is None
    assert asyncio.run(environment._run_docker_compose_command(["build"])) == ["build"]
    assert asyncio.run(environment.hitch_verify_shared_runtime(root, root)) is False
    if environment._hitch_ownership_temp_dir is not None:
        environment._hitch_ownership_temp_dir.cleanup()
assert not any(name in sys.modules for name in ("hitch_shared_runtime", "hitch_compose_preparation", "hitch_image_cache"))
print(json.dumps({"provider_only_repair": "passed"}))
