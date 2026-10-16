import { endpointProbeStream } from "./endpoint-probe-stream.js";
import type { EndpointVerificationV1, ReadyEndpointBindingV1 } from "../domain/index.js";
import { HitchError } from "../foundation/index.js";
import { endpointCredential } from "./endpoints.js";

/** Finite protocol checks; model echo is never a proof of mounted immutable weights. */
export async function verifyEndpoint(ready: ReadyEndpointBindingV1, env: NodeJS.ProcessEnv = process.env): Promise<EndpointVerificationV1> {
  const credential = endpointCredential(ready, env);
  const send = async (payload: Record<string, unknown>): Promise<Response> => {
    const response = await fetch(`${ready.connection.base_url}/${ready.api === "responses" ? "responses" : "chat/completions"}`, {
      method: "POST", headers: { "content-type": "application/json", ...(credential ? { authorization: `Bearer ${credential}` } : {}) },
      body: JSON.stringify({ model: ready.wire_model, ...payload, ...(Array.isArray(payload.tools) && payload.tools.length ? { parallel_tool_calls: false } : {}) }), signal: AbortSignal.timeout(60_000), redirect: "error" });
    if (!response.ok) { await response.body?.cancel(); throw failure(`endpoint protocol verification failed with HTTP ${response.status}`); }
    return response;
  };
  const responses = ready.api === "responses";
  const prompt = [{ role: "user", content: "Reply with the single word ready." }];
  const common = responses ? { input: prompt, max_output_tokens: 1024, store: false } : { messages: prompt, max_tokens: 1024 };
  const check = (body: Record<string, unknown>) => {
    if (body.model !== undefined && body.model !== ready.wire_model) throw failure("endpoint returned a different wire model");
    if (body.error || responses && !["completed", "incomplete"].includes(String(body.status))) throw failure("endpoint returned a failed generation");
    if (responses ? !Array.isArray(body.output) : !Array.isArray(body.choices)) throw failure("endpoint did not return the selected protocol");
    return body;
  };
  const generate = async (payload: Record<string, unknown>, stream = false, tool = false) => {
    const response = await send({ ...payload, ...(stream ? { stream: true, ...(!responses ? { stream_options: { include_usage: true } } : {}) } : {}) });
    if (!stream) return check(await response.json() as Record<string, unknown>);
    const observed = await endpointProbeStream(response, responses, ready.wire_model);
    if (tool && !observed.toolArguments) throw failure("endpoint SSE did not stream function tool arguments");
    if (!tool && !observed.text) throw failure("endpoint SSE did not stream output text");
    return check(observed.body);
  };
  await generate(common);
  if (ready.capabilities.streaming) await generate(common, true);
  if (ready.capabilities.tool_calls) for (const stream of ready.capabilities.streaming ? [false, true] : [false]) {
    const functionTool = { name: "hitch_verify", description: "Return a known verification value", parameters: { type: "object", properties: {}, additionalProperties: false } };
    const tools = responses ? [{ type: "function", ...functionTool }] : [{ type: "function", function: functionTool }];
    const first = await generate({ ...common, tools, tool_choice: responses ? { type: "function", name: "hitch_verify" } : { type: "function", function: { name: "hitch_verify" } }, parallel_tool_calls: false }, stream, true);
    let next: Record<string, unknown>;
    if (responses) {
      const output = first.output as Array<Record<string, unknown>>;
      const tool = output?.find(item => item.type === "function_call" && item.name === "hitch_verify");
      if (!tool || typeof tool.call_id !== "string" || typeof tool.arguments !== "string") throw failure("endpoint did not generate a function tool call");
      JSON.parse(tool.arguments);
      next = { input: [...prompt, ...output, { type: "function_call_output", call_id: tool.call_id, output: "hitch-verification-complete" }], max_output_tokens: 1024, store: false, tools, tool_choice: "none" };
    } else {
      const choices = first.choices as Array<{ message: { tool_calls?: Array<{ id: string; function?: { name?: string; arguments?: string } }> } }>;
      const message = choices?.[0]?.message, tool = message?.tool_calls?.find(t => t.function?.name === "hitch_verify");
      if (!tool?.id || typeof tool.function?.arguments !== "string") throw failure("endpoint did not generate a function tool call");
      JSON.parse(tool.function.arguments);
      next = { messages: [...prompt, message, { role: "tool", tool_call_id: tool.id, content: "hitch-verification-complete" }], max_tokens: 1024, tools, tool_choice: "none" };
    }
    const completed = await generate(next, stream);
    const answer = responses ? (completed.output as Array<{ type?: string }> | undefined)?.some(item => item.type === "message")
      : (completed.choices as Array<{ message?: { content?: string } }> | undefined)?.some(item => typeof item.message?.content === "string");
    if (!answer) throw failure("endpoint did not complete the tool result round trip");
  }
  return { schema_version: "1", endpoint_revision: ready.endpoint_revision, api: ready.api,
    verified_at: new Date().toISOString(), streaming: ready.capabilities.streaming, tool_calls: ready.capabilities.tool_calls, streamed_tool_calls: ready.capabilities.streaming && ready.capabilities.tool_calls };
}
function failure(message: string): HitchError { return new HitchError(message, { code: "endpoint_verification_failed", exitCode: 2 }); }
