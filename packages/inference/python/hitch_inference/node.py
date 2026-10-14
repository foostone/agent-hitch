"""Inference-only RPC and CAS protocol, independent of Gear and task execution."""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from pathlib import Path
from .content import ContentStore, atomic_json, digest_file, digest_json, require, sync_dir
from .node_generation import record_generation
from .state import load, lock
from .runtime import observe_runtime, package_version
MAX_HEADER = 1024 * 1024
BLOCK = 8 * 1024 * 1024
def read_header(stream):
    line = stream.readline(MAX_HEADER + 1)
    require(
        len(line) <= MAX_HEADER and line.endswith(b"\n"),
        "invalid-node-header",
        "node stream header is missing or oversized",
    )
    return json.loads(line)


def boot_identity():
    path = Path("/proc/sys/kernel/random/boot_id")
    if path.is_file():
        return path.read_text().strip()
    # The transport itself needs no GPU/Python ML dependencies on the control
    # machine. macOS exposes a stable boot timestamp through the native sysctl.
    if platform.system() == "Darwin":
        boot = subprocess.check_output(
            ["sysctl", "-n", "kern.boottime"], text=True, timeout=10
        ).strip()
        require(boot, "missing-boot-identity", "operating system returned no boot identity")
        return digest_json({"machine": platform.node(), "bootTime": boot})
    require(
        False,
        "unsupported-node-platform",
        "model-node transport requires Linux or macOS boot identity",
    )


class NodeService:
    def __init__(self, config):
        require(
            config.get("schemaVersion") == 2
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(config.get("nodeId", ""))),
            "invalid-node-config",
            "model node requires schemaVersion 2 and nodeId",
        )
        for key in ("nodeRoot", "storeRoot"):
            require(
                isinstance(config.get(key), str) and Path(config[key]).is_absolute(),
                "invalid-node-config",
                "model node requires absolute " + key,
            )
        self.config = config
        self.root = Path(config["nodeRoot"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = ContentStore(config["storeRoot"])
        with lock(self.root / "identity.lock"):
            identity = load(self.root / "identity.json")
            boot = boot_identity()
            if identity:
                require(
                    identity["nodeId"] == config["nodeId"],
                    "node-id-conflict",
                    "node state belongs to another nodeId",
                )
                record_generation(self.root, identity)
            if not identity or identity["bootIdentity"] != boot:
                identity = {
                    "nodeId": config["nodeId"],
                    "generation": uuid.uuid4().hex,
                    "bootIdentity": boot,
                }
                atomic_json(self.root / "identity.json", identity)
            record_generation(self.root, identity)
        self.identity = {key: identity[key] for key in ("nodeId", "generation")}


    def validate(self, envelope, *, probe=False):
        require(
            isinstance(envelope, dict)
            and set(envelope)
            == {
                "schemaVersion",
                "requestId",
                "node",
                "operation",
                "inputDigest",
                "payload",
            },
            "invalid-node-envelope",
            "node envelope fields are invalid",
        )
        require(
            envelope["schemaVersion"] == 2
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(envelope["requestId"])),
            "invalid-node-envelope",
            "invalid node protocol version or request ID",
        )
        require(
            envelope["inputDigest"] == digest_json(envelope["payload"]),
            "node-input-drift",
            "node request payload differs from its input digest",
        )
        require(
            (probe and envelope["node"] is None) or envelope["node"] == self.identity,
            "node-generation-drift",
            "model node identity/generation changed; reconcile before resubmitting work",
        )


    def response(self, envelope, result):
        return {
            "schemaVersion": 2,
            "requestId": envelope["requestId"],
            "node": self.identity,
            "inputDigest": envelope["inputDigest"],
            "result": result,
        }


    def probe(self):
        runtime = observe_runtime()
        # This is an observation, not GPU or tool-protocol validation evidence.
        try:
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                text=True,
                timeout=15,
            )
            gpus = [line.strip() for line in output.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            gpus = []
        return {
            **self.identity,
            "runtimeDigest": digest_json(runtime),
            "runtime": runtime,
            "gpuUuids": gpus,
            "launchers": ["process"]
            if all(package_version(runtime, p) for p in ("sglang", "psutil"))
            else [],
            "capabilities": {
                "binaryCas": True,
                "durableInferenceProcesses": True,
                "standaloneInference": "1",
            },
        }


    def cas_stat(self, payload):
        path = self.store.path(payload["digest"])
        if not path.exists():
            return {"present": False}
        require(
            path.is_file() and not path.is_symlink() and digest_file(path) == payload["digest"],
            "corrupt-content",
            "node CAS object is corrupt",
        )
        return {"present": True, "size": path.stat().st_size}


    def import_stream(self, envelope, incoming):
        self.validate(envelope)
        require(
            envelope["operation"] == "cas.import",
            "invalid-node-operation",
            "binary input requires cas.import",
        )
        payload = envelope["payload"]
        path = self.store.path(payload["digest"])
        size = payload["size"]
        require(
            type(size) is int and 0 <= size <= 9007199254740991,
            "invalid-content-size",
            "CAS size must be an explicit non-negative safe integer",
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # An interrupted transfer is an uncommitted temporary file. Retrying the
        # same digest never overwrites a published object or exposes partial bytes.
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".incoming")
        try:
            with temporary.open("xb") as output:
                remaining = size
                while remaining:
                    block = incoming.read(min(BLOCK, remaining))
                    require(
                        block,
                        "content-transfer-truncated",
                        "CAS transfer ended before declared size",
                    )
                    output.write(block)
                    remaining -= len(block)
                require(
                    not incoming.read(1),
                    "content-transfer-overflow",
                    "CAS transfer exceeds declared size",
                )
                output.flush()
                os.fsync(output.fileno())
            require(
                digest_file(temporary) == payload["digest"],
                "content-digest-mismatch",
                "transferred object failed its digest",
            )
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            require(
                digest_file(path) == payload["digest"],
                "corrupt-content",
                "existing CAS object failed its digest",
            )
            sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return self.response(envelope, {"present": True, "size": size})


    def rpc(self, envelope):
        operation = envelope.get("operation")
        self.validate(envelope, probe=operation == "probe")
        payload = envelope["payload"]
        if operation == "probe":
            result = self.probe()
        elif operation == "cas.stat":
            result = self.cas_stat(payload)
        elif operation == "cas.hfManifest":
            from .artifacts import hf_manifest
            result = hf_manifest(self.store, payload)
        elif operation.startswith("inference."):
            from .pool import InferenceService
            action = operation.removeprefix("inference.")
            require(action in ("prepare", "start", "inspect", "attach", "stop", "drain", "recover"),
                    "unknown-node-operation", "unknown inference operation")
            result = getattr(InferenceService(self.config, self.identity), action)(payload)
        else:
            require(False, "unknown-node-operation", "only inference and CAS operations are supported")
        return self.response(envelope, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("rpc", "cas-import"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        node = NodeService(load(args.config))
        result = node.rpc(json.load(sys.stdin)) if args.action == "rpc" else node.import_stream(read_header(sys.stdin.buffer), sys.stdin.buffer)
        print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    except Exception as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "node-operation-failed"), "message": str(error)}}))
        sys.exit(1)


if __name__ == "__main__":
    main()
