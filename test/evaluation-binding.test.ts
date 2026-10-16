import test, { after, type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm, readdir, stat, mkdir, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { boundEvaluationEnvironment, prepareEvaluationBinding, runHarborBackend } from "../src/backends/index.js";
import type { RunHarborBackendOptions } from "../src/backends/index.js";
import { DaemonServer } from "../src/daemon/index.js";
import { runCommand } from "../src/foundation/index.js";
import { boundHarborEnvironment, harborRequestEnvironment, readBoundHelperCredentials } from "../src/backends/harbor/evaluation-binding.js";
import { assertEvalEnvironmentBinding } from "../src/evals/environment-binding.js";
import { evaluationCredentialValidity, HOST_CREDENTIAL_VALIDITY_MARGIN_MS } from "../src/backends/harbor/host-credential-helper.js";
import { snapshotHostCredentials } from "../src/backends/harbor/credential-snapshot.js";
import { EvalScheduler, ResourceLedger } from "../src/control-plane/index.js";
import { parseEvalSubmission } from "../src/control-plane/eval-records.js";
import { validateEvalRequest } from "../src/evals/index.js";
import { harborPreparedArtifact } from "../src/evals/prepared-harness.js";
import { nodeRuntimeHarnessFixture } from "../test-support/harbor-node-runtime-fixture.js";

const cli = fileURLToPath(new URL("../bin/hitch.js", import.meta.url));
const model = "provider/model", passEnv = ["PRIVATE_ENDPOINT", "PRIVATE_API_KEY"];
const pythonRoot = await mkdtemp(path.join(tmpdir(), "hitch-binding-python-"));
after(() => rm(pythonRoot, { recursive: true, force: true }));
await runCommand(process.env.HITCH_TEST_PYTHON || "python3", ["-m", "venv", "--without-pip", pythonRoot], { timeoutMs: 30_000 });
const python = path.join(pythonRoot, "bin/python"), launcher = path.join(pythonRoot, "bin/harbor");
await writeFile(launcher, `#!${python}\nprint('harbor 0.21.0')\n`, { mode: 0o755 });
const env = { PATH: process.env.PATH, HITCH_HARBOR_PATH: launcher, PRIVATE_ENDPOINT: "https://private.invalid/v1", PRIVATE_API_KEY: "private-static-key" };
const unavailable = { code: "evaluation_binding_unavailable" };
async function fixture(t: TestContext) {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-binding-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}
const prepare = (root: string, supplied = env) => prepareEvaluationBinding({ root, env: supplied, model, passEnv, minimumValidityMs: 1000 });

test("admission and launch include the bridge margin before consuming resources", async t => {
  const root = await fixture(t);
  const helper = path.join(root, "helper.cjs");
  await writeFile(helper, `console.log(JSON.stringify({version:1,env:{TOKEN:'fixture'},expiresAtMs:${Date.now() + 120000}}));`);
  const supplied = { ...env, HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1,
    argv: [process.execPath, helper], credentialNames: ["TOKEN"], timeoutMs: 5000 }) };
  const binding = await prepareEvaluationBinding({ root, env: supplied, model, passEnv: ["TOKEN"], minimumValidityMs: 1000 });
  const request = await validateEvalRequest({ dataset: "fixture@1.0", harness_ref: "pi@version:1.2.3", model,
    pass_env: ["TOKEN"], timeout_ms: 10000, setup_timeout_ms: 10000, environment_binding: binding.binding_id });
  await assert.rejects(assertEvalEnvironmentBinding(request, root, supplied), unavailable);
  await assert.rejects(harborRequestEnvironment({ root, env: supplied, request, runtimeDirectory: root }), unavailable);
});

test("credential validity covers a full-day task plus setup and the Python bridge margin", async t => {
  const root = await fixture(t);
  const bridge = fileURLToPath(new URL("../../integrations/harbor/hitch_host_credentials.py", import.meta.url));
  const result = await runCommand(python, ["-c", "import runpy,sys;print(runpy.run_path(sys.argv[1])['HOST_CREDENTIAL_VALIDITY_MARGIN_MS'])", bridge]);
  assert.equal(Number(result.stdout.trim()), HOST_CREDENTIAL_VALIDITY_MARGIN_MS);
  const validity = evaluationCredentialValidity({ timeout_ms: 24 * 60 * 60 * 1000, setup_timeout_ms: 0 });
  const helper = path.join(root, "helper.cjs");
  await writeFile(helper, `console.log(JSON.stringify({version:1,env:{TOKEN:'fixture'},expiresAtMs:${Date.now() + validity + 600000}}));`);
  const supplied = { ...env, HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1,
    argv: [process.execPath, helper], credentialNames: ["TOKEN"], timeoutMs: 5000 }) };
  const binding = await prepareEvaluationBinding({ root, env: supplied, model, passEnv: ["TOKEN"], minimumValidityMs: validity });
  assert.deepEqual((await readBoundHelperCredentials(root, binding.binding_id, ["TOKEN"], validity)).env, { TOKEN: "fixture" });
  for (const invalid of [-1, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER]) {
    await assert.rejects(readBoundHelperCredentials(root, binding.binding_id, ["TOKEN"], invalid), { code: "invalid_input" });
  }
});

test("resident bindings are stable across connections, root-private and never disclose values", async t => {
  const root = await fixture(t), other = await fixture(t);
  const values = await Promise.all([prepare(root), prepare(root), prepare(root)]);
  assert.deepEqual(values[0], values[1]); assert.deepEqual(values[1], values[2]);
  assert.notEqual((await prepare(other)).binding_id, values[0]!.binding_id);
  const serialized = JSON.stringify(values);
  assert.ok(!serialized.includes(env.PRIVATE_API_KEY)); assert.ok(!serialized.includes(env.PRIVATE_ENDPOINT));
  const binding = values[0]!;
  assert.deepEqual(await boundEvaluationEnvironment({ root, env, bindingId: binding.binding_id, model, passEnv, minimumValidityMs: 1000 }), env);
  assert.equal((await stat(path.join(root, "evaluation-bindings"))).mode & 0o777, 0o700);
  for (const name of await readdir(path.join(root, "evaluation-bindings"))) assert.equal((await stat(path.join(root, "evaluation-bindings", name))).mode & 0o777, 0o600);
});

test("endpoint, credential and runtime environment drift invalidate the original binding", async t => {
  const root = await fixture(t), first = await prepare(root);
  for (const changed of [{ ...env, PRIVATE_ENDPOINT: "https://changed.invalid" }, { ...env, PRIVATE_API_KEY: "changed-key" }, { ...env, PATH: "/changed" }]) {
    assert.notEqual((await prepare(root, changed)).binding_id, first.binding_id);
    await assert.rejects(boundEvaluationEnvironment({ root, env: changed, bindingId: first.binding_id, model, passEnv, minimumValidityMs: 0 }), unavailable);
  }
  for (const selected of [{ model: "provider/other", passEnv }, { model, passEnv: ["PRIVATE_API_KEY"] }]) {
    await assert.rejects(boundEvaluationEnvironment({ root, env, bindingId: first.binding_id, ...selected, minimumValidityMs: 0 }), unavailable);
  }
});

test("missing or tampered private records cannot be recreated by execution", async t => {
  const root = await fixture(t), binding = await prepare(root);
  const file = path.join(root, "evaluation-bindings", binding.binding_id.slice(12) + ".json");
  const original = await readFile(file, "utf8"), record = JSON.parse(original);
  await writeFile(file, JSON.stringify({ ...record, overrides: { PRIVATE_API_KEY: "tampered" } }));
  const resolve = () => boundEvaluationEnvironment({ root, env, bindingId: binding.binding_id, model, passEnv, minimumValidityMs: 0 });
  await assert.rejects(resolve(), unavailable);
  await writeFile(file, original); await rm(path.join(root, "evaluation-bindings/key.json"));
  await assert.rejects(resolve(), unavailable);
  await assert.rejects(readFile(path.join(root, "evaluation-bindings/key.json")), { code: "ENOENT" });
  await rm(file); await assert.rejects(resolve(), unavailable);
});

test("helper credentials are frozen once and never refreshed by execution", async t => {
  const root = await fixture(t), helper = path.join(root, "helper.cjs"), counter = path.join(root, "calls");
  const expiry = Date.now() + 60_000;
  const script = (secret: string) => `const fs=require('node:fs');let input='';process.stdin.on('data',c=>input+=c);process.stdin.on('end',()=>{const r=JSON.parse(input);if(r.version!==1||r.credentialNames.join()!=='TOKEN')process.exit(2);fs.appendFileSync(${JSON.stringify(counter)},'1');console.log(JSON.stringify({version:1,env:{TOKEN:${JSON.stringify(secret)}},expiresAtMs:${expiry}}));});`;
  await writeFile(helper, script("private-helper-token"));
  const supplied = { ...env, HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1, argv: [process.execPath, helper], credentialNames: ["TOKEN"], timeoutMs: 5000 }) };
  const options = { root, env: supplied, model, passEnv: ["TOKEN"], minimumValidityMs: 1000 };
  const binding = await prepareEvaluationBinding(options);
  await writeFile(helper, script("changed-helper-token"));
  const frozen = await boundEvaluationEnvironment({ ...options, bindingId: binding.binding_id });
  assert.equal(frozen.TOKEN, "private-helper-token"); assert.equal(frozen.HITCH_HOST_CREDENTIAL_HELPER_JSON, undefined);
  assert.equal(await readFile(counter, "utf8"), "1");
  const runtimeDirectory = path.join(root, "runtime");
  await mkdir(runtimeDirectory);
  await symlink(fileURLToPath(new URL("../../", import.meta.url)), path.join(runtimeDirectory, "payload"));
  const privateEnv = await boundHarborEnvironment({ ...options, runtimeDirectory, bindingId: binding.binding_id });
  assert.equal(privateEnv.TOKEN, "");
  assert.ok(!JSON.stringify(privateEnv).includes("private-helper-token"));
  assert.equal((await snapshotHostCredentials(privateEnv, ["TOKEN"], 1000)).overrides.TOKEN, "private-helper-token");
  assert.equal(await readFile(counter, "utf8"), "1", "private transport must never refresh the upstream helper");
  assert.notEqual((await prepareEvaluationBinding(options)).binding_id, binding.binding_id);
  assert.equal(await readFile(counter, "utf8"), "11");
  assert.ok(!JSON.stringify(binding).includes("private-helper-token"));
  await assert.rejects(boundEvaluationEnvironment({ ...options, bindingId: binding.binding_id, minimumValidityMs: 120000 }), unavailable);
});

test("helper failures do not leak private stdout or stderr", async t => {
  const root = await fixture(t), helper = path.join(root, "helper.cjs");
  const supplied = { HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1, argv: [process.execPath, helper], credentialNames: ["TOKEN"], timeoutMs: 1000 }) };
  for (const script of ["console.log('secret-on-stdout');console.error('secret-on-stderr');process.exit(1)", "console.log(JSON.stringify({version:1,env:{TOKEN:'secret-expired'},expiresAtMs:1}))"]) {
    await writeFile(helper, script);
    await assert.rejects(prepareEvaluationBinding({ root, env: supplied, model, passEnv: ["TOKEN"], minimumValidityMs: 1000 }), error => {
      assert.ok(error instanceof Error); assert.ok(!error.message.includes("secret-")); return true;
    });
  }
});

test("bound Harbor execution reads frozen credentials from the selected shared runtime", async t => {
  const root = await fixture(t), bin = path.join(root, "bin"), shared = path.join(root, "shared-runtime");
  await mkdir(bin); await mkdir(shared);
  await symlink(fileURLToPath(new URL("../../", import.meta.url)), path.join(shared, "payload"));
  const harbor = path.join(bin, "harbor"), helper = path.join(root, "helper.cjs"), counter = path.join(root, "helper-calls");
  await writeFile(helper, `const fs=require('node:fs');fs.appendFileSync(${JSON.stringify(counter)},'1');console.log(JSON.stringify({version:1,env:{TOKEN:'frozen-test-token'},expiresAtMs:${Date.now() + 600000}}));`);
  await writeFile(harbor, `#!${python}
import json, os, subprocess, sys
if '--version' in sys.argv:
    print('harbor 0.21.0')
    sys.exit(0)
helper = json.loads(os.environ['HITCH_HOST_CREDENTIAL_HELPER_JSON'])
assert os.environ['TOKEN'] == ''
reply = subprocess.run(helper['argv'], input=json.dumps({'version': 1, 'credentialNames': ['TOKEN'], 'minimumValidityMs': 1000}), text=True, capture_output=True, check=True)
assert json.loads(reply.stdout)['env']['TOKEN'] == 'frozen-test-token'
print(helper['argv'][1])
`, { mode: 0o755 });
  const supplied = { ...env, HITCH_HARBOR_PATH: harbor,
    HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1, argv: [process.execPath, helper], credentialNames: ["TOKEN"], timeoutMs: 5000 }) };
  const binding = await prepareEvaluationBinding({ root, env: supplied, model, passEnv: ["TOKEN"], minimumValidityMs: 2000 });
  const harness = await nodeRuntimeHarnessFixture(root);
  const artifact = { ...harborPreparedArtifact(root, { ...harness.manifest, executable: "node", entrypoint_args: [], cache_hit: false }),
    directory: harness.directory, sharedRuntime: { runtime_directory: shared, artifact_directory: harness.directory } };
  const request = await validateEvalRequest({ dataset: "fixture@1.0", harness_ref: "pi@version:1.2.3", model,
    pass_env: ["TOKEN"], timeout_ms: 1000, setup_timeout_ms: 1000, environment_binding: binding.binding_id,
    artifact_pins: [{ platform: artifact.platform, artifact_id: artifact.artifact_id,
      artifact_integrity: artifact.artifact_integrity, entrypoint_integrity: artifact.entrypoint_integrity }] });
  const evalDirectory = path.join(root, "eval");
  const result = await runHarborBackend({ root, evalId: "eval_" + "f".repeat(32), evalDirectory, request,
    resolvedRevision: harness.manifest.resolved_revision, runtimeDirectory: path.join(root, "removed-source-runtime"),
    preparedArtifact: artifact, harborExecutable: harbor, env: supplied });
  const stderr = await readFile(path.join(evalDirectory, result.backend.stderr_path), "utf8");
  assert.equal(result.backend.process_exit_code, 0, stderr);
  assert.equal((await readFile(path.join(evalDirectory, result.backend.stdout_path), "utf8")).trim(),
    path.join(shared, "payload/dist/src/backends/harbor/bound-credential-helper.js"));
  assert.equal(await readFile(counter, "utf8"), "1", "execution must not refresh the upstream helper");
  const serialized = await readFile(path.join(evalDirectory, result.backend.config_path), "utf8");
  assert.ok(!serialized.includes("frozen-test-token"));
  assert.equal(JSON.parse(serialized).agents[0].kwargs.hitch_runtime_dir, shared);
});

test("actual Harbor launch refuses changed environment before invoking its executable", async t => {
  const root = await fixture(t), binding = await prepare(root), dataset = path.join(root, "dataset"), marker = path.join(root, "started");
  await mkdir(dataset);
  const harbor = path.join(root, "harbor");
  await writeFile(harbor, `#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(marker)},'started');`, { mode: 0o755 });
  const options = { root, evalId: "eval_" + "a".repeat(32), evalDirectory: path.join(root, "eval"), env: { ...env, PRIVATE_ENDPOINT: "https://changed.invalid" }, harborExecutable: harbor,
    preparedArtifact: {}, request: { dataset, model, pass_env: passEnv, timeout_ms: 1000, setup_timeout_ms: 1000, environment_binding: binding.binding_id },
  } as unknown as RunHarborBackendOptions;
  await assert.rejects(runHarborBackend(options), unavailable);
  await assert.rejects(readFile(marker), { code: "ENOENT" });
  await assert.rejects(runHarborBackend({ ...options, env, modelProxy: {} as NonNullable<RunHarborBackendOptions["modelProxy"]> }), { code: "evaluation_binding_unsupported" });
  await assert.rejects(readFile(marker), { code: "ENOENT" });
});

test("same-version Python dependency drift changes binding and fences Harbor execution", async t => {
  const root = await fixture(t), bin = path.join(root, "bin"), marker = path.join(root, "started");
  await mkdir(bin);
  const harbor = path.join(bin, "harbor"), dependency = path.join(bin, "dependency.py");
  await writeFile(harbor, `#!${python}\nimport sys\nfrom pathlib import Path\nif '--version' in sys.argv: print('harbor 0.21.0')\nelse: Path(${JSON.stringify(marker)}).write_text('started')\n`, { mode: 0o755 });
  await writeFile(dependency, "VALUE = 1\n");
  const supplied = { ...env, HITCH_HARBOR_PATH: harbor };
  const first = await prepare(root, supplied);
  const options = { root, env: supplied, bindingId: first.binding_id, model, passEnv, minimumValidityMs: 0 };
  await boundEvaluationEnvironment(options);
  await writeFile(dependency, "VALUE = 2\n");
  assert.notEqual((await prepare(root, supplied)).binding_id, first.binding_id);
  await assert.rejects(boundEvaluationEnvironment(options), unavailable);
  await assert.rejects(runHarborBackend({ root, evalId: "eval_" + "d".repeat(32), evalDirectory: path.join(root, "eval"),
    preparedArtifact: {}, env: supplied, harborExecutable: harbor, request: { dataset: path.join(root, "dataset"), model, pass_env: passEnv,
      timeout_ms: 1000, setup_timeout_ms: 1000, environment_binding: first.binding_id },
  } as unknown as RunHarborBackendOptions), unavailable);
  await assert.rejects(readFile(marker), { code: "ENOENT" });
});

test("authenticated CLI prepares a nonce-bound resident binding without submitting an evaluation", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-binding-daemon-"));
  const server = new DaemonServer({ root, port: 0, maxConcurrent: 1, logger: () => {}, credentialEnv: env });
  t.after(async () => { await server.close(); await rm(root, { recursive: true, force: true }); }); await server.start();
  const response = await fetch(`http://127.0.0.1:${server.port}/v1/evaluation-bindings`, { method: "POST", body: "{}" });
  assert.equal(response.status, 401);
  await assert.rejects(readFile(path.join(root, "evaluation-bindings/key.json")), { code: "ENOENT" });
  const result = JSON.parse((await runCommand(process.execPath, [cli, "--root", root, "eval", "bind", "--model", model,
    ...passEnv.flatMap(name => ["--pass-env", name]), "--minimum-validity", "1s", "--timeout", "2m", "--json"], {
    env: { ...env, PRIVATE_ENDPOINT: "https://caller-must-not-be-authority.invalid" }, timeoutMs: 10000,
  })).stdout);
  assert.equal(result.daemon_instance_id, server.instanceId);
  assert.equal(result.binding.binding_id, (await prepare(root)).binding_id);
  assert.ok(!JSON.stringify(result).includes(env.PRIVATE_API_KEY));
  const listing = JSON.parse((await runCommand(process.execPath, [cli, "--root", root, "eval", "list", "--json"])).stdout);
  assert.deepEqual(listing.evals, []);
});

test("environment file drift invalidates a binding before Harbor and persists only its keyed identity", async t => {
  const root = await fixture(t), file = path.join(root, "private-ca.pem"), content = "private-file-contents";
  await writeFile(file, content);
  const supplied = { ...env, SSL_CERT_FILE: file }, first = await prepare(root, supplied);
  const record = await readFile(path.join(root, "evaluation-bindings", first.binding_id.slice(12) + ".json"), "utf8");
  assert.equal(JSON.parse(record).schema_version, "3");
  for (const secret of [content, file, createHash("sha256").update(content).digest("hex")]) assert.ok(!record.includes(secret));
  await writeFile(file, "changed private file");
  assert.notEqual((await prepare(root, supplied)).binding_id, first.binding_id);
  await assert.rejects(boundEvaluationEnvironment({ root, env: supplied, bindingId: first.binding_id, model, passEnv, minimumValidityMs: 0 }), unavailable);
  const marker = path.join(root, "started"), harbor = path.join(root, "harbor");
  await writeFile(harbor, `#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(marker)},'started');`, { mode: 0o755 });
  await assert.rejects(runHarborBackend({ root, evalId: "eval_" + "e".repeat(32), evalDirectory: path.join(root, "eval"),
    preparedArtifact: {}, env: supplied, harborExecutable: harbor, request: { dataset: path.join(root, "dataset"), model, pass_env: passEnv,
      timeout_ms: 1000, setup_timeout_ms: 1000, environment_binding: first.binding_id },
  } as unknown as RunHarborBackendOptions), unavailable);
  await assert.rejects(readFile(marker), { code: "ENOENT" });
});

test("helper file variables are checked as host placeholders while Target values remain privately frozen", async t => {
  const root = await fixture(t), helper = path.join(root, "helper.cjs");
  await writeFile(helper, `console.log(JSON.stringify({version:1,env:{SSL_CERT_FILE:'/target/only/certificate.pem',PYTHONPATH:'/target/only/modules'},expiresAtMs:${Date.now() + 120000}}));`);
  const supplied = { ...env, SSL_CERT_FILE: "/not/a/host/file", PYTHONPATH: "not-a-host-module-path",
    HITCH_HOST_CREDENTIAL_HELPER_JSON: JSON.stringify({ version: 1, argv: [process.execPath, helper], credentialNames: ["SSL_CERT_FILE", "PYTHONPATH"], timeoutMs: 5000 }) };
  const options = { root, env: supplied, model, passEnv: ["SSL_CERT_FILE", "PYTHONPATH"], minimumValidityMs: 1000 };
  const binding = await prepareEvaluationBinding(options);
  const runtimeDirectory = path.join(root, "runtime");
  await mkdir(runtimeDirectory);
  await symlink(fileURLToPath(new URL("../../", import.meta.url)), path.join(runtimeDirectory, "payload"));
  const host = await boundHarborEnvironment({ ...options, runtimeDirectory, bindingId: binding.binding_id });
  assert.equal(host.SSL_CERT_FILE, ""); assert.equal(host.PYTHONPATH, "");
  const target = await snapshotHostCredentials(host, options.passEnv, 1000);
  assert.equal(target.overrides.SSL_CERT_FILE, "/target/only/certificate.pem");
  assert.equal(target.overrides.PYTHONPATH, "/target/only/modules");
});

test("queued submissions preserve bindings through recovery and reject new drifted work without losing idempotency or cancellation", async t => {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-binding-queue-")), supplied = { ...env };
  const capacity = { cpu_millis: 1000, memory_bytes: 1024, container_slots: 1, build_slots: 0 };
  const resources = new ResourceLedger(capacity); resources.tryAcquire("block", "eval", capacity);
  const scheduler = new EvalScheduler({ root, resources, trialResources: capacity, credentialEnv: supplied,
    executor: async () => { throw new Error("blocked queue must not execute"); } });
  t.after(async () => { await scheduler.shutdown(); await rm(root, { recursive: true, force: true }); });
  await scheduler.initialize();
  const binding = await scheduler.prepareEnvironmentBinding({ model, passEnv, minimumValidityMs: 5000 });
  const input = { dataset: "demo@1.0", harness_ref: "pi@version:1.2.3", model, pass_env: passEnv,
    timeout_ms: 1000, setup_timeout_ms: 1000, environment_binding: binding.binding_id,
    artifact_pins: [{ platform: "linux-x64", artifact_id: `sha256:${"a".repeat(64)}`,
      artifact_integrity: `sha256:${"b".repeat(64)}`, entrypoint_integrity: `sha256:${"c".repeat(64)}` }] };
  for (const override of [{ environment_binding: "invalid" }, { timeout_ms: 0 }, { setup_timeout_ms: 24 * 60 * 60 * 1000 }, { model: "local/example" }]) {
    await assert.rejects(validateEvalRequest({ ...input, ...override }));
  }
  const submission = { request: input, execution: { provider: "local-docker", max_parallelism: 1,
    resources: { default_trial: capacity }, build: { mode: "backend" }, model_capture: { mode: "native", required: false } } };
  const id = await scheduler.submit(submission, { idempotencyKey: "original" });
  const saved = JSON.parse(await readFile(path.join(root, "evals", id, "submission.json"), "utf8"));
  assert.equal((await parseEvalSubmission(saved, id)).request.environment_binding, binding.binding_id);
  assert.deepEqual((await parseEvalSubmission(saved, id)).request.artifact_pins, input.artifact_pins);
  await assert.rejects(scheduler.submit({ ...submission, request: { ...input, artifact_pins: [{ ...input.artifact_pins[0], artifact_id: `sha256:${"d".repeat(64)}` }] } }, { idempotencyKey: "original" }), { code: "idempotency_conflict" });
  supplied.PRIVATE_ENDPOINT = "https://changed.invalid";
  assert.equal(await scheduler.submit(submission, { idempotencyKey: "original" }), id);
  await assert.rejects(scheduler.submit(submission, { idempotencyKey: "new" }), unavailable);
  assert.equal((await readdir(path.join(root, "evals"))).length, 1);
  assert.equal(await scheduler.cancel(id), "accepted");
  assert.equal((await scheduler.status(id))?.control.state, "cancelled");
});
