# Local and remote model inference

Hitch connects tasks to an already deployed model API. Multiple runs, evaluations and daemons can share the same endpoint. Hitch manages task concurrency, request deadlines, cancellation and evidence. Your deployment owns model loading, replicas, GPUs and service lifetime.

## Connect and verify an endpoint

Save a private connection file. `base_url` is the API root, including any path prefix; Hitch appends `responses` or `chat/completions` without inserting another `/v1`.

```json
{
  "schema_version": "1",
  "base_url": "http://127.0.0.1:30000/v1",
  "wire_model": "coder-v1",
  "api": "responses",
  "auth": { "kind": "env", "name": "CODER_API_KEY" },
  "capabilities": {
    "streaming": true, "tool_calls": true,
    "parallel_tool_calls": false, "input_modalities": ["text"]
  }
}
```

Use `{"kind":"none"}` for an explicitly unauthenticated service. Secret values belong in the environment of the process that calls upstream. They do not belong in this file. Start a daemon with that environment; exporting a key in another shell does not update an existing daemon.

```bash
hitch models connect --name coder --file ENDPOINT.json
hitch models inspect endpoint/coder --verify --json
hitch run --harness codex@installed --model endpoint/coder \
  --max-output-tokens 4096 --prompt "Summarize this project" --output json
```

Connect only saves configuration. Verification makes finite real text, SSE and full function-tool/result/answer requests for the declared capabilities. The first submission also verifies an unverified revision. HTTP model-name echo is not proof of mounted weights. Verification can incur ordinary model API usage, separately from task usage.

## Supported Harness combinations

| Harness | Protocol | Current scope |
| --- | --- | --- |
| Codex | Responses | Text, complete history, SSE, Harness function tools and usage; private provider config disables request/stream retries |
| model-call | Responses or Chat Completions | One text request; no tool execution; no Python dependency |
| training-tool | Chat Completions | Harbor-only linear shell-tool loop for ordinary candidate evaluation; no policy lease or exact training capture requirement |

Other combinations fail before launch. Declare `parallel_tool_calls=false`; parallel tool verification is outside this release. Server sessions, previous_response_id, background responses, built-in web tools and multimodal endpoint bindings are outside this matrix. Existing provider-native Harness usage remains available through its own configuration.

Normal budgets such as 1024 or 4096 pass unchanged to upstream. Hitch adds no token compensation or new minimum. Small upstream token reservation differences and tiny budgets are not release gates.

## Freeze submission and recovery

Each connection has a content-addressed revision. Reconnecting an alias affects new submissions. Submitted runs, queued evaluations, repairs and daemon recovery retain their original public `endpoint_binding`, protocol, wire model and generation parameters. Use `endpoint/coder@sha256:REVISION` for an explicitly pinned reference.

Connection URL/auth references live in the private registry. Public requests, plans, work specs and results contain the frozen public projection. Credentials are resolved by the forwarding controller, never packaged for the Harness. The existing task-scoped TypeScript proxy provides authentication, forwarding, cancellation and capture; it does not load, schedule or stop a model service.

For Gear, `--model-binding-file READY.json` accepts the public binding plus an input-only `connection` and optional `deployment_receipt`. The revision hashes normalized `schema_version/base_url/wire_model/api/auth/capabilities`; generation is frozen separately. Importing this file stores the immutable revision without repointing the alias. Public receipt `service_config` only accepts engine/version/digest, quantization, parsers, tokenizer/template digests, cache, isolation and parsing settings. Connection URLs, secret fields and candidate routing identities are rejected there. Gear must trust the receipt source and match its immutable HF snapshot digest before admitting strict candidate evidence.

## Reach the model from a Docker evaluation

After [Harbor setup](evaluations.md), use the same endpoint selector:

```bash
hitch eval run --daemon --dataset DATASET --harness codex@version:VERSION \
  --model endpoint/coder --max-output-tokens 4096 --max-concurrent 16 --output json
```

The current Harbor route uses the existing controller/worker forwarding channel. It resolves the upstream credential in that process and exposes only a task capability inside the container. The remote Worker declares `external_endpoint_binding=1`; exact training retains `training_external_binding=2`. A container's `127.0.0.1` refers to that container, so configure and verify controller-to-upstream and container-to-proxy reachability explicitly. No automatic protocol conversion, model fallback or engine deployment occurs.

## Account for daemon and inference capacity

`--max-concurrent` counts agents/trials. Upstream owns generation queues and replica load. External inference consumes no Hitch controller GPU quota. Task tools that need GPUs still use the task resource contract.

Completion, cancellation, timeout and daemon shutdown terminate only task requests and execution. They do not stop/drain/flush the shared model service or claim its GPU is released. A disconnected streaming consumer aborts its own upstream HTTP request. Existing explicit task attempts retain their evidence; forwarding adds no automatic retry.

Usage retains actual source fields and normalizes input/prompt and output/completion tokens for cumulative result summaries. Missing fields stay unknown. Shared inference GPU seconds stay unknown unless an external owner supplies reliable attributed metering. Explicit rerun summaries include retained invalid and replacement run publications and deduplicate interaction IDs.

## Deploy upstream services and retain history

Use the pinned [SGLang and official Model Gateway examples](../../../examples/external-inference/README.md) for a single replica, TP=2, or two TP=1 replicas. Configure explicit concurrency/queue limits and keep training/inference GPUs statically separate. Bounded 2026-10-15 records cover [real inference topologies](../../validation/external-inference-gpu-2026-10-15.md) and a separate [training/HF export plus Harbor endpoint diagnostic](../../validation/training-gpu-loop-2026-10-15.md). The latter completed one real update and baseline/candidate evidence pairing, both valid with reward 0 and meanDelta 0; it remains validated:false / pending-gpu, with zero loss/gradient and no learning or formal certification claim. Complete local artifact closure (108 objects, 35,467,670,234 bytes), offline finite verification and exact-instance rental destruction passed; Compose containers, a complete benchmark and remote Harbor workers remain unvalidated. CPU contract tests do not certify target GPUs.

`models add`, local manifest/CAS verification and historical inference schemas remain for model artifacts and old evidence. The ordinary Python inference package, `local` service commands, model-node RPC and models add-node have been removed. Historical tasks cannot silently recover against a new endpoint. Stop any old service with the old version before removing its deployment environment; create a new linked task to rerun with the endpoint path.
