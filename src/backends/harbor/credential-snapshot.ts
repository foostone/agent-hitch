import { spawn } from "node:child_process";
import { HitchError, terminateProcess } from "../../foundation/index.js";
import { helperCredentialNamesForRequest, parseHostCredentialHelperConfig } from "./host-credential-helper.js";

/** Resolve configured host credentials once. Only the private binding store receives values. */
export async function snapshotHostCredentials(env: NodeJS.ProcessEnv, passEnv: string[], minimumValidityMs: number, signal?: AbortSignal): Promise<{
  overrides: Record<string, string>; expiresAtMs: number | null;
}> {
  signal?.throwIfAborted();
  const config = parseHostCredentialHelperConfig(env);
  const names = helperCredentialNamesForRequest(config, passEnv);
  for (const name of passEnv) {
    if (env[name] === undefined && !names.includes(name)) throw failed("evaluation_binding_environment_missing");
  }
  if (!config) return { overrides: {}, expiresAtMs: null };
  return new Promise((resolve, reject) => {
    const child = spawn(config.argv[0]!, config.argv.slice(1), {
      env: { ...env }, stdio: ["pipe", "pipe", "ignore"], detached: process.platform !== "win32", windowsHide: true,
    });
    const chunks: Buffer[] = [];
    let bytes = 0, invalid = false, settled = false;
    const stop = () => { invalid = true; void terminateProcess(child).catch(() => {}); };
    const timer = setTimeout(stop, config.timeoutMs);
    const finish = (work: () => void) => {
      if (settled) return;
      settled = true; clearTimeout(timer); signal?.removeEventListener("abort", stop); work();
    };
    signal?.addEventListener("abort", stop, { once: true });
    if (signal?.aborted) stop();
    child.stdin.on("error", () => { invalid = true; });
    child.stdout.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > 64 * 1024) { stop(); return; }
      chunks.push(chunk);
    });
    child.once("error", () => finish(() => reject(failed())));
    child.once("close", code => finish(() => {
      try {
        if (invalid || code !== 0) throw failed();
        const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!value || typeof value !== "object" || Array.isArray(value)
          || Object.keys(value).sort().join(",") !== "env,expiresAtMs,version") throw failed();
        const record = value as Record<string, unknown>, values = record.env;
        if (record.version !== 1 || !Number.isSafeInteger(record.expiresAtMs)
          || Number(record.expiresAtMs) < Date.now() + minimumValidityMs
          || !values || typeof values !== "object" || Array.isArray(values)
          || Object.keys(values).sort().join(",") !== names.join(",")
          || Object.values(values).some(value => typeof value !== "string" || !value || value.includes("\0"))) throw failed();
        resolve({ overrides: values as Record<string, string>, expiresAtMs: Number(record.expiresAtMs) });
      } catch { reject(failed()); }
    }));
    child.stdin.end(JSON.stringify({ version: 1, credentialNames: names, minimumValidityMs }) + "\n");
  });
}

function failed(code = "evaluation_binding_credentials_unavailable") {
  return new HitchError("evaluation credentials could not be frozen with sufficient validity", { code, exitCode: 12 });
}
