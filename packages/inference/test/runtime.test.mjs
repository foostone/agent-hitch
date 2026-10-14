import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
test("standalone real HTTP/process runtime contracts", { timeout: 120000 }, async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  const python = process.env.HITCH_INFERENCE_TEST_PYTHON ?? "python3";
  const child = spawn(python, ["-m", "unittest", "discover", "-s", "test", "-p", "test_*.py", "-v"], { cwd: root,
    env: { ...process.env, PYTHONPATH: [path.join(root, "python"), path.join(root, "test/fixtures")].join(path.delimiter) }, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  for (const stream of [child.stdout, child.stderr]) stream.on("data", data => { output += data.toString(); });
  const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
  assert.equal(code, 0, output);
});
