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


async def main(root):
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
    evidence = {"results": results, "failures": failures}
    (root / "result.json").write_text(json.dumps(evidence, indent=2))
    assert not failures, json.dumps(evidence, indent=2)
    print("Compose image reuse: native control, cold cache, stale tag and qualified alias OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
