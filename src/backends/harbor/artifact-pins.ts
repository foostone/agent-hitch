import type { EvalArtifactPinV1 } from "../../domain/index.js";
import { HitchError, invalidInput } from "../../foundation/index.js";

export function parseEvalArtifactPins(value: unknown): EvalArtifactPinV1[] {
  if (!Array.isArray(value) || !value.length || value.length > 2) throw invalidInput("artifact pins require one exact pin per runtime platform");
  const seen = new Set<string>();
  const pins = value.map(raw => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)
      || Object.keys(raw).sort().join(",") !== "artifact_id,artifact_integrity,entrypoint_integrity,platform") throw invalidInput("invalid artifact pin");
    const pin = raw as Record<string, unknown>;
    if (!["linux-x64", "linux-arm64"].includes(String(pin.platform)) || seen.has(String(pin.platform))
      || [pin.artifact_id, pin.artifact_integrity, pin.entrypoint_integrity].some(value => typeof value !== "string" || !/^sha256:[a-f0-9]{64}$/.test(value))) throw invalidInput("invalid artifact pin");
    seen.add(String(pin.platform));
    return { platform: pin.platform, artifact_id: pin.artifact_id, artifact_integrity: pin.artifact_integrity,
      entrypoint_integrity: pin.entrypoint_integrity } as EvalArtifactPinV1;
  });
  return pins.sort((a, b) => a.platform < b.platform ? -1 : 1);
}

type Prepared = { platform: string; artifact_id: string; artifact_integrity: string; entrypoint_integrity: string };

/** Also used immediately before each physical Harbor invocation, including retries and recovery. */
export function assertHarborArtifactPin(pins: EvalArtifactPinV1[] | undefined, artifact: Prepared): void {
  if (pins === undefined) return;
  const pin = parseEvalArtifactPins(pins).find(pin => pin.platform === artifact.platform);
  if (!pin || pin.artifact_id !== artifact.artifact_id || pin.artifact_integrity !== artifact.artifact_integrity
    || pin.entrypoint_integrity !== artifact.entrypoint_integrity) throw mismatch();
}

/** The requested platform set must match preparation; unused or missing pins are not accepted. */
export function assertHarborArtifactPins(pins: EvalArtifactPinV1[] | undefined, artifacts: Prepared[]): void {
  if (pins === undefined) return;
  const normalized = parseEvalArtifactPins(pins);
  if (normalized.length !== artifacts.length || new Set(artifacts.map(a => a.platform)).size !== artifacts.length) throw mismatch();
  for (const artifact of artifacts) assertHarborArtifactPin(normalized, artifact);
}

function mismatch() {
  return new HitchError("prepared artifact differs from the submitted artifact pins", { code: "eval_artifact_pin_mismatch", exitCode: 12 });
}
