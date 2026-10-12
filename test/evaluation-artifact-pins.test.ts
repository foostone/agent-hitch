import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parseEvalRequest } from "../src/cli/arguments.js";
import { runEval, validateEvalRequest } from "../src/evals/index.js";
import { loadPersistedRerunRequest } from "../src/evals/rerun-inputs.js";
import { runHarborBackend } from "../src/backends/index.js";
import type { RunHarborBackendOptions } from "../src/backends/index.js";
import type { EvalArtifactPinV1 } from "../src/domain/index.js";
import { forceRemove, prepareHostHarborArtifactForTest, writeFakeNpm } from "../test-support/helpers.js";

const pin: EvalArtifactPinV1 = { platform: "linux-x64", artifact_id: `sha256:${"a".repeat(64)}`,
  artifact_integrity: `sha256:${"b".repeat(64)}`, entrypoint_integrity: `sha256:${"c".repeat(64)}` };
const other = { ...pin, platform: "linux-arm64" as const, artifact_id: `sha256:${"d".repeat(64)}` };
const input = { dataset: "demo@1.0", harness_ref: "pi@version:1.2.3" };

test("CLI pins are canonical request fields retained by candidate-restart recovery", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-artifact-pins-")); t.after(() => forceRemove(root));
  const args = ["--dataset", input.dataset, "--harness", input.harness_ref, "--artifact-pin", JSON.stringify(pin), "--artifact-pin", JSON.stringify(other)];
  const request = await validateEvalRequest(parseEvalRequest(args));
  assert.deepEqual(args, []);
  assert.deepEqual(request.artifact_pins, [other, pin]);
  assert.deepEqual((await loadPersistedRerunRequest(request, root)).artifact_pins, [other, pin]);
  assert.throws(() => parseEvalRequest(["--artifact-pin", "not-json"]), /JSON artifact pin/);
  for (const pins of [[], [pin, { ...pin, artifact_id: other.artifact_id }], [{ ...pin, path: "/untrusted" }], [{ ...pin, platform: "darwin-arm64" }], [{ ...pin, artifact_integrity: "missing" }]]) {
    await assert.rejects(validateEvalRequest({ ...input, artifact_pins: pins }), /artifact pin/);
  }
});

test("Harbor rejects altered artifact identity, files, launcher or platform before any executable call", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-artifact-launch-")); t.after(() => forceRemove(root));
  const executable = path.join(root, "harbor"), marker = path.join(root, "started");
  const dataset = path.join(root, "dataset"); await mkdir(dataset);
  await writeFile(executable, `#!/usr/bin/env node\nrequire('node:fs').writeFileSync(${JSON.stringify(marker)},'called');`, { mode: 0o755 });
  for (const changed of [{ artifact_id: other.artifact_id }, { artifact_integrity: other.artifact_id }, { entrypoint_integrity: other.artifact_id }, { platform: other.platform }]) {
    await assert.rejects(runHarborBackend({ root, evalDirectory: path.join(root, "eval"), harborExecutable: executable,
      request: { dataset, artifact_pins: [pin] }, preparedArtifact: { ...pin, ...changed },
    } as unknown as RunHarborBackendOptions), { code: "eval_artifact_pin_mismatch" });
    await assert.rejects(readFile(marker), { code: "ENOENT" });
  }
});

test("fresh preparation cannot replace submitted pins before publishing the executable plan", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-artifact-preparation-")); t.after(() => forceRemove(root));
  const dataset = path.join(root, "dataset"), marker = path.join(root, "harbor-started"), executable = path.join(root, "harbor");
  await mkdir(path.join(dataset, "one"), { recursive: true });
  await writeFile(path.join(dataset, "one/task.toml"), "");
  await writeFile(path.join(root, "python"), `#!${process.execPath}\nprocess.stdout.write(JSON.stringify({schema_version:'1',runtime_platform:'linux/amd64',task:{},verifier:{separate:false},compose_services:[{name:'main',replicas:1}],provider_sidecars:{main_egress:false,verifier_egress:false},environment_images:[],environment_image_fallbacks:[],environment_builds:[]}));`, { mode: 0o755 });
  await writeFile(executable, `#!/usr/bin/env node\nif(process.argv.includes('--version')) { console.log('harbor 0.21.0'); } else { require('node:fs').writeFileSync(${JSON.stringify(marker)},'called'); }`, { mode: 0o755 });
  const npm = await writeFakeNpm(root);
  const result = await runEval({ root, harborExecutable: executable, env: { ...process.env, HITCH_NPM_PATH: npm },
    harborArtifactBuilder: prepareHostHarborArtifactForTest,
    request: { ...input, dataset, model: "test/no-model-call", artifact_pins: [pin] } });
  assert.equal(result.status, "failed");
  assert.equal(result.error?.code, "eval_artifact_pin_mismatch");
  assert.deepEqual(result.trials, []);
  await assert.rejects(readFile(marker), { code: "ENOENT" });
  await assert.rejects(readFile(path.join(root, "evals", result.eval_id, "execution-plan.json")), { code: "ENOENT" });
});

test("published artifact pin schema requires all content digests and excludes local paths", async () => {
  const schema = JSON.parse(await readFile(new URL("../../docs/schemas/eval-artifact-pin.schema.json", import.meta.url), "utf8"));
  const validate = new Ajv2020().compile(schema);
  assert.equal(validate(pin), true);
  assert.equal(validate({ ...pin, directory: "/private/path" }), false);
  assert.equal(validate({ ...pin, entrypoint_integrity: "unverified" }), false);
});
