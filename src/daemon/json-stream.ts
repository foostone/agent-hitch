import type { IncomingMessage, ServerResponse } from "node:http";
import { HitchError } from "../foundation/index.js";
import { errorStatus } from "./http.js";
import { boundedMessage } from "./logging.js";

const HEARTBEAT_INTERVAL_MS = 15_000;
// The request body is bounded at 1 MiB; leave room for the response envelope.
const MAX_EVENT_CHARACTERS = 2 * 1024 * 1024;
export const PREPARATION_STREAM_TYPE = "application/vnd.hitch.preparation+ndjson";

/** Opt-in streaming keeps long preparation within the caller's overall deadline. */
export async function streamJSONResponse(request: IncomingMessage, response: ServerResponse,
  prepare: (signal: AbortSignal) => Promise<unknown>): Promise<void> {
  const controller = new AbortController();
  const abort = () => { if (!response.writableEnded) controller.abort(); };
  request.once("aborted", abort);
  response.once("close", abort);
  response.writeHead(200, { "content-type": PREPARATION_STREAM_TYPE, "cache-control": "no-store" });
  const emit = (event: unknown) => {
    if (!response.destroyed) response.write(JSON.stringify(event) + "\n");
  };
  emit({ type: "progress" });
  const heartbeat = setInterval(() => emit({ type: "progress" }), HEARTBEAT_INTERVAL_MS);
  heartbeat.unref();
  try {
    emit({ type: "result", value: await prepare(controller.signal) });
  } catch (error) {
    emit({ type: "error", error: {
      code: error instanceof HitchError ? error.code : "daemon_request_failed",
      message: error instanceof HitchError ? boundedMessage(error.message) : "daemon preparation failed",
      exit_code: error instanceof HitchError ? error.exitCode : 12,
      status: error instanceof HitchError ? errorStatus(error) : 500,
    } });
    // Preserve the server's bounded/redacted request_error diagnostics. The
    // stream is already complete, so the HTTP handler must not send JSON again.
    throw error;
  } finally {
    clearInterval(heartbeat);
    request.removeListener("aborted", abort);
    response.removeListener("close", abort);
    response.end();
  }
}

export async function readStreamingJSONResponse(response: Response): Promise<Record<string, unknown>> {
  if (!response.body) throw invalidStream();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: Record<string, unknown> | undefined;
  const consume = (line: string) => {
    if (!line.trim()) return;
    if (line.length > MAX_EVENT_CHARACTERS || result !== undefined) throw invalidStream();
    let event: Record<string, unknown>;
    try { event = JSON.parse(line); } catch { throw invalidStream(); }
    if (!event || typeof event !== "object" || Array.isArray(event)) throw invalidStream();
    if (event.type === "error") {
      const error = event.error as { message?: unknown; code?: unknown; exit_code?: unknown; status?: unknown } | undefined;
      if (!error || typeof error.message !== "string" || typeof error.code !== "string"
        || !Number.isInteger(error.exit_code)) throw invalidStream();
      const failure = new HitchError(error.message, { code: error.code, exitCode: error.exit_code as number });
      if (Number.isInteger(error.status)) (failure as { status?: number }).status = error.status as number;
      throw failure;
    }
    if (event.type === "progress") return;
    if (event.type !== "result" || !event.value || typeof event.value !== "object" || Array.isArray(event.value)) throw invalidStream();
    result = event.value as Record<string, unknown>;
  };
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n")) >= 0) {
      consume(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 1);
    }
    if (buffer.length > MAX_EVENT_CHARACTERS) throw invalidStream();
  }
  consume(buffer + decoder.decode());
  if (result === undefined) throw invalidStream();
  return result;
}

function invalidStream() {
  return new HitchError("daemon preparation stream ended without a valid result", { code: "daemon_request_failed", exitCode: 12 });
}
