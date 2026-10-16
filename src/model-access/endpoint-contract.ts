import type { ExternalEndpointBindingV1, ExternalEndpointConfigV1, ReadyEndpointBindingV1 } from "../domain/index.js";
import { invalidInput, sha256JSON } from "../foundation/index.js";

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw invalidInput(`${label} must be an object`);
  return value as Record<string, unknown>;
}
function exact(value: Record<string, unknown>, fields: string[], label: string): void {
  if (Object.keys(value).some(key => !fields.includes(key))) throw invalidInput(`${label} has unknown fields`);
}
function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value || value.length > 4096 || /[\0\r\n]/.test(value)) throw invalidInput(`${label} must be a non-empty string`);
  return value;
}
export function parseEndpointConfig(value: unknown): ExternalEndpointConfigV1 {
  const r = object(value, "endpoint");
  exact(r, ["schema_version", "base_url", "wire_model", "api", "auth", "capabilities"], "endpoint");
  let url: URL;
  try { url = new URL(text(r.base_url, "base_url")); } catch { throw invalidInput("endpoint base_url must be an absolute URL"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw invalidInput("endpoint base_url cannot contain credentials, query or fragment");
  if (r.schema_version !== "1" || !["responses", "chat-completions"].includes(String(r.api))) throw invalidInput("endpoint schema_version/api is invalid");
  const auth = object(r.auth, "endpoint auth");
  if (auth.kind === "none") exact(auth, ["kind"], "endpoint auth");
  else if (auth.kind === "env" && typeof auth.name === "string" && /^[A-Za-z_][A-Za-z0-9_]*$/.test(auth.name)) exact(auth, ["kind", "name"], "endpoint auth");
  else throw invalidInput("endpoint auth must be explicit none or an environment variable reference");
  const cap = object(r.capabilities, "endpoint capabilities");
  exact(cap, ["streaming", "tool_calls", "parallel_tool_calls", "input_modalities"], "endpoint capabilities");
  if ([cap.streaming, cap.tool_calls, cap.parallel_tool_calls].some(v => typeof v !== "boolean") || JSON.stringify(cap.input_modalities) !== '["text"]'
    || cap.parallel_tool_calls && !cap.tool_calls) throw invalidInput("endpoint capabilities must declare text and explicit protocol features");
  if (cap.parallel_tool_calls) throw invalidInput("parallel function calls are not verified by this release; declare parallel_tool_calls=false");
  return { schema_version: "1", base_url: url.toString().replace(/\/+$/, ""), wire_model: text(r.wire_model, "wire_model"),
    api: r.api as ExternalEndpointConfigV1["api"], auth: auth.kind === "none" ? { kind: "none" } : { kind: "env", name: auth.name as string },
    capabilities: { streaming: cap.streaming as boolean, tool_calls: cap.tool_calls as boolean, parallel_tool_calls: cap.parallel_tool_calls as boolean, input_modalities: ["text"] } };
}
export function parseGeneration(value: unknown = {}): Record<string, number> {
  const r = object(value, "generation");
  exact(r, ["temperature", "top_p", "top_k", "min_p", "repetition_penalty", "seed", "max_output_tokens"], "generation");
  for (const [key, v] of Object.entries(r)) {
    if (typeof v !== "number" || !Number.isFinite(v)) throw invalidInput(`generation ${key} must be finite`);
    if (["max_output_tokens", "top_k"].includes(key) && (!Number.isSafeInteger(v) || v < (key === "max_output_tokens" ? 1 : -1))) throw invalidInput(`generation ${key} is invalid`);
  }
  return { ...r } as Record<string, number>;
}
export function parseEndpointBinding(value: unknown): ExternalEndpointBindingV1 {
  const r = object(value, "endpoint binding");
  exact(r, ["schema_version", "kind", "endpoint_ref", "endpoint_revision", "api", "wire_model", "capabilities", "generation", "deployment_receipt"], "endpoint binding");
  if (r.schema_version !== "1" || r.kind !== "external-endpoint" || typeof r.endpoint_ref !== "string"
    || !/^endpoint\/[a-z0-9][a-z0-9._-]{0,63}(?:@sha256:[a-f0-9]{64})?$/.test(r.endpoint_ref)
    || typeof r.endpoint_revision !== "string" || !/^sha256:[a-f0-9]{64}$/.test(r.endpoint_revision)) throw invalidInput("endpoint binding identity is invalid");
  if (r.endpoint_ref.includes("@") && r.endpoint_ref.split("@")[1] !== r.endpoint_revision) throw invalidInput("pinned endpoint ref differs from endpoint_revision");
  const config = parseEndpointConfig({ schema_version: "1", base_url: "http://endpoint.invalid", wire_model: r.wire_model, api: r.api, auth: { kind: "none" }, capabilities: r.capabilities });
  const result: ExternalEndpointBindingV1 = { schema_version: "1", kind: "external-endpoint", endpoint_ref: r.endpoint_ref,
    endpoint_revision: r.endpoint_revision as ExternalEndpointBindingV1["endpoint_revision"], api: config.api, wire_model: config.wire_model,
    capabilities: config.capabilities, generation: parseGeneration(r.generation) };
  if (r.deployment_receipt !== undefined) {
    const receipt = object(r.deployment_receipt, "deployment receipt");
    exact(receipt, ["schema_version", "model_digest", "wire_model", "deployment_ref", "source", "service_config"], "deployment receipt");
    if (receipt.schema_version !== "1" || typeof receipt.model_digest !== "string" || !/^sha256:[a-f0-9]{64}$/.test(receipt.model_digest)
      || receipt.wire_model !== config.wire_model) throw invalidInput("deployment receipt model/wire model is invalid");
    result.deployment_receipt = { schema_version: "1", model_digest: receipt.model_digest as ExternalEndpointBindingV1["endpoint_revision"],
      wire_model: config.wire_model, deployment_ref: text(receipt.deployment_ref, "deployment_ref"), source: text(receipt.source, "receipt source"),
      service_config: parsePublicServiceConfig(receipt.service_config) };
  }
  return result;
}
export function parseReadyEndpointBinding(value: unknown): ReadyEndpointBindingV1 {
  const { connection, ...publicValue } = object(value, "ready endpoint binding");
  const binding = parseEndpointBinding(publicValue), c = object(connection, "endpoint connection");
  exact(c, ["base_url", "auth"], "endpoint connection");
  const config = parseEndpointConfig({ schema_version: "1", ...c, api: binding.api, wire_model: binding.wire_model, capabilities: binding.capabilities });
  if (sha256JSON(config) !== binding.endpoint_revision) throw invalidInput("endpoint revision does not match its immutable configuration");
  return { ...binding, connection: { base_url: config.base_url, auth: config.auth } };
}
export function publicEndpointBinding(ready: ReadyEndpointBindingV1): ExternalEndpointBindingV1 {
  const { connection: _connection, ...binding } = ready;
  return parseEndpointBinding(binding);
}
export function endpointRequest(binding: ExternalEndpointBindingV1, payload: Record<string, unknown>): Record<string, unknown> {
  const generation: Record<string, number | boolean> = { ...binding.generation, ...(Array.isArray(payload.tools) && payload.tools.length ? { parallel_tool_calls: false } : {}) };
  if (binding.api === "chat-completions" && generation.max_output_tokens !== undefined) {
    const maxTokens = generation.max_output_tokens; delete generation.max_output_tokens;
    return { ...payload, ...generation, max_tokens: maxTokens, model: binding.wire_model };
  }
  return { ...payload, ...generation, model: binding.wire_model };
}

function parsePublicServiceConfig(value: unknown): Record<string, unknown> {
  const config = object(value, "service_config");
  exact(config, ["engine", "engine_version", "engine_digest", "quantization", "tool_parser", "reasoning_parser", "tokenizer_digest", "chat_template_digest", "cache_policy", "isolation", "parsing"], "public service_config");
  text(config.engine, "service_config engine");
  const forbidden = /^(?:connection|base_url|url|auth|credentials?|secrets?|token|api_key|password|env|model_digest|wire_model|deployment_ref|endpoint_revision)$/i;
  const walk = (item: unknown): void => {
    if (typeof item === "string" && /(?:https?|ssh|file):\/\//i.test(item)) throw invalidInput("public service_config cannot contain connection URLs");
    if (Array.isArray(item)) for (const v of item) walk(v);
    else if (item && typeof item === "object") for (const [key, v] of Object.entries(item)) {
      if (forbidden.test(key)) throw invalidInput(`private/candidate-specific field is prohibited in public service_config: ${key}`);
      walk(v);
    }
  };
  walk(config); return config;
}
