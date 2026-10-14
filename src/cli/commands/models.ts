import { invalidInput, readJSON } from "../../foundation/index.js";
import { addLocalModel, gcLocalModels, resolveLocalModel, verifyLocalModel } from "../../inference/index.js";
import { connectEndpoint, resolveEndpoint, publicEndpointBinding, verifyEndpoint, saveEndpointVerification } from "../../model-access/index.js";
import { assertNoArgs, takeFlag, takeOption } from "../arguments.js";

export async function modelsCommand(args: string[], root: string): Promise<void> {
  const action = args.shift();
  if (action === "connect") {
    const name = takeOption(args, "--name"), file = takeOption(args, "--file"); takeFlag(args, "--json"); assertNoArgs(args);
    if (!name || !file) throw invalidInput("models connect requires --name NAME --file ENDPOINT.json");
    process.stdout.write(`${JSON.stringify(await connectEndpoint(root, name, await readJSON(file)), null, 2)}\n`); return;
  }
  if (action === "add") {
    const directory = args.shift(), name = takeOption(args, "--name"), force = takeFlag(args, "--force"), json = takeFlag(args, "--json"); assertNoArgs(args);
    if (!directory || !name) throw invalidInput("models add requires a checkpoint directory and --name");
    const manifest = await addLocalModel({ root, directory, name, force });
    process.stdout.write(json ? `${JSON.stringify(manifest, null, 2)}\n` : `Added model artifact local/${name} (${manifest.model_id})\n`); return;
  }
  if (action === "inspect") {
    const reference = args.shift(), json = takeFlag(args, "--json"), verify = takeFlag(args, "--verify"); assertNoArgs(args);
    if (!reference) throw invalidInput("models inspect requires endpoint/<name> or local/<name>");
    if (reference.startsWith("endpoint/")) {
      const ready = await resolveEndpoint(root, reference);
      const verification = verify ? await verifyEndpoint(ready) : undefined;
      if (verification) await saveEndpointVerification(root, verification);
      const result = { ...publicEndpointBinding(ready), identity_verified: false, ...(verification ? { verification } : {}) };
      process.stdout.write(json ? `${JSON.stringify(result, null, 2)}\n` : `${reference} (${ready.endpoint_revision}) ${ready.api} ${ready.wire_model}\n`); return;
    }
    const manifest = await resolveLocalModel(root, reference);
    if (verify) await verifyLocalModel(root, manifest);
    process.stdout.write(json ? `${JSON.stringify({ ...manifest, verified: verify }, null, 2)}\n` : `${reference} (${manifest.model_id})\n`); return;
  }
  if (action === "gc") {
    const apply = takeFlag(args, "--apply"), dryRun = takeFlag(args, "--dry-run"); takeFlag(args, "--json"); assertNoArgs(args);
    if (apply && dryRun) throw invalidInput("models gc accepts only one of --apply and --dry-run");
    process.stdout.write(`${JSON.stringify(await gcLocalModels(root, apply), null, 2)}\n`); return;
  }
  throw invalidInput("models requires connect, add, inspect, or gc");
}
