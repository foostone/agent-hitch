import path from "node:path";
import type { ExternalEndpointBindingV1, RunId } from "../domain/index.js";
import type { AdapterProcessRuntime } from "../adapters/index.js";
import { HostModelProxy, endpointCredential, loadFrozenEndpoint } from "../model-access/index.js";
import { invalidInput } from "../foundation/index.js";

export async function runEndpointRuntime(root: string, runId: RunId, binding: ExternalEndpointBindingV1 | undefined): Promise<{ runtime: AdapterProcessRuntime; close(): Promise<void>; credentials: string[] }> {
  if (!binding) return { runtime: {}, close: async () => {}, credentials: [] };
  const handedOff = process.env.HITCH_EXTERNAL_ENDPOINT_BINDING;
  if (handedOff) {
    if (process.env.HITCH_HARBOR_INTERNAL !== "1" || process.env.HITCH_EXTERNAL_RUN_ID !== runId
      || JSON.stringify(binding) !== JSON.stringify(JSON.parse(handedOff))) throw invalidInput("external endpoint Harbor handoff differs from its frozen binding");
    const base = process.env.OPENAI_BASE_URL;
    const url = new URL(base ?? "");
    if (url.protocol !== "http:" || url.username || url.password || url.search || url.hash
      || !new RegExp(`^/[a-f0-9]{48}/${runId}/openai/?$`).test(url.pathname)) throw invalidInput("invalid external endpoint proxy handoff");
    return { runtime: endpointAdapterRuntime(binding, base!), close: async () => {}, credentials: [base!] };
  }
  const ready = await loadFrozenEndpoint(root, binding), credential = endpointCredential(ready);
  const proxy = await HostModelProxy.start({ captureRoot: path.join(root, "private", "run-model-capture", runId),
    evalId: `eval_${runId.slice(4)}`, mode: "proxy", required: true, advertisedHost: "127.0.0.1",
    upstreams: { openai: ready.connection.base_url }, upstreamAuthorizations: { openai: credential ? `Bearer ${credential}` : "" },
    upstreamWireModels: { openai: binding.wire_model }, credentialValues: [ready.connection.base_url, ...(credential ? [credential] : [])], externalEndpointBinding: binding,
    onRun: async id => { if (id !== runId) throw invalidInput("model request belongs to a different run"); } });
  return { runtime: endpointAdapterRuntime(binding, `${proxy.localBaseUrl}/${runId}/openai`),
    close: async () => { await proxy.close(); await proxy.finalizeRun(runId, path.join(root, "runs", runId)); }, credentials: credential ? [credential] : [] };
}
function endpointAdapterRuntime(binding: ExternalEndpointBindingV1, baseUrl: string): AdapterProcessRuntime {
  return { model_endpoint: { kind: "external-endpoint", api: binding.api, base_url: baseUrl, wire_model: binding.wire_model,
    credential_env_name: "HITCH_LOCAL_MODEL_TOKEN", capabilities: binding.capabilities }, model_endpoint_credential: "hitch-external-endpoint",
    ...(binding.generation.max_output_tokens === undefined ? {} : { model_endpoint_max_output_tokens: binding.generation.max_output_tokens }) };
}

export async function endpointPrivateCredentials(root: string, binding: ExternalEndpointBindingV1 | undefined): Promise<string[]> {
  if (!binding || process.env.HITCH_HARBOR_INTERNAL === "1" && process.env.HITCH_EXTERNAL_ENDPOINT_BINDING) return [];
  const credential = endpointCredential(await loadFrozenEndpoint(root, binding));
  return [ (await loadFrozenEndpoint(root, binding)).connection.base_url, ...(credential ? [credential] : []) ];
}
