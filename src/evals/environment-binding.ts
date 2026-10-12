import type { EvalRequest } from "../domain/index.js";
import { boundEvaluationEnvironment } from "../backends/index.js";

/** Admission checks validity without returning credentials to the control-plane or its journal. */
export async function assertEvalEnvironmentBinding(request: EvalRequest, root: string, env: NodeJS.ProcessEnv): Promise<void> {
  if (!request.environment_binding) return;
  await boundEvaluationEnvironment({ root, env, bindingId: request.environment_binding,
    model: request.model, passEnv: request.pass_env, minimumValidityMs: request.timeout_ms + request.setup_timeout_ms });
}
