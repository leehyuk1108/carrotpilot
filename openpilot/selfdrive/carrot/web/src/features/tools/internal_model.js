"use strict";

// Tools card: choose the internal (comma GPU) driving model. Server: /api/internal_model (features/internal_model.py).

const POLL_INTERVAL_MS = 3000;
let pollTimer = null;
let lastStatus = null;
let applying = false;

const FALLBACK_STRINGS = {
  title: "Driving model",
  stock: "Stock model",
  recommended: "recommended",
  experimental: "experimental",
  apply: "Apply",
  checking: "Checking",
  active: "In use",
  waiting: "Starts when parked",
  downloading: "Downloading",
  verifying: "Verifying download",
  validating: "Checking compatibility",
  compiling: "Compiling",
  ready: "Next drive",
  error: "Preparation failed",
  stock_detail: "The stock driving model is used.",
  stock_next_detail: "The stock driving model is used from the next drive.",
  active_detail: "{name} is the driving model now.",
  waiting_detail: "Download and compilation start once the car is parked with the ignition off.",
  downloading_detail: "Downloading {name}. The current driving model keeps running.",
  verifying_detail: "Checking the downloaded file.",
  validating_detail: "Checking that {name} has the inputs and outputs this comma expects.",
  compiling_detail: "Compiling {name} on the comma (about 10-20 minutes). Turning the ignition on pauses it until you park again.",
  ready_detail: "{name} is ready and is used from the next drive.",
  fallback_crashed: "{name} did not start properly twice in a row, so the stock model was used. Apply it again to retry once.",
  fallback_load_failed: "{name} could not be loaded, so the stock model was used. Apply it again to retry.",
  fallback_stale_build: "After a software update {name} must be compiled again. This runs automatically while parked.",
  fallback_not_compiled: "{name} is not compiled yet, so the stock model was used.",
  fallback_camera: "{name} was compiled for another camera, so the stock model was used.",
  fallback_missing: "The compiled file of {name} is missing, so the stock model was used.",
  failure_network: "The download was interrupted. It retries automatically.",
  failure_waiting_offroad: "Paused because the ignition was turned on. It continues when parked.",
  failure_storage: "Not enough free storage on the comma.",
  failure_sha: "The downloaded file did not match the expected checksum and was discarded.",
  failure_contract: "This model's inputs or outputs differ from what this comma expects, so it cannot be used.",
  failure_compile: "Compilation failed. The stock model stays in use.",
  failure_build: "Waiting for the openpilot build to finish. It retries automatically.",
  failure_unknown: "Preparation failed. The stock model stays in use.",
  onroad_note: "The driving model can only be changed while parked with the ignition off.",
  confirm: "Switch the driving model to {name}? It is downloaded and compiled while parked (about 10-20 minutes) and used from the next drive.",
  confirm_stock: "Switch back to the stock driving model? It is used from the next drive.",
  applied: "Driving model selection saved.",
};

function t(key, values = {}) {
  const fallback = FALLBACK_STRINGS[key] || key;
  const text = typeof getUIText === "function" ? getUIText(`internal_model_${key}`, fallback) : fallback;
  return text.replace(/\{(\w+)\}/g, (_m, name) => (name in values ? String(values[name]) : ""));
}

function modelById(status, id) {
  return (status?.models || []).find((model) => model.id === id) || null;
}

function optionLabel(model, translate = t) {
  const tags = [];
  if (model.stock) tags.push(translate("stock"));
  if (model.recommended) tags.push(translate("recommended"));
  if (model.experimental) tags.push(translate("experimental"));
  return tags.length ? `${model.name} (${tags.join(", ")})` : model.name;
}

// What the card shows: { state, detailKey } for a /api/internal_model payload.
function describe(status) {
  const requested = modelById(status, status?.requested);
  if (!requested || requested.stock) {
    return { state: "stock", detailKey: status?.active && status.active !== status.requested ? "stock_next_detail" : "stock_detail" };
  }
  const phase = String(status.phase || "waiting");
  if (phase === "error") {
    const key = `failure_${status.error_code}`;
    return { state: "error", detailKey: Object.hasOwn(FALLBACK_STRINGS, key) ? key : "failure_unknown" };
  }
  if (phase === "ready") {
    if (status.active === requested.id) return { state: "active", detailKey: "active_detail" };
    const key = `fallback_${status.fallback_reason}`;
    if (status.fallback_reason && Object.hasOwn(FALLBACK_STRINGS, key)) return { state: "ready", detailKey: key };
    return { state: "ready", detailKey: "ready_detail" };
  }
  if (["downloading", "verifying", "validating", "compiling"].includes(phase)) return { state: phase, detailKey: `${phase}_detail` };
  return { state: "waiting", detailKey: "waiting_detail" };
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function elapsedText(startedAt) {
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - (Number(startedAt) || Date.now() / 1000)));
  return `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
}

function render(status = lastStatus) {
  const card = document.getElementById("internalModelCard");
  if (!card || !status?.ok) return;
  lastStatus = status;
  card.hidden = false;
  const requested = modelById(status, status.requested) || { name: status.requested };
  const { state, detailKey } = describe(status);
  card.dataset.state = state === "active" ? "compiled" : state;
  card.classList.toggle("is-running", ["downloading", "verifying", "validating", "compiling"].includes(state));
  document.getElementById("internalModelTitle").textContent = t("title");
  document.getElementById("internalModelState").textContent = state === "stock" ? t("stock") : `${requested.name} · ${t(state)}`;
  document.getElementById("internalModelDetail").textContent = t(detailKey, { name: requested.name });

  const select = document.getElementById("internalModelSelect");
  if (select && document.activeElement !== select) {
    const current = select.value;
    select.replaceChildren(...status.models.map((model) => {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = optionLabel(model);
      return option;
    }));
    select.value = modelById(status, current) ? current : status.requested;
  }
  if (select) select.disabled = !status.can_select || applying;

  const percent = Number(status.progress);
  const showProgress = state === "downloading" && Number.isFinite(percent);
  document.getElementById("internalModelProgress").hidden = !showProgress;
  document.getElementById("internalModelProgressBar").style.width = `${Math.min(100, Math.max(0, percent || 0))}%`;
  document.getElementById("internalModelAmount").textContent = showProgress
    ? `${formatBytes(status.downloaded_bytes)} / ${formatBytes(status.total_bytes)} · ${percent.toFixed(1)}%`
    : (state === "compiling" ? elapsedText(status.started_at) : (status.can_select ? "" : t("onroad_note")));

  const button = document.getElementById("btnInternalModelApply");
  if (button) {
    button.textContent = applying ? t("checking") : t("apply");
    button.disabled = applying || !status.can_select || !select || select.value === status.requested;
  }
}

async function refresh() {
  try {
    render(await getJson("/api/internal_model"));
  } catch (_error) {
    // keep the last card; the next poll retries
  } finally {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(refresh, POLL_INTERVAL_MS);
  }
}

async function applySelection() {
  const select = document.getElementById("internalModelSelect");
  const model = modelById(lastStatus, select?.value);
  if (!model || model.id === lastStatus?.requested) return;
  const message = model.stock ? t("confirm_stock") : t("confirm", { name: model.name });
  if (typeof appConfirm === "function" && !await appConfirm(message, { title: t("title") })) return;
  applying = true;
  render();
  try {
    render(await postJson("/api/internal_model/select", { id: model.id }));
    if (typeof showAppToast === "function") showAppToast(t("applied"));
  } catch (error) {
    if (typeof showAppToast === "function") showAppToast(`${t("error")}: ${error?.message || error}`, { tone: "error", duration: 5000 });
  } finally {
    applying = false;
    await refresh();
  }
}

function init() {
  const button = document.getElementById("btnInternalModelApply");
  if (button && button.dataset.bound !== "1") {
    button.dataset.bound = "1";
    button.addEventListener("click", applySelection);
  }
  const select = document.getElementById("internalModelSelect");
  if (select && select.dataset.bound !== "1") {
    select.dataset.bound = "1";
    select.addEventListener("change", () => render());
  }
  if (!pollTimer) refresh();
}

const CarrotInternalModel = Object.freeze({ init, refresh, render });
globalThis.CarrotInternalModel = CarrotInternalModel;

export { CarrotInternalModel, FALLBACK_STRINGS, describe, optionLabel };
