import path from "node:path";
import type { InferenceLockV1, InferencePoolTopologyV3, LocalInferenceBackend, Sha256 } from "../domain/index.js";
import { HitchError, readJSON, sha256JSON, statePaths } from "../foundation/index.js";
import { inferenceLockIdentity, parseModelNodeBinding } from "./manifest.js";

/** Read historical locks without installing their former model runtime. */
export async function loadInferenceLock(root: string, inferenceId: Sha256): Promise<InferenceLockV1> {
  let value: unknown;
  try {
    value = await readJSON(path.join(statePaths(root).inferenceLocks, inferenceId.slice("sha256:".length), "lock.json"));
  } catch (error) {
    throw new HitchError(`inference lock is unavailable: ${inferenceId}`, { code: "inference_lock_mismatch", exitCode: 2, cause: error });
  }
  const lock = validateInferenceLockShape(value);
  if (lock.inference_id !== inferenceId) {
    throw new HitchError("inference lock path and identity do not match", { code: "inference_lock_mismatch", exitCode: 5 });
  }
  return lock;
}

export function validateInferenceLockShape(value: unknown): InferenceLockV1 {
  const record = exact(value, ["schema_version", "engine", "model_id", "runtime_id", "inference_id", "profile", "execution", "generation", "protocol", "resources", "model_node", "topology"], "inference lock");
  if (!["1", "2", "3"].includes(String(record.schema_version)) || record.engine !== "sglang"
    || !digest(record.model_id) || !digest(record.runtime_id) || !digest(record.inference_id)
    || (record.profile !== "baseline" && record.profile !== "throughput")) throw lockError("inference lock identity is invalid");
  const execution = parseExecution(record.execution, record.schema_version === "3");
  const generation = parseGeneration(record.generation);
  const protocol = parseProtocol(record.protocol);
  const modelNode = record.schema_version !== "1" ? parseModelNodeBinding(record.model_node) : undefined;
  if (!modelNode && record.model_node !== undefined) throw lockError("legacy inference lock cannot contain a model node");
  if (modelNode && (execution.platform.backend !== "cuda" || !execution.platform.device_constraint)) throw lockError("model node requires an explicit physical CUDA device");
  const topology = record.schema_version === "3" ? parseInferencePoolTopology(record.topology, execution) : undefined;
  if (!topology && record.topology !== undefined) throw lockError("legacy inference locks cannot contain a replica pool");
  const gpuCount = topology ? topology.replicas.length * execution.tensor_parallel_size : 1;
  const resources = parseResources(record.resources, execution.platform.backend, !!modelNode, gpuCount);
  const lock: InferenceLockV1 = {
    schema_version: record.schema_version as InferenceLockV1["schema_version"], ...(topology ? { topology } : {}), ...(modelNode ? { model_node: modelNode } : {}), engine: "sglang", model_id: record.model_id, runtime_id: record.runtime_id,
    inference_id: record.inference_id, profile: record.profile, execution, generation, protocol, resources,
  };
  validateProfile(lock);
  const { inference_id: _inferenceId, ...identity } = lock;
  if (inferenceLockIdentity(identity) !== lock.inference_id) throw lockError("inference lock identity mismatch");
  return lock;
}

function parseDevices(value: unknown): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.some(v => typeof v !== "string" || !/^GPU-[A-Za-z0-9-]+$/.test(v))
    || new Set(value).size !== value.length) throw lockError("replica devices require unique physical GPU UUIDs");
  return [...value] as string[];
}

export function parseInferencePoolTopology(value: unknown, execution?: InferenceLockV1["execution"]): InferencePoolTopologyV3 {
  const record = exact(value, ["replicas", "routing", "max_running_requests", "max_queued_requests"], "inference pool");
  if (record.routing !== "least-active" || !Array.isArray(record.replicas) || record.replicas.length === 0) throw lockError("invalid inference pool routing/replicas");
  const replicas = record.replicas.map(value => ({ devices: parseDevices(exact(value, ["devices"], "inference replica").devices) }));
  const tp = replicas[0]!.devices.length;
  const devices = replicas.flatMap(r => r.devices);
  if (replicas.some(r => r.devices.length !== tp) || new Set(devices).size !== devices.length) throw lockError("replicas must use disjoint equally sized TP groups");
  const running = positiveInteger(record.max_running_requests, "pool running capacity");
  const queued = nonNegativeInteger(record.max_queued_requests, "pool queue capacity");
  if (execution && (execution.platform.backend !== "cuda" || execution.tensor_parallel_size !== tp
    || JSON.stringify(execution.platform.device_constraints) !== JSON.stringify(replicas[0]!.devices)
    || execution.platform.device_constraint !== replicas[0]!.devices[0] || running > replicas.length * execution.max_running_requests)) throw lockError("pool does not match per-engine execution capacity and TP placement");
  return { replicas, routing: "least-active", max_running_requests: running, max_queued_requests: queued };
}

function parseExecution(value: unknown, pool: boolean): InferenceLockV1["execution"] {
  const record = exact(value, [
    "platform", "load_format", "dtype", "quantization", "tensor_parallel_size", "data_parallel_size", "pipeline_parallel_size",
    "context_tokens_per_request", "max_running_requests", "max_queued_requests", "max_total_tokens", "chunked_prefill_size",
    "max_prefill_tokens", "kv_cache_dtype", "attention_backend", "sampling_backend", "deterministic_inference", "prefix_cache",
    "hicache", "speculative_decoding", "cpu_offload_gb", "startup_timeout_ms", "queue_timeout_ms", "request_timeout_ms", "idle_ttl_ms",
  ], "inference execution");
  if (record.load_format !== "safetensors" || (pool ? !Number.isSafeInteger(record.tensor_parallel_size) || (record.tensor_parallel_size as number) < 1 : record.tensor_parallel_size !== 1) || record.data_parallel_size !== 1
    || record.pipeline_parallel_size !== 1 || typeof record.dtype !== "string" || !record.dtype
    || record.quantization !== null && (typeof record.quantization !== "string" || !record.quantization)
    || typeof record.deterministic_inference !== "boolean" || record.hicache !== false
    || record.speculative_decoding !== false || record.cpu_offload_gb !== 0) throw lockError("inference execution configuration is invalid");
  const prefix = exact(record.prefix_cache, ["mode", "scope", "initial_state"], "inference prefix cache");
  if ((prefix.mode !== "disabled" && prefix.mode !== "radix") || (prefix.scope !== "run" && prefix.scope !== "eval") || prefix.initial_state !== "empty") {
    throw lockError("inference prefix cache configuration is invalid");
  }
  const positiveFields = [
    "context_tokens_per_request", "max_running_requests", "max_total_tokens", "max_prefill_tokens",
    "startup_timeout_ms", "queue_timeout_ms", "request_timeout_ms",
  ] as const;
  for (const field of positiveFields) positiveInteger(record[field], `inference execution ${field}`);
  nonNegativeInteger(record.max_queued_requests, "inference execution max_queued_requests");
  nonNegativeInteger(record.idle_ttl_ms, "inference execution idle_ttl_ms");
  if (!Number.isSafeInteger(record.chunked_prefill_size) || (record.chunked_prefill_size as number) < -1) throw lockError("inference chunked prefill size is invalid");
  if ((record.max_total_tokens as number) < (record.context_tokens_per_request as number)
    || (record.max_prefill_tokens as number) > (record.context_tokens_per_request as number)) {
    throw lockError("inference token budgets are inconsistent");
  }
  const platform = parsePlatform(record.platform, pool);
  return {
    platform,
    load_format: "safetensors",
    dtype: record.dtype,
    quantization: record.quantization as string | null,
    tensor_parallel_size: record.tensor_parallel_size as number, data_parallel_size: 1, pipeline_parallel_size: 1,
    context_tokens_per_request: record.context_tokens_per_request as number,
    max_running_requests: record.max_running_requests as number,
    max_queued_requests: record.max_queued_requests as number,
    max_total_tokens: record.max_total_tokens as number,
    chunked_prefill_size: record.chunked_prefill_size as number,
    max_prefill_tokens: record.max_prefill_tokens as number,
    kv_cache_dtype: nonEmptyString(record.kv_cache_dtype, "inference KV cache dtype"),
    attention_backend: nonEmptyString(record.attention_backend, "inference attention backend"),
    sampling_backend: nonEmptyString(record.sampling_backend, "inference sampling backend"),
    deterministic_inference: record.deterministic_inference,
    prefix_cache: { mode: prefix.mode, scope: prefix.scope, initial_state: "empty" },
    hicache: false, speculative_decoding: false, cpu_offload_gb: 0,
    startup_timeout_ms: record.startup_timeout_ms as number,
    queue_timeout_ms: record.queue_timeout_ms as number,
    request_timeout_ms: record.request_timeout_ms as number,
    idle_ttl_ms: record.idle_ttl_ms as number,
  };
}

function parsePlatform(value: unknown, pool: boolean): InferenceLockV1["execution"]["platform"] {
  const base = exactRecord(value, "inference platform");
  if (base.backend === "cpu") {
    const record = exact(base, ["backend", "cpu_threads", "numa_policy", "cpu_feature_requirement", "overlap_schedule"], "CPU inference platform");
    if (record.numa_policy !== "single-node" || record.cpu_feature_requirement !== "amx" || record.overlap_schedule !== false) throw lockError("CPU inference platform is invalid");
    return { backend: "cpu", cpu_threads: positiveInteger(record.cpu_threads, "CPU inference threads"), numa_policy: "single-node", cpu_feature_requirement: "amx", overlap_schedule: false };
  }
  if (base.backend === "cuda") {
    const record = exact(base, ["backend", "device_constraint", ...(pool ? ["device_constraints"] : []), "mem_fraction_static", "overlap_schedule", "cuda_graph"], "CUDA inference platform");
    if (record.device_constraint !== undefined && (typeof record.device_constraint !== "string" || !record.device_constraint)
      || typeof record.mem_fraction_static !== "number" || !Number.isFinite(record.mem_fraction_static)
      || record.mem_fraction_static <= 0 || record.mem_fraction_static > 1 || typeof record.overlap_schedule !== "boolean"
      || record.cuda_graph !== "enabled" && record.cuda_graph !== "disabled") throw lockError("CUDA inference platform is invalid");
    return { backend: "cuda", ...(pool ? { device_constraints: parseDevices(record.device_constraints) } : {}), ...(record.device_constraint ? { device_constraint: record.device_constraint as string } : {}), mem_fraction_static: record.mem_fraction_static, overlap_schedule: record.overlap_schedule, cuda_graph: record.cuda_graph };
  }
  if (base.backend === "metal") {
    const record = exact(base, ["backend", "mlx_quantization", "overlap_schedule"], "Metal inference platform");
    if (record.mlx_quantization !== "none" && record.mlx_quantization !== "prequantized" || typeof record.overlap_schedule !== "boolean") throw lockError("Metal inference platform is invalid");
    return { backend: "metal", mlx_quantization: record.mlx_quantization, overlap_schedule: record.overlap_schedule };
  }
  throw lockError("inference platform backend is invalid");
}

function parseGeneration(value: unknown): InferenceLockV1["generation"] {
  const record = exact(value, ["temperature", "top_p", "top_k", "min_p", "repetition_penalty", "seed", "max_output_tokens", "override_policy"], "inference generation");
  const finite = (field: string): number => {
    const number = record[field];
    if (typeof number !== "number" || !Number.isFinite(number)) throw lockError(`inference generation ${field} is invalid`);
    return number;
  };
  const temperature = finite("temperature"), topP = finite("top_p"), minP = finite("min_p"), penalty = finite("repetition_penalty");
  if (temperature < 0 || topP < 0 || topP > 1 || minP < 0 || minP > 1 || penalty <= 0
    || !Number.isSafeInteger(record.top_k) || (record.top_k as number) < 0 || !Number.isSafeInteger(record.seed)
    || record.override_policy !== "reject-conflicts") throw lockError("inference generation configuration is invalid");
  return { temperature, top_p: topP, top_k: record.top_k as number, min_p: minP, repetition_penalty: penalty,
    seed: record.seed as number, max_output_tokens: positiveInteger(record.max_output_tokens, "inference max output tokens"), override_policy: "reject-conflicts" };
}

function parseProtocol(value: unknown): InferenceLockV1["protocol"] {
  const record = exact(value, ["api", "streaming", "tool_calls", "parallel_tool_calls", "input_modalities", "tool_call_parser", "reasoning_parser", "compatibility_profile_id"], "inference protocol");
  if (record.api !== "responses" && record.api !== "chat-completions" || typeof record.streaming !== "boolean"
    || typeof record.tool_calls !== "boolean" || typeof record.parallel_tool_calls !== "boolean"
    || !Array.isArray(record.input_modalities) || record.input_modalities.length !== 1 || record.input_modalities[0] !== "text"
    || record.tool_call_parser !== null && (typeof record.tool_call_parser !== "string" || !record.tool_call_parser)
    || record.reasoning_parser !== null && (typeof record.reasoning_parser !== "string" || !record.reasoning_parser)
    || !digest(record.compatibility_profile_id) || record.parallel_tool_calls && !record.tool_calls) throw lockError("inference protocol configuration is invalid");
  return { api: record.api, streaming: record.streaming, tool_calls: record.tool_calls, parallel_tool_calls: record.parallel_tool_calls,
    input_modalities: ["text"], tool_call_parser: record.tool_call_parser as string | null,
    reasoning_parser: record.reasoning_parser as string | null, compatibility_profile_id: record.compatibility_profile_id };
}

function parseResources(value: unknown, backend: "cpu" | "cuda" | "metal", processNode: boolean, gpuCount: number): InferenceLockV1["resources"] {
  const record = exact(value, ["cpu_millis", "memory_bytes", "container_slots", "build_slots", "gpu_count", "ephemeral_disk_bytes"], "inference resources");
  const resources = {
    cpu_millis: positiveInteger(record.cpu_millis, "inference CPU reservation"),
    memory_bytes: positiveInteger(record.memory_bytes, "inference memory reservation"),
    container_slots: processNode ? nonNegativeInteger(record.container_slots, "inference container reservation") : positiveInteger(record.container_slots, "inference container reservation"),
    build_slots: nonNegativeInteger(record.build_slots, "inference build reservation"),
    ...(record.gpu_count === undefined ? {} : { gpu_count: nonNegativeInteger(record.gpu_count, "inference GPU reservation") }),
    ...(record.ephemeral_disk_bytes === undefined ? {} : { ephemeral_disk_bytes: nonNegativeInteger(record.ephemeral_disk_bytes, "inference disk reservation") }),
  };
  if (resources.build_slots !== 0 || processNode && resources.container_slots !== 0 || backend === "cuda" && resources.gpu_count !== gpuCount
    || backend !== "cuda" && (resources.gpu_count ?? 0) !== 0) throw lockError("inference resource/backend combination is invalid");
  return resources;
}

function validateProfile(lock: InferenceLockV1): void {
  const throughput = lock.profile === "throughput";
  const expectedRunning = throughput ? lock.execution.platform.backend === "cuda" ? 8 : 2 : 1;
  if (lock.execution.max_running_requests !== expectedRunning || lock.execution.max_queued_requests !== expectedRunning
    || throughput !== (lock.execution.prefix_cache.mode === "radix") || lock.execution.prefix_cache.scope !== "eval") {
    throw lockError("inference lock does not match its profile");
  }
}

function exact(value: unknown, fields: readonly string[], label: string): Record<string, unknown> {
  const record = exactRecord(value, label);
  const allowed = new Set(fields);
  const unknown = Object.keys(record).find((field) => !allowed.has(field));
  if (unknown) throw lockError(`${label} has unknown field: ${unknown}`);
  return record;
}

function exactRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw lockError(`${label} must be an object`);
  return value as Record<string, unknown>;
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw lockError(`${label} must be a non-empty string`);
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) throw lockError(`${label} must be a positive safe integer`);
  return value as number;
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw lockError(`${label} must be a non-negative safe integer`);
  return value as number;
}

function toolParserFor(modelType: string): string | null {
  if (/^qwen(?:2|3)/.test(modelType)) return "qwen25";
  if (/^llama/.test(modelType)) return "llama3";
  return null;
}

function digest(value: unknown): value is Sha256 {
  return typeof value === "string" && /^sha256:[a-f0-9]{64}$/.test(value);
}

function lockError(message: string): HitchError {
  return new HitchError(message, { code: "inference_lock_mismatch", exitCode: 5 });
}
