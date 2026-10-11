/** Model-free replay of the dataset checks in eval admission and trial collection. */
import assert from "node:assert/strict";
import fs, { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { channel } from "node:diagnostics_channel";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { loadBenchmarkAdapterManifest } from "../src/evals/benchmark-adapter-manifest.js";
import { verifyBenchmarkExecution, verifyTrialBenchmark, verifiedTrialManifest } from "../src/evals/benchmark-verification.js";
import type { EvalRequest } from "../src/domain/index.js";
import { nativePhaseDescriptor } from "../src/evals/native-phase-evidence.js";

const { values } = parseArgs({ options: { dataset: { type: "string" }, baseline: { type: "string" }, output: { type: "string" } } });
if (!values.dataset || !values.baseline || !values.output) throw new Error("required: --dataset PATH --baseline PACKAGE_DIRECTORY --output JSON_FILE");
const dataset = await realpath(values.dataset), baseline = path.resolve(values.baseline);
const oldManifest = await import(pathToFileURL(path.join(baseline, "dist/src/evals/benchmark-adapter-manifest.js")).href) as { loadBenchmarkAdapterManifest: typeof loadBenchmarkAdapterManifest };
const oldNative = await import(pathToFileURL(path.join(baseline, "dist/src/evals/native-phase-evidence.js")).href) as { nativePhaseDescriptor: typeof nativePhaseDescriptor };
const scratch = await realpath(await mkdtemp(path.join(tmpdir(), "hitch-dataset-replay-")));
const originalRead = fs.readFile;
let reads = 0, bytes = 0;
let optimizedReads = false;
fs.readFile = (async (...args: unknown[]) => {
  const value = await Reflect.apply(originalRead, fs, args) as Buffer | string;
  const file = String(args[0]);
  if (!optimizedReads && file.startsWith(`${dataset}${path.sep}`)
    && path.relative(dataset, file).includes(path.sep)) {
    reads++;
    bytes += typeof value === "string" ? Buffer.byteLength(value) : value.length;
  }
  return value;
}) as typeof fs.readFile;
syncBuiltinESMExports();
const diagnostics = channel("hitch.benchmark.task-content-read");
const observe = (message: unknown) => {
  const value = message as { root: string; bytes: number };
  if (optimizedReads && value.root.startsWith(`${dataset}${path.sep}`)) {
    reads++;
    bytes += value.bytes;
  }
};
diagnostics.subscribe(observe);

async function replay(optimized: boolean) {
  const loader = optimized ? loadBenchmarkAdapterManifest : oldManifest.loadBenchmarkAdapterManifest;
  const native = optimized ? nativePhaseDescriptor : oldNative.nativePhaseDescriptor;
  optimizedReads = optimized;
  reads = 0; bytes = 0;
  const start = performance.now();
  const manifest = await loader(dataset);
  if (!manifest || manifest.schema_version !== "1") throw new Error("replay requires a legacy standardized dataset");
  const evalId = `eval_${"e".repeat(32)}`;
  const evalDirectory = path.join(scratch, "evals", evalId);
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
  const input = { root: scratch, evalId, evalDirectory, request, resolvedRevision,
    benchmarkId: manifest.benchmark.id, benchmarkRevision: manifest.dataset_digest,
    revisionIdentity: resolvedRevision.identity };
  const execution = optimized ? await verifyBenchmarkExecution(input) : null;
  if (optimized) assert.ok(execution);
  else assert.deepEqual(await loader(dataset), manifest); // admission and planning
  const descriptors = [];
  const collectionStart = performance.now();
  for (const [index, task] of manifest.tasks.entries()) {
    const binding = { ...input, taskId: task.task_id, trialDirectory: path.join(evalDirectory, "harbor/job", `${task.task_id}__1`) };
    const receipt = execution ? await verifyTrialBenchmark(binding, task.task_id, execution) : undefined;
    const descriptor = await native({ ...input, ...(receipt ? { verifiedTrialBenchmark: receipt, verifiedTrialBinding: binding } : {}) }, task.task_id);
    descriptors.push(descriptor);
    if (!descriptor) assert.deepEqual(receipt ? await verifiedTrialManifest(receipt, binding) : await loader(dataset), manifest);
    if ((index + 1) % 5 === 0) process.stderr.write(`${optimized ? "optimized" : "baseline"}: ${index + 1}/${manifest.tasks.length} tasks\n`);
  }
  const collectionMs = performance.now() - collectionStart;
  if (optimized) assert.deepEqual(await loader(dataset), manifest);
  return { mode: optimized ? "optimized" : "baseline", tasks: manifest.tasks.length,
    dataset_digest: manifest.dataset_digest, wall_ms: performance.now() - start,
    collection_ms: collectionMs, successful_task_content_reads: reads, logical_task_content_bytes: bytes, descriptors };
}

try {
  // Bracket the baseline so the timing comparison is not only a cold/warm pair.
  const before = await replay(true), baselineResult = await replay(false), after = await replay(true);
  assert.equal(before.dataset_digest, baselineResult.dataset_digest);
  assert.equal(after.dataset_digest, baselineResult.dataset_digest);
  assert.deepEqual(before.descriptors, baselineResult.descriptors);
  assert.deepEqual(after.descriptors, baselineResult.descriptors);
  assert.equal(before.logical_task_content_bytes, after.logical_task_content_bytes);
  assert.ok(baselineResult.logical_task_content_bytes > 0, "baseline package must expose pre-optimization task reads");
  const result = { dataset, baseline, measured_at: new Date().toISOString(), model_calls: 0,
    note: "Sequential dataset-check replay against a pre-optimization package, not end-to-end evaluation; bytes count task content only, exclude root manifest metadata, and are logical reads rather than physical disk I/O.",
    results: [before, baselineResult, after],
    logical_byte_reduction: 1 - after.logical_task_content_bytes / baselineResult.logical_task_content_bytes };
  await writeFile(values.output, `${JSON.stringify(result, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} finally {
  fs.readFile = originalRead; syncBuiltinESMExports();
  diagnostics.unsubscribe(observe);
  await rm(scratch, { recursive: true, force: true });
}
