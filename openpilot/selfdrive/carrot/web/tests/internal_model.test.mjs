import assert from "node:assert/strict";
import test from "node:test";

import { canRetry, describe, optionLabel } from "../src/features/tools/internal_model.js";

const models = [
  { id: "CD210", name: "CD210", stock: true, recommended: false, experimental: false },
  { id: "RDF", name: "RDF", stock: false, recommended: true, experimental: true },
];
const base = { ok: true, models, requested: "RDF", active: "CD210", can_select: true };

test("options name the stock and experimental models", () => {
  const label = (key) => ({ stock: "기본 모델", recommended: "추천", experimental: "실험" })[key];
  assert.equal(optionLabel(models[0], label), "CD210 (기본 모델)");
  assert.equal(optionLabel(models[1], label), "RDF (추천, 실험)");
});

test("stock selection says when the stock model takes over", () => {
  assert.deepEqual(describe({ ...base, requested: "CD210", active: "CD210" }), { state: "stock", detailKey: "stock_detail" });
  assert.deepEqual(describe({ ...base, requested: "CD210", active: "RDF" }), { state: "stock", detailKey: "stock_next_detail" });
});

test("a ready model is active only when modeld used it", () => {
  assert.deepEqual(describe({ ...base, phase: "ready", active: "RDF" }), { state: "active", detailKey: "active_detail" });
  assert.deepEqual(describe({ ...base, phase: "ready" }), { state: "ready", detailKey: "ready_detail" });
  assert.deepEqual(describe({ ...base, phase: "ready", fallback_reason: "crashed" }), { state: "ready", detailKey: "fallback_crashed" });
  assert.deepEqual(describe({ ...base, phase: "ready", fallback_reason: "new-reason" }), { state: "ready", detailKey: "ready_detail" });
});

test("progress and failures map to their own messages", () => {
  assert.deepEqual(describe({ ...base, phase: "compiling" }), { state: "compiling", detailKey: "compiling_detail" });
  assert.deepEqual(describe({ ...base, phase: "error", error_code: "contract" }), { state: "error", detailKey: "failure_contract" });
  assert.deepEqual(describe({ ...base, phase: "error", error_code: "odd" }), { state: "error", detailKey: "failure_unknown" });
  assert.deepEqual(describe({ ...base, phase: undefined }), { state: "waiting", detailKey: "waiting_detail" });
});

test("the requested model can be applied again only to retry a failure or a crash block", () => {
  assert.equal(canRetry({ ...base, phase: "ready" }), false);
  assert.equal(canRetry({ ...base, phase: "ready", fallback_reason: "crashed" }), true);
  assert.equal(canRetry({ ...base, phase: "ready", fallback_reason: "load_failed" }), true);
  assert.equal(canRetry({ ...base, phase: "ready", fallback_reason: "stale_build" }), false);
  assert.equal(canRetry({ ...base, phase: "error", error_code: "compile" }), true);
  assert.equal(canRetry({ ...base, requested: "CD210", phase: "error" }), false);
});
