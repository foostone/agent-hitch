import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
const directory = fileURLToPath(new URL("../", import.meta.url));
const temp = await mkdtemp(path.join(tmpdir(), "inference-packed-"));
let server;
try {
  const pack = spawnSync("npm", ["pack", "--json", "--pack-destination", temp], { cwd: directory, encoding: "utf8" });
  assert.equal(pack.status, 0, pack.stderr);
  const archive = path.join(temp, JSON.parse(pack.stdout)[0].filename);
  const app = path.join(temp, "app"); await mkdir(app);
  await writeFile(path.join(app, "package.json"), '{"private":true,"type":"module"}\n');
  const installed = spawnSync("npm", ["install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund", archive], { cwd: app, encoding: "utf8" });
  assert.equal(installed.status, 0, installed.stderr);
  const config = path.join(temp, "node.json");
  await writeFile(config, JSON.stringify({ schemaVersion: 2, nodeId: "packed-node", nodeRoot: path.join(temp, "node"), storeRoot: path.join(temp, "cas"), port: 0 }));
  const env = { ...process.env, HITCH_INFERENCE_API_TOKEN: "packed-api", HITCH_INFERENCE_MANAGEMENT_TOKEN: "packed-management" };
  delete env.PYTHONPATH; delete env.PYTHONHOME;
  const python = env.HITCH_INFERENCE_TEST_PYTHON ?? "python3";
  const cli = path.join(app, "node_modules/@rsi-gear/inference/src/cli.mjs");
  server = spawn(process.execPath, [cli, "serve", "--python", python, "--config", config], { cwd: app, env, stdio: ["ignore", "pipe", "pipe"] });
  let output = "", errors = "";
  server.stderr.on("data", chunk => { errors += String(chunk); });
  const announcement = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("packed startup timeout: " + errors)), 15000);
    server.once("error", error => { clearTimeout(timer); reject(error); });
    server.once("exit", code => { clearTimeout(timer); reject(new Error(`packed server exited ${code}: ${errors}`)); });
    server.stdout.on("data", chunk => { output += String(chunk); if (output.includes("\n")) { clearTimeout(timer); resolve(JSON.parse(output.split("\n")[0])); } });
  });
  const endpoint = `http://127.0.0.1:${announcement.port}`;
  const models = await fetch(endpoint + "/v1/models", { headers: { Authorization: "Bearer packed-api" } });
  assert.equal(models.status, 200); assert.deepEqual((await models.json()).data, []);
  assert.equal((await fetch(endpoint + "/deployments", { headers: { Authorization: "Bearer packed-api" } })).status, 401);
  assert.equal((await fetch(endpoint + "/deployments", { headers: { Authorization: "Bearer packed-management" } })).status, 200);
  const pkg = JSON.parse(await readFile(path.join(app, "node_modules/@rsi-gear/inference/package.json"), "utf8"));
  assert.equal(pkg.name, "@rsi-gear/inference");
  process.stdout.write(JSON.stringify({ packed_install: "passed", standalone_http: "passed", gear_required: false, training_required: false }) + "\n");
} finally {
  if (server && server.exitCode === null) { server.kill("SIGTERM"); await new Promise(resolve => server.once("exit", resolve)); }
  await rm(temp, { recursive: true, force: true });
}
