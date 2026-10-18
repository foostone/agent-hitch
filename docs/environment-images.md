# Environment image service

Hitch's environment image service turns an explicit Docker build context into
an immutable, verified manifest before backend execution. Fixed task,
Verifier, and Compose registry references can be resolved before backend
execution and injected into Harbor as immutable digest references. Build
contexts are prebuilt when every Dockerfile base is digest-pinned. The Harbor
overlay injects the resulting immutable local config digest and uses Compose's
`!reset` merge tag to remove the original main-service build stanza.

Plain, independent Compose sidecar builds are also prepared during eval
planning, before Harbor starts a trial. This supported form uses unnamed
images, an explicit `linux/amd64` or `linux/arm64` platform, a literal local
context inside the task's `environment/` directory, and a Dockerfile with
digest-pinned bases. The build may specify only `context` and `dockerfile`.
The main Compose build must match `environment/Dockerfile`. Named image
producers or consumers, image mounts, build arguments, targets, secrets,
additional contexts, pull overrides, and other unsupported inputs retain the
native backend path. A context or Dockerfile that escapes the environment or
uses a symlink is not prebuilt.

The execution plan gives each prepared sidecar an explicit service binding.
Harbor receives its immutable local config digest with `build: !reset null`
and `pull_policy: never`; dependencies and health checks retain their original
Compose definitions. Separate verifier sessions do not inherit the candidate's
prepared main or sidecar images. Historical prebuilt image uses without a
service binding still identify the main image.

The service hashes every regular context file (path, content digest, executable
bit), the Dockerfile, non-secret build arguments, platform, target, immutable
base-image digests, and secret names. Secret values are passed to BuildKit only
through `--secret id=NAME,env=NAME`; they are not included in cache keys,
manifests, build records, or errors.

Eval task ownership is recorded by the execution plan's `task_ids`, not by the
build identity. Different tasks in the same immutable benchmark revision that
have byte-identical build contexts therefore share one image ID and one keyed
build; each task still carries its own image-use evidence into its result
bundle.

Builds are serialized by a persistent cache-key lock under `locks/builds/`.
After acquiring the lock, every caller rechecks the stored manifest and probes
the actual local image identity. Ten independent service instances requesting
the same key therefore produce one BuildKit invocation. Successful manifests
are promoted under `store/environment-images/sha256/`; bounded build state is
stored separately under `store/build-records/sha256/`.

`BuildSlotAdmission` reserves `build_slots` in the same global resource ledger
used by evals while keeping `container_slots` independent. The default build
reservation is one build slot and no trial container slot. Operators can pass a
larger CPU/memory build vector when their builder needs it.

Preparation uses the existing 30-minute Docker command deadline and the eval's
cancellation signal; Harbor's task `environment.build_timeout_sec` remains the
independent trial startup limit. Build events and persisted build records expose
queued, started, completed, and failed phases. A failed record retains at most
16,384 characters of redacted diagnostic text, including available BuildKit output.
Historical slow-build causes remain undetermined unless supported by those logs.

`DockerBuildKitBuilder` uses `docker buildx build --load`, a metadata file for
the output manifest/config digests, and `docker image inspect` for platform and
cache-hit verification. Optional registry cache references are derived from
the service cache key under an operator-controlled prefix; user input cannot
select an arbitrary cache tag.

`npm run canary:buildkit-secrets` performs a real, offline-capable Docker
canary against a locally available base image. It rotates a BuildKit secret,
requires the second build to reuse cache without changing the config digest,
and scans the saved image plus exported local or inline cache evidence for both
secret values. `HITCH_DOCKER_CANARY_BASE` and
`HITCH_DOCKER_CANARY_PLATFORM` select a different preloaded base/platform.

`DockerRegistryResolver` resolves both mutable tags and explicit digest
references through a platform-specific pull and local OCI identity probe. The
persisted manifest always uses a `repository@sha256:...` reference; an explicit
input digest must match exactly, and the resolved OS/architecture plus config
digest are verified before promotion. A later request for a mutable tag still
resolves the tag again, so a moved tag creates a new content identity instead
of returning a stale tag-based cache entry.

Eval planning discovers fixed `docker_image` and Compose `image` references,
persists successful resolutions in work-item `image_refs`, and persists every
fallback reason separately. `prebuild-preferred` can fall back; a
`prebuild-required` request fails before Harbor starts if any service cannot be
prebuilt. Harbor's final Compose overlay replaces only exact discovered image
references and the same mapping is reused by infrastructure retries.
Before a planned work item starts Harbor, every referenced global manifest is
reloaded and matched against the immutable plan. Each imported or diagnostic
run then embeds the uses and full, de-duplicated manifests in
`environment/image.manifest.json`; the sealed bundle index records their OCI
digests together with provider/worker/lease and requested/observed resources.
Owned Harbor environments retain containers until the fenced Hitch reaper runs.
The resource observer records Docker's configured image reference and config
digest, and a planned immutable image is accepted only when that config digest
was actually observed during the trial.

The Docker observer records Compose service labels with image identities, so a
prepared sidecar must be observed under its planned service. If Harbor reports
an explicit setup exception with both Target and verifier executions null,
missing containers preserve that upstream diagnostic. An observed conflicting
image still fails verification, and successful trials require image evidence.

Owned trial cleanup omits Compose's `--rmi local` operation. That operation
enumerates image metadata and can fail before pending network and volume
removal. Container, network, and volume cleanup remains owner-scoped. Prepared
images remain subject to `hitch images gc`; images built by native fallback
paths are retained because they lack Hitch's immutable image ownership proof.
Those unregistered fallback images may require explicit operator cleanup.
No host-wide image prune is performed.

The manual cleanup canary uses the installed Compose binary against a local
fake Engine that rejects image enumeration. It verifies that the original
`--rmi local` path fails and leaves network/volume cleanup pending, while the
updated command removes project resources without an image API request:

```sh
python3 test-support/compose_cleanup_engine_canary.py /tmp/hitch-compose-cleanup-canary
```

Pass `--compose-binary /path/to/docker-compose` when the binary is not on PATH.
This proves the installed Compose command boundary; it does not reproduce a
large containerd image store or certify a particular deployed Compose version.

`npm run canary:docker-images` starts three ownership-labeled containers with
distinct main, sidecar, and separate-Verifier config digests. It runs the real
Docker observer, verifies every planned digest, records each container's
Engine-reported cumulative CPU nanoseconds, and proves a forged Verifier digest
fails closed. The command builds from a temporary export of the selected local
base, so it does not require registry access.

Every Hitch-built image carries root-id and cache-key labels. Eval planning
writes a provisional image-reference record under a global reference fence
before the execution plan is published. `hitch images gc` is a dry run by
default; `--apply` is required to remove anything. GC holds the same fence and
retains images referenced by non-terminal evals, self-consistent sealed bundle
indexes, or explicit `hitch images pin` records. A local build tag is eligible
only after its config digest, root-id label, and cache-key label all match the
stored manifest. Registry-owned images are never deleted. The default minimum
age is 24 hours and can be changed with `--minimum-age`.

Successful, failed, and in-progress records have a stable `build_<id>` index.
An authenticated daemon exposes them at `GET /v1/builds/{build-id}` together
with the verified manifest when one has been promoted.

Any unresolved or unsupported build-context path remains explicitly
`backend-build`; it is never described as prebuilt.
