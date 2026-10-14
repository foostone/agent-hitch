import assert from "node:assert/strict";
import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { chmod, copyFile, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { packageRoot } from "../src/foundation/index.js";

// Installed-package canary: PATH contains Node only, with no Python/CUDA/model engine requirement.
const directory = await mkdtemp(path.join(tmpdir(), "hitch-endpoint-canary-"));
const calls: Record<string, unknown>[] = [];
const upstream = createServer(async (request, response) => {
  let raw = ""; for await (const chunk of request) raw += chunk;
  const body = JSON.parse(raw); calls.push(body);
  assert.equal(request.url, "/tenant/v1/responses"); assert.equal(body.max_output_tokens, 1024);
  response.setHeader("content-type", "application/json");
  response.end(JSON.stringify({ id: "resp_canary", status: "completed", model: "canary-v1", usage: { input_tokens: 12, output_tokens: 7 },
    output: [{ type: "message", content: [{ type: "output_text", text: "endpoint canary passed" }] }] }));
});
await new Promise<void>(r => upstream.listen(0, "127.0.0.1", r));
try {
  const config = path.join(directory, "endpoint.json"), root = path.join(directory, "state");
  await writeFile(config, JSON.stringify({ schema_version: "1", base_url: `http://127.0.0.1:${(upstream.address() as { port: number }).port}/tenant/v1`,
    api: "responses", wire_model: "canary-v1", auth: { kind: "none" }, capabilities: { streaming: false, tool_calls: false, parallel_tool_calls: false, input_modalities: ["text"] } }));
  // Installed discovery requires an executable; keep the packaged source's
  // trusted bytes while making a task-local executable for the real CLI path.
  const executable = path.join(directory, "model-call");
  await copyFile(path.join(packageRoot(), "integrations/model-call/cli.js"), executable); await chmod(executable, 0o755);
  const env = { PATH: path.dirname(process.execPath), HITCH_MODEL_CALL_PATH: executable };
  const cli = (...args: string[]) => promisify(execFile)(process.execPath, [path.join(packageRoot(), "dist/bin/hitch.js"), "--root", root, ...args], { env });
  await cli("models", "connect", "--name", "canary", "--file", config);
  const result = JSON.parse((await cli("run", "--harness", "model-call@installed", "--model", "endpoint/canary", "--max-output-tokens", "1024", "--cwd", directory, "--prompt", "ordinary text", "--output", "json")).stdout);
  assert.equal(result.status, "succeeded", JSON.stringify(result)); assert.equal(result.output, "endpoint canary passed");
  assert.equal(result.usage.input_tokens, 12); assert.equal(calls.length, 2);
  process.stdout.write("External endpoint installed-package canary passed without Python/CUDA.\n");
} finally {
  upstream.closeAllConnections(); await new Promise<void>(r => upstream.close(() => r()));
  await writable(directory); await rm(directory, { recursive: true, force: true });
}
async function writable(directory: string): Promise<void> {
  await chmod(directory, 0o700);
  for (const entry of await readdir(directory, { withFileTypes: true })) if (entry.isDirectory()) await writable(path.join(directory, entry.name));
}
