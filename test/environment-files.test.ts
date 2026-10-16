import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm, symlink, utimes, truncate } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { evaluationEnvironmentFiles } from "../src/backends/harbor/environment-files.js";
import { runCommand } from "../src/foundation/index.js";

async function fixture(t: TestContext) {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-environment-files-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}
const unavailable = { code: "evaluation_environment_files_unavailable" };

test("same-path content changes identity while timestamp-only changes do not", async t => {
  const root = await fixture(t), file = path.join(root, "certificate.pem"), env = { SSL_CERT_FILE: file };
  await writeFile(file, "first");
  const first = await evaluationEnvironmentFiles(env);
  await utimes(file, new Date(1000), new Date(1000));
  assert.deepEqual(await evaluationEnvironmentFiles(env), first);
  await writeFile(file, "other");
  assert.notDeepEqual(await evaluationEnvironmentFiles(env), first);
  await writeFile(file, "first");
  assert.deepEqual(await evaluationEnvironmentFiles(env), first);
});

test("default credential/config files appearing or disappearing change identity", async t => {
  const root = await fixture(t), env = { HOME: root };
  const first = await evaluationEnvironmentFiles(env);
  assert.ok(first.every(item => item.kind === "missing"));
  for (const name of [".netrc", ".aws/config", ".aws/credentials", ".config/gcloud/application_default_credentials.json", ".docker/config.json"]) {
    const file = path.join(root, name);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, "private contents");
    assert.notDeepEqual(await evaluationEnvironmentFiles(env), first);
    await rm(file);
    assert.deepEqual(await evaluationEnvironmentFiles(env), first);
  }
});

test("an empty Docker config override still binds the default config", async t => {
  const root = await fixture(t);
  const env = { HOME: root, DOCKER_CONFIG: "" };
  const first = await evaluationEnvironmentFiles(env);
  assert.deepEqual(first, await evaluationEnvironmentFiles({ HOME: root }));
  await mkdir(path.join(root, ".docker"));
  await writeFile(path.join(root, ".docker/config.json"), "{}");
  assert.notDeepEqual(first, await evaluationEnvironmentFiles(env));
});

test("certificate directories bind entries and symlink targets and reject cycles", async t => {
  const root = await fixture(t), dir = path.join(root, "certs");
  await mkdir(dir);
  await writeFile(path.join(root, "first"), "same contents");
  await writeFile(path.join(root, "second"), "same contents");
  const alias = path.join(dir, "cert");
  await symlink(path.join(root, "first"), alias);
  const env = { SSL_CERT_DIR: dir }, first = await evaluationEnvironmentFiles(env);
  await rm(alias); await symlink(path.join(root, "second"), alias);
  assert.notDeepEqual(await evaluationEnvironmentFiles(env), first);
  await writeFile(path.join(dir, "added"), "additional CA");
  const added = await evaluationEnvironmentFiles(env);
  await rm(path.join(dir, "added"));
  assert.notDeepEqual(await evaluationEnvironmentFiles(env), added);
  await symlink(dir, path.join(dir, "cycle"));
  await assert.rejects(evaluationEnvironmentFiles(env), unavailable);
});

test("custom file variables are declared explicitly and unset/empty values are ignored", async t => {
  const root = await fixture(t), file = path.join(root, "custom");
  await writeFile(file, "private custom input");
  const env = { CUSTOM_INPUT: file, HITCH_EVALUATION_FILE_ENV_JSON: '["CUSTOM_INPUT","UNSET","EMPTY"]', EMPTY: "" };
  assert.equal((await evaluationEnvironmentFiles(env)).length, 1);
  assert.deepEqual(await evaluationEnvironmentFiles({ CUSTOM_INPUT: file }), []);
  assert.deepEqual(await evaluationEnvironmentFiles({ SSL_CERT_FILE: "", SSL_CERT_DIR: "" }), []);
  for (const declaration of ['{}', '["BAD-NAME"]', '[1]', 'invalid', JSON.stringify(Array(129).fill("VALID"))]) {
    await assert.rejects(evaluationEnvironmentFiles({ HITCH_EVALUATION_FILE_ENV_JSON: declaration }), unavailable);
  }
});

test("explicit missing/relative/special/oversized references fail without private diagnostics", async t => {
  const root = await fixture(t), large = path.join(root, "large"), fifo = path.join(root, "private-fifo");
  await writeFile(large, ""); await truncate(large, 16 * 1024 * 1024 + 1);
  await runCommand("mkfifo", [fifo]);
  for (const env of [{ SSL_CERT_FILE: path.join(root, "private-missing") }, { SSL_CERT_FILE: "private-relative" },
    { SSL_CERT_FILE: root }, { SSL_CERT_FILE: large }, { SSL_CERT_FILE: fifo }, { SSL_CERT_DIR: root + path.delimiter },
    { HOME: "private-relative" }, { DOCKER_CONFIG: "private-relative" }]) {
    await assert.rejects(evaluationEnvironmentFiles(env), error => {
      assert.equal((error as { code?: string }).code, unavailable.code);
      assert.ok(error instanceof Error && !error.message.includes("private-") && !error.message.includes(root));
      return true;
    });
  }
});

test("Docker certificate and config overrides bind their selected files", async t => {
  const root = await fixture(t), env = { DOCKER_CONFIG: root, DOCKER_CERT_PATH: root };
  const first = await evaluationEnvironmentFiles(env);
  assert.equal(first.length, 4);
  for (const name of ["config.json", "ca.pem", "cert.pem", "key.pem"]) {
    await writeFile(path.join(root, name), "private Docker input");
    assert.notDeepEqual(await evaluationEnvironmentFiles(env), first);
    await rm(path.join(root, name));
  }
});
