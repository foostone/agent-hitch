"""Durable immutable SGLang process services on the selected model node.

The SSH command is only a control request. Detached supervisors own services;
workers register their PID creation time before exec, so a lost start reply or
stop racing a delayed launch cannot create an unowned engine.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from hitch_inference.device_lease import NodeDeviceLedger
from hitch_inference.runtime import observe_runtime, package_version
from hitch_inference.hardware import gpu_processes
from hitch_inference.process_identity import owned_alive, process_identity, stop_owned
from hitch_inference.content import ContentStore, atomic_json, digest_json, require
from hitch_inference.snapshots import materialize, seal_directory
from hitch_inference.state import load, lock
from .process_service import ProcessService
from .launch import runtime_matches, sglang_arguments
from .topology import selected_devices
from .observation import validate_engine_info

def service_for_directory(directory):
    directory = Path(directory)
    config = load(directory / "config.json")
    identity = load(directory / "identity.json")
    service = ProcessService(config, identity["node"])
    service.devices._read()  # Check current boot before interpreting any PID.
    return service, directory, load(directory / "request.json")


def engine(directory):
    service, directory, request = service_for_directory(directory)
    with lock(directory / "launch.lock"):
        status = load(directory / "status.json")
        handle = status["handle"]
        supervisor = {
            "pid": handle["process"]["pid"],
            "createdAt": handle["process"]["created_at"],
        }
        require(
            status["state"] == "starting"
            and not (directory / "stop.json").exists()
            and owned_alive(supervisor),
            "inference-launch-fenced",
            "supervisor is no longer authorizing this engine",
        )
        identity = process_identity(os.getpid())
        service.devices.track(service.owner(directory.name), [identity], launching=True)
        atomic_json(directory / "engine.json", identity)
        access = load(directory / "access.json")
        arguments = sglang_arguments(
            request, service.root / "models" / request["model"]["model_id"][7:], access
        )
    os.execv(sys.executable, [sys.executable, "-m", "sglang.launch_server", *arguments])


def supervisor(directory):
    service, directory, request = service_for_directory(directory)
    try:
        with lock(directory / "worker.lock", blocking=False):
            with lock(directory / "launch.lock"):
                status = load(directory / "status.json")
                if (
                    status["state"] != "starting"
                    or status.get("handle")
                    or (directory / "stop.json").exists()
                ):
                    return
                identity = process_identity(os.getpid())
                service.devices.track(service.owner(directory.name), [identity], launching=True)
                status["handle"] = {
                    "schema_version": "2",
                    "kind": "process",
                    "node_id": service.node["nodeId"],
                    "generation": service.node["generation"],
                    "service_id": directory.name,
                    "process": {
                        "pid": identity["pid"],
                        "created_at": identity["createdAt"],
                    },
                }
                atomic_json(directory / "status.json", status)
            run_engine(service, directory, request)
    except BlockingIOError:
        return


def run_engine(service, directory, request):
    process = None
    owned = {}
    error = None
    owner = service.owner(directory.name)
    try:
        actual = observe_runtime()
        runtime_matches(request["runtime"], actual)
        backend = request["lock"]["execution"]["platform"]
        environment = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "CUDA_VISIBLE_DEVICES": ",".join(selected_devices(request["lock"])),
        }
        if backend["backend"] == "cpu":
            environment.update(
                SGLANG_USE_CPU_ENGINE="1",
                OMP_NUM_THREADS=str(backend["cpu_threads"]),
                SGLANG_CPU_OMP_THREADS=str(backend["cpu_threads"]),
            )
        with lock(directory / "launch.lock"):
            if (
                load(directory / "status.json")["state"] != "starting"
                or (directory / "stop.json").exists()
            ):
                return
            with (directory / "engine.log").open("ab") as output:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "hitch_inference.worker",
                        "_engine",
                        "--directory",
                        str(directory),
                    ],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
            # Stop snapshots the ledger after taking this same launch fence.
            # Register the child before releasing it; the child's own check
            # still protects the Popen/registration window if we crash here.
            identity = process_identity(process.pid)
            require(
                identity is not None,
                "inference-engine-exited",
                "engine exited before ownership registration",
            )
            service.devices.track(owner, [identity], launching=True)
        access = load(directory / "access.json")
        deadline = time.monotonic() + request["lock"]["execution"]["startup_timeout_ms"] / 1000
        while not (directory / "stop.json").exists():
            import psutil

            try:
                parent = psutil.Process(process.pid)
                for child in [parent, *parent.children(recursive=True)]:
                    identity = process_identity(child.pid)
                    if identity:
                        owned[digest_json(identity)] = identity
            except psutil.NoSuchProcess:
                pass
            service.devices.track(owner, list(owned.values()))
            if process.poll() is not None:
                raise RuntimeError("SGLang engine exited")
            status = load(directory / "status.json")
            if status["state"] == "starting":
                require(
                    time.monotonic() < deadline,
                    "inference-start-timeout",
                    "SGLang did not become healthy within the locked timeout",
                )
                try:
                    base = "http://127.0.0.1:" + str(access["port"])
                    with urllib.request.urlopen(base + "/health", timeout=1) as response:
                        require(
                            response.status == 200,
                            "inference-not-healthy",
                            "SGLang is not healthy",
                        )
                    query = urllib.request.Request(
                        base + "/server_info",
                        headers={"Authorization": "Bearer " + access["engineToken"]},
                    )
                    with urllib.request.urlopen(query, timeout=2) as response:
                        info = json.load(response)
                    if backend["backend"] == "cuda":
                        used = gpu_processes(selected_devices(request["lock"]))
                        require(
                            {row["device"] for row in used} == set(selected_devices(request["lock"]))
                            and all(
                                row["pid"] in {p["pid"] for p in owned.values()} for row in used
                            ),
                            "inference-gpu-unconfirmed",
                            "GPU compute ownership is not confirmed for this service",
                        )
                    replica_observation = validate_engine_info(info, request["lock"], request["runtime"])
                    # Raw server_info can contain API keys. Only the private
                    # control record stores it; Hitch emits whitelisted evidence.
                    with lock(directory / "launch.lock"):
                        status = load(directory / "status.json")
                        if status["state"] == "starting" and not (directory / "stop.json").exists():
                            status.update(
                                state="ready",
                                runtime=actual,
                                serverInfo=info,
                                gpuUuid=backend.get("device_constraint"),
                                gpuUuids=selected_devices(request["lock"]),
                                replicaObservation=replica_observation,
                            )
                            atomic_json(directory / "status.json", status)
                except urllib.error.HTTPError as exc:
                    require(
                        exc.code not in (401, 403),
                        "inference-authentication-failed",
                        "SGLang rejected the service credentials",
                    )
                except (OSError, ValueError):
                    pass
            time.sleep(0.2)
    except Exception as exc:
        error = getattr(exc, "code", "inference-engine-failed")
    finally:
        with lock(directory / "launch.lock"):
            atomic_json(directory / "stop.json", {"reason": error or "owner-stop"})
            service.devices.fence(owner)
        # The engine self-registers before exec, including if its parent dies
        # between Popen and the first psutil enumeration.
        entry = service.devices.inspect(owner)
        stop_owned([identity for identity in entry["processes"] if identity["pid"] != os.getpid()])
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        with lock(directory / "launch.lock"):
            status = load(directory / "status.json")
            status.update(state="failed" if error else "stopped", resourcesReleased=False)
            if error:
                status["error"] = error
            atomic_json(directory / "status.json", status)
