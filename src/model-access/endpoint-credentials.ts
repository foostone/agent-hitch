import { chmod, lstat } from "node:fs/promises";
import path from "node:path";
import type { ReadyEndpointBindingV1 } from "../domain/index.js";
import { atomicWriteJSON, ensureDir, HitchError, readJSON, withFileLock } from "../foundation/index.js";

const credentials = new WeakMap<ReadyEndpointBindingV1, string>();
const directory = (root: string) => path.join(root, "private", "model-endpoints", "credentials");
const file = (root: string, binding: ReadyEndpointBindingV1) => path.join(directory(root), `${binding.endpoint_revision.slice(7)}.json`);
function failure(): never { throw new HitchError("immutable host endpoint credential is missing, unsafe or changed", { code: "endpoint_credential_conflict", exitCode: 12 }); }
async function readCredential(root: string, binding: ReadyEndpointBindingV1): Promise<string | undefined> {
  if (binding.connection.auth.kind === "none") return undefined;
  let metadata;
  try { metadata = await lstat(file(root, binding)); } catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.mode & 0o077 || process.getuid && metadata.uid !== process.getuid()) failure();
  const value = await readJSON<Record<string, unknown>>(file(root, binding));
  if (value.schema_version !== "1" || value.endpoint_revision !== binding.endpoint_revision || value.auth_name !== binding.connection.auth.name
    || typeof value.credential !== "string" || !value.credential || /[\r\n\0]/.test(value.credential)) failure();
  return value.credential;
}
/** Explicit host-only handoff, keyed by an immutable incarnation revision. */
export async function persistEndpointCredential(root: string, binding: ReadyEndpointBindingV1, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  if (binding.connection.auth.kind === "none") return;
  const name = binding.connection.auth.name, credential = env[name];
  if (!credential || /[\r\n\0]/.test(credential)) failure();
  await chmod(await ensureDir(directory(root)), 0o700);
  await withFileLock(path.join(directory(root), "locks"), binding.endpoint_revision.slice(7), async () => {
    const saved = await readCredential(root, binding);
    if (saved !== undefined && saved !== credential) failure();
    if (saved === undefined) { await atomicWriteJSON(file(root, binding), { schema_version: "1", endpoint_revision: binding.endpoint_revision, auth_name: name, credential }); await chmod(file(root, binding), 0o600); }
  });
  credentials.set(binding, credential);
}
export async function restoreEndpointCredential(root: string, binding: ReadyEndpointBindingV1): Promise<ReadyEndpointBindingV1> {
  const value = await readCredential(root, binding);
  if (value !== undefined) credentials.set(binding, value);
  return binding;
}
export const persistedEndpointCredential = (binding: ReadyEndpointBindingV1): string | undefined => credentials.get(binding);
