#!/usr/bin/env node
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { readFile } from "node:fs/promises";
import { InferenceClient } from "./index.mjs";

const args = process.argv.slice(2);
const action = args.shift();
function option(name, fallback) {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`${name} requires a value`);
  args.splice(index, 2);
  return value;
}
const python = option("--python", process.env.HITCH_INFERENCE_PYTHON ?? "python3");
if (["serve", "plan", "node"].includes(action)) {
  const modules = { serve: "hitch_inference.http_server", plan: "hitch_inference.plan", node: "hitch_inference.node" };
  // This is the installed npm package's own runtime asset, never a Gear source path.
  const bundled = fileURLToPath(new URL("../python/", import.meta.url));
  const child = spawn(python, ["-m", modules[action], ...args], { stdio: "inherit", env: {
    ...process.env, PYTHONPATH: [bundled, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  } });
  child.once("error", error => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
  child.once("exit", (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
  for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => child.kill(signal));
} else if (["deploy", "models", "status", "stop"].includes(action)) {
  const client = new InferenceClient({ baseUrl: option("--url", "http://127.0.0.1:8080"),
    apiToken: process.env.HITCH_INFERENCE_API_TOKEN, managementToken: process.env.HITCH_INFERENCE_MANAGEMENT_TOKEN });
  let result;
  if (action === "deploy") result = await client.deploy(JSON.parse(await readFile(option("--file"), "utf8")));
  if (action === "models") result = await client.models();
  if (action === "status") result = args[0] ? await client.inspect(args.shift()) : await client.deployments();
  if (action === "stop") result = await client.stop(args.shift());
  if (args.length) throw new Error("unknown arguments: " + args.join(" "));
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
} else {
  process.stdout.write("hitch-inference serve|plan|node [--python PATH] ...\nhitch-inference deploy --file PLAN.json [--url URL]\nhitch-inference models|status|stop ID [--url URL]\n");
  process.exitCode = action === "--help" || action === undefined ? 0 : 2;
}
