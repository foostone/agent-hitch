import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import { InferenceClient } from "../src/index.mjs";
const args = process.argv.slice(2), model = args[0], mode = args[1] ?? "tp2";
const devices = (process.env.HITCH_INFERENCE_SMOKE_GPUS ?? "").split(",").filter(Boolean);
const needed = mode === "tp2-replicas2" ? 4 : 2;
if (!model || devices.length !== needed) throw new Error(`Provide a local HF model path and ${needed} physical GPU UUIDs in HITCH_INFERENCE_SMOKE_GPUS`);
const directory = fileURLToPath(new URL("../", import.meta.url));
const temp = await mkdtemp(path.join(tmpdir(), "inference-hardware-"));
const cli = path.join(directory, "src/cli.mjs");
const python = process.env.HITCH_INFERENCE_PYTHON ?? "python3";
const apiToken = randomBytes(32).toString("hex"), managementToken = randomBytes(32).toString("hex");
const env = { ...process.env, HITCH_INFERENCE_API_TOKEN: apiToken, HITCH_INFERENCE_MANAGEMENT_TOKEN: managementToken };
let server, client, deployment, released = false;
try {
  const config = path.join(temp, "node.json");
  await writeFile(config, JSON.stringify({ schemaVersion: 2, nodeId: "hardware-smoke", nodeRoot: path.join(temp, "node"), storeRoot: path.join(temp, "cas"), port: 0 }));
  const replicaArgs = devices.flatMap((device, index) => index % 2 ? [] : ["--replica", devices.slice(index, index + 2).join(",")]);
  const planned = spawnSync(process.execPath, [cli, "plan", "--python", python, "--config", config, "--model", model, ...replicaArgs], { cwd: temp, env, encoding: "utf8" });
  if (planned.status !== 0) throw new Error(planned.stderr);
  deployment = JSON.parse(planned.stdout);
  server = spawn(process.execPath, [cli, "serve", "--python", python, "--config", config], { cwd: temp, env, stdio: ["ignore", "pipe", "inherit"] });
  const announcement = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(new Error("service startup timed out")), 15000);
    server.once("error", error => { clearTimeout(timer); reject(error); });
    server.once("exit", code => { clearTimeout(timer); reject(new Error(`service exited before announcement: ${code}`)); });
    server.stdout.on("data", chunk => { text += String(chunk); if (text.includes("\n")) { clearTimeout(timer); resolve(JSON.parse(text.split("\n")[0])); } });
  });
  client = new InferenceClient({ baseUrl: `http://127.0.0.1:${announcement.port}`, apiToken, managementToken });
  await client.deploy(deployment);
  let status;
  const deadline = Date.now() + 600000;
  do {
    status = await client.inspect(deployment.serviceId);
    if (status.state === "failed" || Date.now() > deadline) throw new Error("hardware startup failed: " + JSON.stringify(status));
    if (status.state !== "ready") await new Promise(resolve => setTimeout(resolve, 500));
  } while (status.state !== "ready");
  if (JSON.stringify(status.gpuUuids) !== JSON.stringify(devices) || status.replicaObservations.some(value => value.configuration.tp_size !== 2)) throw new Error("observed GPU topology differs");
  const replies = await Promise.all(Array.from({ length: needed / 2 }, () => client.chatCompletions({ model: deployment.lock.inference_id, messages: [{ role: "user", content: "Reply with one word: ready" }], max_tokens: 8 })));
  if (replies.some(value => !value.usage || !value.choices?.length)) throw new Error("OpenAI result/usage unavailable");
  const stopped = await client.stop(deployment.serviceId);
  released = stopped.resourcesReleased === true;
  if (!released) throw new Error("GPU/process release is unconfirmed");
  process.stdout.write(JSON.stringify({ status: "passed", mode, inference_id: deployment.lock.inference_id, observations: status.replicaObservations, gpu_seconds: stopped.gpuSeconds }) + "\n");
} finally {
  if (client && deployment) {
    const stopped = await client.stop(deployment.serviceId).catch(() => null);
    released ||= stopped?.resourcesReleased === true;
  }
  if (server && server.exitCode === null) { server.kill("SIGTERM"); await new Promise(resolve => server.once("exit", resolve)); }
  if (released || !deployment) await rm(temp, { recursive: true, force: true });
  else process.stderr.write(JSON.stringify({ retained_state: temp, recovery: `Set new distinct API/management tokens and run hitch-inference serve --config ${path.join(temp, "node.json")}; inspect/stop ${deployment.serviceId} before removing this directory.` }) + "\n");
}
