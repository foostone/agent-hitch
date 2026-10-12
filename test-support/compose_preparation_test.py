"""Compose-level preparation preserves build sharing and task lifecycle choices."""
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
from hitch_image_cache import ImagePreparationCache
from hitch_build_inputs import UnsupportedBuild
from hitch_compose_preparation import ComposePreparation, _DEFAULT_COMMAND, compose_services, require_independent_build_images

BASE = "example.test/base@sha256:" + "a" * 64


class ComposeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.env = SimpleNamespace(_environment_docker_compose_path=self.root / "compose.yaml", extra_docker_compose_paths=[],
            _compose_env_vars=lambda: {}, _main_image_name="sha256:" + "c" * 64,
            logger=logging.getLogger("test"), trial_paths=SimpleNamespace(trial_dir=self.root / "trial"), session_id="case__verifier__one")
        self.spec = {"services": {"main": {"command": _DEFAULT_COMMAND, "image": self.env._main_image_name}}}
        self.image = {"Os": "linux", "Config": {}}
        self.prep = ComposePreparation(self.env, managed_keepalive=True)

    def tearDown(self):
        if self.prep.temporary: self.prep.temporary.cleanup()
        self.temp.cleanup()

    async def compose_call(self, args):
        self.assertEqual(args, ["config", "--format", "json"])
        return SimpleNamespace(stdout=json.dumps(self.spec))

    async def docker(self, args, **kwargs):
        return json.dumps(self.image)

    async def test_only_harbor_keepalive_is_managed(self):
        with patch("hitch_compose_preparation.docker", self.docker):
            await self.prep.runtime(self.compose_call)
        self.assertIn('$$child', self.prep.path.read_text())
        self.assertEqual(self.prep.receipts[-1]["event"], "managed-keepalive")

    async def test_custom_runtime_declarations_and_entrypoints_are_preserved(self):
        for key in ("command", "entrypoint", "init", "stop_signal", "stop_grace_period", "pre_stop"):
            for content in (json.dumps({"services": {"main": {key: None}}}), f"services:\n  main:\n    {key}: null\n"):
                with self.subTest(key=key, content=content):
                    self.env._environment_docker_compose_path.write_text(content)
                    candidate = ComposePreparation(self.env, managed_keepalive=True)
                    await candidate.runtime(self.compose_call)
                    self.assertIsNone(candidate.path)
        self.env._environment_docker_compose_path.unlink()
        for config in ({"Entrypoint": ["/custom"]}, {"StopSignal": "SIGQUIT"}):
            self.image["Config"] = config
            candidate = ComposePreparation(self.env, managed_keepalive=True)
            with patch("hitch_compose_preparation.docker", self.docker): await candidate.runtime(self.compose_call)
            self.assertIsNone(candidate.path)

    async def test_force_build_bypasses_cache(self):
        self.prep.cache_directory = str(self.root / "cache")
        self.prep.force_build = True
        async def forbidden(*args): raise AssertionError("cache must not inspect force-build inputs")
        self.assertIsNone(await self.prep.build(forbidden))

    async def test_image_only_composition_does_not_require_a_builder(self):
        result = SimpleNamespace(stdout=json.dumps(self.spec), model_copy=lambda **_: "no-builds")
        prep = ComposePreparation(self.env, str(self.root / "cache"))
        with patch.object(ImagePreparationCache, "identity", new_callable=AsyncMock,
                          side_effect=RuntimeError("builder unavailable")) as identity:
            self.assertEqual(await prep.build(AsyncMock(return_value=result)), "no-builds")
            identity.assert_not_awaited()
        self.assertEqual(prep.receipts, [])
        self.assertIsNone(prep.path)

    async def test_image_mounts_participate_in_build_tag_dependencies(self):
        for built, source in (("app:local", "app:local"), ("app", "docker.io/library/app:latest"),
                              (None, "project-main:latest")):
            for consumer in ("main", "worker"):
                with self.subTest(built=built, source=source, consumer=consumer):
                    main = {"build": {"context": str(self.root)}}
                    if built:
                        main["image"] = built
                    services = {"main": main, "worker": {"image": "reader:local"}}
                    services[consumer]["volumes"] = [{"type": "image", "source": source, "target": "/data"}]
                    self.spec = {"services": services}
                    prep = ComposePreparation(self.env, str(self.root / "cache"))
                    with patch.object(ImagePreparationCache, "identity", side_effect=AssertionError("must fall back before preparing")):
                        self.assertIsNone(await prep.build(self.compose_call))
                    self.assertIsNone(prep.path)
        require_independent_build_images({"main": {"build": {"context": str(self.root)}, "image": "independent:local",
            "volumes": [{"type": "image", "source": "external-data:local", "target": "/data"}]}})

    async def test_compose_tags_and_indirect_lifecycle_declarations_are_preserved(self):
        for content in (
            "services:\n  main:\n    command: !override [sh, -c, sleep infinity]\n",
            "services:\n  main:\n    command: !reset null\n",
            "include: [lifecycle.yaml]\n",
            "services:\n  main:\n    extends: {file: lifecycle.yaml, service: main}\n",
        ):
            with self.subTest(content=content):
                self.env._environment_docker_compose_path.write_text(content)
                prep = ComposePreparation(self.env, managed_keepalive=True)
                run = AsyncMock(side_effect=AssertionError("custom lifecycle must be left to Compose"))
                await prep.runtime(run)
                run.assert_not_awaited()
                self.assertIsNone(prep.path)

    async def test_effective_lifecycle_controls_are_preserved(self):
        for key, value in (("init", True), ("stop_signal", "SIGTERM"),
                           ("stop_grace_period", "30s"), ("pre_stop", [{"command": "cleanup"}])):
            with self.subTest(key=key):
                self.spec["services"]["main"][key] = value
                prep = ComposePreparation(self.env, managed_keepalive=True)
                try:
                    with patch("hitch_compose_preparation.docker", self.docker):
                        await prep.runtime(self.compose_call)
                    self.assertIsNone(prep.path)
                finally:
                    if prep.temporary:
                        prep.temporary.cleanup()
                    del self.spec["services"]["main"][key]

    async def test_shared_build_tags_fall_back_before_preparation(self):
        cases = [
            ("app:local", "app:local"),
            ("app", "app:latest"),
            ("app:local", "docker.io/library/app:local"),
            ("index.docker.io/app", "library/app:latest"),
            ("team/app", "docker.io/team/app:latest"),
            ("localhost:5000/team/app", "localhost:5000/team/app:latest"),
            ("[::1]:5000/app", "[::1]:5000/app:latest"),
            (None, "project-main:latest"),
        ]
        for built, consumed in cases:
            for consumer_build in (False, True):
                with self.subTest(built=built, consumed=consumed, consumer_build=consumer_build):
                    producer = {"build": {"context": str(self.root)}}
                    if built is not None:
                        producer["image"] = built
                    consumer = {"image": consumed, "pull_policy": "never"}
                    if consumer_build:
                        consumer["build"] = {"context": str(self.root / "other")}
                    # Include an unrelated build first: fallback must cover the
                    # entire composition without publishing a partial overlay.
                    self.spec = {"services": {
                        "unrelated": {"build": {"context": str(self.root)}, "image": "other:local"},
                        "main": producer, "worker": consumer,
                    }}
                    original = copy.deepcopy(self.spec)
                    prep = ComposePreparation(self.env, str(self.root / "cache"))
                    with patch.object(ImagePreparationCache, "identity", new_callable=AsyncMock,
                                      side_effect=AssertionError("shared-image composition reached cache probing")) as identity:
                        self.assertIsNone(await prep.build(self.compose_call))
                        identity.assert_not_awaited()
                    reason = "implicit-build-image" if built is None else "shared-build-image"
                    self.assertEqual(prep.receipts, [{"event": "fallback", "reason": reason}])
                    self.assertEqual(prep.images, {})
                    self.assertEqual(prep.overlay, {})
                    self.assertIsNone(prep.path)
                    self.assertEqual(self.spec, original)

    async def test_unrelated_image_services_keep_preparation(self):
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        producer = {"build": {"context": str(self.root)}, "image": "app:local"}
        receipt = {"image": "sha256:" + "a" * 64, "cache_hit": True}
        for image in ("app:other", "another:local", "example.test/app:local", "team/app:local", BASE):
            with self.subTest(image=image):
                self.spec = {"services": {"main": producer, "worker": {"image": image}}}
                original = copy.deepcopy(self.spec)
                result = SimpleNamespace(stdout=json.dumps(self.spec), model_copy=lambda **_: "prepared")
                prep = ComposePreparation(self.env, str(self.root / "cache"))
                try:
                    with patch.object(ImagePreparationCache, "identity", new_callable=AsyncMock,
                                      return_value={"platform": "linux/amd64"}), \
                         patch.object(ImagePreparationCache, "prepare", new_callable=AsyncMock, return_value=receipt) as prepare:
                        self.assertEqual(await prep.build(AsyncMock(return_value=result)), "prepared")
                        prepare.assert_awaited_once_with(producer)
                    self.assertEqual(set(prep.overlay), {"main"})
                    self.assertEqual(self.spec, original)
                finally:
                    if prep.temporary:
                        prep.temporary.cleanup()

    async def test_independent_unnamed_builds_keep_preparation(self):
        services = {}
        for name in ("main", "worker"):
            context = self.root / name
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\n")
            services[name] = {"build": {"context": str(context)}}
        result = SimpleNamespace(stdout=json.dumps({"services": services}), model_copy=lambda **_: "prepared")
        prep = ComposePreparation(self.env, str(self.root / "cache"))
        try:
            with patch.object(ImagePreparationCache, "identity", new_callable=AsyncMock, return_value={"platform": "linux/amd64"}), \
                 patch.object(ImagePreparationCache, "prepare", new_callable=AsyncMock,
                              return_value={"image": "sha256:" + "a" * 64, "cache_hit": True}) as prepare:
                self.assertEqual(await prep.build(AsyncMock(return_value=result)), "prepared")
                self.assertEqual(prepare.await_count, 2)
            self.assertEqual(set(prep.images), set(services))
        finally:
            if prep.temporary:
                prep.temporary.cleanup()

    async def test_compose_warnings_do_not_break_json_or_change_services(self):
        self.assertEqual(compose_services('warning: ${UNSET} defaults to empty\n' + json.dumps(self.spec)), self.spec['services'])
        with self.assertRaises(UnsupportedBuild): compose_services('warning only')
        with self.assertRaises(UnsupportedBuild): compose_services(json.dumps(self.spec) + '\n{other invalid output}')


if __name__ == "__main__": unittest.main(verbosity=2)
