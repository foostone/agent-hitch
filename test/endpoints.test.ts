import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { connectEndpoint, freezeEndpointRequest, resolveEndpoint, verifyEndpoint, HostModelProxy, publicEndpointBinding, parseReadyEndpointBinding, loadInteractionCapture } from "../src/model-access/index.js";
import { endpointHttp } from "../test-support/endpoint-http.js";
import { forceRemove } from "../test-support/helpers.js";
import { delay, sha256JSON } from "../src/foundation/index.js";

const harness = "model-call@commit:" + "a".repeat(40);
async function root(t: import("node:test").TestContext) {
  const value = await mkdtemp(path.join(tmpdir(), "hitch-endpoints-")); t.after(() => forceRemove(value)); return value;
}

test("endpoint aliases freeze before submission and resume the original revision/configuration", async t => {
  const state = await root(t), http = await endpointHttp(t);
  const first = await connectEndpoint(state, "coder", http.config);
  const old = await freezeEndpointRequest(state, { model: "endpoint/coder", harness_ref: harness, generation: { max_output_tokens: 1024, temperature: 0 } });
  await connectEndpoint(state, "coder", { ...http.config, wire_model: "candidate-v2" });
  const fresh = await freezeEndpointRequest(state, { model: "endpoint/coder", harness_ref: harness, generation: { max_output_tokens: 4096 } });
  const resumed = await freezeEndpointRequest(state, old);
  assert.deepEqual(resumed, old); assert.notEqual((fresh as unknown as { endpoint_binding: { endpoint_revision: string } }).endpoint_binding.endpoint_revision, first.endpoint_revision);
  assert.equal((old as unknown as { endpoint_binding: { wire_model: string } }).endpoint_binding.wire_model, "candidate-v1");
  assert.equal(JSON.stringify(old).includes(http.config.base_url), false);
  assert.equal(http.requests.every(r => r.path === "/tenant/prefix/v1/responses"), true);
  assert.equal(http.requests.every(r => !/stop|drain|flush|lease/.test(r.path)), true);
  const ready = await resolveEndpoint(state, "endpoint/coder");
  await assert.rejects(async () => parseReadyEndpointBinding({ ...ready, endpoint_ref: `endpoint/coder@${"sha256:" + "a".repeat(64)}` }), /pinned endpoint/);
});

for (const api of ["responses", "chat-completions"] as const) test(`${api} verifies SSE, full tool result round trip and unmodified normal budgets`, async t => {
  const http = await endpointHttp(t, api), state = await root(t);
  await connectEndpoint(state, "verified", http.config);
  const result = await verifyEndpoint(await resolveEndpoint(state, "endpoint/verified"));
  assert.equal(result.streaming, true); assert.equal(result.tool_calls, true); assert.equal(result.streamed_tool_calls, true);
  assert.equal(http.requests.length, 6);
  for (const request of http.requests) assert.equal(request.body[api === "responses" ? "max_output_tokens" : "max_tokens"], 1024);
  const last = http.requests.at(-1)!.body;
  assert.ok(JSON.stringify(last).includes("hitch-verification-complete"));
  assert.ok(JSON.stringify(last).includes("call_verify"));
});

test("two task proxies share one endpoint; cancelling one stream leaves the other consumer running", async t => {
  const http = await endpointHttp(t), state = await root(t);
  await connectEndpoint(state, "shared", http.config);
  const ready = await resolveEndpoint(state, "endpoint/shared", { max_output_tokens: 4096 });
  const proxies = await Promise.all(["a", "b"].map(id => HostModelProxy.start({ captureRoot: path.join(state, id), evalId: `eval_${id.repeat(32)}`,
    mode: "proxy", required: true, advertisedHost: "127.0.0.1", upstreams: { openai: ready.connection.base_url },
    upstreamAuthorizations: { openai: "" }, upstreamWireModels: { openai: ready.wire_model }, externalEndpointBinding: publicEndpointBinding(ready) })));
  t.after(() => Promise.all(proxies.map(p => p.close())));
  const requests = await Promise.all(proxies.map((proxy, i) => fetch(`${proxy.localBaseUrl}/run_${String(i + 1).repeat(32)}/openai/responses`, {
    method: "POST", headers: { "content-type": "application/json", authorization: "Bearer harness-sentinel" }, body: JSON.stringify({ input: `hold:${i}`, max_output_tokens: 4096 }) })));
  const readers = requests.map(r => r.body!.getReader()); await Promise.all(readers.map(r => r.read()));
  await readers[0]!.cancel();
  for (let i = 0; i < 100 && !http.disconnected.has("0"); i++) await delay(10);
  assert.equal(http.disconnected.has("0"), true); assert.equal(http.active.has("1"), true);
  assert.equal(http.requests.every(r => r.authorization === undefined), true);
  http.active.get("1")!.end('data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":7}}}\n\n');
  while (!(await readers[1]!.read()).done) { /* consume completion */ }
  assert.equal(http.requests.length, 2); assert.equal(http.requests[1]!.body.max_output_tokens, 4096);
  // Closing immediately after cancellation waits for the failed interaction
  // to be recorded; absent upstream token usage stays absent.
  await proxies[0]!.close(); const cancelledRun = path.join(state, "cancelled");
  await proxies[0]!.finalizeRun(`run_${"1".repeat(32)}`, cancelledRun);
  const cancelled = await loadInteractionCapture(cancelledRun);
  assert.equal(cancelled.interactions.length, 1); assert.equal(cancelled.interactions[0]!.status, "failed");
  assert.equal(cancelled.interactions[0]!.usage, undefined);
});

for (const api of ["responses", "chat-completions"] as const) test(`${api} rejects successful text streaming with broken streamed tool calls`, async t => {
  const http = await endpointHttp(t, api), state = await root(t); http.setBrokenToolStream(true);
  await connectEndpoint(state, "broken", http.config);
  await assert.rejects(verifyEndpoint(await resolveEndpoint(state, "endpoint/broken")), /stream function tool arguments/);
  assert.equal(http.requests.some(r => r.body.stream === true && typeof r.body.tool_choice === "object"), true);
  assert.equal(http.requests.filter(r => Array.isArray(r.body.tools) && r.body.tools.length).every(r => r.body.parallel_tool_calls === false), true);
});

test("nested Responses SSE model drift is preserved in evidence and fails the frozen endpoint", async t => {
  const http = await endpointHttp(t), state = await root(t);
  await connectEndpoint(state, "drift", http.config); const ready = await resolveEndpoint(state, "endpoint/drift");
  http.setResponseModel("unexpected-fallback");
  const proxy = await HostModelProxy.start({ captureRoot: path.join(state, "capture"), evalId: `eval_${"d".repeat(32)}`,
    mode: "proxy", required: true, advertisedHost: "127.0.0.1", upstreams: { openai: ready.connection.base_url },
    upstreamAuthorizations: { openai: "" }, upstreamWireModels: { openai: ready.wire_model }, externalEndpointBinding: publicEndpointBinding(ready) });
  t.after(() => proxy.close()); const runId = `run_${"d".repeat(32)}`, destination = path.join(state, "run");
  const response = await fetch(`${proxy.localBaseUrl}/${runId}/openai/responses`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ input: "ordinary stream", stream: true, max_output_tokens: 1024 }) });
  await response.text(); await proxy.close();
  await assert.rejects(proxy.finalizeRun(runId, destination), (error: unknown) => (error as { code?: string }).code === "endpoint_model_mismatch");
  const capture = await loadInteractionCapture(destination); assert.equal(capture.interactions[0]!.effective_model, "unexpected-fallback");
});

test("429 propagates without transparent generation retries and missing secrets fail before verification", async t => {
  const http = await endpointHttp(t), state = await root(t); http.setStatus(429);
  await connectEndpoint(state, "busy", http.config);
  await assert.rejects(freezeEndpointRequest(state, { model: "endpoint/busy", harness_ref: harness }), /HTTP 429/);
  assert.equal(http.requests.length, 1);
  await connectEndpoint(state, "private", { ...http.config, auth: { kind: "env", name: "HITCH_TEST_MISSING_EXTERNAL_KEY" } });
  await assert.rejects(freezeEndpointRequest(state, { model: "endpoint/private", harness_ref: harness }), /credential environment variable/);
  assert.equal(http.requests.length, 1);
});
