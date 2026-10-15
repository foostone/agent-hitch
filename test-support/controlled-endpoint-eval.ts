import path from "node:path";
import { readFile } from "node:fs/promises";
import type { EvalRequest, EvalTrialRefV1, RunId } from "../src/domain/index.js";
import type { PreparedArtifact, ResolvedRevision } from "../src/artifacts/index.js";
import { executeRun, newRunId, benchmarkTaskDigest, benchmarkVerifierIdentity, writeResultBundleIndex } from "../src/runs/index.js";
import { atomicWriteJSON, packageRoot, readJSON, sha256Bytes, sha256JSON } from "../src/foundation/index.js";
import { writeEvalTrialPublication } from "../src/evals/trial-publication.js";

/** Actual bundled harness and HTTP model calls; deterministic host verifier.
 * This software fixture deliberately does not claim Harbor/Docker/GPU acceptance. */
export async function controlledEndpointTrial(root: string, evalId: string, request: EvalRequest, taskId: string, repair: boolean, signal?: AbortSignal): Promise<EvalTrialRefV1> {
  const runner = path.join(packageRoot(), "integrations/model-call/cli.js"), commit = "a".repeat(40), ref = request.harness_ref;
  const resolution = { schema_version: "1", requested_ref: ref, canonical_ref: ref, harness_id: "model-call", selector: { type: "commit", value: commit },
    source: { type: "git", url: "https://github.com/rsi-gear/agent-hitch.git" }, revision: { type: "commit", commit }, identity: sha256JSON(ref), resolved_at: new Date().toISOString() } as unknown as ResolvedRevision;
  const prepared: PreparedArtifact = { schema_version: "1", artifact_id: sha256JSON(runner), harness_id: "model-call", revision_identity: resolution.identity,
    source_type: "git", adapter: "model-call", adapter_version: "1", recipe_version: "1", platform: `${process.platform}-${process.arch}`, entrypoint: runner,
    toolchain: {}, resolved_revision: resolution, artifact_integrity: sha256Bytes(await readFile(runner)), entrypoint_integrity: sha256Bytes(await readFile(runner)),
    prepared_at: new Date().toISOString(), executable: process.execPath, entrypoint_args: [runner], cache_hit: true };
  const runId = newRunId(), trialId = `${taskId}__${runId.slice(4)}`, directory = path.join(root, "runs", runId);
  const result = await executeRun({ runId, root, runsRoot: path.join(root, "runs"), resolvedRevision: resolution, preparedArtifact: prepared,
    request: { harness_ref: ref, model: request.model, endpoint_binding: request.endpoint_binding, cwd: root, prompt: `task:${taskId}`,
      timeout_ms: 5_000, context: { kind: "benchmark_task", benchmark_id: request.benchmark_id, benchmark_revision: request.benchmark_revision, task_id: taskId,
        task_digest: benchmarkTaskDigest(request.benchmark_id, request.benchmark_revision, taskId), verifier_identity: benchmarkVerifierIdentity(request.benchmark_id, request.benchmark_revision) },
      parent: { kind: "eval", eval_id: evalId, trial_id: trialId, attempt: 1 }, defer_benchmark_observation: true }, ...(signal ? { signal } : {}) });
  if (taskId === "zero" && result.status !== "succeeded") throw new Error(JSON.stringify(result));
  const valid = result.status === "succeeded", reward = taskId === "zero" ? 0 : 1;
  const observation = valid ? { status: "valid", reward, verifier_result_ref: "verifier/result.json" } : { status: "invalid", invalid_reason: "infrastructure_failure" };
  if (valid) await atomicWriteJSON(path.join(directory, "verifier/result.json"), { rewards: { reward } });
  const manifest = await readJSON<Record<string, unknown>>(path.join(directory, "manifest.json"));
  await atomicWriteJSON(path.join(directory, "manifest.json"), { ...manifest, observation, sealed: true });
  const trial: EvalTrialRefV1 = { trial_id: trialId, task_id: taskId, attempt: 1, run_id: runId as RunId,
    observation_status: valid ? "valid" : "invalid", ...(valid ? { reward, verifier_result_ref: "verifier/result.json" } : { invalid_reason: "infrastructure_failure" }) };
  await writeEvalTrialPublication(directory, evalId, repair ? "replace-invalid" : "settle", trial);
  await writeResultBundleIndex(directory);
  return trial;
}
