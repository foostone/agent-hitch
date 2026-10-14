"""Explicit immutable TP and disjoint replica placement; no hardware certification."""
import socket
import re
from .content import require
from .state import load


def selected_devices(frozen):
    backend = frozen["execution"]["platform"]
    if backend["backend"] != "cuda":
        return []
    devices = backend.get("device_constraints", [backend.get("device_constraint")])
    require(isinstance(devices, list) and devices and len(devices) == len(set(devices))
            and all(isinstance(d, str) and re.fullmatch(r"GPU-[A-Za-z0-9-]+", d) for d in devices),
            "invalid-device-placement", "explicit unique physical GPU UUIDs are required")
    require(len(devices) == frozen["execution"]["tensor_parallel_size"],
            "invalid-device-placement", "one physical UUID is required per tensor-parallel rank")
    return devices


def validate_topology(frozen):
    topology = frozen.get("topology")
    require(frozen.get("schema_version") == "3" and isinstance(topology, dict)
            and set(topology) == {"replicas", "routing", "max_running_requests", "max_queued_requests"}
            and topology["routing"] == "least-active"
            and isinstance(topology["replicas"], list) and topology["replicas"],
            "invalid-inference-pool", "v3 requires explicit replicas and least-active routing")
    tp = frozen["execution"]["tensor_parallel_size"]
    require(type(tp) is int and tp >= 1, "invalid-inference-pool", "TP must be positive")
    seen = set()
    for replica in topology["replicas"]:
        require(isinstance(replica, dict) and set(replica) == {"devices"}
                and isinstance(replica["devices"], list) and len(replica["devices"]) == tp
                and all(isinstance(d, str) and re.fullmatch(r"GPU-[A-Za-z0-9-]+", d) for d in replica["devices"])
                and len(set(replica["devices"])) == tp and not seen.intersection(replica["devices"]),
                "invalid-inference-pool", "replicas require disjoint TP-sized physical GPU groups")
        seen.update(replica["devices"])
    require(selected_devices(frozen) == topology["replicas"][0]["devices"],
            "invalid-inference-pool", "primary execution must match the first replica")
    require(type(topology["max_running_requests"]) is int and topology["max_running_requests"] >= 1
            and topology["max_running_requests"] <= len(topology["replicas"]) * frozen["execution"]["max_running_requests"]
            and type(topology["max_queued_requests"]) is int and topology["max_queued_requests"] >= 0,
            "invalid-inference-pool", "pool capacity must fit the explicit engine capacities")
    return topology


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def service_port(config, directory):
    existing = load(directory / "access.json")
    if existing:
        allocation = load(directory / "allocation.json")
        require(allocation is None or allocation["port"] == existing["port"],
                "inference-port-drift", "private access changed its durable port allocation")
        return existing["port"]
    claimed = set()
    node_root = directory.parent.parent
    for namespace in ("inference", "inference-pools"):
        for access in (node_root / namespace).glob("*/access.json"):
            if access.parent == directory:
                continue
            if load(access.parent / "status.json", {}).get("resourcesReleased") is not True:
                claimed.add(load(access)["port"])
    if not config.get("dynamicInferencePorts"):
        port = config.get("inferencePort")
        require(port not in claimed, "inference-route-reserved", "another unreleased service reserves the fixed port")
        return port
    for _ in range(128):
        port = available_port()
        if port not in claimed:
            return port
    require(False, "inference-ports-unavailable", "no unreserved inference port is available")
