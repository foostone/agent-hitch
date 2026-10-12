import type { EvaluationBindingV1 } from "../../domain/index.js";
import { createHmac, randomBytes } from "node:crypto";
import { chmod, mkdir } from "node:fs/promises";
import path from "node:path";
import { snapshotHostCredentials } from "./credential-snapshot.js";
import { HOST_CREDENTIAL_HELPER_ENV, withHostCredentialPlaceholders } from "./host-credential-helper.js";
import { observeHarborPythonRuntime, type HarborPythonRuntimeV1 } from "./python-runtime.js";
import { evaluationEnvironmentFiles } from "./environment-files.js";
import { canonicalJSON, durableWriteJSON, HitchError, invalidInput, readJSON, withFileLock } from "../../foundation/index.js";

interface PrivateBinding {
  schema_version: "3";
  model: string;
  pass_env: string[];
  source_environment: string;
  python_runtime: HarborPythonRuntimeV1;
  environment_files: string;
  overrides: Record<string, string>;
  expires_at_ms: number | null;
}
const validId = (id: string) => /^hmac-sha256:[a-f0-9]{64}$/.test(id);
const mac = (key: string, kind: string, value: unknown) =>
  `hmac-sha256:${createHmac("sha256", Buffer.from(key, "hex")).update(kind + "\n" + canonicalJSON(value)).digest("hex")}`;
const environment = (env: NodeJS.ProcessEnv) => Object.fromEntries(Object.entries(env).filter((entry): entry is [string, string] => entry[1] !== undefined));
const directory = (root: string) => path.join(root, "evaluation-bindings");

function selection(model: string, names: readonly string[]) {
  if (typeof model !== "string" || !model || model.length > 1024 || /[\0\r\n]/.test(model)
    || !Array.isArray(names) || names.length > 128 || names.some(name => typeof name !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name))) {
    throw invalidInput("evaluation binding requires a model and environment variable names");
  }
  return { model, pass_env: [...new Set(names)].sort() };
}
function validity(value: number) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 24 * 60 * 60 * 1000) throw invalidInput("invalid evaluation binding validity");
}
async function keyFor(root: string, create: boolean): Promise<string> {
  const file = path.join(directory(root), "key.json");
  const value = await readJSON<unknown>(file, null).catch(() => { throw unavailable(); });
  if (value === null && create) {
    const key = randomBytes(32).toString("hex");
    await durableWriteJSON(file, { version: 1, key });
    return key;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).sort().join(",") !== "key,version"
    || (value as { version?: unknown }).version !== 1
    || typeof (value as { key?: unknown }).key !== "string"
    || !/^[a-f0-9]{64}$/.test((value as { key: string }).key)) throw unavailable();
  return (value as { key: string }).key;
}

/** The resident executor's environment is the authority, never the connecting CLI's process.env. */
export async function prepareEvaluationBinding(input: {
  root: string; env: NodeJS.ProcessEnv; model: string; passEnv: string[]; minimumValidityMs: number; signal?: AbortSignal;
}): Promise<EvaluationBindingV1> {
  const selected = selection(input.model, input.passEnv);
  validity(input.minimumValidityMs);
  const frozen = environment(input.env);
  const credentials = await snapshotHostCredentials(frozen, selected.pass_env, input.minimumValidityMs, input.signal);
  const executionEnv = withHostCredentialPlaceholders(frozen, Object.keys(credentials.overrides));
  const files = await evaluationEnvironmentFiles(executionEnv);
  const pythonRuntime = await observeHarborPythonRuntime({ root: input.root, env: executionEnv, ...(input.signal ? { signal: input.signal } : {}) });
  if (credentials.expiresAtMs !== null && credentials.expiresAtMs < Date.now() + input.minimumValidityMs) throw unavailable();
  input.signal?.throwIfAborted();
  await mkdir(directory(input.root), { recursive: true, mode: 0o700 });
  await chmod(directory(input.root), 0o700);
  return withFileLock(path.join(input.root, "locks"), "evaluation-bindings", async () => {
    const key = await keyFor(input.root, true);
    if (canonicalJSON(environment(input.env)) !== canonicalJSON(frozen)
      || canonicalJSON(await evaluationEnvironmentFiles(executionEnv)) !== canonicalJSON(files)
      || credentials.expiresAtMs !== null && credentials.expiresAtMs < Date.now() + input.minimumValidityMs) throw unavailable();
    const record: PrivateBinding = { schema_version: "3", ...selected,
      source_environment: mac(key, "resident-environment-v1", frozen),
      python_runtime: pythonRuntime,
      environment_files: mac(key, "evaluation-files-v1", files),
      overrides: credentials.overrides, expires_at_ms: credentials.expiresAtMs };
    const bindingId = mac(key, "evaluation-binding-v1", record);
    await durableWriteJSON(path.join(directory(input.root), bindingId.slice(12) + ".json"), record);
    return { schema_version: "1", binding_id: bindingId, ...selected, expires_at_ms: record.expires_at_ms };
  });
}

/** A missing, changed or expired binding never refreshes credentials or authorizes another plan. */
export async function boundEvaluationEnvironment(input: {
  root: string; env: NodeJS.ProcessEnv; bindingId: string; model: string; passEnv: string[]; minimumValidityMs: number;
  harborExecutable?: string;
}): Promise<NodeJS.ProcessEnv> {
  const selected = selection(input.model, input.passEnv);
  validity(input.minimumValidityMs);
  if (!validId(input.bindingId)) throw unavailable();
  try {
    const { key, record } = await verifiedRecord(input.root, input.bindingId, input.minimumValidityMs);
    if (record.model !== selected.model
      || canonicalJSON(record.pass_env) !== canonicalJSON(selected.pass_env)
      || record.source_environment !== mac(key, "resident-environment-v1", environment(input.env))
      || record.expires_at_ms !== null && record.expires_at_ms < Date.now() + input.minimumValidityMs) throw unavailable();
    const executionEnv = withHostCredentialPlaceholders(input.env, Object.keys(record.overrides));
    const checkFiles = async () => mac(key, "evaluation-files-v1", await evaluationEnvironmentFiles(executionEnv)) === record.environment_files;
    if (!await checkFiles()) throw unavailable();
    const pythonRuntime = await observeHarborPythonRuntime({ root: input.root, env: executionEnv,
      ...(input.harborExecutable ? { harborExecutable: input.harborExecutable } : {}) });
    if (canonicalJSON(pythonRuntime) !== canonicalJSON(record.python_runtime)
      || !await checkFiles()
      || record.source_environment !== mac(key, "resident-environment-v1", environment(input.env))
      || record.expires_at_ms !== null && record.expires_at_ms < Date.now() + input.minimumValidityMs) throw unavailable();
    // The helper result is now frozen. The Harbor child must not run the helper again.
    const result = { ...input.env, ...record.overrides };
    delete result[HOST_CREDENTIAL_HELPER_ENV];
    return result;
  } catch { throw unavailable(); }
}

async function verifiedRecord(root: string, bindingId: string, minimumValidityMs: number) {
  if (!validId(bindingId)) throw unavailable();
  const key = await keyFor(root, false);
  const record = await readJSON<PrivateBinding>(path.join(directory(root), bindingId.slice(12) + ".json"));
  if (mac(key, "evaluation-binding-v1", record) !== bindingId || record.schema_version !== "3"
    || record.expires_at_ms !== null && record.expires_at_ms < Date.now() + minimumValidityMs) throw unavailable();
  return { key, record };
}

/** Preserve Harbor's private credential transport: helper secrets never become ordinary agent env. */
export async function boundHarborEnvironment(input: Parameters<typeof boundEvaluationEnvironment>[0] & { runtimeDirectory: string }): Promise<NodeJS.ProcessEnv> {
  const env = await boundEvaluationEnvironment(input);
  const { record } = await verifiedRecord(input.root, input.bindingId, input.minimumValidityMs);
  const names = Object.keys(record.overrides).sort();
  if (names.length) {
    for (const name of names) env[name] = "";
    env[HOST_CREDENTIAL_HELPER_ENV] = JSON.stringify({ version: 1,
      argv: [process.execPath, path.join(input.runtimeDirectory, "payload/dist/src/backends/harbor/bound-credential-helper.js"), input.root, input.bindingId],
      credentialNames: names, timeoutMs: 10_000 });
  }
  return env;
}

/** Called only by the host-side private helper; it reads a frozen record and cannot refresh it. */
export async function readBoundHelperCredentials(root: string, bindingId: string, names: string[], minimumValidityMs: number) {
  validity(minimumValidityMs);
  try {
    const { record } = await verifiedRecord(root, bindingId, minimumValidityMs);
    if (canonicalJSON(names) !== canonicalJSON(Object.keys(record.overrides).sort()) || record.expires_at_ms === null) throw unavailable();
    return { version: 1, env: record.overrides, expiresAtMs: record.expires_at_ms };
  } catch { throw unavailable(); }
}

function unavailable() {
  return new HitchError("evaluation environment binding is missing, changed or expired; prepare a new run", {
    code: "evaluation_binding_unavailable", exitCode: 12,
  });
}
