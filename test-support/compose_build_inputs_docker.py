"""Real Compose defaults and ignored unreadable inputs, without models or pulls."""
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/harbor"))
from hitch_compose_preparation import ComposePreparation


class Result(SimpleNamespace):
    def model_copy(self, *, update):
        return Result(**{**vars(self), **update})


async def main(root, base_image):
    root = Path(root).resolve()
    context = root / "external-context"
    project = root / "project"
    context.mkdir(parents=True)
    project.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY value /value\n")
    (context / "value").write_text(uuid.uuid4().hex)
    (context / ".dockerignore").write_text("ignored\n")
    prefix = "hci-" + uuid.uuid4().hex[:10]
    tag = prefix + ":local"
    environment = dict(os.environ)
    for key in ("DOCKER_DEFAULT_PLATFORM", "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE"):
        environment.pop(key, None)
    environment["COMPOSE_BAKE"] = "false"
    compose = project / "compose.yaml"
    dotenv = project / ".env"
    service = {"build": {"context": str(context)}, "image": tag}

    def definition():
        compose.write_text(json.dumps({"services": {"main": service}}))

    async def command(args, check=True):
        process = await asyncio.create_subprocess_exec("docker", *args, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        result = Result(stdout=stdout.decode(), stderr=stderr.decode(), return_code=process.returncode)
        if check and process.returncode:
            raise RuntimeError(result.stderr)
        return result

    async def run(args, check=True):
        return await command(["compose", "--project-name", prefix, "--project-directory", str(project),
                              "-f", str(compose), *args], check)

    trial = root / "trial"
    trial.mkdir()
    env = SimpleNamespace(_compose_env_vars=lambda: environment, session_id=prefix,
        trial_paths=SimpleNamespace(trial_dir=trial), logger=logging.getLogger("fixture"))
    cache = root / "cache"
    native_images = set()
    preparations = []
    results = {"platforms": [], "conflicts": [], "ignored": []}

    def preparation():
        instance = ComposePreparation(env, str(cache))
        preparations.append(instance)
        return instance

    async def native_build():
        await run(["build"])
        info = json.loads((await command(["image", "inspect", "--format", "{{json .}}", tag])).stdout)
        native_images.add(info["Id"])
        return f"{info['Os']}/{info['Architecture']}"

    try:
        definition()
        # .env is outside the context: only the effective platform can separate
        # these otherwise identical cache inputs, including the warm repeat.
        keys = []
        for platform in ("linux/arm64", "linux/amd64", "linux/arm64"):
            dotenv.write_text(f"DOCKER_DEFAULT_PLATFORM={platform}\n")
            assert await native_build() == platform
            prep = preparation()
            await prep.build(run)
            assert prep.images, prep.receipts
            observed = await command(["image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", prep.images["main"]])
            assert observed.stdout.strip() == platform
            keys.append(prep.receipts[0]["cache_key"])
            results["platforms"].append(prep.receipts[0])
        assert keys[0] != keys[1] and keys[0] == keys[2]
        assert results["platforms"][-1]["cache_hit"]

        service["platform"] = "linux/amd64"
        definition()
        for exported in (False, True):
            if exported:
                dotenv.unlink()
                environment["DOCKER_DEFAULT_PLATFORM"] = "linux/arm64"
            assert (await run(["build"], check=False)).return_code != 0
            prep = preparation()
            assert await prep.build(run) is None
            assert prep.receipts == [{"event": "fallback", "reason": "compose-build-plan"}]
            results["conflicts"].append({"exported": exported, "fallback": True})
        environment.pop("DOCKER_DEFAULT_PLATFORM", None)

        # Actual permissions matter here; root can read mode 000 and does not
        # encounter this failure. Unit tests also inject EACCES on every host.
        if os.geteuid() != 0:
            ignored = context / "ignored"
            for directory in (False, True):
                if directory:
                    ignored.mkdir()
                else:
                    ignored.write_text("excluded from Docker's context")
                ignored.chmod(0)
                try:
                    assert await native_build() == "linux/amd64"
                    prep = preparation()
                    assert await prep.build(run) is None
                    assert prep.receipts == [{"event": "fallback", "reason": "context-read"}]
                    assert not prep.images and prep.path is None
                    assert not list(cache.glob(".context-*"))
                    results["ignored"].append({"directory": directory, "fallback": True})
                finally:
                    ignored.chmod(0o700)
                    if directory:
                        ignored.rmdir()
                    else:
                        ignored.unlink()
        # Docker's directory COPY preserves links. An independent snapshot
        # must not silently split them or alias an unlinked context's key.
        os.link(context / "value", context / "other")
        (context / "Dockerfile").write_text(f"FROM {base_image}\nCOPY . /data\nRUN test /data/value -ef /data/other\n")
        assert await native_build() == "linux/amd64"
        prep = preparation()
        assert await prep.build(run) is None
        assert prep.receipts == [{"event": "fallback", "reason": "context-hardlinks"}]
        results["hardlinks"] = {"native_passed": True, "fallback": True}
        (context / "other").unlink()

        # Git metadata may live outside the build context, so copying its
        # bytes into an unrelated cache directory loses requested labels.
        repository = root / "repository"
        repository.mkdir()
        context = context.rename(repository / "context")
        (context / "Dockerfile").write_text("FROM scratch\nCOPY value /value\n")
        def git(*args):
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", *args], cwd=repository,
                text=True, capture_output=True, check=True)
            return result.stdout.strip()
        git("init")
        git("remote", "add", "origin", "https://example.invalid/fixture.git")
        git("add", ".")
        git("commit", "-m", "fixture")
        service["build"]["context"] = str(context)
        definition()
        environment.update(COMPOSE_BAKE="true", BUILDX_GIT_LABELS="full")
        assert await native_build() == "linux/amd64"
        labels = json.loads((await command(["image", "inspect", "--format", "{{json .Config.Labels}}", tag])).stdout)
        assert labels["org.opencontainers.image.revision"] == git("rev-parse", "HEAD"), labels
        assert "fixture.git" in labels["org.opencontainers.image.source"], labels
        prep = preparation()
        assert await prep.build(run) is None
        assert prep.receipts == [{"event": "fallback", "reason": "git-build-metadata"}]
        results["git_labels"] = {"native_labels": labels, "fallback": True}
        (root / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print("Compose build inputs: dotenv platforms, warm identity, native conflicts and ignored input fallback OK", flush=True)
        print("context metadata: native hardlinks and Git-derived labels preserved by fallback OK", flush=True)
        if os.geteuid() == 0:
            print("unreadable input cases skipped: root can read mode 000", flush=True)
    finally:
        for prep in preparations:
            if prep.temporary:
                prep.temporary.cleanup()
        await command(["image", "rm", tag], check=False)
        for record in cache.glob("image-*.json"):
            await command(["image", "rm", json.loads(record.read_text())["tag"]], check=False)
        for identity in native_images:
            await command(["image", "rm", identity], check=False)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
