import { chmod } from "node:fs/promises";
import { restoreEndpointCredential, persistedEndpointCredential } from "./endpoint-credentials.js";
import path from "node:path";
import type { EndpointVerificationV1, ExternalEndpointBindingV1, ExternalEndpointConfigV1, ReadyEndpointBindingV1 } from "../domain/index.js";
import { atomicWriteJSON, ensureDir, HitchError, invalidInput, readJSON, sha256JSON, statePaths, withFileLock } from "../foundation/index.js";
import { parseEndpointBinding, parseEndpointConfig, parseGeneration, parseReadyEndpointBinding, publicEndpointBinding } from "./endpoint-contract.js";

function directory(root: string): string { return path.join(statePaths(root).root, "private", "model-endpoints"); }
function configFile(root: string, revision: string): string { return path.join(directory(root), "revisions", `${revision.slice(7)}.json`); }
export async function connectEndpoint(root: string, name: string, value: unknown): Promise<ExternalEndpointBindingV1> {
  if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(name)) throw invalidInput("endpoint name must use lowercase letters, digits, '.', '_' or '-'");
  const config = parseEndpointConfig(value), revision = sha256JSON(config), base = await ensureDir(directory(root));
  await chmod(base, 0o700);
  await withFileLock(path.join(base, "locks"), "registry", async () => {
    await atomicWriteJSON(configFile(root, revision), config); await chmod(configFile(root, revision), 0o600);
    const aliases = await readJSON<Record<string, string>>(path.join(base, "aliases.json"), {});
    await atomicWriteJSON(path.join(base, "aliases.json"), { ...aliases, [name]: revision });
  });
  return { schema_version: "1", kind: "external-endpoint", endpoint_ref: `endpoint/${name}`, endpoint_revision: revision,
    api: config.api, wire_model: config.wire_model, capabilities: config.capabilities, generation: {} };
}
export async function resolveEndpoint(root: string, reference: string, generation: unknown = {}): Promise<ReadyEndpointBindingV1> {
  const match = reference.match(/^endpoint\/([a-z0-9][a-z0-9._-]{0,63})(?:@(sha256:[a-f0-9]{64}))?$/);
  if (!match) throw invalidInput("model endpoint must be endpoint/<name> or endpoint/<name>@sha256:<revision>");
  const aliases = await readJSON<Record<string, string>>(path.join(directory(root), "aliases.json"), {});
  const revision = match[2] ?? aliases[match[1]!];
  if (!revision || !/^sha256:[a-f0-9]{64}$/.test(revision)) throw invalidInput(`endpoint is not registered: ${reference}`);
  const config = parseEndpointConfig(await readJSON(configFile(root, revision)));
  if (sha256JSON(config) !== revision) throw invalidInput("stored endpoint revision has changed");
  return restoreEndpointCredential(root, { schema_version: "1", kind: "external-endpoint", endpoint_ref: reference, endpoint_revision: revision as ExternalEndpointBindingV1["endpoint_revision"],
    api: config.api, wire_model: config.wire_model, capabilities: config.capabilities, generation: parseGeneration(generation),
    connection: { base_url: config.base_url, auth: config.auth } });
}
export async function loadFrozenEndpoint(root: string, binding: ExternalEndpointBindingV1): Promise<ReadyEndpointBindingV1> {
  const publicBinding = parseEndpointBinding(binding), config = parseEndpointConfig(await readJSON(configFile(root, binding.endpoint_revision)));
  return restoreEndpointCredential(root, parseReadyEndpointBinding({ ...publicBinding, connection: { base_url: config.base_url, auth: config.auth } }));
}
export function endpointCredential(ready: ReadyEndpointBindingV1, env: NodeJS.ProcessEnv = process.env): string | undefined {
  if (ready.connection.auth.kind === "none") return undefined;
  const value = persistedEndpointCredential(ready) ?? env[ready.connection.auth.name];
  if (!value || /[\r\n\0]/.test(value)) throw new HitchError(`endpoint credential environment variable is unavailable: ${ready.connection.auth.name}`, { code: "endpoint_credential_missing", exitCode: 2 });
  return value;
}
export async function freezeEndpointRequest<T extends { model?: unknown; harness_ref?: unknown; endpoint_binding?: unknown; model_binding?: unknown; generation?: unknown }>(root: string, request: T, env?: NodeJS.ProcessEnv): Promise<T> {
  const endpoint = typeof request.model === "string" && request.model.startsWith("endpoint/");
  if (!endpoint) {
    if (request.endpoint_binding || request.model_binding || request.generation) throw invalidInput("endpoint binding/generation requires an endpoint/<name> model");
    return request;
  }
  let ready: ReadyEndpointBindingV1;
  if (request.model_binding) {
    ready = await restoreEndpointCredential(root, parseReadyEndpointBinding(request.model_binding));
    if (request.generation !== undefined && sha256JSON(parseGeneration(request.generation)) !== sha256JSON(ready.generation)) throw invalidInput("generation conflicts with ready endpoint binding");
  } else if (request.endpoint_binding) ready = await loadFrozenEndpoint(root, parseEndpointBinding(request.endpoint_binding));
  else ready = await resolveEndpoint(root, request.model as string, request.generation);
  if (ready.endpoint_ref !== request.model) throw invalidInput("endpoint binding does not match requested model");
  assertEndpointHarness(ready, String(request.harness_ref));
  endpointCredential(ready, env);
  if (request.model_binding) {
    await ensureDir(path.dirname(configFile(root, ready.endpoint_revision)));
    await atomicWriteJSON(configFile(root, ready.endpoint_revision), { schema_version: "1", ...ready.connection, api: ready.api, wire_model: ready.wire_model, capabilities: ready.capabilities });
    await chmod(configFile(root, ready.endpoint_revision), 0o600);
  }
  await requireEndpointVerification(root, publicEndpointBinding(ready), env);
  const { model_binding: _input, generation: _generation, ...rest } = request;
  return { ...rest, endpoint_binding: publicEndpointBinding(ready) } as T;
}
export function assertEndpointHarness(binding: ExternalEndpointBindingV1, harness: string): void {
  const id = harness.split("@")[0];
  if (id !== "codex" && id !== "model-call" && id !== "training-tool") throw invalidInput(`external endpoints currently support codex, model-call and training-tool; unsupported Harness: ${id}`);
  if (id === "training-tool" && (binding.api !== "chat-completions" || !binding.capabilities.tool_calls)) throw invalidInput("training-tool ordinary evaluation requires a Chat Completions endpoint with function tools");
  if (id === "codex" && (binding.api !== "responses" || !binding.capabilities.streaming || !binding.capabilities.tool_calls)) throw invalidInput("Codex requires a Responses endpoint with streaming and function tools");
}
export async function saveEndpointVerification(root: string, value: EndpointVerificationV1): Promise<void> {
  await atomicWriteJSON(path.join(directory(root), "verification", `${value.endpoint_revision.slice(7)}.json`), value);
}
export async function requireEndpointVerification(root: string, binding: ExternalEndpointBindingV1, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const v = await readJSON<EndpointVerificationV1 | null>(path.join(directory(root), "verification", `${binding.endpoint_revision.slice(7)}.json`), null);
  if (!v || v.endpoint_revision !== binding.endpoint_revision || v.api !== binding.api
    || binding.capabilities.streaming && !v.streaming || binding.capabilities.tool_calls && !v.tool_calls || binding.capabilities.streaming && binding.capabilities.tool_calls && !v.streamed_tool_calls) {
    const { verifyEndpoint } = await import("./endpoint-verification.js");
    await saveEndpointVerification(root, await verifyEndpoint(await loadFrozenEndpoint(root, binding), env));
  }
}

export async function freezeEndpointSubmission<T>(root: string, input: T, env: NodeJS.ProcessEnv = process.env): Promise<T> {
  const record = input as Record<string, unknown>;
  if (record.request && typeof record.request === "object") return { ...record, request: await freezeEndpointRequest(root, record.request as Record<string, unknown>, env) } as T;
  return await freezeEndpointRequest(root, record, env) as T;
}
