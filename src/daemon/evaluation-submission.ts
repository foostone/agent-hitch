import type { IncomingMessage, ServerResponse } from "node:http";
import { orderedEvalControl, type EvalScheduler, type EvalRerunScheduler } from "../control-plane/index.js";
import { invalidInput, SCHEMA_VERSION } from "../foundation/index.js";
import { json, readBodyJSON } from "./http.js";
import { PREPARATION_STREAM_TYPE, streamJSONResponse } from "./json-stream.js";

export async function evaluationSubmission(request: IncomingMessage, response: ServerResponse, input: {
  root: string; scheduler: EvalScheduler; rerunScheduler: EvalRerunScheduler; ordered: boolean;
}): Promise<void> {
  const body = await readBodyJSON(request);
  const header = request.headers["idempotency-key"];
  if (Array.isArray(header)) throw invalidInput("idempotency-key header must appear once");
  const submit = async () => {
    if (input.ordered) return orderedEvalControl(input.root, body, input.scheduler, input.rerunScheduler);
    const evalId = await input.scheduler.submit(body as Parameters<EvalScheduler["submit"]>[0], header ? { idempotencyKey: header } : {});
    return { schema_version: SCHEMA_VERSION, eval_id: evalId, status: "queued", links: {
      self: `/v1/evals/${evalId}`, events: `/v1/evals/${evalId}/events`, cancel: `/v1/evals/${evalId}/cancel`,
    } };
  };
  if (request.headers.accept === PREPARATION_STREAM_TYPE) {
    // Submission is durable work: losing the reply must not cancel or replay it.
    // Clients recover the accepted identity using their original control/key.
    await streamJSONResponse(request, response, submit);
  } else {
    json(response, input.ordered ? 200 : 202, await submit());
  }
}
