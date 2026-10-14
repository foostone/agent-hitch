"""Durable replica groups. Stop fences every replica and confirms all resources dead."""
import copy
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from .content import atomic_json, digest_json, require
from .gateway import GatewayServer, Router
from .process_identity import owned_alive, process_identity, stop_owned
from .process_service import ProcessService
from .state import load, lock
from .topology import service_port, validate_topology


class InferenceService:
    def __init__(self, config, node):
        self.config, self.node = config, node
        self.legacy = ProcessService(config, node)
        self.root = Path(config["nodeRoot"]) / "inference-pools"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, payload):
        return self.root / self.legacy.directory(payload).name

    def is_pool(self, payload):
        return payload.get("lock", {}).get("schema_version") == "3" or (self.directory(payload) / "identity.json").exists()

    def owner(self, directory, payload):
        identity = load(directory / "identity.json")
        require(identity and identity["node"] == self.node and identity["ownerId"] == payload["ownerId"]
                and (not payload.get("inferenceId") or payload["inferenceId"] == identity["inferenceId"]),
                "inference-owner-drift", "pool owner, generation or immutable identity changed")
        return identity

    def children(self, payload):
        frozen = payload["lock"]
        require(frozen.get("inference_id") == digest_json({k: v for k, v in frozen.items() if k != "inference_id"}),
                "inference-lock-drift", "parent pool identity differs from its immutable model/runtime/topology")
        result = []
        for index, replica in enumerate(validate_topology(payload["lock"])["replicas"]):
            child = copy.deepcopy(payload)
            child["serviceId"] = "inference_" + digest_json({"pool": payload["serviceId"], "replica": index})[7:39]
            frozen = child["lock"]
            frozen["execution"]["platform"]["device_constraints"] = replica["devices"]
            frozen["execution"]["platform"]["device_constraint"] = replica["devices"][0]
            # Child identity freezes its exact placement, independent of the pool routing identity.
            frozen["topology"]["replicas"] = [replica]
            frozen["topology"]["max_running_requests"] = frozen["execution"]["max_running_requests"]
            frozen["resources"]["gpu_count"] = len(replica["devices"])
            frozen["resources"]["cpu_millis"] //= len(payload["lock"]["topology"]["replicas"])
            frozen["resources"]["memory_bytes"] //= len(payload["lock"]["topology"]["replicas"])
            frozen["inference_id"] = digest_json({k: v for k, v in frozen.items() if k != "inference_id"})
            result.append(child)
        return result

    def prepare(self, payload):
        if not self.is_pool(payload):
            return self.legacy.prepare(payload)
        for child in self.children(payload):
            self.legacy.prepare(child)
        return {"inferenceId": payload["lock"]["inference_id"], "replicas": len(self.children(payload))}

    def start(self, payload):
        if not self.is_pool(payload):
            return self.legacy.start(payload)
        directory = self.directory(payload)
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"), lock(directory / "launch.lock"):
            existing = load(directory / "identity.json")
            status = load(directory / "status.json", {})
            admit = True
            if existing:
                self.owner(directory, payload)
                require(existing["inputDigest"] is None or existing["inputDigest"] == digest_json(payload),
                        "inference-start-conflict", "pool ID already freezes another request")
                admit = existing["inputDigest"] is not None and not status.get("handle") and not (directory / "stop.json").exists()
            else:
                self.prepare(payload)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(directory / "identity.json", {"node": self.node, "ownerId": payload["ownerId"],
                            "inferenceId": payload["lock"]["inference_id"], "inputDigest": digest_json(payload)})
            if admit:
                port = service_port({**self.config, "dynamicInferencePorts": not self.config.get("inferencePort")}, directory)
                atomic_json(directory / "request.json", payload)
                atomic_json(directory / "config.json", self.config)
                if not (directory / "access.json").exists():
                    atomic_json(directory / "access.json", {"port": port, "wireModel": "hitch-" + payload["model"]["model_id"][7:23],
                                "engineToken": secrets.token_hex(32), "adminToken": secrets.token_hex(32)})
                    atomic_json(directory / "allocation.json", {"port": port, "inputDigest": digest_json(payload)})
                atomic_json(directory / "status.json", {"schemaVersion": 2, "state": "starting", "resourcesReleased": False})
                with (directory / "supervisor.log").open("ab") as output:
                    subprocess.Popen([sys.executable, "-m", "hitch_inference.worker", "_pool", "--directory", str(directory)],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        return self.inspect(payload)

    def inspect(self, payload):
        if not self.is_pool(payload):
            return self.legacy.inspect(payload)
        directory = self.directory(payload)
        stored = load(directory / "identity.json")
        if stored and stored["node"] != self.node:
            require(stored["ownerId"] == payload["ownerId"] and stored["inferenceId"] == payload.get("inferenceId", stored["inferenceId"]),
                    "inference-owner-drift", "archived pool belongs to another owner")
            previous = load(directory / "generation-release.json")
            if previous and previous["node"] == self.node:
                return {"schemaVersion": 2, **previous, "inferenceId": stored["inferenceId"]}
            return {"schemaVersion": 2, "state": "failed", "resourcesReleased": False, "inferenceId": stored["inferenceId"],
                    "gpuSeconds": load(directory / "status.json", {}).get("gpuSeconds", 0), "recoveryRequired": True}
        identity = self.owner(directory, payload)
        request = load(directory / "request.json")
        children = [self.legacy.inspect({"serviceId": c["serviceId"], "ownerId": c["ownerId"], "inferenceId": c["lock"]["inference_id"]}) for c in self.children(request)] if request else []
        with lock(directory / "launch.lock"):
            status = load(directory / "status.json", {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False})
            handle = status.get("handle")
            process = {"pid": handle["process"]["pid"], "createdAt": handle["process"]["created_at"]} if handle else None
            alive = bool(process and owned_alive(process))
            if process and not alive and status["state"] in ("starting", "ready"):
                atomic_json(directory / "stop.json", {"reason": "pool-supervisor-exited"})
                status.update(state="failed", resourcesReleased=False)
            if status["state"] == "ready" and any(c["state"] != "ready" for c in children):
                atomic_json(directory / "stop.json", {"reason": "replica-exited"})
                status.update(state="failed", resourcesReleased=False)
            status["gpuSeconds"] = sum(c.get("gpuSeconds", 0) for c in children)
            status["resourcesReleased"] = not alive and (not request or all(c["resourcesReleased"] for c in children))
            if status["resourcesReleased"] and status["state"] == "stopping":
                status["state"] = "stopped"
            status["replicas"] = [{k: v for k, v in child.items() if k not in ("access", "serverInfo", "runtime")} for child in children]
            atomic_json(directory / "status.json", status)
            return {**status, "inferenceId": identity["inferenceId"], "inputDigest": identity["inputDigest"], "access": load(directory / "access.json")}

    def attach(self, payload):
        if not self.is_pool(payload):
            return self.legacy.attach(payload)
        directory = self.directory(payload)
        identity = self.owner(directory, payload)
        status = self.inspect(payload)
        require(status["state"] == "ready" and status["handle"] == payload["expectedHandle"]
                and identity["inputDigest"] == payload["inputDigest"] and not (directory / "stop.json").exists(),
                "inference-attach-drift", "attachment requires the original live unfenced pool")
        require(load(directory / "allocation.json") == {"port": status["access"]["port"], "inputDigest": identity["inputDigest"]},
                "inference-attach-drift", "pool frontend allocation changed")
        request = load(directory / "request.json")
        for child in self.children(request):
            observed = self.legacy.inspect({"serviceId": child["serviceId"], "ownerId": child["ownerId"], "inferenceId": child["lock"]["inference_id"]})
            self.legacy.attach({"serviceId": child["serviceId"], "ownerId": child["ownerId"], "inferenceId": child["lock"]["inference_id"],
                                "inputDigest": digest_json(child), "expectedHandle": observed["handle"]})
        return status

    def drain(self, payload):
        directory = self.directory(payload)
        self.owner(directory, payload)
        timeout = payload.get("drainTimeoutMs", 30000)
        require(type(timeout) is int and 0 <= timeout <= 60000, "invalid-drain-timeout", "drain timeout must be 0..60000ms")
        with lock(directory / "launch.lock"):
            deadline = time.time() + timeout / 1000
            previous = load(directory / "drain.json")
            atomic_json(directory / "drain.json", {"deadline": min(deadline, previous["deadline"]) if previous else deadline})
            status = load(directory / "status.json")
            startup = status["state"] in ("admitting", "starting")
            if status["state"] in ("admitting", "starting", "ready"):
                status["state"] = "stopping"
                atomic_json(directory / "status.json", status)
        if startup:
            # No request has been admitted yet. Fence every possible delayed
            # child launch instead of leaving unadmitted ownership unresolved.
            return self.stop({**payload, "force": True})
        return self.inspect(payload)

    def stop(self, payload):
        directory = self.directory(payload)
        if self.is_pool(payload) and load(directory / "status.json", {}).get("state") in ("ready", "stopping") and not payload.get("force"):
            self.drain(payload)
            deadline = time.monotonic() + payload.get("drainTimeoutMs", 30000) / 1000 + 1
            while time.monotonic() < deadline:
                status = self.inspect(payload)
                if status["resourcesReleased"]:
                    return status
                if status["state"] == "failed":
                    break
                time.sleep(0.05)
        legacy = False
        with lock(Path(self.config["nodeRoot"]) / "inference-admission.lock"), lock(self.root / "admission.lock"):
            pool_identity = load(directory / "identity.json")
            legacy_directory = self.legacy.directory(payload)
            if not pool_identity and (legacy_directory / "identity.json").exists():
                legacy = True
            elif not pool_identity:
                # Recheck after acquiring the shared admission fence. A raced
                # start must never have its immutable ownership overwritten.
                for target in (directory, legacy_directory):
                    with lock(target / "launch.lock"):
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                        atomic_json(target / "identity.json", {"node": self.node, "ownerId": payload["ownerId"],
                                    "inferenceId": payload.get("inferenceId"), "inputDigest": None})
                        atomic_json(target / "status.json", {"schemaVersion": 2, "state": "stopped", "resourcesReleased": True})
                        atomic_json(target / "stop.json", {"reason": "owner-stop"})
            if not legacy:
                self.owner(directory, payload)
                atomic_json(directory / "stop.json", {"reason": "owner-stop"})
        if legacy:
            return self.legacy.stop(payload)
        request = load(directory / "request.json")
        for child in self.children(request) if request else []:
            self.legacy.stop({"serviceId": child["serviceId"], "ownerId": child["ownerId"], "inferenceId": child["lock"]["inference_id"]})
        status = self.inspect(payload)
        handle = status.get("handle")
        if handle:
            stop_owned([{ "pid": handle["process"]["pid"], "createdAt": handle["process"]["created_at"] }])
        with lock(directory / "launch.lock"):
            status = load(directory / "status.json")
            status.update(state="stopped")
            atomic_json(directory / "status.json", status)
        return self.inspect(payload)

    def recover(self, payload):
        if not self.is_pool(payload):
            return self.legacy.recover(payload)
        if "previousNode" in payload:
            from .pool_recovery import recover_previous_pool
            return recover_previous_pool(self, payload)
        return self.stop(payload)


def pool_worker(directory):
    directory = Path(directory)
    config, request = load(directory / "config.json"), load(directory / "request.json")
    node = load(directory / "identity.json")["node"]
    service = InferenceService(config, node)
    children = service.children(request)
    server = None
    owns_worker = False
    try:
        with lock(directory / "worker.lock", blocking=False):
            with lock(directory / "launch.lock"):
                status = load(directory / "status.json")
                if status.get("handle") or (directory / "stop.json").exists() or (directory / "drain.json").exists():
                    return
                owns_worker = True
                identity = process_identity(os.getpid())
                status["handle"] = {"schema_version": "2", "kind": "process", "node_id": node["nodeId"], "generation": node["generation"],
                                    "service_id": directory.name, "process": {"pid": identity["pid"], "created_at": identity["createdAt"]}}
                atomic_json(directory / "status.json", status)
            runtime = ProcessService({**config, "dynamicInferencePorts": True}, node)
            for child in children:
                if (directory / "stop.json").exists() or (directory / "drain.json").exists():
                    return
                runtime.start(child)
            deadline = time.monotonic() + request["lock"]["execution"]["startup_timeout_ms"] / 1000
            while True:
                if (directory / "stop.json").exists() or (directory / "drain.json").exists():
                    return
                statuses = [runtime.inspect({"serviceId": c["serviceId"], "ownerId": c["ownerId"], "inferenceId": c["lock"]["inference_id"]}) for c in children]
                require(all(s["state"] not in ("failed", "stopped", "stopping") for s in statuses), "replica-start-failed", "a replica failed startup")
                if all(s["state"] == "ready" for s in statuses):
                    break
                require(time.monotonic() < deadline, "pool-start-timeout", "replicas did not become ready")
                time.sleep(0.1)
            frozen, topology = request["lock"], validate_topology(request["lock"])
            router = Router([{**s["access"], "baseUrl": "http://127.0.0.1:" + str(s["access"]["port"])} for s in statuses],
                            running=topology["max_running_requests"], queued=topology["max_queued_requests"],
                            queue_timeout=frozen["execution"]["queue_timeout_ms"] / 1000,
                            request_timeout=frozen["execution"]["request_timeout_ms"] / 1000,
                            engine_capacity=frozen["execution"]["max_running_requests"], frozen=frozen)
            access = load(directory / "access.json")
            server = GatewayServer(("127.0.0.1", access["port"]), router, access)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            with lock(directory / "launch.lock"):
                status = load(directory / "status.json")
                require(not (directory / "stop.json").exists() and not (directory / "drain.json").exists(), "pool-start-fenced", "pool was stopped during startup")
                status.update(state="ready", runtime=statuses[0]["runtime"], serverInfo=statuses[0]["serverInfo"],
                              gpuUuid=topology["replicas"][0]["devices"][0], gpuUuids=[d for r in topology["replicas"] for d in r["devices"]],
                              replicaObservations=[s["replicaObservation"] for s in statuses])
                atomic_json(directory / "status.json", status)
            while not (directory / "stop.json").exists():
                draining = load(directory / "drain.json")
                if draining:
                    router.drain()
                    if sum(router.active) == 0 or time.time() >= draining["deadline"]:
                        break
                with lock(directory / "launch.lock"):
                    status = load(directory / "status.json")
                    status["activeRequests"] = sum(router.active)
                    atomic_json(directory / "status.json", status)
                statuses = [runtime.inspect({"serviceId": c["serviceId"], "ownerId": c["ownerId"], "inferenceId": c["lock"]["inference_id"]}) for c in children]
                require(all(s["state"] == "ready" for s in statuses), "replica-exited", "a replica exited; pool must be drained")
                time.sleep(0.2)
    except BlockingIOError:
        return
    except Exception as error:
        with lock(directory / "launch.lock"):
            status = load(directory / "status.json")
            status.update(state="failed", resourcesReleased=False, error=getattr(error, "code", "pool-worker-failed"))
            atomic_json(directory / "status.json", status)
    finally:
        if server:
            server.router.drain()
            server.shutdown()
            server.server_close()
        # Fence every child before reporting release. A timeout is never release proof.
        for child in children if owns_worker else []:
            try:
                service.legacy.stop({"serviceId": child["serviceId"], "ownerId": child["ownerId"], "inferenceId": child["lock"]["inference_id"]})
            except Exception:
                pass
        if owns_worker:
            with lock(directory / "launch.lock"):
                status = load(directory / "status.json")
                if status["state"] != "failed":
                    status.update(state="stopped", resourcesReleased=False)
                    atomic_json(directory / "status.json", status)
