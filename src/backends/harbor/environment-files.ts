import { constants } from "node:fs";
import { lstat, open, readdir, realpath } from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { HitchError } from "../../foundation/index.js";

const FILE_NAMES = [
  "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
  "AWS_CA_BUNDLE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
  "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "BOTO_CONFIG", "NETRC",
] as const;
const DECLARATION = "HITCH_EVALUATION_FILE_ENV_JSON";
type FileIdentity = { source: string; path: string; kind: "missing" | "file" | "directory"; target?: string; digest?: string; mode?: number };

/** Private inputs only. The caller HMACs this manifest and never persists its paths or plain hashes. */
export async function evaluationEnvironmentFiles(env: NodeJS.ProcessEnv): Promise<FileIdentity[]> {
  try {
    const extra: unknown = env[DECLARATION] ? JSON.parse(env[DECLARATION]!) : [];
    if (!Array.isArray(extra) || extra.length > 128
      || extra.some(name => typeof name !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name))) throw unavailable();
    const references: Array<{ source: string; file: string; required: boolean; tree: boolean }> = [];
    const add = (source: string, file: string | undefined, required: boolean, tree = false) => {
      if (!file) return;
      if (!path.isAbsolute(file) || file.includes("\0")) throw unavailable();
      references.push({ source, file, required, tree });
    };
    for (const name of new Set<string>([...FILE_NAMES, ...extra as string[]])) add(name, env[name], true);
    for (const file of env.SSL_CERT_DIR ? env.SSL_CERT_DIR.split(path.delimiter) : []) {
      if (!file) throw unavailable();
      add("SSL_CERT_DIR", file, true, true);
    }
    if (env.DOCKER_CERT_PATH) {
      if (!path.isAbsolute(env.DOCKER_CERT_PATH)) throw unavailable();
      for (const name of ["ca.pem", "cert.pem", "key.pem"]) add("DOCKER_CERT_PATH/" + name, path.join(env.DOCKER_CERT_PATH, name), false);
    }
    const home = env.HOME;
    if (home && !path.isAbsolute(home)) throw unavailable();
    if (home) {
      for (const [variable, name] of [["NETRC", ".netrc"], ["AWS_CONFIG_FILE", ".aws/config"],
        ["AWS_SHARED_CREDENTIALS_FILE", ".aws/credentials"], ["GOOGLE_APPLICATION_CREDENTIALS", ".config/gcloud/application_default_credentials.json"]]) {
        if (!env[variable!]) add("default/" + name, path.join(home, name!), false);
      }
    }
    const dockerConfig = env.DOCKER_CONFIG ?? (home ? path.join(home, ".docker") : undefined);
    if (dockerConfig) {
      if (!path.isAbsolute(dockerConfig)) throw unavailable();
      add("DOCKER_CONFIG/config.json", path.join(dockerConfig, "config.json"), false);
    }
    const manifest: FileIdentity[] = [];
    const checks: Array<() => Promise<void>> = [];
    let entries = 0, bytes = 0;
    const signature = (s: Awaited<ReturnType<typeof lstat>>) => [s.dev, s.ino, s.size, s.mode, s.mtimeMs, s.ctimeMs].join(":");
    const visit = async (source: string, file: string, required: boolean, tree: boolean, ancestors: Set<string>) => {
      if (++entries > 4096 || ancestors.size > 32) throw unavailable();
      let resolved: string;
      try { resolved = await realpath(file); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT" || required) throw unavailable();
        manifest.push({ source, path: file, kind: "missing" });
        checks.push(async () => {
          try { await lstat(file); }
          catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return; throw unavailable(); }
          throw unavailable();
        });
        return;
      }
      const before = await lstat(resolved);
      checks.push(async () => {
        if (await realpath(file) !== resolved || signature(await lstat(resolved)) !== signature(before)) throw unavailable();
      });
      if (before.isDirectory() && tree) {
        if (ancestors.has(resolved)) throw unavailable();
        const names = (await readdir(resolved)).sort();
        manifest.push({ source, path: file, kind: "directory", target: resolved, mode: before.mode & 0o777 });
        checks.push(async () => { if (JSON.stringify((await readdir(resolved)).sort()) !== JSON.stringify(names)) throw unavailable(); });
        for (const name of names) await visit(source, path.join(file, name), true, true, new Set([...ancestors, resolved]));
        return;
      }
      if (!before.isFile() || before.size > 16 * 1024 * 1024 || bytes + before.size > 64 * 1024 * 1024) throw unavailable();
      const handle = await open(resolved, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        if (signature(await handle.stat()) !== signature(before)) throw unavailable();
        const hash = createHash("sha256"), buffer = Buffer.alloc(64 * 1024);
        let size = 0;
        for (;;) {
          const chunk = await handle.read(buffer, 0, buffer.length, null);
          if (!chunk.bytesRead) break;
          size += chunk.bytesRead; bytes += chunk.bytesRead;
          if (size > 16 * 1024 * 1024 || bytes > 64 * 1024 * 1024) throw unavailable();
          hash.update(buffer.subarray(0, chunk.bytesRead));
        }
        if (signature(await handle.stat()) !== signature(before)) throw unavailable();
        manifest.push({ source, path: file, kind: "file", target: resolved, digest: hash.digest("hex"), mode: before.mode & 0o777 });
      } finally { await handle.close(); }
    };
    const compare = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;
    for (const ref of references.sort((a, b) => compare(a.source, b.source) || compare(a.file, b.file))) {
      await visit(ref.source, ref.file, ref.required, ref.tree, new Set());
    }
    for (const check of checks) await check();
    return manifest;
  } catch { throw unavailable(); }
}

function unavailable() {
  return new HitchError("evaluation environment files are unavailable, changed or outside supported limits; use absolute regular-file references", {
    code: "evaluation_environment_files_unavailable", exitCode: 12,
  });
}
