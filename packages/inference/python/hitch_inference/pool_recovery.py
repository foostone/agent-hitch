"""Archived pool drain uses OS-boot proof, never old numeric PIDs."""
from .content import atomic_json, digest_json, require
from .node_generation import previous_boot_proof
from .recovery import _handle, recover_previous_service
from .state import load, lock


def recover_previous_pool(service, payload):
    directory = service.directory(payload)
    with lock(service.root / "admission.lock"), lock(directory / "launch.lock"):
        identity = load(directory / "identity.json")
        require(identity and identity["node"] == payload["previousNode"]
                and identity["ownerId"] == payload["ownerId"] and identity["inferenceId"] == payload["inferenceId"],
                "inference-owner-drift", "archived pool belongs to another owner, lock or generation")
        proof = previous_boot_proof(service.config["nodeRoot"], service.node, payload["previousNode"])
        status = load(directory / "status.json", {"state": "admitting"})
        handle = status.get("handle")
        _handle(handle, directory.name, payload["previousNode"])
        require(payload["expectedHandle"] is None or payload["expectedHandle"] == handle,
                "inference-handle-drift", "archived pool handle differs from its controller")
        atomic_json(directory / "stop.json", {"reason": "generation-recovery"})
        request = load(directory / "request.json")
        receipts = []
        for child in service.children(request) if request else []:
            child_directory = service.legacy.directory(child)
            # Pool may have crashed before admitting this replica. Fence its exact
            # identity in the previous generation without signaling any process.
            if not (child_directory / "identity.json").exists():
                child_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(child_directory / "identity.json", {"node": identity["node"], "ownerId": child["ownerId"],
                            "inputDigest": None, "inferenceId": child["lock"]["inference_id"]})
                atomic_json(child_directory / "status.json", {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False})
            child_status = load(child_directory / "status.json", {})
            receipts.append(recover_previous_service(service.legacy, {"serviceId": child["serviceId"], "ownerId": child["ownerId"],
                "inferenceId": child["lock"]["inference_id"], "previousNode": identity["node"], "expectedHandle": child_status.get("handle")}))
        devices = [device for receipt in receipts for device in receipt["devices"]]
        require(len(devices) == len(set(devices)), "pool-release-drift", "archived replica devices overlap")
        receipt = {"schemaVersion": 2, "kind": "inference-generation-release", "serviceId": directory.name,
            "ownerId": identity["ownerId"], "inferenceId": identity["inferenceId"], "previousNode": identity["node"], "node": service.node,
            "sourceIdentityDigest": digest_json(identity), "handle": handle, "admissionOnly": not handle and not devices,
            "state": "stopped", "resourcesReleased": True, "devices": devices,
            "gpuSeconds": sum(r["gpuSeconds"] for r in receipts), **proof}
        atomic_json(directory / "replica-generation-releases.json", receipts)
        atomic_json(directory / "generation-release.json", receipt)
        atomic_json(directory / "status.json", {**status, "state": "stopped", "resourcesReleased": True, "gpuSeconds": receipt["gpuSeconds"]})
        return receipt
