export interface EvaluationBindingV1 {
  schema_version: "1";
  binding_id: string;
  model: string;
  pass_env: string[];
  expires_at_ms: number | null;
}
