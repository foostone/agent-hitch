import { aggregateModelUsage } from "../model-access/index.js";
import { readEvalTrialPublications } from "./trial-publication-recovery.js";
export async function cumulativeEndpointUsage(root: string, evalId: string, currentRunIds: readonly string[]): Promise<Record<string, unknown>> {
  const previous = await readEvalTrialPublications(root, evalId);
  const all = [...currentRunIds, ...previous.flatMap(item => item.trial.run_id ? [item.trial.run_id] : [])];
  return aggregateModelUsage(root, all);
}
