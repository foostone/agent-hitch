export interface InferenceClientOptions {
  baseUrl: string;
  apiToken: string;
  managementToken?: string;
  fetch?: typeof fetch;
}
export interface RequestOptions { signal?: AbortSignal }
export class InferenceClient {
  constructor(options: InferenceClientOptions);
  models(options?: RequestOptions): Promise<unknown>;
  deployments(options?: RequestOptions): Promise<unknown>;
  deploy(deployment: unknown, options?: RequestOptions): Promise<unknown>;
  inspect(id: string, options?: RequestOptions): Promise<unknown>;
  drain(id: string, options?: RequestOptions & { timeoutMs?: number }): Promise<unknown>;
  recover(id: string, options?: RequestOptions): Promise<unknown>;
  stop(id: string, options?: RequestOptions): Promise<unknown>;
  chatCompletions(body: { model: string; messages: unknown[]; stream?: boolean; [key: string]: unknown }, options?: RequestOptions): Promise<unknown | Response>;
}
