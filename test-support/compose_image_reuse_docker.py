"""Real Harbor/Compose regression for services consuming another build's tag.

FROM scratch and container creation keep this check offline and model-free.
"""
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


async def scenario(root, prefix, *, cached, stale, qualified):
    root.mkdir(parents=True)
    context = root / "context"
    context.mkdir()
    (context / "Dockerfile").write_text('FROM scratch\nCOPY value /value\nCMD ["/unused"]\n')
    tag = f"{prefix}:local"
    consumer_tag = f"docker.io/library/{tag}" if qualified else tag
    (context / "docker-compose.yaml").write_text(json.dumps({"services": {
        "main": {"build": {"context": "."}, "image": tag},
        "worker": {"image": consumer_tag, "pull_policy": "never", "command": ["/unused"]},
    }}))
    paths = TrialPaths(root / "trial")
    paths.mkdir()
    cache = root / "cache"
    environment = HitchHarborDockerEnvironment(
        environment_dir=context, environment_name=prefix, session_id=prefix,
        trial_paths=paths, task_env_config=EnvironmentConfig(cpus=1, memory_mb=512),
        mounts=[], hitch_image_cache_dir=str(cache) if cached else None,
    )
    process_env = dict(os.environ)
    try:
        if stale:
            (context / "value").write_text("stale")
            await docker(["build", "--tag", tag, str(context)], env=process_env)
        else:
            assert await docker(["image", "inspect", tag], env=process_env, check=False) is None
        (context / "value").write_text("current")
        await environment._run_docker_compose_command(["build"])
        await environment._run_docker_compose_command(["create", "--no-build", "--pull", "never"])
        image_ids = []
        for service in ("main", "worker"):
            container = (await environment._run_docker_compose_command(["ps", "--all", "--quiet", service])).stdout.strip()
            assert container, service
            info = json.loads(await docker(["inspect", "--format", "{{json .}}", container], env=process_env))
            image_ids.append(info["Image"])
            output = root / f"{service}-value"
            await docker(["cp", f"{container}:/value", str(output)], env=process_env)
            assert output.read_text() == "current", f"{service} consumed stale contents"
        assert image_ids[0] == image_ids[1], image_ids
        preparation = environment._hitch_preparation
        if cached:
            assert preparation.receipts == [{"event": "fallback", "reason": "shared-build-image"}]
            assert preparation.images == {} and preparation.path is None
            assert not list(cache.glob("image-*.json"))
        return {"cached": cached, "stale": stale, "qualified": qualified, "image": image_ids[0]}
    finally:
        try:
            await environment._run_docker_compose_command(["down", "--volumes", "--remove-orphans"])
        finally:
            await docker(["image", "rm", tag], env=process_env, check=False)
            for record in cache.glob("image-*.json"):
                await docker(["image", "rm", json.loads(record.read_text())["tag"]], env=process_env, check=False)


async def implicit_collision(root, prefix, *, cached, implicit_service, qualified=False):
    root.mkdir(parents=True)
    tag = f"{prefix}-{implicit_service}:latest"
    services = {}
    for name in ("main", "worker"):
        context = root / name
        context.mkdir()
        (context / "Dockerfile").write_text('FROM scratch\nCOPY value /value\nCMD ["/unused"]\n')
        (context / "value").write_text(name)
        services[name] = {"build": {"context": f"./{name}"}, "command": ["/unused"]}
        if name != implicit_service:
            services[name]["image"] = f"docker.io/library/{tag}" if qualified else tag
    (root / "docker-compose.yaml").write_text(json.dumps({"services": services}))
    paths = TrialPaths(root / "trial")
    paths.mkdir()
    cache = root / "cache"
    environment = HitchHarborDockerEnvironment(
        environment_dir=root, environment_name=prefix, session_id=prefix,
        trial_paths=paths, task_env_config=EnvironmentConfig(cpus=1, memory_mb=512),
        mounts=[], hitch_image_cache_dir=str(cache) if cached else None,
    )
    process_env = dict(os.environ)
    try:
        await environment._run_docker_compose_command(["build"])
        await environment._run_docker_compose_command(["create", "--no-build", "--pull", "never"])
        images, contents = [], []
        for name in services:
            container = (await environment._run_docker_compose_command(["ps", "--all", "--quiet", name])).stdout.strip()
            info = json.loads(await docker(["inspect", "--format", "{{json .}}", container], env=process_env))
            images.append(info["Image"])
            output = root / f"{name}-value"
            await docker(["cp", f"{container}:/value", str(output)], env=process_env)
            contents.append(output.read_text())
        # Compose controls which producer publishes last. Regardless of that
        # order, both consumers must see the same image and filesystem.
        assert len(set(images)) == 1 and len(set(contents)) == 1, {"images": images, "contents": contents}
        assert contents[0] in services
        if cached:
            prep = environment._hitch_preparation
            assert prep.receipts == [{"event": "fallback", "reason": "implicit-build-image"}]
            assert prep.path is None and not list(cache.glob("image-*.json"))
        return {"cached": cached, "implicit_service": implicit_service, "qualified": qualified,
                "images": images, "contents": contents}
    finally:
        try:
            await environment._run_docker_compose_command(["down", "--volumes", "--remove-orphans"])
        finally:
            await docker(["image", "rm", tag], env=process_env, check=False)
            for record in cache.glob("image-*.json"):
                await docker(["image", "rm", json.loads(record.read_text())["tag"]], env=process_env, check=False)


async def main(root):
    # Bake can race two exports to the same tag even with Compose's parallel
    # limit set. Exercise native sequential publication in both test arms.
    os.environ.update(COMPOSE_BAKE="false", COMPOSE_PARALLEL_LIMIT="1")
    root = Path(root).resolve()
    prefix = "hitch-compose-reuse-" + uuid.uuid4().hex[:12]
    results = []
    failures = []
    for cached, stale, qualified in [(False, False, False), (True, False, False),
                                     (True, True, False), (True, False, True)]:
        name = f"cache-{int(cached)}-stale-{int(stale)}-qualified-{int(qualified)}"
        try:
            results.append(await scenario(root / name, prefix + "-" + name,
                                          cached=cached, stale=stale, qualified=qualified))
        except Exception as error:
            failures.append({"case": name, "error": str(error)})
    for implicit_service in ("main", "worker"):
        for cached, qualified in ((False, False), (True, False), (True, True)):
            name = f"implicit-{implicit_service}-cache-{int(cached)}-qualified-{int(qualified)}"
            try:
                results.append(await implicit_collision(root / name, prefix + "-" + name,
                                                       cached=cached, implicit_service=implicit_service, qualified=qualified))
            except Exception as error:
                failures.append({"case": name, "error": str(error)})
    evidence = {"results": results, "failures": failures}
    (root / "result.json").write_text(json.dumps(evidence, indent=2))
    assert not failures, json.dumps(evidence, indent=2)
    print("Compose image reuse: explicit and implicit collisions, native controls, cold cache, stale tags and aliases OK", flush=True)


async def image_mounts(root, runner_image):
    os.environ.update(COMPOSE_BAKE="false", COMPOSE_PARALLEL_LIMIT="1")
    root = Path(root).resolve()
    results = []
    for cached, stale, qualified in ((False, False, False), (True, False, False),
                                     (True, True, False), (True, False, True)):
        case = root / f"{int(cached)}-{int(stale)}-{int(qualified)}"
        context = case / "context"
        context.mkdir(parents=True)
        (context / "Dockerfile").write_text('FROM scratch\nCOPY value /value\nCMD ["/unused"]\n')
        # Docker's overlayfs image-mount key encodes the reference into one
        # filename; leave room for Docker Hub's fully qualified spelling.
        prefix = "hmi-" + uuid.uuid4().hex[:10]
        tag = prefix + ":local"
        mounted = "docker.io/library/" + tag if qualified else tag
        (context / "docker-compose.yaml").write_text(json.dumps({"services": {
            "main": {"build": {"context": "."}, "image": tag},
            "reader": {"image": runner_image, "volumes": [{"type": "image", "source": mounted, "target": "/data"}],
                       "command": ["node", "-e", "console.log('mount-value=' + require('node:fs').readFileSync('/data/value','utf8'))"]},
        }}))
        paths = TrialPaths(case / "trial"); paths.mkdir()
        cache = case / "cache"
        environment = HitchHarborDockerEnvironment(
            environment_dir=context, environment_name=prefix, session_id=prefix, trial_paths=paths,
            task_env_config=EnvironmentConfig(cpus=1, memory_mb=512), mounts=[],
            hitch_image_cache_dir=str(cache) if cached else None,
        )
        process_env = dict(os.environ)
        try:
            if stale:
                (context / "value").write_text("stale")
                await docker(["build", "--tag", tag, str(context)], env=process_env)
            (context / "value").write_text("current")
            await environment._run_docker_compose_command(["build"])
            if cached:
                assert environment._hitch_preparation.receipts == [{"event": "fallback", "reason": "shared-build-image"}]
                assert not list(cache.glob("image-*.json"))
            result = await environment._run_docker_compose_command(["run", "--rm", "--no-deps", "--pull", "never", "reader"])
            assert "mount-value=current" in result.stdout, result.stdout
            assert "mount-value=stale" not in result.stdout
            results.append({"cached": cached, "stale": stale, "qualified": qualified, "mounted_contents": "current"})
        finally:
            try:
                await environment._run_docker_compose_command(["down", "--volumes", "--remove-orphans"])
            finally:
                await docker(["image", "rm", tag], env=process_env, check=False)
                for record in cache.glob("image-*.json"):
                    await docker(["image", "rm", json.loads(record.read_text())["tag"]], env=process_env, check=False)
    (root / "result.json").write_text(json.dumps(results, indent=2))
    print("Compose image mounts: native, cold, stale and aliased data images OK", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        asyncio.run(image_mounts(*sys.argv[1:]))
    else:
        asyncio.run(main(sys.argv[1]))
