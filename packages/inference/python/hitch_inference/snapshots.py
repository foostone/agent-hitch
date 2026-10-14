"""Seal and materialize HF serving snapshots only."""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import uuid
from pathlib import Path

from hitch_inference.model_config import language_metadata
from hitch_inference.content import digest_bytes, digest_file, digest_json, require, sync_dir

DTYPE_BYTES = {
    "BOOL": 1,
    "I8": 1,
    "U8": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}


def safetensors_layout(path):
    size = path.stat().st_size
    with path.open("rb") as f:
        raw = f.read(8)
        require(len(raw) == 8, "incomplete-safetensors", "safetensors header is missing")
        header_size = struct.unpack("<Q", raw)[0]
        require(
            2 <= header_size <= min(size - 8, 100_000_000),
            "invalid-safetensors-header",
            "invalid tensor header length",
        )

        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(
                    key not in result,
                    "duplicate-tensor-key",
                    "duplicate safetensors header key",
                )
                result[key] = value
            return result

        header = json.loads(f.read(header_size), object_pairs_hook=unique)
    tensors, spans = {}, []
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        require(
            isinstance(tensor, dict) and set(tensor) == {"dtype", "shape", "data_offsets"},
            "invalid-tensor",
            "invalid tensor header fields",
        )
        require(
            tensor["dtype"] in DTYPE_BYTES
            and isinstance(tensor["shape"], list)
            and all(type(x) is int and x >= 0 for x in tensor["shape"]),
            "invalid-tensor-shape",
            "unsupported dtype or shape",
        )
        offsets = tensor["data_offsets"]
        require(
            isinstance(offsets, list)
            and len(offsets) == 2
            and all(type(x) is int and x >= 0 for x in offsets),
            "invalid-tensor-offsets",
            "invalid tensor offsets",
        )
        count = 1
        for dimension in tensor["shape"]:
            count *= dimension
        require(
            offsets[1] - offsets[0] == count * DTYPE_BYTES[tensor["dtype"]]
            and offsets[1] <= size - 8 - header_size,
            "incomplete-tensor",
            "tensor bytes do not match declared dtype and shape",
        )
        tensors[name] = {"dtype": tensor["dtype"], "shape": tensor["shape"]}
        spans.append(tuple(offsets))
    end = 0
    for start, stop in sorted(spans):
        require(start == end, "invalid-tensor-layout", "tensor spans overlap or leave gaps")
        end = stop
    require(
        tensors and end == size - 8 - header_size,
        "incomplete-weights",
        "safetensors payload has missing or unclaimed bytes",
    )
    return tensors


def seal_directory(store, directory, serving=True, verify_finite=False):
    root = Path(directory).resolve()
    require(root.is_dir(), "missing-export", "export directory does not exist")
    require(serving is True, "invalid-snapshot-kind", "standalone inference accepts only HF serving snapshots")
    files, tensors, weights = [], {}, []
    for path in sorted(root.rglob("*")):
        # local_dir downloads create volatile lock/metadata files here. They
        # are not serving inputs, and must not change tokenizer/model identity.
        if path.relative_to(root).parts[:2] == (".cache", "huggingface"):
            continue
        require(
            not path.is_symlink(),
            "export-symlink",
            "sealed exports cannot contain symlinks",
        )
        if path.is_dir():
            continue
        require(path.is_file(), "export-special-file", "only regular files can be sealed")
        name = path.relative_to(root).as_posix()
        require(
            not re.search(r"\.(bin|pt|pth|pkl|pickle|py|so|dylib|dll)$", name, re.I),
            "unsafe-serving-export",
            "trainer state and executable code do not belong in HF serving snapshots",
        )
        before = path.stat()
        if name.endswith(".safetensors"):
            layout = safetensors_layout(path)
            require(
                not set(tensors).intersection(layout),
                "duplicate-tensor",
                "tensor occurs in more than one HF shard",
            )
            tensors.update({k: {**v, "file": name} for k, v in layout.items()})
            if verify_finite:
                import torch
                from safetensors import safe_open

                with safe_open(str(path), framework="pt", device="cpu") as f:
                    for key in f.keys():
                        tensor = f.get_tensor(key)
                        require(
                            bool(torch.isfinite(tensor).all()),
                            "non-finite-weights",
                            "HF export contains NaN/Inf: " + key,
                        )
        ref = store.put_file(path)
        after = path.stat()
        require(
            (before.st_size, before.st_mtime_ns, before.st_mode)
            == (after.st_size, after.st_mtime_ns, after.st_mode),
            "export-still-writing",
            "export changed during sealing",
        )
        entry = {
            "path": name,
            "size": before.st_size,
            "sha256": ref["digest"],
            "contentRef": ref,
        }
        files.append(entry)
        if name.endswith(".safetensors"):
            weights.append({"path": name, "sha256": ref["digest"]})
    require(files, "empty-export", "cannot seal an empty export")
    manifest = {
        "schemaVersion": 1,
        "format": "hf-safetensors",
        "files": files,
    }
    names = {f["path"] for f in files}
    require(
        "config.json" in names
        and "tokenizer_config.json" in names
        and ("tokenizer.json" in names or "tokenizer.model" in names)
        and weights,
        "incomplete-hf-export",
        "HF snapshot requires weights, config, tokenizer and template",
    )
    config = json.loads((root / "config.json").read_text())
    token_config = json.loads((root / "tokenizer_config.json").read_text())
    require(
        not config.get("auto_map")
        and not token_config.get("auto_map")
        and not config.get("quantization_config"),
        "model-code-or-quantization",
        "v1 requires a dense safetensors model without dynamic code or quantization",
    )
    template = (
        (root / "chat_template.jinja").read_text()
        if (root / "chat_template.jinja").exists()
        else token_config.get("chat_template")
    )
    require(
        isinstance(template, str) and template,
        "missing-chat-template",
        "one explicit chat template is required",
    )
    architecture = (config.get("architectures") or [None])[0]
    dtype, _ = language_metadata(config)
    require(
        isinstance(architecture, str) and isinstance(dtype, str),
        "invalid-model-config",
        "model config must declare architecture and dtype",
    )
    index_path = root / "model.safetensors.index.json"
    if index_path.exists():
        mapping = json.loads(index_path.read_text()).get("weight_map")
        require(
            isinstance(mapping, dict)
            and set(mapping) == set(tensors)
            and all(tensors[k]["file"] == file for k, file in mapping.items()),
            "invalid-shard-index",
            "HF shard index must cover every tensor exactly",
        )
    elif len(weights) != 1:
        require(
            False,
            "missing-shard-index",
            "multiple safetensors shards require an index",
        )
    tokenizer_files = [
        {"path": f["path"], "sha256": f["sha256"]}
        for f in files
        if re.search(
            r"(?:^|/)(?:tokenizer|special_tokens_map|added_tokens|tokenizer_config)(?:\.|$)",
            f["path"],
        )
    ]
    manifest.update(
        {
            "weightsDigest": digest_json(weights),
            "tokenizerDigest": digest_json(tokenizer_files),
            "chatTemplateDigest": digest_bytes(template.encode()),
            "architecture": architecture,
            "dtype": dtype,
            "tensorLayoutDigest": digest_json(tensors),
        }
    )
    return store.put_json(manifest)



def materialize(store, ref, destination):
    manifest = store.read_json(ref)
    require(manifest.get("schemaVersion") == 1 and manifest.get("format") == "hf-safetensors",
            "invalid-file-manifest", "standalone inference accepts only HF serving snapshots")
    files = manifest.get("files")
    require(isinstance(files, list) and files, "invalid-file-manifest", "HF snapshot files are missing")
    seen = set()
    for item in files:
        name = item["path"]
        require(isinstance(name, str) and name not in seen and "\\" not in name and "\0" not in name
                and not Path(name).is_absolute() and all(p not in ("", ".", "..") for p in name.split("/")),
                "invalid-export-path", "file manifest path escapes or duplicates the snapshot")
        require(item["sha256"] == item["contentRef"]["digest"] and type(item["size"]) is int and item["size"] >= 0,
                "invalid-export-file", "file identity or size mismatch")
        seen.add(name)
    destination = Path(destination)
    if destination.exists():
        require(destination.is_dir() and not destination.is_symlink(), "materialization-drift", "snapshot root must be a directory")
        actual = set()
        for path in destination.rglob("*"):
            require(not path.is_symlink() and (path.is_dir() or path.is_file()), "materialization-drift", "snapshot contains a symlink or special file")
            if path.is_file():
                actual.add(path.relative_to(destination).as_posix())
        require(actual == seen, "materialization-drift", "snapshot has missing or unexpected files")
        for item in files:
            path = destination / item["path"]
            require(path.is_file() and path.stat().st_size == item["size"] and digest_file(path) == item["sha256"],
                    "materialization-drift", "existing snapshot differs from sealed content")
        return destination
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.mkdir(parents=True, mode=0o700)
    try:
        for item in files:
            path = temporary / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            store.copy_ref(item["contentRef"], path)
            require(path.stat().st_size == item["size"], "invalid-export-file", "file manifest size mismatch")
        for directory in sorted((p for p in temporary.rglob("*") if p.is_dir()), reverse=True):
            sync_dir(directory)
        sync_dir(temporary)
        os.rename(temporary, destination)
        sync_dir(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
