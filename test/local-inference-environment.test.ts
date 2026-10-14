import test from "node:test";
import assert from "node:assert/strict";
import { harnessChildEnvironment } from "../src/runs/local-inference-environment.js";
import { scrubLocalInferenceEnvironment } from "../src/model-access/index.js";
import type { RunId } from "../src/domain/index.js";

const runId = `run_${"a".repeat(32)}` as RunId;
const identity = {
  inference_id: `sha256:${"c".repeat(64)}` as const,
  model_id: `sha256:${"d".repeat(64)}` as const,
};

test("managed local inference removes cloud and model-runtime environment variables", () => {
  const scrubbed = scrubLocalInferenceEnvironment({
    PATH: "/bin",
    HITCH_CODEX_PATH: "/opt/codex",
    OPENAI_API_KEY: "cloud-secret",
    ANTHROPIC_BASE_URL: "https://cloud.invalid",
    HF_TOKEN: "download-secret",
    SGLANG_USE_CPU_ENGINE: "0",
    TRANSFORMERS_OFFLINE: "0",
    TORCH_LOGS: "all",
    CODEX_HOME: "/user/config",
    HITCH_LOCAL_MODEL_TOKEN: "stale-local-token",
  });
  assert.deepEqual(scrubbed, { PATH: "/bin", HITCH_CODEX_PATH: "/opt/codex" });
});

test("external Harness receives only task proxy credentials and strips the private upstream key", () => {
  const child = harnessChildEnvironment({ parent: { PATH: "/bin", CODER_KEY: "private-upstream", OPENAI_API_KEY: "cloud", OPENAI_BASE_URL: "https://cloud.invalid" },
    adapter: { HITCH_LOCAL_MODEL_TOKEN: "task-proxy" }, privateCredentials: ["private-upstream"], cwd: "/workspace", managedLocal: true });
  assert.equal(child.CODER_KEY, undefined); assert.equal(child.OPENAI_API_KEY, undefined); assert.equal(child.OPENAI_BASE_URL, undefined);
  assert.equal(child.HITCH_LOCAL_MODEL_TOKEN, "task-proxy");
});
