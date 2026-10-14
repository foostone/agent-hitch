import { createServer } from "node:http";
import type { ServerResponse } from "node:http";
import { once } from "node:events";
import type { TestContext } from "node:test";
import type { ExternalEndpointConfigV1 } from "../src/domain/index.js";

/** Standard protocol/fault fixture. It has no deployment, node, GPU or admin API. */
export async function endpointHttp(t: TestContext, api: "responses" | "chat-completions" = "responses") {
  const requests: Array<{ path: string; body: Record<string, unknown>; authorization?: string }> = [];
  const active = new Map<string, ServerResponse>();
  const disconnected = new Set<string>();
  let status = 200, responseModel: string | undefined, brokenToolStream = false, usageOverride: Record<string, number> | undefined;
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = []; for await (const chunk of req) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    requests.push({ path: req.url!, body, ...(req.headers.authorization ? { authorization: req.headers.authorization } : {}) });
    if (status !== 200) { res.writeHead(status, { "content-type": "application/json", "retry-after": "1" }); res.end('{"error":{"message":"busy"}}'); return; }
    if (typeof body.input === "string" && body.input.startsWith("hold:")) {
      const id = body.input.slice(5); active.set(id, res); res.once("close", () => { disconnected.add(id); active.delete(id); });
      res.writeHead(200, { "content-type": "text/event-stream" }); res.write('data: {"type":"response.created"}\n\n'); return;
    }
    const tool = body.tool_choice && typeof body.tool_choice === "object";
    const model = responseModel ?? body.model;
    const usage = usageOverride ?? (api === "responses" ? { input_tokens: 12, output_tokens: 7, total_tokens: 19 } : { prompt_tokens: 12, completion_tokens: 7, total_tokens: 19 });
    const result = api === "responses" ? { id: "resp_test", object: "response", model, status: "completed", usage, output: tool
      ? [{ type: "function_call", call_id: "call_verify", name: "hitch_verify", arguments: "{}" }]
      : [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "ready" }] }] }
      : { id: "chat_test", model, usage, choices: [{ index: 0, finish_reason: tool ? "tool_calls" : "stop", message: tool
        ? { role: "assistant", content: null, tool_calls: [{ id: "call_verify", type: "function", function: { name: "hitch_verify", arguments: "{}" } }] }
        : { role: "assistant", content: "ready" } }] };
    res.setHeader("content-type", body.stream ? "text/event-stream" : "application/json");
    if (body.stream) {
      if (tool && brokenToolStream) {
        res.end(api === "responses" ? `data: ${JSON.stringify({ type: "response.completed", response: result })}\n\n`
          : `data: ${JSON.stringify({ model, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }], usage })}\n\ndata: [DONE]\n\n`); return;
      }
      if (api === "responses") res.end(`data: ${JSON.stringify(tool ? { type: "response.function_call_arguments.delta", delta: "{}" } : { type: "response.output_text.delta", delta: "ready" })}\n\ndata: ${JSON.stringify({ type: "response.completed", response: result })}\n\n`);
      else res.end(`data: ${JSON.stringify({ model, choices: [{ index: 0, delta: tool ? { tool_calls: [{ index: 0, id: "call_verify", type: "function", function: { name: "hitch_verify", arguments: "{}" } }] } : { content: "ready" }, finish_reason: tool ? "tool_calls" : "stop" }], usage })}\n\ndata: [DONE]\n\n`);
    }
    else res.end(JSON.stringify(result));
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  t.after(async () => { server.closeAllConnections(); await new Promise<void>(r => server.close(() => r())); });
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}/tenant/prefix/v1`;
  const config: ExternalEndpointConfigV1 = { schema_version: "1", base_url: base, wire_model: "candidate-v1", api,
    auth: { kind: "none" }, capabilities: { streaming: true, tool_calls: true, parallel_tool_calls: false, input_modalities: ["text"] } };
  return { config, requests, active, disconnected, setStatus: (s: number) => { status = s; },
    setResponseModel: (model: string) => { responseModel = model; }, setBrokenToolStream: (broken: boolean) => { brokenToolStream = broken; },
    setUsage: (usage: Record<string, number>) => { usageOverride = usage; } };
}
