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
from .preparation import ModelPreparation
from hitch_inference.state import load, lock
from .launch import runtime_matches
from .topology import selected_devices, service_port

class ProcessService:
    def __init__(self, config, node):
        self.config, self.node = config, node
        self.root = Path(config["nodeRoot"]) / "inference"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = ContentStore(config["storeRoot"])
        self.devices = NodeDeviceLedger(config["nodeRoot"], node)
        self.preparation = ModelPreparation(config, node)

    def directory(self, payload):
        service_id = payload.get("serviceId")
        require(
            isinstance(service_id, str)
            and re.fullmatch(r"inference_[a-f0-9]{32}", service_id)
            and isinstance(payload.get("ownerId"), str)
            and payload["ownerId"],
            "invalid-inference-owner",
            "service ID and owner ID are required",
        )
        return self.root / service_id

    def owner(self, service_id):
        return "inference/" + service_id

    def identity(self, directory, payload):
        identity = load(directory / "identity.json")
        require(
            identity
            and identity["node"] == self.node
            and identity["ownerId"] == payload["ownerId"],
            "inference-owner-drift",
            "service belongs to another owner or node generation",
        )
        return identity

    def admit(self, payload):
        model, frozen, runtime = payload["model"], payload["lock"], payload["runtime"]
        require(
            model.get("schema_version") == "1" and model.get("format") == "hf-safetensors",
            "invalid-model-manifest",
            "expected immutable HF model manifest",
        )
        keys = (
            "format",
            "files",
            "architecture",
            "model_type",
            "dtype",
            "quantization",
            "context_tokens",
            "tokenizer_digest",
            "template_digest",
        )
        require(
            model["model_id"] == digest_json({key: model[key] for key in keys}),
            "model-identity-drift",
            "model manifest identity differs",
        )
        require(
            frozen["inference_id"]
            == digest_json({k: v for k, v in frozen.items() if k != "inference_id"})
            and frozen["model_id"] == model["model_id"]
            and frozen["runtime_id"] == runtime["runtime_id"],
            "inference-lock-drift",
            "inference lock does not identify the model/runtime",
        )
        execution = frozen["execution"]
        require(
            frozen.get("schema_version") in ("1", "2", "3")
            and frozen.get("engine") == "sglang"
            and execution["platform"]["backend"] == runtime["backend"]
            and execution["data_parallel_size"] == execution["pipeline_parallel_size"] == 1
            and type(execution["tensor_parallel_size"]) is int
            and execution["tensor_parallel_size"] >= 1
            and (frozen["schema_version"] == "3" or execution["tensor_parallel_size"] == 1)
            and execution["load_format"] == "safetensors"
            and execution["quantization"] is None
            and not execution["hicache"]
            and not execution["speculative_decoding"]
            and execution["cpu_offload_gb"] == 0,
            "unsupported-process-lock",
            "node process requires locked dense SGLang with TP, DP=1 and PP=1",
        )
        actual = observe_runtime()
        if frozen["schema_version"] in ("2", "3"):
            require(
                frozen.get("model_node")
                == {
                    "schema_version": "2",
                    "node_id": self.node["nodeId"],
                    "generation": self.node["generation"],
                    "runtime_digest": digest_json(actual),
                    "launcher": "process",
                },
                "inference-node-drift",
                "inference lock belongs to another model node or runtime",
            )
        else:
            require(
                "model_node" not in frozen,
                "inference-node-drift",
                "legacy lock cannot contain a model node",
            )
        runtime_matches(runtime, actual)
        backend = execution["platform"]
        if backend["backend"] == "cuda":
            require(
                re.fullmatch(r"GPU-[A-Za-z0-9-]+", str(backend.get("device_constraint", ""))),
                "missing-device-uuid",
                "CUDA process requires a physical GPU UUID",
            )
            known = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                text=True,
                timeout=15,
            ).splitlines()
            require(
                all(device in [line.strip() for line in known] for device in selected_devices(frozen)),
                "unknown-node-gpu",
                "locked GPU is absent on this model node",
            )
        return {
            "modelId": model["model_id"],
            "inferenceId": frozen["inference_id"],
            "runtimeId": runtime["runtime_id"],
        }

    def preparation_guard(self, payload):
        directory = self.directory(payload)
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"):
            if (directory / "identity.json").exists():
                identity = self.identity(directory, payload)
                require(identity["inputDigest"] is not None and identity["inputDigest"] == digest_json(payload)
                        and not (directory / "stop.json").exists(), "inference-start-fenced", "model preparation was stopped or changed")

    def prepare(self, payload, *, prepared=None):
        result = self.admit(payload)
        if prepared is None:
            self.preparation.prepare(payload, lambda: self.preparation_guard(payload))
        else:
            # This argument is supplied only by the trusted pool worker, never RPC payload.
            require(payload["model"] == prepared["model"] and payload["runtime"] == prepared["runtime"],
                    "prepared-identity-drift", "replica differs from its deployment model/runtime")
            self.preparation.verify(prepared)
        return result

    def start(self, payload, *, prepared=None):
        directory = self.directory(payload)
        digest = digest_json(payload)
        # Publish identity before heavy I/O, allowing concurrent stop to install a fence.
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"), lock(directory / "launch.lock"):
            identity = load(directory / "identity.json")
            if identity:
                self.identity(directory, payload)
                require(identity["inputDigest"] is None or identity["inputDigest"] == digest,
                        "inference-start-conflict", "service ID already identifies another immutable start request")
                status = load(directory / "status.json", {})
                if not status:
                    status = {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False}
                    atomic_json(directory / "status.json", status)
                admit = identity["inputDigest"] is not None and not (directory / "stop.json").exists() and status.get("state") in ("admitting", "starting") and not status.get("handle")
            else:
                directory.mkdir(mode=0o700, exist_ok=True)
                atomic_json(directory / "identity.json", {"node": self.node, "ownerId": payload["ownerId"],
                            "inputDigest": digest, "inferenceId": payload["lock"]["inference_id"]})
                atomic_json(directory / "status.json", {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False})
                admit = True
            if admit:
                atomic_json(directory / "request.json", payload)
                atomic_json(directory / "config.json", self.config)
        if not admit:
            return self.inspect(payload)
        self.prepare(payload, prepared=prepared)
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"), lock(directory / "launch.lock"):
            identity = self.identity(directory, payload)
            require(identity["inputDigest"] == digest, "inference-start-conflict", "start identity changed during preparation")
            status = load(directory / "status.json", {})
            admit = not (directory / "stop.json").exists() and status.get("state") in ("admitting", "starting") and not status.get("handle")
            if admit:
                if status["state"] == "admitting":
                    port = service_port(self.config, directory)
                    require(type(port) is int and 1 <= port <= 65535, "invalid-inference-port", "node configuration requires a stable inferencePort")
                    if not (directory / "access.json").exists():
                        atomic_json(directory / "access.json", {"port": port, "wireModel": "hitch-" + payload["model"]["model_id"][7:23],
                                    "engineToken": secrets.token_hex(32), "adminToken": secrets.token_hex(32)})
                    atomic_json(directory / "allocation.json", {"port": load(directory / "access.json")["port"], "inputDigest": digest})
                    self.devices.acquire(self.owner(directory.name), selected_devices(payload["lock"]))
                    atomic_json(directory / "status.json", {"schemaVersion": 2, "state": "starting", "resourcesReleased": False})
                with (directory / "supervisor.log").open("ab") as output:
                    subprocess.Popen([sys.executable, "-m", "hitch_inference.worker", "_supervisor", "--directory", str(directory)],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        return self.inspect(payload)

    def attach(self, payload):
        from hitch_inference.attach import attach_service

        return attach_service(self, payload)

    def inspect(self, payload):
        directory = self.directory(payload)
        if not (directory / "identity.json").exists():
            # Hitch publishes its local start intent before uploading the model.
            # Absence is pending admission, never proof that a late start is fenced.
            with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"):
                if not directory.exists() and not self.devices.exists(self.owner(directory.name)):
                    require(
                        re.fullmatch(r"sha256:[a-f0-9]{64}", str(payload.get("inferenceId", ""))),
                        "inference-owner-drift",
                        "an unadmitted service needs its intended inference lock",
                    )
                    return {
                        "schemaVersion": 2,
                        "state": "admitting",
                        "inferenceId": payload["inferenceId"],
                        "resourcesReleased": False,
                        "gpuSeconds": 0,
                    }
        identity = self.identity(directory, payload)
        require(
            not payload.get("inferenceId") or identity.get("inferenceId") == payload["inferenceId"],
            "inference-lock-drift",
            "service belongs to another inference lock",
        )
        with lock(directory / "launch.lock"):
            status = load(
                directory / "status.json",
                {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False},
            )
            reserved = self.devices.exists(self.owner(directory.name))
            handle = status.get("handle")
            worker = (
                {
                    "pid": handle["process"]["pid"],
                    "createdAt": handle["process"]["created_at"],
                }
                if handle
                else None
            )
            if (
                worker
                and not owned_alive(worker)
                and status["state"] in ("starting", "ready", "stopping")
            ):
                atomic_json(directory / "stop.json", {"reason": "supervisor-exited"})
                if reserved:
                    self.devices.fence(self.owner(directory.name))
                status.update(state="failed", error="supervisor-exited", resourcesReleased=False)
            if status["state"] in ("failed", "stopped"):
                if reserved:
                    self.devices.fence(self.owner(directory.name))
                try:
                    status["resourcesReleased"] = not reserved or self.devices.release(
                        self.owner(directory.name)
                    )
                except Exception:
                    status["resourcesReleased"] = False
            usage = (
                self.devices.inspect(self.owner(directory.name)) if reserved else {"gpuSeconds": 0}
            )
            status["gpuSeconds"] = usage["gpuSeconds"]
            atomic_json(directory / "status.json", status)
            # Access credentials are private control-RPC data, never CAS evidence.
            return {
                **status,
                "inferenceId": identity.get("inferenceId"),
                "access": load(directory / "access.json"),
            }

    def stop(self, payload):
        directory = self.directory(payload)
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"):
            if not (directory / "identity.json").exists():
                # A stop can beat an in-flight start request. Persist an owner
                # tombstone so a delayed/retried start cannot revive that ID.
                directory.mkdir(mode=0o700, exist_ok=True)
                atomic_json(
                    directory / "identity.json",
                    {
                        "node": self.node,
                        "ownerId": payload["ownerId"],
                        "inputDigest": None,
                        "inferenceId": payload.get("inferenceId"),
                    },
                )
            identity = self.identity(directory, payload)
            require(
                not payload.get("inferenceId")
                or identity.get("inferenceId") == payload["inferenceId"],
                "inference-lock-drift",
                "service belongs to another inference lock",
            )
            self._fence(directory)
        if self.devices.exists(self.owner(directory.name)):
            entry = self.devices.inspect(self.owner(directory.name))
            stop_owned(entry["processes"])
        with lock(directory / "launch.lock"):
            status = load(directory / "status.json")
            status.update(state="stopped", resourcesReleased=False)
            atomic_json(directory / "status.json", status)
        return self.inspect(payload)

    def _fence(self, directory):
        with lock(directory / "launch.lock"):
            atomic_json(directory / "stop.json", {"reason": "owner-stop"})
            if self.devices.exists(self.owner(directory.name)):
                self.devices.fence(self.owner(directory.name))
            status = load(
                directory / "status.json",
                {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False},
            )
            if status["state"] in ("admitting", "starting", "ready"):
                status.update(state="stopping", resourcesReleased=False)
            atomic_json(directory / "status.json", status)

    def recover(self, payload):
        # Recovery is explicitly a drain, never an implicit weight reload.
        if "previousNode" in payload:
            from hitch_inference.recovery import (
                recover_previous_service,
            )

            return recover_previous_service(self, payload)
        return self.stop(payload)
