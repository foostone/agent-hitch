import type { Sha256 } from "./ids.js";

/** Mutable training policies never claim an immutable managed-local model ID. */
export interface TrainingExternalBindingV1 {
  kind: "training-external";
  bindingId: string;
  trainingRunId: string;
  policyLeaseRef: { uri: string; digest: Sha256; mediaType: "application/json" };
  expectedPolicyVersion: string;
  fencingToken: string;
  expiresAt: string;
  endpointRef: string;
  credentialRef: string;
  generationContractDigest: Sha256;
  requiredCapture: "exact-policy-tokens-v1";
  api: "chat-completions";
  maxOutputTokens: number;
  maxEpisodeSteps: number;
}

/** Public projection: it contains no private gateway address or credential. */
export interface TrainingProxyIdentityV1 {
  binding_id: string;
  training_run_id: string;
  policy_version: string;
  generation_contract_digest: Sha256;
  api: "chat-completions";
  max_output_tokens: number;
  max_episode_steps: number;
}

/** Public worker model contract. Connection coordinates and credentials stay on the controller. */
export type RemoteModelBindingV2 = {
  schema_version: "2";
  kind: "training-external";
  training: TrainingExternalBindingV1;
} | {
  schema_version: "2";
  kind: "external-endpoint";
  endpoint: import("./endpoints.js").ExternalEndpointBindingV1;
};
