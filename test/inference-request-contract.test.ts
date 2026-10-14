import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import type { InferenceLockV1 } from "../src/domain/index.js";
import { sglangRequestBody, validateRequestBody } from "../src/model-access/local-gateway-request.js";

test("shared TS/Python request fixtures keep user budgets until exactly one engine conversion", async () => {
  const root = path.resolve("packages/inference");
  const file = path.join(root, "test/fixtures/request-contract.json");
  const fixture = JSON.parse(await readFile(file, "utf8")) as { lock: InferenceLockV1; cases: Array<{
    name: string; api: string; request: Record<string, unknown>; valid: boolean; canonical: Record<string, unknown>; engine: Record<string, unknown>;
  }> };
  const serial: Array<{ api: string; body: Record<string, unknown> }> = [];
  for (const row of fixture.cases) {
    const lock = { ...fixture.lock, protocol: { ...fixture.lock.protocol, api: row.api } } as InferenceLockV1;
    const validate = (body: unknown) => validateRequestBody(Buffer.from(JSON.stringify(body)), lock, "fixture-model");
    if (!row.valid) { assert.throws(() => validate(row.request), Error, row.name); serial.push({ api: row.api, body: row.request }); continue; }
    const canonical = validate(row.request);
    assert.deepEqual(canonical, row.canonical, row.name);
    assert.deepEqual(validate(canonical), canonical, `${row.name}: repeated TS validation`);
    assert.deepEqual(sglangRequestBody(canonical, lock), row.engine, row.name);
    serial.push({ api: row.api, body: canonical });
  }
  const child = spawn(process.env.HITCH_INFERENCE_TEST_PYTHON ?? "python3", [path.join(root, "test/contract_projection.py"), file], {
    env: { ...process.env, PYTHONPATH: path.join(root, "python") }, stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "", errors = "";
  child.stdout.on("data", value => { output += value; }); child.stderr.on("data", value => { errors += value; });
  child.stdin.end(JSON.stringify(serial));
  assert.equal(await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", resolve); }), 0, errors);
  const projected = JSON.parse(output);
  for (const [index, row] of fixture.cases.entries()) {
    assert.deepEqual(projected[index], row.valid ? { valid: true, canonical: row.canonical, engine: row.engine } : { valid: false }, `${row.name}: TS → Python → Python → engine`);
  }
});
