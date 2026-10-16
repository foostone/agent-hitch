import type { Sha256 } from "./ids.js";

export type EndpointAuthV1 = { kind: "none" } | { kind: "env"; name: string };
export interface EndpointCapabilitiesV1 {
  streaming: boolean;
  tool_calls: boolean;
  parallel_tool_calls: boolean;
  input_modalities: ["text"];
}
export interface ExternalEndpointConfigV1 {
  schema_version: "1";
  base_url: string;
  wire_model: string;
  api: "responses" | "chat-completions";
  auth: EndpointAuthV1;
  capabilities: EndpointCapabilitiesV1;
}
export interface DeploymentReceiptV1 {
  schema_version: "1";
  model_digest: Sha256;
  wire_model: string;
  deployment_ref: string;
  source: string;
  service_config: Record<string, unknown>;
}
/** Public frozen task identity. No connection coordinates or secret values. */
export interface ExternalEndpointBindingV1 {
  schema_version: "1";
  kind: "external-endpoint";
  endpoint_ref: string;
  endpoint_revision: Sha256;
  api: ExternalEndpointConfigV1["api"];
  wire_model: string;
  capabilities: EndpointCapabilitiesV1;
  generation: Record<string, number>;
  deployment_receipt?: DeploymentReceiptV1;
}
/** Input/private persistence only. This is never transported in a public work spec. */
export interface ReadyEndpointBindingV1 extends ExternalEndpointBindingV1 {
  connection: { base_url: string; auth: EndpointAuthV1 };
}
export interface EndpointVerificationV1 {
  schema_version: "1";
  endpoint_revision: Sha256;
  verified_at: string;
  api: ExternalEndpointConfigV1["api"];
  streaming: boolean;
  tool_calls: boolean;
  streamed_tool_calls: boolean;
}
