import { randomBytes } from "node:crypto";
import { daemonClient, PREPARATION_STREAM_TYPE } from "../../daemon/index.js";
import { DEFAULT_EVALUATION_BINDING_TIMEOUT_MS, MAX_EVALUATION_BINDING_TIMEOUT_MS } from "../../domain/index.js";
import { HitchError, invalidInput, parseDuration } from "../../foundation/index.js";
import { assertNoArgs, takeFlag, takeOption, takeRepeatedOption } from "../arguments.js";

export async function evalBindingCommand(args: string[], root: string): Promise<void> {
  const model = takeOption(args, "--model");
  const passEnv = takeRepeatedOption(args, "--pass-env");
  const minimumValidity = takeOption(args, "--minimum-validity") ?? "30m";
  const timeout = takeOption(args, "--timeout");
  const timeoutMs = timeout === undefined ? DEFAULT_EVALUATION_BINDING_TIMEOUT_MS : parseDuration(timeout);
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_EVALUATION_BINDING_TIMEOUT_MS) {
    throw invalidInput("eval bind --timeout must be a positive duration supported by Node timers");
  }
  if (!takeFlag(args, "--json") || !model) throw invalidInput("eval bind requires --model and --json");
  assertNoArgs(args);
  const client = await daemonClient(root), nonce = randomBytes(16).toString("hex");
  const result = await client.requestWithMetadata("/v1/evaluation-bindings", {
    method: "POST", headers: { accept: PREPARATION_STREAM_TYPE }, signal: AbortSignal.timeout(timeoutMs), body: JSON.stringify({
      schema_version: "1", provider: "local-docker", model, pass_env: passEnv, minimum_validity_ms: parseDuration(minimumValidity), nonce,
    }),
  });
  const value = result.payload;
  if (typeof value !== "object" || value === null || value.schema_version !== "1"
    || value.nonce !== nonce || value.daemon_instance_id !== client.state.instance_id
    || value.provider !== "local-docker" || result.headers.get("cache-control") !== "no-store") {
    throw new HitchError("evaluation binding does not match the requested daemon/provider/nonce", { code: "evaluation_binding_mismatch", exitCode: 12 });
  }
  process.stdout.write(JSON.stringify(value) + "\n");
}
