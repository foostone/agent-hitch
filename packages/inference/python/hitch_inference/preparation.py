"""Deployment-scoped HF preparation, serialized independently of node admission."""
import stat
from pathlib import Path
from .content import ContentStore, atomic_json, digest_json, require
from .model_config import language_metadata
from .snapshots import materialize, seal_directory
from .state import load, lock


def witness(directory):
    """Cheap replay proof: include every directory, file and replacement-sensitive stat."""
    directory = Path(directory)
    result = []
    for path in [directory.parent, directory, *sorted(directory.rglob("*"))]:
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                "materialization-drift", "prepared model contains a symlink or special file")
        # Sibling deployments may add models; the parent needs replacement identity only.
        if path == directory.parent:
            result.append({"path": "..", "stat": [str(info.st_dev), str(info.st_ino), str(info.st_mode)]})
            continue
        result.append({"path": "." if path == directory else path.relative_to(directory).as_posix(), "stat": list(map(str, (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)))})
    return result


class ModelPreparation:
    def __init__(self, config, node):
        self.node = node
        self.root = Path(config["nodeRoot"]) / "inference"
        self.store = ContentStore(config["storeRoot"])

    def identity(self, payload):
        return {"schemaVersion": 1, "node": self.node, "serviceId": payload["serviceId"], "ownerId": payload["ownerId"],
                "inputDigest": digest_json(payload), "modelId": payload["model"]["model_id"], "runtimeId": payload["runtime"]["runtime_id"],
                "validator": "hf-preparation-v1", "storeRoot": str(self.store.root)}

    def path(self, payload):
        return self.root / "preparations" / (payload["serviceId"] + ".json")

    def destination(self, payload):
        return self.root / "models" / payload["model"]["model_id"][7:]

    def verify(self, payload):
        receipt = load(self.path(payload))
        require(receipt and receipt.get("identity") == self.identity(payload)
                and receipt.get("receiptDigest") == digest_json({k: v for k, v in receipt.items() if k != "receiptDigest"}),
                "prepared-identity-drift", "prepared model receipt does not bind this deployment, generation and request")
        require(receipt["witness"] == witness(self.destination(payload)), "materialization-drift", "prepared model changed after verification")
        return receipt

    def prepare(self, payload, guard):
        path = self.path(payload)
        # Neither the node admission lock nor the HTTP deployments lock is held.
        with lock(path.with_suffix(".lock")):
            guard()
            if path.exists():
                result = self.verify(payload)
            else:
                model = payload["model"]
                files = [{**item, "contentRef": {"uri": "cas:" + item["sha256"], "digest": item["sha256"],
                          "mediaType": "application/octet-stream"}} for item in model["files"]]
                snapshot = self.store.put_json({"schemaVersion": 1, "format": "hf-safetensors", "files": files})
                destination = self.destination(payload)
                with lock(destination.with_suffix(".lock")):
                    materialize(self.store, snapshot, destination)
                    before = witness(destination)
                    sealed = seal_directory(self.store, destination, serving=True)
                    after = witness(destination)
                    require(before == after, "materialization-drift", "model changed during preparation")
                    metadata = self.store.read_json(sealed)
                    import json
                    config = json.loads((destination / "config.json").read_text())
                    dtype, context = language_metadata(config)
                    require(metadata["files"] == files and metadata["architecture"] == model["architecture"]
                            and dtype == model["dtype"] and config.get("model_type") == model["model_type"]
                            and (context if type(context) is int and context > 0 else None) == model["context_tokens"]
                            and metadata["tokenizerDigest"] == model["tokenizer_digest"]
                            and metadata["chatTemplateDigest"] == model["template_digest"],
                            "prepared-model-drift", "prepared HF semantics differ from the immutable model manifest")
                    result = {"identity": self.identity(payload), "snapshotRef": sealed, "witness": after}
                    result["receiptDigest"] = digest_json(result)
                    guard()
                    atomic_json(path, result)
            guard()
            return result
