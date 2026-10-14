import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import net from "node:net";
import type { InferenceLockV1, InferenceRuntimeManifestV1, InferenceServiceRecordV1, LocalModelManifestV1 } from "../src/domain/index.js";
import { hitchRootId, sha256JSON } from "../src/foundation/index.js";
import { ProcessSGLangLauncher, PythonInferenceNodeClient } from "../src/inference/index.js";
import type { SGLangLaunchedService, SGLangLaunchInput } from "../src/inference/index.js";
import { safetensorsFixture } from "../test-support/helpers.js";

test("real standalone Python TP pool startup/attachment feeds Hitch's replica evidence validator", { timeout: 120_000 }, async t => {
  const directory = await mkdtemp(path.join(tmpdir(), "hitch-independent-pool-"));
  let service: SGLangLaunchedService | undefined;
  t.after(async () => { await service?.stop(); await rm(directory, { recursive: true, force: true }); });
  const packageRoot = path.resolve("packages/inference");
  const configuredPython = process.env.HITCH_INFERENCE_TEST_PYTHON ?? "python3";
  const python = (await promisify(execFile)(configuredPython, ["-c", "import sys; print(sys.executable)"])).stdout.trim();
  const modelDirectory = path.join(directory, "model");
  const bin = path.join(directory, "bin");
  const gpu = path.join(directory, "gpu");
  await Promise.all([mkdir(modelDirectory), mkdir(bin), mkdir(gpu)]);
  await writeFile(path.join(modelDirectory, "config.json"), JSON.stringify({ architectures: ["Qwen3ForCausalLM"], model_type: "qwen3", torch_dtype: "float32", max_position_embeddings: 128 }));
  await writeFile(path.join(modelDirectory, "tokenizer.json"), "{}");
  await writeFile(path.join(modelDirectory, "tokenizer_config.json"), JSON.stringify({ chat_template: "{{ messages }}" }));
  await writeFile(path.join(modelDirectory, "model.safetensors"), safetensorsFixture());
  const nvidia = path.join(bin, "nvidia-smi");
  await writeFile(nvidia, `#!${python}\n${await readFile(path.join(packageRoot, "test/fixtures/nvidia-smi.py"), "utf8")}`);
  await chmod(nvidia, 0o755);
  const env = { ...process.env, PATH: [bin, process.env.PATH].join(path.delimiter),
    PYTHONPATH: [path.join(packageRoot, "python"), path.join(packageRoot, "test/fixtures")].join(path.delimiter),
    HITCH_INFERENCE_FAKE_GPU_STATE: gpu };
  const listener = net.createServer();
  await new Promise<void>(resolve => listener.listen(0, "127.0.0.1", resolve));
  const port = (listener.address() as net.AddressInfo).port;
  await new Promise<void>(resolve => listener.close(() => resolve()));
  const configPath = path.join(directory, "node.json");
  await writeFile(configPath, JSON.stringify({ schemaVersion: 2, nodeId: "independent-pool", nodeRoot: path.join(directory, "node"),
    storeRoot: path.join(directory, "cas"), inferencePort: port }));
  const planned = JSON.parse((await promisify(execFile)(python, ["-m", "hitch_inference.plan", "--config", configPath, "--model", modelDirectory,
    "--replica", "GPU-fixture-0,GPU-fixture-1", "--replica", "GPU-fixture-2,GPU-fixture-3"], { env, timeout: 30_000 })).stdout) as {
      serviceId: string; model: LocalModelManifestV1; runtime: InferenceRuntimeManifestV1; lock: InferenceLockV1;
    };
  planned.lock.execution.startup_timeout_ms = 60_000;
  const { inference_id: _old, ...identity } = planned.lock;
  planned.lock.inference_id = sha256JSON(identity);
  const binding = planned.lock.model_node!;
  const client = new PythonInferenceNodeClient({ runtime: "standalone-v1", transport: { type: "local" }, python: [python], configPath,
    gateway: { localPort: port, nodePort: port } }, { nodeId: binding.node_id, generation: binding.generation }, env);
  // The independent planner has already sealed these bytes in the node CAS.
  // The launcher still performs the real model/runtime admission RPC.
  client.prepare = async input => { await client.call("inference.prepare", { serviceId: input.serviceId, ownerId: hitchRootId(input.root),
    model: input.model, runtime: input.runtime, lock: input.lock }); };
  const input: SGLangLaunchInput = { root: path.join(directory, "hitch"), ...planned };
  const launcher = new ProcessSGLangLauncher(client);
  try {
    service = await launcher.start(input);
  } catch (error) {
    const diagnostic = await promisify(execFile)(python, [path.join(packageRoot, "test/diagnostics.py"), directory], { env, timeout: 10_000 });
    t.diagnostic(diagnostic.stdout);
    throw error;
  }
  assert.equal(service.observation?.schema_version, "3");
  if (service.observation?.schema_version !== "3") throw new Error("missing v3 observation");
  assert.equal(service.observation.replica_observations.length, 2);
  for (const replica of service.observation.replica_observations) {
    assert.equal(replica.configuration.tp_size, 2);
    assert.equal(replica.configuration.mem_fraction_static, 0.8);
  }
  const now = new Date().toISOString();
  const record: InferenceServiceRecordV1 = { schema_version: "1", service_id: input.serviceId, inference_id: input.lock.inference_id,
    isolation_key: sha256JSON("pool-attachment"), state: "ready", epoch: 1, owner_id: "controller", lease_owner_ids: ["controller"],
    backend: "cuda", model_node: binding, service_handle: service.service_handle!, started_at: now, updated_at: now };
  const before = await readFile(path.join(gpu, "requests.jsonl"), "utf8");
  const attached = await launcher.attach({ root: input.root, model: input.model, runtime: input.runtime, lock: input.lock, record, observation: service.observation });
  assert.deepEqual(attached.observation, service.observation);
  assert.equal(await readFile(path.join(gpu, "requests.jsonl"), "utf8"), before, "attachment must not run new inference probes");
  await attached.checkHealth?.();
  await service.stop();
  assert.equal((await client.inspect({ serviceId: input.serviceId, ownerId: hitchRootId(input.root), inferenceId: input.lock.inference_id }) as { resourcesReleased: boolean }).resourcesReleased, true);
});
