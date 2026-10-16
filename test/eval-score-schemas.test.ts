import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parseVerifierScores } from "../src/domain/index.js";
import type { EvalTrialRefV1 } from "../src/domain/index.js";

const schemaBase = "https://agent-hitch.local/schemas/";
const scoreSchemas = [
  "eval-trial-reference.schema.json",
  "eval-result.schema.json",
  "eval-progress.schema.json",
  "eval-trial-publication.schema.json",
] as const;

test("published eval schemas accept standard, total-only, and legacy score channels", async () => {
  const ajv = await schemaValidator();
  const scoreCases = [
    undefined,
    parseVerifierScores({ rewards: { reward: 0 } }),
    parseVerifierScores({ rewards: { reward: 0, total_score: 0 } }),
    parseVerifierScores({ rewards: { reward: 0, total_score: 0, process_score: 0.5 } }),
  ];
  for (const scores of scoreCases) {
    const trial: EvalTrialRefV1 = {
      ...trialFixture,
      ...(scores === undefined ? {} : { scores }),
    };
    for (const [schema, document] of documentsWithTrial(trial)) {
      assert.equal(ajv.validate(`${schemaBase}${schema}`, document), true, `${schema}: ${ajv.errorsText()}`);
    }
  }
});

test("published eval schemas reject malformed and inconsistent score channels", async () => {
  const ajv = await schemaValidator();
  const invalidScores = [
    {},
    { total_score: 0 },
    { normalization: "standard" },
    { total_score: "0", normalization: "standard" },
    { total_score: 0, process_score: "0.5", normalization: "standard" },
    { total_score: 0, normalization: "unknown" },
    { total_score: 0, process_score: 0.5, normalization: "legacy-reward" },
    { total_score: 0, normalization: "standard", extra: true },
  ];
  for (const scores of invalidScores) {
    for (const [schema, document] of documentsWithTrial({ ...trialFixture, scores })) {
      assert.equal(ajv.validate(`${schemaBase}${schema}`, document), false, `${schema} accepted ${JSON.stringify(scores)}`);
    }
  }
  const invalidTrial = { ...trialFixture, observation_status: "invalid", invalid_reason: "verifier_result_missing" };
  for (const [schema, document] of documentsWithTrial({ ...invalidTrial, scores: { total_score: 0, normalization: "standard" } })) {
    assert.equal(ajv.validate(`${schemaBase}${schema}`, document), false, `${schema} accepted scores for an invalid trial`);
  }
});

test("published progress schema accepts terminal status projections", async () => {
  const ajv = await schemaValidator();
  const progress = documentsWithTrial(trialFixture).find(([schema]) => schema === "eval-progress.schema.json")![1] as Record<string, unknown>;
  for (const status of ["running", "succeeded", "failed", "cancelled"]) {
    assert.equal(ajv.validate(`${schemaBase}eval-progress.schema.json`, { ...progress, status }), true, ajv.errorsText());
  }
  assert.equal(ajv.validate(`${schemaBase}eval-progress.schema.json`, { ...progress, status: "unknown" }), false);
});

test("published endpoint schemas separate public evidence from private ready inputs", async () => {
  const ajv = await schemaValidator(), capabilities = { streaming: true, tool_calls: true, parallel_tool_calls: false, input_modalities: ["text"] };
  const connection = { base_url: "http://127.0.0.1:30000/tenant/v1", auth: { kind: "env", name: "HITCH_TEST_KEY" } };
  const binding = { schema_version: "1", kind: "external-endpoint", endpoint_ref: "endpoint/coder", endpoint_revision: `sha256:${"a".repeat(64)}`,
    api: "responses", wire_model: "candidate-v1", capabilities, generation: { max_output_tokens: 4096, temperature: 0 } };
  const valid = (name: string, document: unknown) => assert.equal(ajv.validate(`${schemaBase}${name}.schema.json`, document), true, ajv.errorsText());
  const invalid = (name: string, document: unknown) => assert.equal(ajv.validate(`${schemaBase}${name}.schema.json`, document), false, `${name} accepted ${JSON.stringify(document)}`);
  valid("external-endpoint-binding", binding);
  valid("ready-endpoint-binding", { ...binding, connection });
  valid("external-endpoint-config", { schema_version: "1", ...connection, api: binding.api, wire_model: binding.wire_model, capabilities });
  const result = documentsWithTrial(trialFixture).find(([name]) => name === "eval-result.schema.json")![1] as Record<string, unknown>;
  valid("eval-result", { ...result, endpoint_binding: binding, usage: { source: "model-interactions-v1", completeness: "partial" }, task_completed: true });
  invalid("eval-result", { ...result, endpoint_binding: { ...binding, connection } });
  for (const document of [
    { ...binding, connection }, { ...binding, auth: connection.auth },
    { ...binding, endpoint_ref: "endpoint/Uppercase" }, { ...binding, endpoint_revision: "sha256:invalid" },
    { ...binding, capabilities: { ...capabilities, parallel_tool_calls: true } },
    { ...binding, generation: { max_output_tokens: 4096, stop: ["hidden budget override"] } },
  ]) invalid("external-endpoint-binding", document);
  invalid("ready-endpoint-binding", binding);
  invalid("ready-endpoint-binding", { ...binding, connection: { ...connection, auth: { kind: "env", name: "HITCH_TEST_KEY", value: "inline-secret" } } });
});

const trialFixture = {
  trial_id: "task-one__1", run_id: `run_${"a".repeat(32)}`, task_id: "task-one",
  attempt: 1, observation_status: "valid" as const, reward: 0, verifier_result_ref: "verifier/result.json",
};

function documentsWithTrial(trial: unknown): Array<[typeof scoreSchemas[number], unknown]> {
  const identity = { schema_version: "1", eval_id: `eval_${"e".repeat(32)}`, benchmark_id: "fixture", benchmark_revision: "revision-1" };
  const timestamp = "2026-09-05T00:00:00.000Z";
  return [
    ["eval-trial-reference.schema.json", trial],
    ["eval-result.schema.json", {
      ...identity, status: "succeeded", exit_code: 0, trials: [trial], started_at: timestamp, completed_at: timestamp,
    }],
    ["eval-progress.schema.json", {
      ...identity, status: "running", generation: 1, planned_tasks: 1, planned_trials: 1, trials: [trial],
      summary: { settled_trials: 1, valid_trials: 1, invalid_trials: 0 }, started_at: timestamp, updated_at: timestamp,
    }],
    ["eval-trial-publication.schema.json", {
      schema_version: "1", eval_id: identity.eval_id, mode: "settle", trial, created_at: timestamp,
    }],
  ];
}

async function schemaValidator(): Promise<Ajv2020> {
  const ajv = new Ajv2020({ strict: false, strictNumbers: true, validateFormats: false, allErrors: true });
  for (const name of [...scoreSchemas, "verifier-evidence.schema.json", "regrade-assessment-reference.schema.json", "external-endpoint-binding.schema.json", "external-endpoint-config.schema.json", "ready-endpoint-binding.schema.json"]) {
    const schema: unknown = JSON.parse(await readFile(new URL(`../../docs/schemas/${name}`, import.meta.url), "utf8"));
    ajv.addSchema(schema as object, `${schemaBase}${name}`);
  }
  return ajv;
}
