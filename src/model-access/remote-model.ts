import type { RemoteModelBindingV2 } from "../domain/index.js";
import { HitchError } from "../foundation/index.js";
import { bindTrainingRun, parseTrainingBinding } from "./training.js";
import type { RegisteredTrainingEndpoint } from "./training.js";
import { parseEndpointBinding, publicEndpointBinding, endpointRequest } from "./endpoint-contract.js";
import { endpointCredential } from "./endpoints.js";
export type RemoteModelTargetV2 = { kind: "training-external"; endpoint: RegisteredTrainingEndpoint }
  | { kind: "external-endpoint"; endpoint: import("../domain/index.js").ReadyEndpointBindingV1 };
export function parseRemoteModelBinding(value: unknown): RemoteModelBindingV2 {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("remote model binding must be an object");
  const r = value as Record<string, unknown>, fields = r.kind === "training-external" ? ["schema_version", "kind", "training"] : ["schema_version", "kind", "endpoint"];
  if (r.schema_version !== "2" || Object.keys(r).some(k => !fields.includes(k)) || fields.some(k => !(k in r))) throw new TypeError("invalid remote model binding fields");
  if (r.kind === "training-external") return { schema_version: "2", kind: r.kind, training: parseTrainingBinding(r.training) };
  if (r.kind !== "external-endpoint") throw new TypeError("retired or unsupported remote model route; create a new endpoint task");
  return { schema_version: "2", kind: r.kind, endpoint: parseEndpointBinding(r.endpoint) };
}
export function remoteModelBinding(target: RemoteModelTargetV2): RemoteModelBindingV2 {
  return target.kind === "training-external" ? { schema_version: "2", kind: target.kind, training: target.endpoint.binding }
    : { schema_version: "2", kind: target.kind, endpoint: publicEndpointBinding(target.endpoint) };
}
/** The caller authenticates an exact worker lease; this route exposes generation only, without retries. */
export async function callRemoteModel(target: RemoteModelTargetV2, runId: string, operation: "bind" | "generate", body: unknown, signal: AbortSignal, env: NodeJS.ProcessEnv = process.env): Promise<Response> {
  const binding = remoteModelBinding(target);
  if (!/^run_[a-f0-9]{32}$/.test(runId)) throw new TypeError("remote model needs a canonical run identity");
  if (target.kind === "training-external") {
    if (Date.parse(target.endpoint.binding.expiresAt) <= Date.now()) throw new HitchError("training policy lease expired", { code: "training_lease_expired", exitCode: 12 });
    if (operation === "bind") await bindTrainingRun(target.endpoint, runId, signal);
  }
  if (operation === "bind") return Response.json({ schema_version: "2", run_id: runId, model_binding: binding });
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new TypeError("remote generation requires a JSON envelope");
  const request = body as { payload?: unknown; idempotency_key?: unknown };
  if (Object.keys(body).some(k => !["payload", "idempotency_key"].includes(k)) || !request.payload || typeof request.payload !== "object" || Array.isArray(request.payload)
    || request.idempotency_key !== undefined && (typeof request.idempotency_key !== "string" || !request.idempotency_key || request.idempotency_key.length > 256 || /[\s\0]/.test(request.idempotency_key))) throw new TypeError("invalid remote generation envelope");
  const api = target.kind === "training-external" ? "chat-completions" : target.endpoint.api;
  const url = target.kind === "training-external" ? target.endpoint.base_url : target.endpoint.connection.base_url;
  const credential = target.kind === "training-external" ? target.endpoint.credential : endpointCredential(target.endpoint, env);
  const payload = target.kind === "training-external" ? { ...request.payload, model: target.endpoint.binding.expectedPolicyVersion }
    : endpointRequest(target.endpoint, request.payload as Record<string, unknown>);
  return fetch(`${url.replace(/\/+$/, "")}/${api === "responses" ? "responses" : "chat/completions"}`, {
    method: "POST", headers: { "content-type": "application/json", ...(credential ? { authorization: `Bearer ${credential}` } : {}),
      ...(typeof request.idempotency_key === "string" ? { "idempotency-key": request.idempotency_key } : {}) }, body: JSON.stringify(payload), signal, redirect: "error" });
}
