import path from "node:path";
import type { ModelInteractionV1 } from "../domain/index.js";
import { readJSON } from "../foundation/index.js";
import { loadInteractionCapture } from "./records.js";

/** A field is unknown if any included request lacks it. Never invent missing token usage. */
export async function aggregateModelUsage(root: string, runIds: readonly string[]): Promise<Record<string, unknown>> {
  const rows = new Map<string, ModelInteractionV1>();
  let missing = false;
  for (const runId of new Set(runIds)) {
    if (!/^run_[a-f0-9]{32}$/.test(runId)) throw new TypeError("usage aggregation requires canonical run IDs");
    const directory = path.join(root, "runs", runId);
    if (!await readJSON(path.join(directory, "interactions", "interaction.ref.json"), null)) { missing = true; continue; }
    const capture = await loadInteractionCapture(directory);
    if (capture.ref.completeness === "partial" || capture.ref.redaction.status === "failed") missing = true;
    for (const row of capture.interactions) rows.set(row.interaction_id, row);
  }
  const result: Record<string, unknown> = { ...(missing ? {} : { requests: rows.size }), source: "model-interactions-v1", completeness: missing ? "partial" : "complete" };
  for (const [field, aliases] of [["input_tokens", ["input_tokens", "prompt_tokens"]], ["output_tokens", ["output_tokens", "completion_tokens"]], ["total_tokens", ["total_tokens"]]] as const) {
    const values = [...rows.values()].map(row => aliases.map(key => row.usage?.[key]).find(value => value !== undefined));
    if (!missing && values.length && values.every((value): value is number => value !== undefined)) result[field] = values.reduce((sum, value) => sum + value, 0);
  }
  return result;
}
