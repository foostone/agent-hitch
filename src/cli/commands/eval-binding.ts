import { randomBytes } from "node:crypto";
import { daemonClient } from "../../daemon/index.js";
import { HitchError, invalidInput, parseDuration } from "../../foundation/index.js";
import { assertNoArgs, takeFlag, takeOption, takeRepeatedOption } from "../arguments.js";

export async function evalBindingCommand(args: string[], root: string): Promise<void> {
  const model = takeOption(args, "--model");
  const passEnv = takeRepeatedOption(args, "--pass-env");
  const minimumValidity = takeOption(args, "--minimum-validity") ?? "30m";
  if (!takeFlag(args, "--json") || !model) throw invalidInput("eval bind requires --model and --json");
  assertNoArgs(args);
  const client = await daemonClient(root), nonce = randomBytes(16).toString("hex");
  const result = await client.requestWithMetadata("/v1/evaluation-bindings", {
    method: "POST", signal: AbortSignal.timeout(75_000), body: JSON.stringify({
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
