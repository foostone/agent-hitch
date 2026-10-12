import path from "node:path";
import { readFile } from "node:fs/promises";
import { HitchError, packageRoot, runCommand } from "../../foundation/index.js";
import { locateHarbor } from "./tools.js";

export interface HarborPythonRuntimeV1 {
  schema_version: "1";
  integrity: string;
  python_version: string;
  file_count: number;
}

/** Inspect the actual launcher interpreter, preserving its virtualenv symlink. */
export async function observeHarborPythonRuntime(input: {
  root: string; env: NodeJS.ProcessEnv; harborExecutable?: string; signal?: AbortSignal;
}): Promise<HarborPythonRuntimeV1> {
  try {
    if (input.env.PYTHONPATH
      && input.env.PYTHONPATH.split(path.delimiter).some(value => !path.isAbsolute(value))) throw unavailable();
    const harbor = await locateHarbor({ root: input.root, env: input.env, explicit: input.harborExecutable });
    if (!harbor.executable) throw unavailable();
    const firstLine = (await readFile(harbor.executable, "utf8")).split(/\r?\n/, 1)[0] ?? "";
    const python = firstLine.match(/^#!(\/[^\s]+)$/)?.[1];
    if (!python || !/^python(?:[0-9.]+)?$/.test(path.basename(python))) throw unavailable();
    const probe = path.join(packageRoot(), "integrations", "harbor", "hitch_python_runtime.py");
    const result = await runCommand(python, ["-B", probe, harbor.executable], {
      env: { ...input.env, PYTHONDONTWRITEBYTECODE: "1" }, timeoutMs: 45_000,
      ...(input.signal ? { signal: input.signal } : {}),
    });
    const value: unknown = JSON.parse(result.stdout);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw unavailable();
    const object = value as Record<string, unknown>;
    if (Object.keys(object).sort().join(",") !== "file_count,integrity,python_version,schema_version"
      || object.schema_version !== "1" || typeof object.integrity !== "string" || !/^sha256:[a-f0-9]{64}$/.test(object.integrity)
      || typeof object.python_version !== "string" || !/^3\.\d+\.\d+$/.test(object.python_version)
      || !Number.isSafeInteger(object.file_count) || (object.file_count as number) < 1) throw unavailable();
    return value as HarborPythonRuntimeV1;
  } catch { throw unavailable(); }
}

function unavailable() {
  return new HitchError("Harbor Python runtime cannot be verified; use a Python launcher with absolute module paths and unchanged package files", {
    code: "harbor_python_runtime_unavailable", exitCode: 12,
  });
}
