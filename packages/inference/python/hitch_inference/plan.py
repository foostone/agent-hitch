"""Register a local HF directory and freeze an explicit ordinary inference plan."""
import argparse
import json
import uuid
from .artifacts import hf_manifest
from .content import digest_json, require
from .node import NodeService
from .runtime import observe_runtime, package_version
from .snapshots import seal_directory
from .state import load
from .topology import validate_topology


def plan(config, directory, replicas, profile="baseline", running=None, queued=None, api="chat-completions"):
    node = NodeService(config)
    runtime = observe_runtime()
    version = package_version(runtime, "sglang")
    require(version == "0.5.15.post1", "unsupported-engine-interface", "standalone SGLang adapter requires version 0.5.15.post1")
    snapshot = seal_directory(node.store, directory, serving=True)
    model = hf_manifest(node.store, {"snapshotRef": snapshot})
    require(model["quantization"] is None and model["dtype"] != "unknown",
            "unsupported-model", "standalone v1 requires declared dtype and unquantized dense safetensors")
    runtime_body = {"schema_version": "2", "engine": "sglang", "sglang_version": version,
        "sglang_commit": next(p["commit"] for p in runtime["packages"] if p["name"] == "sglang"),
        "backend": "cuda", "package": {"kind": "python-env", "environment_digest": digest_json(runtime),
            "python_version": runtime["pythonVersion"], "packages_digest": runtime["packagesDigest"]},
        "compatibility_profile": "standalone-sglang-0.5.15.post1"}
    manifest = {**runtime_body, "runtime_id": digest_json(runtime_body)}
    require(profile in ("baseline", "throughput"), "invalid-profile", "profile must be baseline or throughput")
    throughput = profile == "throughput"
    capacity = 8 if throughput else 1
    context = min(8192, model["context_tokens"] or 8192)
    parser = "qwen25" if model["model_type"].startswith(("qwen2", "qwen3")) else "llama3" if model["model_type"].startswith("llama") else None
    topology = {"replicas": [{"devices": devices} for devices in replicas], "routing": "least-active",
        "max_running_requests": running if running is not None else capacity * len(replicas),
        "max_queued_requests": queued if queued is not None else capacity * len(replicas)}
    binding = {"schema_version": "2", "node_id": node.identity["nodeId"], "generation": node.identity["generation"],
        "runtime_digest": digest_json(runtime), "launcher": "process"}
    weights = sum(f["size"] for f in model["files"] if f["path"].endswith(".safetensors"))
    frozen = {"schema_version": "3", "engine": "sglang", "model_node": binding,
        "model_id": model["model_id"], "runtime_id": manifest["runtime_id"], "profile": profile, "topology": topology,
        "execution": {"platform": {"backend": "cuda", "device_constraint": replicas[0][0], "device_constraints": replicas[0],
            "mem_fraction_static": 0.8, "overlap_schedule": throughput, "cuda_graph": "enabled" if throughput else "disabled"},
            "load_format": "safetensors", "dtype": model["dtype"], "quantization": None,
            "tensor_parallel_size": len(replicas[0]), "data_parallel_size": 1, "pipeline_parallel_size": 1,
            "context_tokens_per_request": context, "max_running_requests": capacity, "max_queued_requests": capacity,
            "max_total_tokens": context * (capacity + 1), "chunked_prefill_size": min(4096, context) if throughput else -1,
            "max_prefill_tokens": context, "kv_cache_dtype": "bfloat16" if model["dtype"] in ("bf16", "bfloat16") else "auto",
            "attention_backend": "flashinfer", "sampling_backend": "flashinfer", "deterministic_inference": False,
            "prefix_cache": {"mode": "radix" if throughput else "disabled", "scope": "eval", "initial_state": "empty"},
            "hicache": False, "speculative_decoding": False, "cpu_offload_gb": 0,
            "startup_timeout_ms": 600000, "queue_timeout_ms": 60000, "request_timeout_ms": 600000, "idle_ttl_ms": 60000},
        "generation": {"temperature": 0, "top_p": 1, "top_k": 0, "min_p": 0, "repetition_penalty": 1, "seed": 0,
            "max_output_tokens": min(2048, max(1, context // 4)), "override_policy": "reject-conflicts"},
        "protocol": {"api": api, "streaming": True, "tool_calls": parser is not None, "parallel_tool_calls": False,
            "input_modalities": ["text"], "tool_call_parser": parser, "reasoning_parser": None,
            "compatibility_profile_id": digest_json({"engine": "sglang", "runtime_id": manifest["runtime_id"],
                "model_type": model["model_type"], "template_digest": model["template_digest"], "api": api, "tool_call_parser": parser})},
        "resources": {"cpu_millis": 2000 * len(replicas), "memory_bytes": len(replicas) * max(2 * 1024**3, int(weights * 1.25 + 1024**3 + 0.999999)),
            "container_slots": 0, "build_slots": 0, "gpu_count": sum(map(len, replicas)), "ephemeral_disk_bytes": 4 * 1024**3}}
    validate_topology(frozen)
    return {"serviceId": "inference_" + uuid.uuid4().hex, "model": model, "runtime": manifest,
            "lock": {**frozen, "inference_id": digest_json(frozen)}}


def main():
    parser = argparse.ArgumentParser(description="Register an HF model and emit an immutable inference deployment")
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--replica", action="append", required=True, help="comma-separated physical GPU UUIDs, one per TP rank; repeat for replicas")
    parser.add_argument("--profile", choices=("baseline", "throughput"), default="baseline")
    parser.add_argument("--max-running", type=int)
    parser.add_argument("--max-queued", type=int)
    args = parser.parse_args()
    print(json.dumps(plan(load(args.config), args.model, [v.split(",") for v in args.replica], args.profile, args.max_running, args.max_queued)))


if __name__ == "__main__":
    main()
