/** Ordinary inference client. Abort and client disposal never stop a deployment. */
export class InferenceClient {
  constructor({ baseUrl, apiToken, managementToken, fetch: request = fetch }) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.apiToken = apiToken;
    this.managementToken = managementToken;
    this.request = request;
  }
  async call(route, { management = false, body, signal } = {}) {
    const response = await this.request(this.baseUrl + route, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${management ? this.managementToken : this.apiToken}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), ...(signal ? { signal } : {}),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw Object.assign(new Error(error.error?.message ?? `Inference HTTP ${response.status}`), { status: response.status, code: error.error?.code });
    }
    return response;
  }
  async models(options) { return (await this.call("/v1/models", options)).json(); }
  async deployments(options) { return (await this.call("/deployments", { ...options, management: true })).json(); }
  async deploy(deployment, options) { return (await this.call("/deployments", { ...options, management: true, body: deployment })).json(); }
  async inspect(id, options) { return (await this.call(`/deployments/${encodeURIComponent(id)}`, { ...options, management: true })).json(); }
  async drain(id, { timeoutMs = 30000, ...options } = {}) { return (await this.call(`/deployments/${encodeURIComponent(id)}/drain`, { ...options, management: true, body: { timeout_ms: timeoutMs } })).json(); }
  async recover(id, options) { return (await this.call(`/deployments/${encodeURIComponent(id)}/recover`, { ...options, management: true, body: {} })).json(); }
  async stop(id, options) { return (await this.call(`/deployments/${encodeURIComponent(id)}/stop`, { ...options, management: true, body: {} })).json(); }
  async chatCompletions(body, options) {
    const response = await this.call("/v1/chat/completions", { ...options, body });
    return body.stream ? response : response.json();
  }
}
