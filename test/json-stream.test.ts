import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { PREPARATION_STREAM_TYPE, readStreamingJSONResponse, streamJSONResponse } from "../src/daemon/json-stream.js";
import { HitchError } from "../src/foundation/index.js";
import { DaemonServer } from "../src/daemon/index.js";
import { EvalScheduler } from "../src/control-plane/index.js";

async function endpoint(t: TestContext, prepare: (signal: AbortSignal) => Promise<unknown>) {
  const server = createServer((request, response) => { void streamJSONResponse(request, response, prepare).catch(() => {}); });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  return `http://127.0.0.1:${address.port}`;
}

test("preparation sends headers and heartbeats before work finishes", async t => {
  let complete!: (value: unknown) => void;
  const pending = new Promise<unknown>(resolve => { complete = resolve; });
  t.after(() => complete({ stopped: true }));
  const url = await endpoint(t, () => pending);
  t.mock.timers.enable({ apis: ["setInterval"] });
  const response = await fetch(url, { signal: AbortSignal.timeout(5000) });
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("content-type"), PREPARATION_STREAM_TYPE);
  const reader = response.body!.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /"progress"/);
  t.mock.timers.tick(15_000);
  assert.match(new TextDecoder().decode((await reader.read()).value), /"progress"/);
  reader.releaseLock();
  complete({ binding: "fixture" });
  assert.deepEqual(await readStreamingJSONResponse(response), { binding: "fixture" });
});

test("streaming preserves structured errors and cancels disconnected preparation", async t => {
  const failed = await endpoint(t, async () => { throw new HitchError("fixture timeout", { code: "fixture_timeout", exitCode: 12 }); });
  await assert.rejects(readStreamingJSONResponse(await fetch(failed)), { code: "fixture_timeout" });
  let confirmCancellation!: () => void;
  const cancelled = new Promise<void>(resolve => { confirmCancellation = resolve; });
  const url = await endpoint(t, signal => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => { confirmCancellation(); reject(signal.reason); }, { once: true });
    t.after(() => resolve({ stopped: true }));
  }));
  const controller = new AbortController();
  await fetch(url, { signal: controller.signal });
  controller.abort();
  await cancelled;
});

test("stream reader rejects incomplete, duplicate and oversized responses", async () => {
  const result = JSON.stringify({ type: "result", value: { ok: true } }) + "\n";
  for (const body of ["", '{"type":"progress"}\n', "invalid", result + result,
    JSON.stringify({ type: "result", value: "invalid" }), "x".repeat(2 * 1024 * 1024 + 1)]) {
    await assert.rejects(readStreamingJSONResponse(new Response(body)), { code: "daemon_request_failed" });
  }
  assert.deepEqual(await readStreamingJSONResponse(new Response('{"type":"progress"}\n' + result)), { ok: true });
});

test("streaming bounds and redacts public error messages", async t => {
  const secret = "streaming-fixture-credential";
  const url = await endpoint(t, async () => {
    throw new HitchError(`unsupported harness https://example.test/harness?token=${secret} ${"x".repeat(5000)}`,
      { code: "invalid_input", exitCode: 2 });
  });
  const body = await (await fetch(url)).text();
  assert.ok(!body.includes(secret));
  await assert.rejects(readStreamingJSONResponse(new Response(body)), (error: unknown) => {
    assert.ok(error instanceof HitchError);
    assert.equal(error.code, "invalid_input");
    assert.equal(error.exitCode, 2);
    assert.equal((error as { status?: number }).status, 400);
    assert.ok(error.message.length <= 4096);
    return true;
  });
});

test("streaming safely reports untyped failures", async t => {
  for (const failure of [null, undefined, new Error("private failure detail")]) {
    const url = await endpoint(t, async () => { throw failure; });
    await assert.rejects(readStreamingJSONResponse(await fetch(url)), {
      code: "daemon_request_failed", message: "daemon preparation failed", status: 500,
    });
  }
});

test("streamed submission failures retain bounded redacted daemon diagnostics", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-stream-diagnostic-"));
  const logs: Array<{ type: string; fields: Record<string, unknown> }> = [];
  const server = new DaemonServer({ root, port: 0, maxConcurrent: 1, credentialEnv: {},
    logger: (type, fields) => logs.push({ type, fields }) });
  t.after(async () => { await server.close(); await rm(root, { recursive: true, force: true }); });
  await server.start();
  const token = (await readFile(server.paths.token, "utf8")).trim();
  const secret = "streaming-log-credential";
  t.mock.method(EvalScheduler.prototype, "submit", async () => {
    throw new Error(`ENOSPC https://example.test?token=${secret} ${"x".repeat(5000)}`);
  });
  const response = await fetch(`http://127.0.0.1:${server.port}/v1/evals`, { method: "POST", body: "{}",
    headers: { authorization: `Bearer ${token}`, accept: PREPARATION_STREAM_TYPE } });
  await assert.rejects(readStreamingJSONResponse(response), {
    code: "daemon_request_failed", message: "daemon preparation failed", status: 500,
  });
  const errors = logs.filter(item => item.type === "request_error");
  assert.equal(errors.length, 1);
  assert.match(String(errors[0]!.fields.error), /ENOSPC/);
  assert.ok(String(errors[0]!.fields.error).length <= 4096);
  assert.equal(errors[0]!.fields.path, "/v1/evals");
  assert.ok(!JSON.stringify(logs).includes(secret));
});
