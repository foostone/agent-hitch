import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, realpath, rename, rm, stat, symlink, utimes, writeFile } from "node:fs/promises";
import { channel } from "node:diagnostics_channel";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { type TestContext } from "node:test";
import { buildBenchmarkAdapterManifest, loadBenchmarkAdapterManifest, persistTrialVerifierDiagnostics, runEval } from "../src/evals/index.js";
import { nativePhaseDescriptor } from "../src/evals/native-phase-evidence.js";
import type { BenchmarkAdapterManifestV1 } from "../src/evals/benchmark-adapter-manifest.js";
import { verifyBenchmarkExecution, verifyTrialBenchmark, verifiedTrialManifest } from "../src/evals/benchmark-verification.js";
import type { EvalRequest } from "../src/domain/index.js";
import { forceRemove, prepareHostHarborArtifactForTest, writeFakeHarbor, writeFakeNpm } from "../test-support/helpers.js";
import { resourceFixture } from "../test-support/resource-fixture.js";
import { selectResources } from "../src/resources/index.js";

async function fixture(t: TestContext, ids = ["alpha", "beta"], benchmark = "arbitrary-suite") {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "hitch-dataset-verification-")));
  t.after(() => forceRemove(root));
  const dataset = path.join(root, "dataset");
  for (const id of ids) {
    await mkdir(path.join(dataset, id, "arbitrary", "nested"), { recursive: true });
    await writeFile(path.join(dataset, id, "task.toml"), "# independent fixture\n");
    await writeFile(path.join(dataset, id, "arbitrary", "nested", "input.bin"), Buffer.from([0, 42, 255, id.length]));
  }
  const manifest = await buildBenchmarkAdapterManifest({
    dataset, taskIds: ids,
    benchmark: { id: benchmark, revision: "release-1" },
    adapter: { id: "arbitrary-adapter", revision: "revision-1", output_protocol: "gear-harbor-eval-result-v1" },
    scoring: { total_score: { source_metric: "accuracy", direction: "maximize", range: [0, 1], reducer: "task-macro-mean" } },
  });
  const manifestFile = path.join(dataset, "benchmark.adapter.json");
  await writeFile(manifestFile, JSON.stringify(manifest));
  return { root, dataset, manifest, manifestFile, ...await admit(root, dataset, manifest) };
}

async function admit(root: string, dataset: string, manifest: BenchmarkAdapterManifestV1) {
  const evalId = `eval_${"e".repeat(32)}`;
  const evalDirectory = path.join(root, "evals", evalId);
  const request: EvalRequest = {
    schema_version: "1", backend: "harbor", dataset, harness_ref: "pi@version:1.2.3", model: "synthetic-model",
    attempts: 1, max_concurrent: 1, infrastructure_retries: 0, infrastructure_retry_backoff_ms: 0,
    timeout_ms: 0, setup_timeout_ms: 1000, agent_args: [], pass_env: [],
    benchmark_id: manifest.benchmark.id, benchmark_revision: manifest.dataset_digest,
  };
  const resolvedRevision = {
    schema_version: "1", requested_ref: request.harness_ref, canonical_ref: request.harness_ref,
    harness_id: "pi", selector: { type: "version", value: "1.2.3" }, source: { type: "npm" },
    revision: { type: "version", version: "1.2.3" }, identity: `sha256:${"a".repeat(64)}`,
    resolved_at: "2026-10-11T00:00:00.000Z",
  } as const;
  await mkdir(evalDirectory, { recursive: true });
  await writeFile(path.join(evalDirectory, "request.json"), JSON.stringify(request));
  const input = { root, evalId, evalDirectory, request, resolvedRevision,
    revisionIdentity: resolvedRevision.identity, benchmarkId: manifest.benchmark.id, benchmarkRevision: manifest.dataset_digest };
  const execution = await verifyBenchmarkExecution(input);
  assert.ok(execution);
  const trial = async (taskId: string) => {
    const verifiedTrialBinding = { ...input, taskId, trialDirectory: path.join(evalDirectory, "harbor/job", `${taskId}__1`) };
    const verifiedTrialBenchmark = await verifyTrialBenchmark(verifiedTrialBinding, taskId, execution);
    return { ...input, verifiedTrialBinding, verifiedTrialBenchmark };
  };
  const verify = async (taskId: string) => {
    const input = await trial(taskId);
    return verifiedTrialManifest(input.verifiedTrialBenchmark, input.verifiedTrialBinding);
  };
  return { trial, verify };
}

function trackReads(t: TestContext) {
  const reads: string[] = [];
  const observer = (message: unknown) => {
    const value = message as { root: string; path: string };
    reads.push(path.join(value.root, value.path));
  };
  const diagnostics = channel("hitch.benchmark.task-content-read");
  diagnostics.subscribe(observer);
  t.after(() => diagnostics.unsubscribe(observer));
  return reads;
}

test("collection reads task payloads linearly across arbitrary dataset sizes", async t => {
  for (const count of [1, 3, 20]) await t.test(`${count} tasks`, async t => {
    const ids = Array.from({ length: count }, (_, i) => `case-${i}`);
    const { dataset, manifest, trial } = await fixture(t, ids);
    const reads = trackReads(t);
    assert.deepEqual(await loadBenchmarkAdapterManifest(dataset), manifest);
    for (const taskId of ids) {
      const input = await trial(taskId);
      assert.equal(await nativePhaseDescriptor(input, taskId), null);
      assert.deepEqual(await verifiedTrialManifest(input.verifiedTrialBenchmark, input.verifiedTrialBinding), manifest);
    }
    assert.deepEqual(await loadBenchmarkAdapterManifest(dataset), manifest);
    const payloadReads = reads.filter(file => file.endsWith("task.toml") || file.endsWith("input.bin"));
    assert.equal(payloadReads.length, count * 2 * 3, "admission + one read per collected task + final sweep");
  });
});

test("task-scoped checks reject content, mode, membership and link changes without a metadata cache", async t => {
  const mutations: Array<[string, (task: string) => Promise<unknown>]> = [
    ["content with restored size and mtime", async task => {
      const file = path.join(task, "arbitrary/nested/input.bin"), before = await stat(file);
      await writeFile(file, Buffer.from([9, 8, 7, 6]));
      await utimes(file, before.atime, before.mtime);
    }],
    ["executable bit", task => chmod(path.join(task, "task.toml"), 0o755)],
    ["new file", task => writeFile(path.join(task, "new-file"), "added")],
    ["deleted file", task => rm(path.join(task, "arbitrary/nested/input.bin"))],
    ["renamed file", task => rename(path.join(task, "arbitrary/nested/input.bin"), path.join(task, "arbitrary/nested/renamed.bin"))],
    ["file symlink", async task => {
      await rm(path.join(task, "arbitrary/nested/input.bin"));
      await symlink("../../task.toml", path.join(task, "arbitrary/nested/input.bin"));
    }],
    ["task symlink", async task => {
      await rename(task, `${task}-original`);
      await symlink(`${task}-original`, task);
    }],
  ];
  for (const [name, mutate] of mutations) await t.test(name, async t => {
    const { dataset, verify } = await fixture(t);
    await verify("alpha");
    await mutate(path.join(dataset, "alpha"));
    await assert.rejects(verify("alpha"), /digest mismatch|symlink|not a directory/);
    await assert.rejects(loadBenchmarkAdapterManifest(dataset));
  });
});

test("pinned contracts reject forged or replaced manifests, unknown tasks, and changed task sets", async t => {
  const { dataset, manifest, manifestFile, verify } = await fixture(t);
  await assert.rejects(verify("unknown"), /absent from the admitted/);
  await assert.rejects(verify("../alpha"), /absent from the admitted/);
  const forged = structuredClone(manifest);
  forged.scoring.total_score.range = [0, 100];
  await writeFile(manifestFile, JSON.stringify(forged));
  await assert.rejects(verify("alpha"), /dataset digest mismatch/);
  const replaced = await buildBenchmarkAdapterManifest({ ...forged, dataset, taskIds: ["alpha", "beta"] });
  await writeFile(manifestFile, JSON.stringify(replaced));
  await assert.rejects(verify("alpha"), /changed after eval admission/);
  await writeFile(manifestFile, JSON.stringify(manifest));
  await mkdir(path.join(dataset, "extra"));
  await writeFile(path.join(dataset, "extra/task.toml"), "");
  await assert.rejects(verify("alpha"), /task set does not match/);
  await rm(path.join(dataset, "extra"), { recursive: true });
  await rm(path.join(dataset, "beta"), { recursive: true });
  await assert.rejects(verify("alpha"), /manifest task is missing/);
});

test("concurrent datasets and retries do not share stale validation results", async t => {
  const a = await fixture(t), b = await fixture(t, ["alpha", "beta"], "another-suite");
  await Promise.all([a.verify("alpha"), b.verify("alpha"), a.verify("beta"), b.verify("beta")]);
  const file = path.join(a.dataset, "alpha/arbitrary/nested/input.bin"), original = await readFile(file);
  await writeFile(file, "changed");
  const results = await Promise.allSettled([a.verify("alpha"), b.verify("alpha"), a.verify("beta")]);
  assert.deepEqual(results.map(result => result.status), ["rejected", "fulfilled", "fulfilled"]);
  // A late edit to a task already collected is still caught by the final sweep.
  await assert.rejects(loadBenchmarkAdapterManifest(a.dataset), /task digest mismatch/);
  await writeFile(file, original);
  assert.deepEqual(await a.verify("alpha"), a.manifest);
  assert.deepEqual(await loadBenchmarkAdapterManifest(a.dataset), a.manifest);
});

test("score collection uses the current task and preserves the scoring contract", async t => {
  const { root, dataset, manifest, trial } = await fixture(t);
  const reads = trackReads(t);
  const verified = await trial("alpha");
  const trialDirectory = verified.verifiedTrialBinding.trialDirectory;
  await mkdir(path.join(trialDirectory, "verifier"), { recursive: true });
  const input = { ...verified, dataset, benchmarkRevision: manifest.dataset_digest, trialDirectory,
    runDirectory: path.join(root, "run"), verifierResult: { rewards: { reward: 1, total_score: 1 } } };
  assert.deepEqual((await persistTrialVerifierDiagnostics(input)).scores, { total_score: 1, normalization: "standard" });
  assert.ok(reads.some(file => file.includes("/alpha/")));
  assert.equal(reads.some(file => file.includes("/beta/")), false);
  const outOfRange = await persistTrialVerifierDiagnostics({ ...input, verifierResult: { rewards: { reward: 2, total_score: 2 } } });
  assert.match(outOfRange.issue!, /outside the benchmark range/);
  await writeFile(path.join(dataset, "alpha/task.toml"), "mutated");
  await assert.rejects(trial("alpha"), /task digest mismatch/);
});

test("exported native phase descriptors verify their own task; standalone callers retain full verification", async t => {
  const { root, dataset, manifest, manifestFile } = await fixture(t);
  const descriptorFile = path.join(dataset, "alpha/.hitch-benchmark.json");
  const descriptor = { task_id: "alpha", task_digest: "source-task-digest", primary_metric: "accuracy",
    score_contract: { total_score: "accuracy" }, metrics: {}, agent_timeout_sec: 60,
    task: { driver: { kind: "tool-server", config: { native_phases: { audit_path: "/audit.jsonl" } } } } };
  await writeFile(descriptorFile, JSON.stringify(descriptor));
  const sealed = await buildBenchmarkAdapterManifest({ ...manifest, dataset, taskIds: ["alpha", "beta"] });
  await writeFile(manifestFile, JSON.stringify(sealed));
  const { trial } = await admit(root, dataset, sealed);
  const reads = trackReads(t);
  const input = await trial("alpha");
  assert.equal((await nativePhaseDescriptor(input, "alpha"))?.agent_timeout_ms, 60_000);
  assert.equal(reads.some(file => file.includes("/beta/")), false);
  await writeFile(path.join(dataset, "beta/task.toml"), "changed");
  const { verifiedTrialBenchmark: _receipt, verifiedTrialBinding: _binding, ...standalone } = input;
  await assert.rejects(nativePhaseDescriptor(standalone, "alpha"), /task digest mismatch: beta/);
  await writeFile(descriptorFile, JSON.stringify({ ...descriptor, agent_timeout_sec: 120 }));
  await assert.rejects(trial("alpha"), /task digest mismatch: alpha/);
});

test("resource datasets and selections retain their CAS identities and task membership", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-verification-resources-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const { source, dataset } = await resourceFixture(root);
  assert.deepEqual(await loadBenchmarkAdapterManifest(source), dataset);
  const selection = selectResources(dataset, ["task-1"]), file = path.join(root, "selection.json");
  await writeFile(file, JSON.stringify(selection));
  const selected = await loadBenchmarkAdapterManifest(file);
  assert.equal(selected?.dataset_digest, selection.digest);
  assert.deepEqual(selected?.tasks.map(task => task.task_id), ["task-1"]);
});

test("eval finalization catches late payload, manifest and scoring-contract changes", async t => {
  for (const change of ["none", "payload", "manifest", "contract"] as const) await t.test(change, async t => {
    const { root, dataset, manifest, manifestFile } = await fixture(t, ["one", "two"]);
    const harbor = await writeFakeHarbor(root), npm = await writeFakeNpm(root);
    let finalized = false;
    const result = await runEval({
      root, harborExecutable: harbor, harborArtifactBuilder: prepareHostHarborArtifactForTest,
      env: { ...process.env, HITCH_NPM_PATH: npm },
      request: { dataset, harness_ref: "pi@version:1.2.3", model: "openai/test-model", infrastructure_retries: 0 },
      onControlPhase: async phase => {
        if (phase !== "finalizing") return;
        finalized = true;
        if (change === "payload") await writeFile(path.join(dataset, "one/arbitrary/nested/input.bin"), "late edit");
        if (change === "manifest") await rm(manifestFile);
        if (change === "contract") {
          const scoring = { total_score: { ...manifest.scoring.total_score, source_metric: "changed" } };
          await writeFile(manifestFile, JSON.stringify(await buildBenchmarkAdapterManifest({ ...manifest, dataset, taskIds: ["one", "two"], scoring })));
        }
      },
    });
    assert.equal(finalized, true, JSON.stringify(result.error));
    if (change === "none") {
      assert.notEqual(result.failure_stage, "finalizing");
      assert.ok(Array.isArray(result.trials));
      assert.deepEqual(result.trials.map(trial => trial.task_id).sort(), ["one", "two"]);
    } else {
      assert.equal(result.status, "failed");
      assert.equal(result.failure_stage, "finalizing");
      const expected = { payload: /task digest mismatch/, manifest: /manifest disappeared/, contract: /manifest changed/ };
      assert.match(result.error!.message, expected[change]);
    }
  });
});
