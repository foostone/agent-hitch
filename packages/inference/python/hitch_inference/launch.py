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

def runtime_matches(manifest, actual):
    require(
        manifest.get("schema_version") == "2"
        and manifest.get("engine") == "sglang"
        and manifest.get("backend") in ("cpu", "cuda")
        and manifest.get("package", {}).get("kind") == "python-env",
        "invalid-process-runtime",
        "process services require a v2 Python CPU/CUDA runtime",
    )
    require(
        manifest["runtime_id"]
        == digest_json({k: v for k, v in manifest.items() if k != "runtime_id"}),
        "runtime-identity-drift",
        "runtime manifest identity differs",
    )
    require(
        manifest["package"]
        == {
            "kind": "python-env",
            "environment_digest": digest_json(actual),
            "python_version": actual["pythonVersion"],
            "packages_digest": actual["packagesDigest"],
        }
        and manifest["sglang_version"] == package_version(actual, "sglang"),
        "node-runtime-drift",
        "observed Python environment differs from the immutable inference runtime",
    )
    installed = [p for p in actual["packages"] if p["name"] == "sglang"]
    require(
        len(installed) == 1 and manifest.get("sglang_commit") == installed[0]["commit"],
        "node-runtime-drift",
        "SGLang source commit differs; wheels without VCS provenance must declare null",
    )


def sglang_arguments(request, model_path, access):
    frozen = request["lock"]
    execution = frozen["execution"]
    backend = execution["platform"]
    protocol = frozen["protocol"]
    args = [
        "--model-path",
        str(model_path),
        "--served-model-name",
        access["wireModel"],
        "--host",
        "127.0.0.1",
        "--port",
        str(access["port"]),
        "--api-key",
        access["engineToken"],
        "--admin-api-key",
        access["adminToken"],
        "--random-seed",
        str(frozen["generation"]["seed"]),
        "--tp-size",
        str(execution["tensor_parallel_size"]),
        "--dp-size",
        "1",
        "--pp-size",
        "1",
    ]
    # Request logging defaults to false. SGLang has no --disable-request-logging.
    for name, flag in (
        ("load_format", "load-format"),
        ("dtype", "dtype"),
        ("context_tokens_per_request", "context-length"),
        ("max_running_requests", "max-running-requests"),
        ("max_total_tokens", "max-total-tokens"),
        ("chunked_prefill_size", "chunked-prefill-size"),
        ("max_prefill_tokens", "max-prefill-tokens"),
        ("kv_cache_dtype", "kv-cache-dtype"),
        ("attention_backend", "attention-backend"),
        ("sampling_backend", "sampling-backend"),
    ):
        args += ["--" + flag, str(execution[name])]
    if execution["prefix_cache"]["mode"] == "disabled":
        args += ["--disable-radix-cache"]
    if not backend["overlap_schedule"]:
        args += ["--disable-overlap-schedule"]
    if backend["backend"] == "cuda":
        args += ["--mem-fraction-static", str(backend["mem_fraction_static"])]
        if backend["cuda_graph"] == "disabled":
            args += ["--disable-cuda-graph"]
    else:
        args += ["--device", "cpu"]
    for field in ("tool_call_parser", "reasoning_parser"):
        if protocol[field]:
            args += ["--" + field.replace("_", "-"), protocol[field]]
    if execution["deterministic_inference"]:
        args += ["--enable-deterministic-inference"]
    return args
