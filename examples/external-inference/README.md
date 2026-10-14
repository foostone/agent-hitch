# External SGLang endpoints

These examples deploy an upstream model API independently of Hitch runs and Gear evaluations. Docker Compose owns service restart and device access; SGLang owns generation; its official Model Gateway owns replica routing. `accept.mjs` is a one-shot HTTP acceptance tool, not a service or model manager.

The configured combination is **SGLang v0.5.21 + its bundled official Model Gateway/sglang-router 0.3.2**, from release commit `e00930c5489053f26d86b179cee0d087f846acbb`. Both services use `lmsysorg/sglang:v0.5.21`; there is no separately floating Gateway installation. The example model is **Qwen/Qwen2.5-7B-Instruct**, HF revision `a09a35458c702b33eeacc393d103063234e8bc28`, with the `qwen25` tool parser. Pin the pulled image digest in `.env` before recording a reproducible deployment. These are configured versions, not a claim that the combination has passed GPU acceptance here.

| Validation | Current status |
| --- | --- |
| Compose parsing: single TP=1, single TP=2, two TP=1 replicas | Checked locally; no containers started |
| Probe syntax and controlled standard HTTP protocol exercise | Checked locally; software checks only |
| Real SGLang TP=1 + selected Harness with tools | **NOT RUN: GPU host not supplied** |
| Real SGLang TP=2 and actual two-card execution | **NOT RUN: GPU host not supplied** |
| Official Gateway + two TP=1 replicas, distribution and cancel isolation | **NOT RUN: GPU host not supplied** |
| Static training/inference split, training rollout and HF export regression | **NOT RUN: GPU host not supplied** |

Historical custom inference-pool records cannot substitute for these checks. A successful HTTP probe does not establish GPU topology, trusted weight identity, or actual Harness behavior.

## Host and device requirements

Use a Linux NVIDIA GPU host with Docker Engine, NVIDIA Container Toolkit, a driver compatible with this CUDA 13 image, and Docker Compose **2.24.4 or later** (`!override` is used by the TP=2 override). The HTTP probe needs Node.js **22 or later**, with no npm packages or Python runtime on the Hitch client.

For this unquantized 7B BF16 example, plan for an Ampere-or-newer NVIDIA GPU with at least 24 GiB for each TP=1 replica. This is a conservative starting target, not measured capacity: model, kernels, context, batching and KV-cache allocation must fit the chosen hardware. TP=2 needs two suitable GPUs on one host with working NCCL/P2P communication. Validate the interconnect and actual rank execution; two device IDs in a file are not proof of TP execution. The default context is 8192, per-worker running/queued request limits are 4/8, and the Gateway running/queued limits are 8/16 with a 30-second queue deadline. Adjust these to the measured deployment capacity and record the change.

Reserve distinct physical GPU UUIDs for inference and training in the resource owner's allocation system. For example, on a three-GPU host reserve GPU A/B for the two-replica endpoint and GPU C for training. Constrain the trainer to C and the Compose workers to A/B. One inference GPU plus one training GPU needs at least two GPUs; TP=2 or two replicas plus a one-GPU trainer needs at least three. These Compose device selections restrict visibility; they do not exclude other jobs or create a global allocation ledger. Dynamic sharing is outside this example.

## Prepare the immutable snapshot and secret

Run these steps as the deployment owner in this directory, on the GPU host. Edit all paths and UUIDs for that host:

```sh
cp .env.example .env
# Edit .env: absolute snapshot/secret paths, reserved GPU UUIDs, image digest.
set -a
. ./.env
set +a

install -d -m 700 ./secrets
(umask 077; openssl rand -hex 32 > ./secrets/api-key)
# Set INFERENCE_API_KEY_FILE in .env to this file's absolute path.
```

The same secret protects Gateway and workers in the two-replica example. Compose mounts the file as `/run/secrets/api_key`; a small shell entrypoint reads it and execs the upstream launcher. The secret is absent from endpoint JSON and `.env`. Upstream `--api-key` arguments are visible to privileged process inspectors on the deployment host; use a trusted host and keep its diagnostics private. Never put a real key in a public task, receipt, or report.

Use the existing CAS/HF export flow for a candidate. It must produce a complete, immutable snapshot containing weights, config, tokenizer and template. For the public demonstration model, the fixed image can prepare a snapshot without GPU access:

```sh
# Reload .env after editing its paths.
set -a
. ./.env
set +a
mkdir -p "$HF_SNAPSHOT_DIR"
docker pull "$SGLANG_IMAGE"
docker run --rm --entrypoint hf \
  --mount "type=bind,src=$HF_SNAPSHOT_DIR,dst=/model" \
  "$SGLANG_IMAGE" download Qwen/Qwen2.5-7B-Instruct \
  --revision a09a35458c702b33eeacc393d103063234e8bc28 --local-dir /model

docker image inspect --format '{{index .RepoDigests 0}}' "$SGLANG_IMAGE"
# Replace SGLANG_IMAGE in .env with that returned lmsysorg/sglang@sha256:... value.
```

Download only into a new snapshot directory. Do not point at a mutable `main` checkout or a cache snapshot whose symlinks target files outside the mount. The deployment owner must verify the complete files against the existing model manifest/CAS digest, prevent host-side mutation, and preserve the digest and receipt source. Every worker mounts exactly that directory read-only and runs with Hub/Transformers offline mode. The container mount alone does not prove host immutability or which weights were loaded.

Use a distinct `WIRE_MODEL` for each candidate version, and use the same value in endpoint JSON and the trusted deployment receipt. Do not replace the mounted weights behind an already-frozen model name. All replicas behind one Gateway in this example must serve the same snapshot and parser/template configuration.

## Launch one topology

Reload `.env` after every edit, including the resolved image digest; exported shell values take precedence over `--env-file`:

```sh
set -a
. ./.env
set +a
```

For one TP=1 replica:

```sh
docker compose --env-file .env -f compose.single.yaml config --quiet
docker compose --env-file .env -f compose.single.yaml up -d --wait --wait-timeout 1800
```

For one TP=2 replica, select two distinct reserved UUIDs in `.env`:

```sh
docker compose --env-file .env -f compose.single.yaml -f compose.tp2.yaml config --quiet
docker compose --env-file .env -f compose.single.yaml -f compose.tp2.yaml up -d --wait --wait-timeout 1800
```

For two TP=1 replicas behind the official Gateway:

```sh
docker compose --env-file .env -f compose.gateway.yaml config --quiet
docker compose --env-file .env -f compose.gateway.yaml up -d --wait --wait-timeout 1800
```

Choose one topology at a time. These files share a Compose project and public port; changing topology is a deployment-owner operation after existing consumers finish. Gateway explicitly uses `--disable-retries`; the probe uses builtin `fetch` without retries. Disable transparent retries/fallback in the selected Harness or SDK as well for strict evaluation. Docker restarting a service is a deployment policy, not request replay.

The Gateway explicitly has no visible NVIDIA/CUDA devices. Only loopback ports are published: API 30000, two-replica worker diagnostics 30001/30002, Gateway Prometheus 10001. Remote workers and Harbor containers need a genuinely reachable URL; container `127.0.0.1` is not the GPU host. Use the environment's existing private networking/TLS ingress or an operator-managed tunnel, then edit `base_url`. Do not publish these example HTTP ports to an untrusted network.

## Connect Hitch and exercise the actual Harness

`endpoint.responses.example.json` and `endpoint.chat-completions.example.json` explicitly select one API. Their capability declarations are proposed settings until the selected protocol and Harness have passed real requests. This Hitch release accepts serial function tool calls only: both examples declare `parallel_tool_calls: false`, and a declaration of `true` is rejected. Choose the API required by that Harness; there is no protocol autodetection or conversion.

```sh
cp endpoint.responses.example.json endpoint.json
# Update base_url and wire_model to the deployed values.
export INFERENCE_API_KEY="$(cat ./secrets/api-key)"
hitch models connect --name coder --file endpoint.json
hitch models inspect endpoint/coder --verify --json
hitch run --harness "$HARNESS" --model endpoint/coder \
  --prompt "Use the available shell tool to compute the SHA-256 of README.md and report the result." \
  --output json
```

Set `HARNESS` to a concrete supported Harness ref with the required tool and workspace available. Save its version, provider/adapter configuration, full tool roundtrip, result and usage. Its actual upstream HTTP budget must be a normal business value (for example 1024 or 4096) and must pass unchanged. No `+2` compensation or tiny-budget test is part of these examples. Run the actual container/remote execution path if that is what the evaluation uses, with its credential available in the process that sends upstream requests.

Hitch freezes the registered endpoint revision for a task. Gear strict candidate evaluation additionally requires its trusted ready-binding/receipt contract and the matching existing model artifact digest. `deployment.example.json` is public operator metadata for this probe, **not** a signed or trusted receipt and **not** a Gear ready-binding file. Copy the actual deployment pipeline receipt into the Gear binding workflow; a model-name echo or a self-filled digest is insufficient.

## Capture configuration and run the HTTP acceptance tool

Prepare a private evidence directory and copy the metadata template:

```sh
mkdir -p artifacts
cp deployment.example.json deployment.json
docker compose --env-file .env -f compose.single.yaml config > artifacts/compose.resolved.yaml
docker compose --env-file .env -f compose.single.yaml exec -T engine \
  python3 -c 'import importlib.metadata as m,json; print(json.dumps({k:m.version(k) for k in ("sglang","sglang-router")},sort_keys=True))' \
  > artifacts/package-versions.json
nvidia-smi --query-gpu=uuid,name,driver_version,memory.total --format=csv > artifacts/gpus.csv
```

For TP=2 use both Compose files in the capture commands. For Gateway use `compose.gateway.yaml` and `worker-1` instead of `engine`; capture package versions from `worker-2` and `gateway` too. Copy the observed package versions and resolved image digest into `deployment.json`, and record exact topology, UUIDs, allocation/receipt references, model/tokenizer/template digest, parser, quantization, cache/isolation policy and any changed capacity settings. Keep the configured and observed versions distinct. Preserve the original trusted receipt alongside the public metadata. Do not copy a process listing containing API-key arguments into public evidence.

Single-replica acceptance (run separately for TP=1 and TP=2):

```sh
node accept.mjs --endpoint endpoint.json --deployment deployment.json \
  --budget 1024 --concurrent 4 --worker-metrics http://127.0.0.1:30000/metrics \
  --expect-workers 1 --out artifacts/single-responses.json
```

Two-replica acceptance through the Gateway:

```sh
node accept.mjs --endpoint endpoint.json --deployment deployment.json \
  --budget 1024 --concurrent 4 \
  --worker-metrics http://127.0.0.1:30001/metrics \
  --worker-metrics http://127.0.0.1:30002/metrics \
  --gateway-metrics http://127.0.0.1:10001/metrics --expect-workers 2 \
  --out artifacts/gateway-responses.json
```

Use `endpoint.chat-completions.example.json` and a different output filename when validating that API. A passing result for one API does not declare the other supported. The report records each exact request, HTTP status, generated text/SSE events, actual usage, tool input/output, local probe version/hash and supplied deployment configuration. Known API keys are redacted; keep reports private because they contain prompts and model output. A failing probe exits nonzero and still saves the observations it obtained.

The checks cover:

- Authentication denial on the generation endpoint, ordinary generation, full-history multi-turn input and unchanged output budget.
- Text SSE deltas, terminal response/finish reason and real input/output usage; missing usage is an error, not a fabricated zero.
- Both JSON and streaming tool calls: the model emits a named function call, the tool runs locally, its actual result returns with the call ID, and the model answers using that result.
- Simultaneous independent requests, followed by two active text streams. After both emit text, only one is aborted; the other must finish, and a subsequent request must succeed. This establishes observed client cancellation isolation, not physical GPU release.
- For the Gateway, positive before/after deltas of `sglang:num_requests_total` from **each worker**. Gateway aggregate counters alone do not prove replica distribution. The probe does not modify workers, caches, models or deployment state.

Finish hardware acceptance by saving server logs/metrics and GPU process/rank observations establishing actual TP=2 execution, plus the real Harness artifacts. In the static allocation setup, run the retained training rollout and HF export checks while independent evaluation uses its reserved devices; preserve training evidence and allocation records. Mark only those scenarios actually observed as passed. TP=2 with two replicas would require four inference GPUs and is not a first-version requirement.

A completed, cancelled or timed-out evaluation never runs `docker compose down`, stop, drain, cache flush or GPU release. The shared endpoint remains available to other consumers. The deployment owner may stop it after confirming all relevant consumers have ended.

## Verified upstream references

- [SGLang v0.5.21 release and official NVIDIA image](https://github.com/sgl-project/sglang/releases/tag/v0.5.21).
- [Pinned image build: bundled Gateway binary and Python wheel](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/docker/Dockerfile), [Gateway package version 0.3.2](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/sgl-model-gateway/bindings/python/pyproject.toml).
- [Pinned Gateway launch flags, retries, queue and Prometheus settings](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/sgl-model-gateway/bindings/python/src/sglang_router/router_args.py), [official Gateway guide](https://docs.sglang.io/docs/advanced_features/sgl_model_gateway).
- [Pinned engine scheduling/capacity arguments](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/python/sglang/srt/arg_groups/fields/schedule.py), [serving/authentication arguments](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/python/sglang/srt/arg_groups/fields/serving.py), [TP arguments](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/python/sglang/srt/arg_groups/fields/parallel.py).
- [Pinned Responses implementation](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/python/sglang/srt/entrypoints/openai/serving_responses.py), [worker request metric](https://github.com/sgl-project/sglang/blob/e00930c5489053f26d86b179cee0d087f846acbb/python/sglang/srt/observability/metrics_collector.py).
- [Qwen example snapshot](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/tree/a09a35458c702b33eeacc393d103063234e8bc28), [Docker Compose GPU device access](https://docs.docker.com/compose/how-tos/gpu-support/), [Compose merge/override behavior](https://docs.docker.com/reference/compose-file/merge/), [NVIDIA CUDA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/).
