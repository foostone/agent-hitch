import type { IncomingMessage, ServerResponse } from "node:http";
import type { EvaluationBindingV1 } from "../domain/index.js";
import { invalidInput } from "../foundation/index.js";
import { json, readBodyJSON } from "./http.js";

export async function evaluationBinding(request: IncomingMessage, response: ServerResponse, input: {
  instanceId: string;
  prepare(value: { model: string; passEnv: string[]; minimumValidityMs: number; signal: AbortSignal }): Promise<EvaluationBindingV1>;
}): Promise<void> {
  const value = await readBodyJSON(request) as Record<string, unknown>;
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).sort().join(",") !== "minimum_validity_ms,model,nonce,pass_env,provider,schema_version"
    || value.schema_version !== "1" || value.provider !== "local-docker"
    || typeof value.nonce !== "string" || !/^[a-f0-9]{32}$/.test(value.nonce)
    || typeof value.model !== "string" || !Array.isArray(value.pass_env)
    || typeof value.minimum_validity_ms !== "number") throw invalidInput("invalid evaluation binding preparation request");
  const controller = new AbortController(), abort = () => controller.abort();
  response.once("close", abort);
  response.setHeader("cache-control", "no-store");
  try {
    const binding = await input.prepare({ model: value.model,
      passEnv: value.pass_env as string[], minimumValidityMs: value.minimum_validity_ms, signal: controller.signal });
    json(response, 200, { schema_version: "1", nonce: value.nonce, provider: "local-docker",
      daemon_instance_id: input.instanceId, binding });
  } finally { response.removeListener("close", abort); }
}
