import test from "node:test";
import assert from "node:assert/strict";
import { chmod, lstat, mkdtemp, readFile, readdir, symlink, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { ensureControllerRuntime, useControllerRuntimeDirectory } from "../src/controller-runtime/index.js";
import { artifactDirectoryIntegrity, verifyPreparedArtifact } from "../src/artifacts/index.js";
import { SharedRuntimeSnapshots } from "../src/evals/shared-runtime.js";
import { validateEvalRequest } from "../src/evals/index.js";
import { harborPreparedArtifact } from "../src/evals/prepared-harness.js";
import { runHarborBackend } from "../src/backends/harbor/index.js";
import type { HarborPreparedArtifactUse } from "../src/backends/harbor/index.js";
import { nodeRuntimeHarnessFixture } from "../test-support/harbor-node-runtime-fixture.js";
import { forceRemove } from "../test-support/helpers.js";

test("concurrent tasks share verified installation snapshots, isolated from subsequent cache writes", { skip: process.platform !== "linux" }, async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-shared-runtime-"));
  t.after(() => forceRemove(root));
  const controller = await ensureControllerRuntime({ root });
  const fixture = await nodeRuntimeHarnessFixture(root);
  await symlink("entry.js", path.join(fixture.directory, "alias.js"));
  await chmod(path.join(fixture.directory, "entry.js"), 0o750);
  fixture.manifest.artifact_integrity = await artifactDirectoryIntegrity(fixture.directory);
  await writeFile(path.join(fixture.directory, "artifact.json"), JSON.stringify(fixture.manifest));
  await chmod(fixture.directory, 0o700);
  await chmod(path.join(fixture.directory, "artifact.json"), 0o600);
  const artifact = { ...harborPreparedArtifact(root, { ...fixture.manifest, executable: "node", entrypoint_args: [], cache_hit: false }), directory: fixture.directory };
  const base = path.join(root, "eval", "shared-runtime");
  const alias = path.join(root, "controller-alias");
  await symlink(controller.directory, alias);
  try {
    await assert.rejects(new SharedRuntimeSnapshots(base, { ...controller, directory: alias }, "readonly-bind").prepare(artifact), /regular directory/);
  } finally {
    await unlink(alias);
  }
  const snapshots = new SharedRuntimeSnapshots(base, controller, "readonly-bind");
  const tasks = await Promise.all(Array.from({ length: 20 }, () => snapshots.prepare(artifact)));
  const shared = tasks[0]!.sharedRuntime!;
  assert.equal(new Set(tasks.map((x) => x.sharedRuntime!.artifact_directory)).size, 1);
  assert.equal(new Set(tasks.map((x) => x.sharedRuntime!.runtime_directory)).size, 1);
  assert.equal((await readdir(base)).length, 2);
  assert.notEqual(shared.runtime_directory, controller.directory);
  assert.notEqual(shared.artifact_directory, artifact.directory);
  assert.equal((await lstat(shared.artifact_directory)).mode & 0o777, 0o755);
  assert.equal((await lstat(path.join(shared.artifact_directory, "artifact.json"))).mode & 0o777, 0o644);
  assert.equal((await lstat(path.join(shared.artifact_directory, "entry.js"))).mode & 0o777, 0o750);
  assert.equal((await lstat(artifact.directory)).mode & 0o777, 0o700);
  assert.equal((await lstat(path.join(artifact.directory, "artifact.json"))).mode & 0o777, 0o600);
  await writeFile(path.join(artifact.directory, "entry.js"), "console.log('changed original cache')\n");
  const controllerFile = path.join(controller.directory, "payload", "package.json");
  await chmod(controllerFile, 0o644);
  await writeFile(controllerFile, "{}\n");
  await useControllerRuntimeDirectory(shared.runtime_directory, controller.runtime_id);
  await verifyPreparedArtifact(shared.artifact_directory, artifact);
  assert.match(await readFile(path.join(shared.artifact_directory, "alias.js"), "utf8"), /offline harness OK/);
  assert.deepEqual((await snapshots.prepare(artifact)).sharedRuntime, shared);
  // A fresh evaluation must not accept the now-corrupt source cache.
  await assert.rejects(new SharedRuntimeSnapshots(path.join(root, "new-eval"), controller, "readonly-bind").prepare(artifact), /integrity mismatch/);
});

test("corrupt dependencies are rejected before a snapshot is published; upload remains compatible", { skip: process.platform !== "linux" }, async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-shared-corrupt-"));
  t.after(() => forceRemove(root));
  const controller = await ensureControllerRuntime({ root });
  const fixture = await nodeRuntimeHarnessFixture(root);
  const artifact = { ...harborPreparedArtifact(root, { ...fixture.manifest, executable: "node", entrypoint_args: [], cache_hit: false }), directory: fixture.directory };
  const base = path.join(root, "eval", "shared-runtime");
  await writeFile(path.join(artifact.directory, "extra-dependency.js"), "unexpected code");
  await assert.rejects(new SharedRuntimeSnapshots(base, controller, "readonly-bind").prepare(artifact), /content verification/);
  assert.equal((await readdir(base)).some((name) => name.startsWith("artifact") || name.startsWith(".")), false);
  await unlink(path.join(artifact.directory, "extra-dependency.js"));
  const externalMetadata = path.join(root, "external-artifact.json");
  await writeFile(externalMetadata, JSON.stringify(fixture.manifest), { mode: 0o600 });
  await unlink(path.join(artifact.directory, "artifact.json"));
  await symlink(externalMetadata, path.join(artifact.directory, "artifact.json"));
  await assert.rejects(new SharedRuntimeSnapshots(path.join(root, "linked-metadata"), controller, "readonly-bind").prepare(artifact), /metadata must be a regular file/);
  assert.equal((await lstat(externalMetadata)).mode & 0o777, 0o600);
  assert.equal(await new SharedRuntimeSnapshots(undefined, controller, "upload").prepare(artifact), artifact);
  assert.throws(() => new SharedRuntimeSnapshots(undefined, controller, "readonly-bind"), /local Linux/);
  assert.throws(() => new SharedRuntimeSnapshots(base, controller, "typo"), /must be upload or readonly-bind/);
  const cancellation = new AbortController(); cancellation.abort();
  await assert.rejects(new SharedRuntimeSnapshots(base, controller, "readonly-bind").prepare(artifact, cancellation.signal), /abort/i);
});

test("Harbor imports and checks the selected snapshot after the source cache changes", { skip: process.platform !== "linux" }, async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-shared-bridge-"));
  t.after(() => forceRemove(root));
  const controller = await ensureControllerRuntime({ root });
  const fixture = await nodeRuntimeHarnessFixture(root);
  const artifact = { ...harborPreparedArtifact(root, { ...fixture.manifest, executable: "node", entrypoint_args: [], cache_hit: false }), directory: fixture.directory };
  const request = await validateEvalRequest({ dataset: "fixture@1.0", harness_ref: "pi@version:1.2.3", timeout_ms: 1_000, pass_env: ["FIXTURE_TOKEN"] });
  const executable = path.join(root, "harbor-probe.mjs");
  const python = process.env.HITCH_TEST_PYTHON_PATH || "python3";
  await writeFile(executable, `#!/usr/bin/env node
import { spawnSync } from "node:child_process";
if (process.argv.includes("--version")) { console.log("harbor 0.21.0"); process.exit(0); }
const result = spawnSync(${JSON.stringify(python)}, ["-c", "import hitch_benchmark; print(hitch_benchmark.__file__)"], { env: process.env, encoding: "utf8" });
process.stdout.write(result.stdout || "");
process.stderr.write(result.stderr || "");
process.exit(result.status ?? 1);
`, { mode: 0o755 });
  const sourceBridge = path.join(controller.directory, "payload", "integrations", "harbor");
  // The executable fixture imports the real bridge; no helper or model runs.
  const env = { PATH: process.env.PATH, PYTHONPATH: sourceBridge,
    HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1, argv: [executable], credentialNames: ["FIXTURE_TOKEN"], timeoutMs: 1_000 }) };
  async function run(name: string, preparedArtifact: HarborPreparedArtifactUse, expectedDirectory: string): Promise<void> {
    const evalDirectory = path.join(root, name);
    const result = await runHarborBackend({ root, evalId: `eval_${"a".repeat(32)}`, evalDirectory, request,
      resolvedRevision: fixture.manifest.resolved_revision, runtimeDirectory: controller.directory,
      runtimeId: controller.runtime_id, preparedArtifact, harborExecutable: executable, env });
    const stderr = await readFile(path.join(evalDirectory, result.backend.stderr_path!), "utf8");
    assert.equal(result.backend.process_exit_code, 0, stderr);
    const imported = await readFile(path.join(evalDirectory, result.backend.stdout_path!), "utf8");
    assert.equal(imported.trim(), path.join(expectedDirectory, "payload", "integrations", "harbor", "hitch_benchmark.py"));
    const config = JSON.parse(await readFile(path.join(evalDirectory, result.backend.config_path), "utf8"));
    assert.equal(config.agents[0].kwargs.hitch_runtime_dir, expectedDirectory);
  }
  await run("upload", artifact, controller.directory);
  const prepared = await new SharedRuntimeSnapshots(path.join(root, "shared"), controller, "readonly-bind").prepare(artifact);
  const original = path.join(sourceBridge, "hitch_benchmark.py");
  await chmod(original, 0o644);
  await writeFile(original, "raise RuntimeError('changed original bridge imported')\n");
  await chmod(sourceBridge, 0o755);
  await unlink(path.join(sourceBridge, "hitch_host_credentials.py"));
  const snapshot = prepared.sharedRuntime!.runtime_directory;
  await useControllerRuntimeDirectory(snapshot, controller.runtime_id);
  await run("shared", prepared, snapshot);
  await useControllerRuntimeDirectory(snapshot, controller.runtime_id);
});

test("shared runtime bridge enforces read-only mounts and verifier isolation", () => {
  const result = spawnSync("python3", ["test-support/shared_runtime_smoke.py"], { encoding: "utf8", timeout: 30_000 });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stderr, /OK/);
});
