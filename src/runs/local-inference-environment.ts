import { scrubLocalInferenceEnvironment } from "../model-access/index.js";
export function harnessChildEnvironment(input: {
  parent: NodeJS.ProcessEnv;
  adapter?: Record<string, string>;
  cwd: string;
  managedLocal: boolean;
  privateCredentials?: string[];
}): NodeJS.ProcessEnv {
  const parent = input.managedLocal ? scrubLocalInferenceEnvironment(input.parent) : { ...input.parent };
  for (const [name, value] of Object.entries(parent)) if (value && input.privateCredentials?.includes(value)) delete parent[name];
  const result: NodeJS.ProcessEnv = { ...parent, ...input.adapter, PWD: input.cwd };
  delete result.OLDPWD; return result;
}
