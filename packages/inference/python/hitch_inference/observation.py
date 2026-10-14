"""Whitelisted checks performed for every replica before pool readiness."""
from .content import require, digest_json
from .topology import selected_devices


def validate_engine_info(info, frozen, runtime):
    execution, backend = frozen["execution"], frozen["execution"]["platform"]
    expected = {"version": runtime["sglang_version"], "device": backend["backend"],
        "dtype": execution["dtype"], "kv_cache_dtype": execution["kv_cache_dtype"],
        "attention_backend": execution["attention_backend"], "sampling_backend": execution["sampling_backend"],
        "context_length": execution["context_tokens_per_request"], "max_running_requests": execution["max_running_requests"],
        "tp_size": execution["tensor_parallel_size"], "dp_size": 1, "pp_size": 1,
        "disable_radix_cache": execution["prefix_cache"]["mode"] == "disabled",
        "disable_overlap_schedule": not backend["overlap_schedule"]}
    if backend["backend"] == "cuda":
        expected["mem_fraction_static"] = backend["mem_fraction_static"]
    require(isinstance(info, dict), "inference-runtime-mismatch", "invalid SGLang server observation")
    aliases = {"bf16": "bfloat16", "half": "float16", "fp16": "float16", "float": "float32"}
    for key, value in expected.items():
        actual = info.get(key)
        if key in ("dtype", "kv_cache_dtype"):
            actual, value = aliases.get(actual, actual), aliases.get(value, value)
        require(actual == value, "inference-runtime-mismatch", "replica server configuration differs: " + key)
    require(type(info.get("max_total_num_tokens")) is int and info["max_total_num_tokens"] >= execution["max_total_tokens"],
            "inference-runtime-mismatch", "replica token capacity is below the frozen budget")
    if backend["backend"] == "cuda":
        require(info.get("mem_fraction_static") == backend["mem_fraction_static"],
                "inference-runtime-mismatch", "replica CUDA memory fraction changed")
    return {"devices": selected_devices(frozen), "configuration": {key: info[key] for key in expected},
            "max_total_num_tokens": info["max_total_num_tokens"], "runtime_id": runtime["runtime_id"],
            "inference_id": frozen["inference_id"]}
