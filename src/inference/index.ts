/** Model artifacts and historical inference evidence only; no service lifecycle. */
export { inferenceLockIdentity, inferenceRuntimeIdentity, localModelIdentity, parseInferenceRuntimeManifest, parseInferenceServiceRecord, parseInferenceServiceHandle, parseLocalModelManifest, parseModelNodeBinding } from "./manifest.js";
export { addLocalModel, gcLocalModels, resolveLocalModel, verifyLocalModel } from "./model-store.js";
export type { AddLocalModelOptions, LocalModelGcResultV1 } from "./model-store.js";
export { loadInferenceLock, validateInferenceLockShape, parseInferencePoolTopology } from "./lock.js";
