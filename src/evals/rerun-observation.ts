import { readdir } from "node:fs/promises";
import path from "node:path";
import { readJSON } from "../foundation/index.js";
import type { AdmittedEndpointAttemptV1 } from "./endpoint-attempts.js";

/** Admission intents without a terminal receipt remain pending, including crash gaps. */
export async function pendingEvalReruns(directory: string, attempts: AdmittedEndpointAttemptV1[]): Promise<boolean> {
  const ids = new Set(attempts.flatMap(attempt => attempt.rerun_id ? [attempt.rerun_id] : []));
  const base = path.join(directory, "reruns");
  const entries = await readdir(base, { withFileTypes: true }).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return []; throw error;
  });
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^rerun_[a-f0-9]{32}$/.test(entry.name)) continue;
    if (await readJSON(path.join(base, entry.name, "submission.json"), null)) ids.add(entry.name);
  }
  for (const id of ids) {
    const state = await readJSON<{ status?: string; error?: { code?: string } } | null>(path.join(base, id, "state.json"), null);
    if (!state) {
      if (!await readJSON(path.join(base, id, "submission.json"), null) && await readJSON(path.join(base, id, "cancellation.json"), null)) continue;
      return true;
    }
    if (!["completed", "failed", "cancelled"].includes(String(state.status)) || state.error?.code === "execution_state_ambiguous") return true;
  }
  return false;
}
