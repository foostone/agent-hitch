"""Public transport boundaries: isolation, malformed mounts and legacy setup."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
import hitch_shared_runtime as shared


class SharedMountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "controller/payload").mkdir(parents=True)
        (self.root / "artifact").mkdir()
        self.raw = {"runtime_directory": str(self.root / "controller"), "artifact_directory": str(self.root / "artifact")}
        self.sources = shared.configure(self.raw, True)
        self.inspected = {"privileged": False, "cap_add": None, "mounts": [
            {"Type": "bind", "Source": source, "Destination": target, "RW": False}
            for target, source in self.sources.items()
        ]}

    def test_candidate_only(self):
        self.assertIsNone(shared.configure(None, True))
        self.assertIsNone(shared.configure(self.raw, False))
        shared.validate_mounts(self.inspected, self.sources)
        for mount in shared.compose_mounts(self.sources):
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])

    def test_non_candidates_do_not_require_snapshot_directories(self):
        raw = {key: value + "/missing" for key, value in self.raw.items()}
        self.assertIsNone(shared.configure(raw, False))
        with self.assertRaises(ValueError):
            shared.configure(raw, True)

    def test_reject_rw_wrong_missing_and_nested_mounts(self):
        for mutation in [
            lambda x: x["mounts"][0].update(RW=True),
            lambda x: x["mounts"][0].update(Source="/other"),
            lambda x: x["mounts"].pop(),
            lambda x: x["mounts"].append({"Destination": "/opt/hitch-harness-artifact/entry.js"}),
            lambda x: x.update(privileged=True),
            lambda x: x.update(cap_add=["SYS_ADMIN"]),
            lambda x: x.update(cap_add=["CAP_SYS_ADMIN"]),
        ]:
            broken = copy.deepcopy(self.inspected); mutation(broken)
            with self.assertRaises(RuntimeError):
                shared.validate_mounts(broken, self.sources)

    def test_conflicting_compose_mounts_and_privilege(self):
        for config in [
            {"volumes": ["/host:/opt/hitch:rw"]},
            {"volumes": [{"source": "x", "target": "/opt/hitch-harness-artifact/node_modules"}]},
            {"privileged": True}, {"cap_add": ["SYS_ADMIN"]}, {"cap_add": ["CAP_SYS_ADMIN"]},
        ]:
            with self.assertRaises(ValueError):
                shared.reject_conflicts(config)
        shared.reject_conflicts({"volumes": ["/host:/workspace:rw"], "cap_add": ["NET_ADMIN"]})

    def test_missing_and_symlink_sources_do_not_become_empty_mounts(self):
        broken = {**self.raw, "artifact_directory": str(self.root / "missing")}
        with self.assertRaises(ValueError):shared.configure(broken, True)
        (self.root / "link").symlink_to(self.root / "artifact")
        with self.assertRaises(ValueError):shared.configure({**self.raw, "artifact_directory": str(self.root / "link")}, True)
        with self.assertRaises(ValueError):shared.configure({**self.raw, "extra": "unexpected"}, True)

    async def test_mount_inspection_uses_the_same_docker_connection_as_compose(self):
        connection = {"DOCKER_HOST": "unix:///custom/docker.sock", "DOCKER_CONFIG": "/custom/client"}
        env = SimpleNamespace(
            _hitch_shared_runtime=self.sources, _compose_env_vars=lambda: connection,
            _run_docker_compose_command=AsyncMock(return_value=SimpleNamespace(stdout="a" * 64)),
        )
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(self.inspected).encode(), b"")))

        async def spawn(*args, **kwargs):
            self.assertEqual(kwargs.get("env"), connection, "inspection must target the daemon that created the candidate")
            self.assertEqual(args[-1], "a" * 64)
            return process

        with patch.object(shared.asyncio, "create_subprocess_exec", spawn):
            self.assertTrue(await shared.verify_environment(env, self.root / "controller/payload", self.root / "artifact"))


if __name__ == "__main__":
    unittest.main()
