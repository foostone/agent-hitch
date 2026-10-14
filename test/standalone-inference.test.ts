import { spawn } from "node:child_process";
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

test("independent inference package HTTP/process contracts", { timeout: 120_000 }, async () => {
  const directory = fileURLToPath(new URL("../../packages/inference/", import.meta.url));
  const python = process.env.HITCH_INFERENCE_TEST_PYTHON ?? "python3";
  const child = spawn(python, ["-m", "unittest", "discover", "-s", "test", "-p", "test_*.py", "-v", "-f"], { cwd: directory,
    env: { ...process.env, PYTHONPATH: [path.join(directory, "python"), path.join(directory, "test/fixtures")].join(path.delimiter) },
    stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", chunk => { output += String(chunk); });
  child.stderr.on("data", chunk => {
    const text = String(chunk);
    output += text;
    process.stderr.write(text.replace(/[a-f0-9]{64}/g, "[digest/token]"));
  });
  const code = await new Promise<number | null>((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
  assert.equal(code, 0, output);
});
