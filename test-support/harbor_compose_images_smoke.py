"""Discover the public main + simulator topology without Harbor or Docker."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

config = ModuleType("harbor.models.task.config")


class TaskConfig:
    @staticmethod
    def model_validate(value):
        environment = SimpleNamespace(cpus=1, memory_mb=128, docker_image=None,
                                      os=SimpleNamespace(value="linux"), network_mode=None)
        return SimpleNamespace(environment=environment, agent=SimpleNamespace(network_mode=None),
                               verifier=SimpleNamespace(environment_mode=None, environment=None, network_mode=None))


config.TaskConfig = TaskConfig
config.VerifierEnvironmentMode = SimpleNamespace(SEPARATE="separate")
sys.modules["harbor.models.task.config"] = config
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
from hitch_harbor_task_resources import inspect_task


class ComposeImageDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.environment = self.root / "environment"
        self.environment.mkdir()
        (self.root / "task.toml").write_text('schema_version = "1.4"\n')
        for name in ("Dockerfile", "Dockerfile.simulator"):
            (self.environment / name).write_text("FROM scratch\n")
        self.document = {"services": {
            "main": {"platform": "linux/amd64", "build": {"context": ".", "dockerfile": "Dockerfile"},
                     "depends_on": {"simulator": {"condition": "service_healthy"}}},
            "simulator": {"platform": "linux/amd64", "build": {"context": ".", "dockerfile": "Dockerfile.simulator"},
                          "healthcheck": {"test": ["CMD", "true"]}},
        }}

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self, document):
        compose = self.environment / "docker-compose.yaml"
        original = json.dumps(document)
        compose.write_text(original)
        result = inspect_task(self.root)
        self.assertEqual(compose.read_text(), original)
        return result

    def test_plain_simulator_is_prepared_beside_legacy_main(self):
        result = self.inspect(self.document)
        self.assertEqual(result["environment_image_fallbacks"], [])
        self.assertEqual(result["environment_builds"], [
            {"source": "compose", "service": "simulator", "context": "environment", "dockerfile": "Dockerfile.simulator", "platform": "linux/amd64"},
            {"source": "task", "service": "main", "context": "environment", "dockerfile": "Dockerfile"},
        ])

    def test_context_and_dockerfile_paths_cannot_escape_or_use_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Dockerfile").write_text("FROM scratch\n")
        (self.environment / "linked").symlink_to(outside, target_is_directory=True)
        for build in ({"context": "../outside"}, {"context": str(outside)}, {"context": "linked"},
                      {"context": ".", "dockerfile": "../outside/Dockerfile"},
                      {"context": ".", "dockerfile": "./Dockerfile"},
                      {"context": "${CONTEXT}", "dockerfile": "Dockerfile"}):
            with self.subTest(build=build):
                document = copy.deepcopy(self.document)
                document["services"]["simulator"]["build"] = build
                self.assert_fallback(document)

    def assert_fallback(self, document):
        result = self.inspect(document)
        self.assertEqual([build for build in result["environment_builds"] if build["source"] == "compose"], [])
        self.assertEqual(result["environment_builds"], [])
        self.assertIn({"source": "compose", "service": "simulator", "code": "backend-build"}, result["environment_image_fallbacks"])

    def test_unsupported_builds_and_alias_consumers_keep_original_compose_path(self):
        variants = [
            {"build": {"context": ".", "args": {"MODE": "release"}}},
            {"build": {"context": ".", "target": "production"}},
            {"build": {"context": ".", "additional_contexts": {"main": "service:main"}}},
            {"image": "shared:latest"}, {"pull_policy": "build"},
            {"platform": None}, {"platform": "${PLATFORM}"},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                document = copy.deepcopy(self.document)
                document["services"]["simulator"].update(changes)
                self.assert_fallback(document)
        for consumer in ({"image": "some-project-simulator"},
                         {"volumes": [{"type": "image", "source": "some-project-simulator", "target": "/input"}]}):
            with self.subTest(consumer=consumer):
                document = copy.deepcopy(self.document)
                document["services"]["consumer"] = consumer
                self.assert_fallback(document)


if __name__ == "__main__":
    unittest.main()
