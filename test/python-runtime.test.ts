import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { runCommand, packageRoot } from "../src/foundation/index.js";
import { observeHarborPythonRuntime } from "../src/backends/harbor/python-runtime.js";

test("Python runtime inventory verifies file drift, bytecode caches and resource bounds", async () => {
  const result = await runCommand(process.env.HITCH_TEST_PYTHON || "python3",
    ["-B", path.join(packageRoot(), "test-support/python-runtime-inventory.py")], { timeoutMs: 30_000 });
  assert.match(result.stderr, /Ran 8 tests/);
  assert.match(result.stderr, /\bOK\b/);
});

test("unsupported launchers and relative module paths fail without exposing diagnostics", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-python-runtime-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const bin = path.join(root, "bin"); await mkdir(bin);
  const harbor = path.join(bin, "harbor");
  await writeFile(harbor, `#!${process.execPath}\nconsole.error('private-diagnostic');console.log('harbor 0.21.0');\n`, { mode: 0o755 });
  for (const env of [{ HITCH_HARBOR_PATH: harbor }, { HITCH_HARBOR_PATH: harbor, PYTHONPATH: "relative" }]) {
    await assert.rejects(observeHarborPythonRuntime({ root, env }), error => {
      assert.equal((error as { code?: string }).code, "harbor_python_runtime_unavailable");
      assert.ok(!(error as Error).message.includes("private-diagnostic"));
      assert.ok(!(error as Error).message.includes(root));
      return true;
    });
  }
});
