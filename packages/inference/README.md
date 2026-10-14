# Standalone inference

`@rsi-gear/inference` (npm) and `hitch-inference` (Python) run ordinary model inference without Gear, Hitch evaluation state, Slime, Megatron, Harbor, or Docker. The first engine adapter is explicit local-process SGLang **0.5.15.post1** on a single Linux CUDA host. Python 3.11+, psutil, the pinned SGLang runtime, and suitable model/hardware dependencies must be installed on that host. CPU fake-engine tests establish contracts, not GPU certification.

From this directory, install the Python runtime and npm CLI independently:

```sh
python3 -m pip install '.[sglang]'
npm install -g .
```

A packed npm install contains its own Python runtime source. Its CLI uses that installed asset; Python callers and remote Hitch nodes use the installed wheel. There are no sibling-checkout imports. For a remote model node, install the wheel with the **same interpreter** configured in Hitch. Extra packages or changed runtime source change its immutable runtime digest and require a new plan.

Create `node.json` using absolute directories:

```json
{"schemaVersion":2,"nodeId":"inference-host","nodeRoot":"/srv/inference/node","storeRoot":"/srv/inference/cas","host":"127.0.0.1","port":8080}
```

Set distinct private credentials, register an HF safetensors directory, and freeze an explicit topology:

```sh
export HITCH_INFERENCE_API_TOKEN='your-inference-token'
export HITCH_INFERENCE_MANAGEMENT_TOKEN='your-management-token'
hitch-inference plan --config /srv/inference/node.json --model /models/model \
  --replica GPU-a,GPU-b --replica GPU-c,GPU-d --profile throughput > deployment.json
hitch-inference serve --config /srv/inference/node.json
# In another terminal:
hitch-inference deploy --file deployment.json
hitch-inference models
```

Each `--replica` lists its ordered TP ranks. Two UUIDs per replica means TP=2; two groups mean two independent replicas. SGLang DP and PP remain 1 per engine. UUID groups must be disjoint. Baseline permits one active request per engine; throughput permits eight. Pool `--max-running` and `--max-queued` can reduce or bound admission. Model, runtime, generation defaults, topology and capacity form the immutable inference ID. Change any of them by creating a new deployment ID. The API model name returned by `/v1/models` is that inference ID.

No automatic hardware sizing/calibration is claimed. Select an explicit topology and run the applicable smoke below. The supported router is the built-in least-active relay with independent worker credentials. SGLang Router has no validated compatibility profile in this release.

The API supports authenticated `GET /v1/models` and the lock-selected `POST /v1/chat/completions` or `POST /v1/responses`, including SSE, tools and usage. The ordinary planner defaults to Chat Completions; Hitch planning defaults to Responses. Full-lock parity tests pass an explicit API when comparing those entrypoints. Requests inherit frozen generation defaults; conflicting sampling options are rejected. Output token limits may be reduced. Canonical validation keeps the user's output budget and `top_k=0` intact across the ordinary API, Hitch gateway and pool. Only the final direct SGLang hop maps `top_k` to `-1` and adds the two internally reserved Responses tokens: user budgets 1 and 32 become engine limits 3 and 34 once. V1/v2 direct-engine gateways apply that conversion themselves. Background Responses, stored/previous response state, automatic truncation and conflicting generation settings are rejected; complete history is required. Closing a request or aborting a client affects only that upstream request. API-server restarts retain durable deployments; no client owns the engine lifetime.

Management uses its distinct token: `POST /deployments`, `GET /deployments`, `GET /deployments/<id>`, `POST /deployments/<id>/drain` with `{"timeout_ms":30000}`, `POST /deployments/<id>/stop` with `{}` or `{"force":true}`, and `POST /deployments/<id>/recover` with `{}` after a verified OS boot change. Drain rejects new requests and lets admitted requests finish until its deadline; default stop drains for at most 30 seconds. Release is reported only after every replica and supervisor is confirmed dead and the physical devices are empty. A crashed supervisor leaves ownership reserved until explicit stop/recovery. Reboot recovery requires archived distinct OS boot proof and never signals old numeric PIDs.

Node identity and the GPU ledger live in `nodeRoot/identity.json`, `generations/`, `device-leases.json` and `device-leases.lock`. Reuse the **same nodeRoot** when sharing GPUs with Gear training. Service restarts reuse the current OS-boot generation. Gear can select the installed shared ledger authority with `deviceLeaseRuntime: "standalone-v1"`; the explicitly retained `gear-legacy-v2` adapter uses the same v2 path, schema and locks for older configs. No connection-loss expiry releases GPUs. `hitch_inference.device_lease` owns future ledger behavior. Gear's `gear-legacy-v2` implementation is frozen compatibility code; ledger changes require an explicit cross-package migration with shared schema/lock and restart-fencing tests.

Model preparation is shared by the deployment, outside node admission and HTTP management locks. One process-locked receipt under `inference/preparations/` binds the complete model/runtime/lock input, owner, service ID, node generation and validator. Replicas reuse it and independently admit their runtime, lock and GPU ranks. Replay checks the complete path set, symlinks, directory replacement and device/inode/size/mtime/ctime witnesses; modified content fails closed, including same-sized edits with restored mtime. A new service ID or boot generation requires full verification; receipts are not transferable. Stop remains fenced while preparation is in flight. Retain node state for recovery. The standalone snapshot API accepts HF serving data only; trainer checkpoints, Harbor datasets, tasks and verifiers remain owned by Gear/Hitch.

Hitch uses `hitch_inference.node` for new registrations (`connection.runtime: "standalone-v1"`). Existing registrations without an adapter retain `gear-legacy-v2`. V1/v2 single-GPU/Docker locks and evidence keep their historical identities. New TP/replica deployments use lock v3 and per-replica observations. Harbor task containers and verifier execution remain in Hitch/Gear.

```sh
HITCH_INFERENCE_TEST_PYTHON=/path/to/python3.12 npm test
HITCH_INFERENCE_TEST_PYTHON=/path/to/python3.12 npm run pack:smoke
# Operator-run hardware checks; these load the model onto the specified GPUs:
HITCH_INFERENCE_SMOKE_GPUS=GPU-a,GPU-b npm run smoke:gpu -- /models/model tp2
HITCH_INFERENCE_SMOKE_GPUS=GPU-a,GPU-b,GPU-c,GPU-d npm run smoke:gpu -- /models/model tp2-replicas2
```

The CPU suite launches real detached fake engine processes and real HTTP sockets. Shared request fixtures and complete-lock TS/Python parity fixtures cover both APIs, profile defaults, context boundaries and large/odd weight byte sizes. Cross-process prepare replay, tamper detection, partial-admission recovery and slow-prepare/stop races are regression gates. It covers TP argv/UUID allocation, concurrent replicas, queues, timeout, authentication, SSE/tools/usage, cancellation, drain, crash/recovery, shared legacy lease format and fenced startup. Hardware smoke emits immutable IDs, whitelisted observed replica settings, GPU seconds and confirmed release. Hardware support remains unverified until that smoke passes on the intended host.
