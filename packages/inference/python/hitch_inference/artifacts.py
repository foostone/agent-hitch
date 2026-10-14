"""Retain model-node CAS closures and return bounded controller metadata."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from hitch_inference.model_config import language_metadata
from hitch_inference.content import (
    atomic_json,
    digest_bytes,
    digest_file,
    digest_json,
    require,
    sync_dir,
)

MAX_METADATA = 16 * 1024 * 1024
MAX_OBJECTS = 4096

def validate_ref(ref):
    require(
        isinstance(ref, dict)
        and set(ref) == {"uri", "digest", "mediaType"}
        and isinstance(ref.get("digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", ref["digest"])
        and ref["uri"] == "cas:" + ref["digest"]
        and isinstance(ref["mediaType"], str),
        "invalid-content-ref",
        "node artifacts require a portable CAS reference",
    )
    return ref


def snapshot_files(value):
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != 1
        or value.get("format") != "hf-safetensors"
    ):
        return None
    files = value.get("files")
    require(
        isinstance(files, list) and 0 < len(files) <= MAX_OBJECTS,
        "invalid-file-manifest",
        "snapshot files are missing or exceed the limit",
    )
    names = set()
    for item in files:
        require(isinstance(item, dict), "invalid-file-manifest", "invalid snapshot entry")
        name = item.get("path")
        require(
            isinstance(name, str)
            and "\\" not in name
            and "\0" not in name
            and not name.startswith("/")
            and all(p not in ("", ".", "..") for p in name.split("/"))
            and name not in names,
            "invalid-export-path",
            "snapshot path is unsafe or duplicated",
        )
        ref = validate_ref(item.get("contentRef"))
        require(
            item.get("sha256") == ref["digest"]
            and type(item.get("size")) is int
            and 0 <= item["size"] <= 9007199254740991,
            "invalid-export-file",
            "snapshot file identity or size differs",
        )
        names.add(name)
    return files


def hf_manifest(store, payload):
    from hitch_inference.snapshots import safetensors_layout

    require(
        isinstance(payload, dict) and set(payload) == {"snapshotRef"},
        "invalid-model-inspection",
        "HF inspection requires a snapshot reference",
    )
    snapshot_ref = validate_ref(payload["snapshotRef"])
    require(
        store.path(snapshot_ref["digest"]).stat().st_size <= MAX_METADATA,
        "content-metadata-limit",
        "HF manifest exceeds 16 MiB",
    )
    snapshot = store.read_json(snapshot_ref)
    files = snapshot_files(snapshot)
    require(
        files is not None and snapshot["format"] == "hf-safetensors",
        "invalid-hf-snapshot",
        "serving requires an HF snapshot",
    )
    names = {item["path"]: item for item in files}
    require(
        "config.json" in names
        and "tokenizer_config.json" in names
        and ("tokenizer.json" in names or "tokenizer.model" in names),
        "incomplete-hf-export",
        "required HF configuration is absent",
    )
    tensors = {}
    for item in files:
        name = item["path"]
        require(
            not re.search(r"\.(bin|pt|pth|pkl|pickle|py|so|dylib|dll)$", name, re.I),
            "unsafe-serving-export",
            "unsafe serving file",
        )
        path = store.path(item["sha256"])
        require(
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == item["size"]
            and digest_file(path) == item["sha256"],
            "corrupt-content",
            "HF model content failed verification",
        )
        if name.endswith(".safetensors"):
            layout = safetensors_layout(path)
            require(
                not set(tensors).intersection(layout),
                "duplicate-tensor",
                "duplicate HF tensor",
            )
            tensors.update({key: name for key in layout})

    def read(name):
        item = names[name]
        require(
            item["size"] <= MAX_METADATA,
            "content-metadata-limit",
            "model configuration exceeds 16 MiB",
        )
        return store.path(item["sha256"]).read_bytes()

    config = json.loads(read("config.json"))
    tokenizer = json.loads(read("tokenizer_config.json"))
    require(
        isinstance(config, dict)
        and isinstance(tokenizer, dict)
        and "auto_map" not in config
        and "auto_map" not in tokenizer
        and config.get("trust_remote_code") is not True
        and tokenizer.get("trust_remote_code") is not True
        and not config.get("quantization_config"),
        "model-code-or-quantization",
        "dynamic code and quantization are unsupported",
    )
    weights = [item for item in files if item["path"].endswith(".safetensors")]
    require(weights, "incomplete-hf-export", "HF snapshot has no weights")
    if "model.safetensors.index.json" in names:
        require(
            json.loads(read("model.safetensors.index.json")).get("weight_map") == tensors,
            "invalid-shard-index",
            "HF shard index differs from tensors",
        )
    else:
        require(
            len(weights) == 1,
            "missing-shard-index",
            "multiple HF shards require an index",
        )
    template = (
        read("chat_template.jinja")
        if "chat_template.jinja" in names
        else tokenizer.get("chat_template")
    )
    if isinstance(template, str):
        template = template.encode()
    require(
        isinstance(template, bytes) and template,
        "missing-chat-template",
        "HF model needs one explicit template",
    )
    projected = sorted(
        ({key: item[key] for key in ("path", "size", "sha256")} for item in files),
        key=lambda item: item["path"],
    )
    token_files = [
        {"path": item["path"], "sha256": item["sha256"]}
        for item in projected
        if re.search(
            r"(?:^|/)(?:tokenizer|special_tokens_map|added_tokens|tokenizer_config)(?:\.|$)",
            item["path"],
        )
    ]
    architecture = (config.get("architectures") or [None])[0]
    dtype, context = language_metadata(config)
    require(
        isinstance(architecture, str)
        and isinstance(dtype, str)
        and isinstance(config.get("model_type"), str),
        "invalid-model-config",
        "model semantics are missing",
    )
    body = {
        "format": "hf-safetensors",
        "files": projected,
        "architecture": architecture,
        "model_type": config["model_type"],
        "dtype": dtype,
        "quantization": None,
        "context_tokens": context if type(context) is int and context > 0 else None,
        "tokenizer_digest": digest_json(token_files),
        "template_digest": digest_bytes(template),
    }
    for key, value in {
        "architecture": architecture,
        "dtype": dtype,
        "tokenizerDigest": body["tokenizer_digest"],
        "chatTemplateDigest": body["template_digest"],
        "weightsDigest": digest_json(
            [
                {"path": item["path"], "sha256": item["sha256"]}
                for item in projected
                if item["path"].endswith(".safetensors")
            ]
        ),
    }.items():
        require(
            snapshot.get(key) == value,
            "hf-semantics-drift",
            "HF snapshot metadata differs from actual files",
        )
    return {
        "schema_version": "1",
        **body,
        "model_id": digest_json(body),
        "source": {
            "kind": "local-directory",
            "label": "model-node-" + snapshot_ref["digest"][7:19],
            "license": config.get("license") if isinstance(config.get("license"), str) else None,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
