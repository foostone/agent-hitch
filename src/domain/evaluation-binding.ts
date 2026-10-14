/** Environment preparation can inventory an entire interpreter and installed dependencies. */
export const DEFAULT_EVALUATION_BINDING_TIMEOUT_MS = 30 * 60 * 1_000;
// Larger Node timer delays wrap to one millisecond instead of extending the deadline.
export const MAX_EVALUATION_BINDING_TIMEOUT_MS = 2 ** 31 - 1;

export interface EvaluationBindingV1 {
  schema_version: "1";
  binding_id: string;
  model: string;
  pass_env: string[];
  expires_at_ms: number | null;
}
