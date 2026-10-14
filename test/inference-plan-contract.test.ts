import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";
import type { InferenceLockV1, InferenceRuntimeManifestV1, LocalModelManifestV1, ModelNodeBindingV2 } from "../src/domain/index.js";
import { buildInferenceLock } from "../src/inference/index.js";

test("Python and TS standalone plan defaults produce identical complete locks including large-weight rounding", async () => {
  const root = path.resolve("packages/inference");
  const file = path.join(root, "test/fixtures/plan-contract.json");
  const fixture = JSON.parse(await readFile(file, "utf8")) as { model: LocalModelManifestV1; runtime: InferenceRuntimeManifestV1; binding: ModelNodeBindingV2;
    cases: Array<{ weights: number; context: number | null; profile: "baseline" | "throughput"; model_type: string; dtype: string; replicas: string[][]; api: "responses" | "chat-completions" }> };
  const projected = JSON.parse((await promisify(execFile)(process.env.HITCH_INFERENCE_TEST_PYTHON ?? "python3", [path.join(root, "test/plan_projection.py"), file], {
    env: { ...process.env, PYTHONPATH: path.join(root, "python") },
  })).stdout) as InferenceLockV1[];
  for (const [index, row] of fixture.cases.entries()) {
    const model = { ...fixture.model, dtype: row.dtype, model_type: row.model_type, context_tokens: row.context,
      files: [{ path: "model.safetensors", size: row.weights, sha256: `sha256:${"a".repeat(64)}` as const }] };
    const tp = row.replicas[0]!.length, capacity = row.profile === "throughput" ? 8 : 1;
    const lock = buildInferenceLock(model, fixture.runtime, { backend: "cuda", profile: row.profile, api: row.api, modelNode: fixture.binding,
      deviceConstraint: row.replicas[0]![0]!, tensorParallelSize: tp, pool: { replicas: row.replicas.map(devices => ({ devices })), routing: "least-active",
        max_running_requests: capacity * row.replicas.length, max_queued_requests: capacity * row.replicas.length } });
    assert.deepEqual(projected[index], lock, JSON.stringify(row));
  }
});
