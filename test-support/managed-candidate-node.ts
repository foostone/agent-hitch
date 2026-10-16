import { once } from "node:events";
import http from "node:http";
import { connectEndpoint, resolveEndpoint, publicEndpointBinding } from "../src/model-access/index.js";

/** Standard upstream service, with no deployment, device, PID, lease or admin RPC. */
export async function managedCandidateNode(_directory: string, root: string) {
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const engine = http.createServer(async (request, response) => {
    let raw = ""; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw); requests.push({ url: request.url!, body });
    const output = body.tool_choice && typeof body.tool_choice === "object"
      ? [{ type: "function_call", call_id: "call_verify", name: "hitch_verify", arguments: "{}" }]
      : [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "fixture answer" }] }];
    const result = { id: `resp_${requests.length}`, object: "response", status: "completed", model: body.model,
      output, usage: { input_tokens: 12, output_tokens: 7, total_tokens: 19 } };
    response.setHeader("content-type", body.stream ? "text/event-stream" : "application/json");
    response.end(body.stream ? `data: ${JSON.stringify(body.tool_choice && typeof body.tool_choice === "object" ? { type: "response.function_call_arguments.delta", delta: "{}" } : { type: "response.output_text.delta", delta: "ready" })}\n\ndata: ${JSON.stringify({ type: "response.completed", response: result })}\n\n` : JSON.stringify(result));
  });
  engine.listen(0, "127.0.0.1"); await once(engine, "listening");
  const close = async () => { engine.closeAllConnections(); await new Promise<void>(r => engine.close(() => r())); };
  try {
    await connectEndpoint(root, "test", { schema_version: "1", base_url: `http://127.0.0.1:${(engine.address() as { port: number }).port}/v1`,
      auth: { kind: "none" }, api: "responses", wire_model: "candidate-v1",
      capabilities: { streaming: true, tool_calls: true, parallel_tool_calls: false, input_modalities: ["text"] } });
    const ready = await resolveEndpoint(root, "endpoint/test", { max_output_tokens: 1024 });
    return { ready, binding: publicEndpointBinding(ready), requests, access: { engineToken: "unused-private-marker" }, close };
  } catch (error) { await close(); throw error; }
}
