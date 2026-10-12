"""Read-only, evaluation-local installation snapshots for Harbor main services.

This changes transport, not content authentication. The bridge and in-container
Hitch keep their existing pinned manifest and payload checks.
"""
from __future__ import annotations

import asyncio
import json
import posixpath
import re
from pathlib import Path
from typing import Any

CONTROLLER_TARGET = "/opt/hitch"
ARTIFACT_TARGET = "/opt/hitch-harness-artifact"
TARGETS = (CONTROLLER_TARGET, ARTIFACT_TARGET)


def configure(raw: Any, candidate: bool) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"runtime_directory", "artifact_directory"}:
        raise ValueError("shared runtime transport metadata is invalid")
    for value in raw.values():
        if not isinstance(value, str) or not Path(value).is_absolute() or any(ord(c) < 32 for c in value):
            raise ValueError("shared runtime transport requires absolute host paths")
    # Harbor also forwards environment kwargs to separate verifier containers.
    # Only the candidate session receives harness code.
    if not candidate:
        return None
    result = {
        CONTROLLER_TARGET: str(Path(raw["runtime_directory"]) / "payload"),
        ARTIFACT_TARGET: raw["artifact_directory"],
    }
    for source in result.values():
        if not Path(source).is_dir() or Path(source).is_symlink():
            raise ValueError("shared runtime snapshot must be an existing regular directory")
    return result


def reject_conflicts(services: dict[str, Any]) -> None:
    """Validate resolved services before exposing any shared installation."""
    # Volume inheritance can copy main's mounts into sidecars transitively,
    # or import unknown external mounts into main. A read-only declaration
    # cannot protect a mount inherited by a privileged consumer. Require
    # explicit mounts throughout this composition instead of guessing which
    # container aliases or inheritance chains remain isolated.
    if any(service.get("volumes_from") for service in services.values()):
        raise ValueError("shared runtime does not support Compose volume inheritance")
    # A sidecar can also join main's PID namespace and enter its mount
    # namespace. Apply the privilege boundary to the entire composition.
    for service in services.values():
        if service.get("privileged") or any(str(cap).upper().removeprefix("CAP_") in {"ALL", "SYS_ADMIN"} for cap in (service.get("cap_add") or [])):
            raise ValueError("shared runtime requires unprivileged services without CAP_SYS_ADMIN")
    config = services["main"]
    for mount in config.get("volumes") or []:
        if isinstance(mount, dict):
            target = mount.get("target")
        elif isinstance(mount, str):
            parts = mount.split(":")
            target = parts[1] if len(parts) > 1 else parts[0]
        else:
            raise ValueError("shared runtime cannot validate candidate mounts")
        if not isinstance(target, str):
            raise ValueError("shared runtime cannot validate candidate mount target")
        target = posixpath.normpath(target)
        if any(target == reserved or target.startswith(reserved + "/") for reserved in TARGETS):
            raise ValueError("task mount conflicts with a shared runtime installation")


def compose_mounts(sources: dict[str, str]) -> list[dict[str, Any]]:
    return [{"type": "bind", "source": source.replace("$", "$$"), "target": target,
             "read_only": True, "bind": {"create_host_path": False}}
            for target, source in sources.items()]


def validate_mounts(inspected: Any, expected: dict[str, str]) -> None:
    if not isinstance(inspected, dict) or not isinstance(inspected.get("mounts"), list):
        raise RuntimeError("shared runtime Docker mount inspection is invalid")
    if inspected.get("privileged") or any(str(c).upper().removeprefix("CAP_") in {"ALL", "SYS_ADMIN"} for c in (inspected.get("cap_add") or [])):
        raise RuntimeError("shared runtime requires an unprivileged candidate without CAP_SYS_ADMIN")
    found = {}
    for mount in inspected["mounts"]:
        target = mount.get("Destination", "")
        if any(target.startswith(reserved + "/") for reserved in TARGETS):
            raise RuntimeError("nested mount shadows a shared runtime installation")
        if target in expected:
            if target in found or mount.get("Type") != "bind" or mount.get("RW") is not False:
                raise RuntimeError("shared runtime installation is not a unique read-only bind mount")
            if mount.get("Source") != expected[target]:
                raise RuntimeError("shared runtime bind source does not match its job")
            found[target] = mount
    if set(found) != set(expected):
        raise RuntimeError("shared runtime installation mount is missing")


async def verify_environment(environment: Any, controller_payload: Path, artifact_directory: Path) -> bool:
    expected = environment._hitch_shared_runtime
    if expected is None:
        return False
    if expected != {CONTROLLER_TARGET: str(controller_payload), ARTIFACT_TARGET: str(artifact_directory)}:
        raise RuntimeError("shared runtime environment and agent handoff disagree")
    result = await environment._run_docker_compose_command(["ps", "--all", "--quiet", "main"])
    identity = result.stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise RuntimeError("shared runtime requires exactly one candidate container")
    template = '{"mounts":{{json .Mounts}},"privileged":{{json .HostConfig.Privileged}},"cap_add":{{json .HostConfig.CapAdd}}}'
    process = await asyncio.create_subprocess_exec(
        "docker", "inspect", "--format", template, identity,
        env=environment._compose_env_vars(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0 or len(stdout) > 1024 * 1024:
        raise RuntimeError("shared runtime Docker mount inspection failed")
    validate_mounts(json.loads(stdout), expected)
    return True
