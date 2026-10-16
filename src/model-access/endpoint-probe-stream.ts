import { HitchError } from "../foundation/index.js";
/** Bounded one-shot readiness probe decoder, not a runtime protocol conversion service. */
export async function endpointProbeStream(response: Response, responses: boolean, wireModel: string): Promise<{ body: Record<string, unknown>; toolArguments: string; text: string }> {
  if (!response.headers.get("content-type")?.includes("text/event-stream")) { await response.body?.cancel(); throw failure("endpoint does not support SSE"); }
  let raw = "", bytes = 0; const decoder = new TextDecoder();
  for await (const chunk of response.body!) { bytes += chunk.byteLength; if (bytes > 8 * 1024 * 1024) throw failure("endpoint verification stream exceeds 8 MiB"); raw += decoder.decode(chunk, { stream: true }); }
  raw += decoder.decode();
  let completed: Record<string, unknown> | undefined, done = false, finish: unknown, usage: unknown, text = "", argumentsText = "";
  const calls = new Map<number, { id: string; type: "function"; function: { name: string; arguments: string } }>();
  for (const block of raw.split(/\r?\n\r?\n/)) {
    const data = block.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
    if (!data) continue; if (data === "[DONE]") { done = true; continue; }
    const event = JSON.parse(data) as Record<string, unknown>;
    if (event.error || event.type === "error" || event.type === "response.failed") throw failure("endpoint SSE reported an error");
    const nested = event.response && typeof event.response === "object" ? event.response as Record<string, unknown> : undefined;
    if (event.model !== undefined && event.model !== wireModel || nested?.model !== undefined && nested.model !== wireModel) throw failure("endpoint SSE returned a different wire model");
    if (nested?.error || nested?.status === "failed") throw failure("endpoint SSE reported a failed response");
    if (responses) {
      if (event.type === "response.function_call_arguments.delta") argumentsText += String(event.delta ?? "");
      if (event.type === "response.output_text.delta") text += String(event.delta ?? "");
      if (event.type === "response.completed" || event.type === "response.incomplete") completed = event.response as Record<string, unknown>;
    } else {
      if (event.usage) usage = event.usage;
      for (const choice of (event.choices ?? []) as Array<{ delta?: { content?: string; tool_calls?: Array<{ index: number; id?: string; function?: { name?: string; arguments?: string } }> }; finish_reason?: string }>) {
        if (choice.finish_reason) finish = choice.finish_reason;
        text += choice.delta?.content ?? "";
        for (const delta of choice.delta?.tool_calls ?? []) {
          const call = calls.get(delta.index) ?? { id: "", type: "function" as const, function: { name: "", arguments: "" } };
          call.id += delta.id ?? ""; call.function.name += delta.function?.name ?? ""; call.function.arguments += delta.function?.arguments ?? "";
          argumentsText += delta.function?.arguments ?? ""; calls.set(delta.index, call);
        }
      }
    }
  }
  if (responses && !completed || !responses && (!done || !finish)) throw failure("endpoint SSE ended without a completion event");
  return { body: responses ? completed! : { model: wireModel, usage, choices: [{ finish_reason: finish, message: { role: "assistant", content: text || null, ...(calls.size ? { tool_calls: [...calls.values()] } : {}) } }] }, toolArguments: argumentsText, text };
}
function failure(message: string) { return new HitchError(message, { code: "endpoint_verification_failed", exitCode: 2 }); }
