"use strict";

// My Traverse (Wayon) device key. The status call returns only a masked key; the full key is fetched
// once per explicit copy and, when the browser cannot copy on plain http, shown for 60 s and cleared.
// Hiding the tab, leaving the page, going back to the settings group list or re-rendering the panel bumps
// the generation, so a reveal or copy that is still in flight is dropped instead of putting the key back
// on screen or onto the clipboard. While the key is shown, a short watch clears it as soon as the panel
// stops being visible by any other route.

const WAYON_KEY_REVEAL_MS = 60000;
const WAYON_KEY_WATCH_MS = 250;
let wayonKeyState = null;
let wayonKeyHideTimer = null;
let wayonKeyWatchTimer = null;
let wayonKeyShownArea = null;
let wayonKeyGeneration = 0;

function renderWayonKeyPanel() {
  return `<section id="wayonKeyPanel" class="remote-support" aria-label="My Traverse 키">
    <div class="remote-support__heading"><h3>My Traverse 키</h3><span id="wayonKeyState" role="status">확인 중</span></div>
    <dl class="remote-support__details">
      <div><dt>기기 ID</dt><dd id="wayonKeyDeviceId">-</dd></div>
      <div><dt>서버</dt><dd id="wayonKeyEndpoint">-</dd></div>
      <div><dt>키</dt><dd id="wayonKeyMasked">-</dd></div>
    </dl>
    <div class="remote-support__actions">
      <button type="button" class="smallBtn" id="wayonKeyCopyId" disabled>기기 ID 복사</button>
      <button type="button" class="smallBtn btn--filled" id="wayonKeyCopy" disabled>키 복사</button>
    </div>
    <label class="remote-support__field" for="wayonKeyReveal" id="wayonKeyRevealLabel" hidden>자동 복사가 안 되면 아래 칸을 길게 눌러 복사하세요. 60초 뒤 지워집니다.</label>
    <textarea id="wayonKeyReveal" rows="2" readonly spellcheck="false" autocapitalize="off" autocomplete="off" hidden></textarea>
    <p id="wayonKeyMessage" class="remote-support__message" role="status" aria-live="polite"></p>
    <p id="wayonKeyError" class="remote-support__error" role="alert"></p>
    <p class="remote-support__message">콤마는 이 키로 My Traverse 클라우드에 주행 기록과 사진을 올립니다. 다른 사람에게 보내거나 공개된 곳에 붙여 넣지 마세요.</p>
  </section>`;
}

function wayonKeyText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value || "-";
}

function wayonKeyReasonText(reason) {
  return {
    missing: "이 콤마에는 My Traverse 키가 설정돼 있지 않습니다.",
    unreadable: "키 파일을 읽을 수 없습니다.",
    invalid: "키 파일 형식이 올바르지 않습니다.",
    no_key: "키 파일에 키가 없습니다.",
  }[reason] || "키를 확인할 수 없습니다.";
}

function hideWayonKey() {
  wayonKeyGeneration += 1;
  if (wayonKeyHideTimer) clearTimeout(wayonKeyHideTimer);
  wayonKeyHideTimer = null;
  if (wayonKeyWatchTimer) clearInterval(wayonKeyWatchTimer);
  wayonKeyWatchTimer = null;
  // The panel may already be gone from the page (another settings group was rendered); clear the field
  // we filled even if it is detached.
  if (wayonKeyShownArea) { wayonKeyShownArea.value = ""; wayonKeyShownArea.hidden = true; }
  wayonKeyShownArea = null;
  const area = document.getElementById("wayonKeyReveal");
  if (area) { area.value = ""; area.hidden = true; }
  const label = document.getElementById("wayonKeyRevealLabel");
  if (label) label.hidden = true;
}

function wayonKeyPanelVisible() {
  if (document.hidden) return false;
  const panel = document.getElementById("wayonKeyPanel");
  if (!panel || panel.isConnected === false) return false;
  if (panel.closest?.("[hidden], [inert], .hidden")) return false;
  // display:none anywhere above (the settings items screen once it is put away) leaves no boxes.
  if (typeof panel.getClientRects === "function" && panel.getClientRects().length === 0) return false;
  return true;
}

function wayonKeyStillWanted(generation) {
  return generation === wayonKeyGeneration && wayonKeyPanelVisible();
}

function wayonKeyFramed() {
  try {
    return Boolean(window.top) && window.top !== window.self;
  } catch {
    return true;
  }
}

function updateWayonKeyPanel(value) {
  wayonKeyState = value;
  const configured = Boolean(value?.configured);
  wayonKeyText("wayonKeyState", configured ? "설정됨" : "없음");
  wayonKeyText("wayonKeyDeviceId", configured ? value.device_id : "-");
  wayonKeyText("wayonKeyEndpoint", configured ? value.endpoint_host : "-");
  wayonKeyText("wayonKeyMasked", configured ? `${value.key_masked} (${value.key_length}자)` : "-");
  const copyId = document.getElementById("wayonKeyCopyId");
  if (copyId) copyId.disabled = !configured || !value.device_id;
  const copy = document.getElementById("wayonKeyCopy");
  if (copy) copy.disabled = !configured;
  wayonKeyText("wayonKeyError", configured ? " " : wayonKeyReasonText(value?.reason));
}

function wayonCanUseClipboardApi() {
  return Boolean(window.isSecureContext && navigator.clipboard?.writeText);
}

async function wayonClipboardApiCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// Synchronous, so nothing can change between the caller's last check and the copy.
function wayonLegacyCopy(text) {
  const area = document.createElement("textarea");
  let copied = false;
  try {
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    area.value = "";
    area.remove();
  }
  return copied;
}

async function wayonCopyText(text) {
  if (wayonCanUseClipboardApi() && await wayonClipboardApiCopy(text)) return true;
  return wayonLegacyCopy(text);
}

async function refreshWayonKey() {
  try {
    updateWayonKeyPanel(await getJson("/api/wayon_key"));
  } catch (error) {
    wayonKeyState = null;
    wayonKeyText("wayonKeyState", "확인 실패");
    wayonKeyText("wayonKeyError", error.message);
    ["wayonKeyCopyId", "wayonKeyCopy"].forEach((id) => {
      const button = document.getElementById(id);
      if (button) button.disabled = true;
    });
  }
}

async function copyWayonKey() {
  wayonKeyText("wayonKeyError", " ");
  if (!wayonKeyState?.csrf) throw new Error("화면을 새로 고친 뒤 다시 시도해 주세요.");
  if (wayonKeyFramed()) throw new Error("다른 화면 안에 열린 당근웹에서는 키를 복사할 수 없습니다.");
  hideWayonKey();
  const generation = wayonKeyGeneration;
  const confirmed = await appConfirm("My Traverse 키를 클립보드에 복사합니다. 이 키가 있으면 콤마처럼 클라우드에 접속할 수 있으니 본인 기기에만 붙여 넣으세요. 복사할까요?", { title: "My Traverse 키 복사", confirmLabel: "복사" });
  if (!confirmed || !wayonKeyStillWanted(generation)) return;
  const result = await requestJson("/api/wayon_key/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Carrot-CSRF": wayonKeyState.csrf },
    body: JSON.stringify({ confirmed: true }),
  });
  if (!wayonKeyStillWanted(generation)) return;
  let copied = false;
  if (wayonCanUseClipboardApi()) {
    copied = await wayonClipboardApiCopy(result.key);
    // A write the browser already finished cannot be taken back, but a cancelled copy must not start the
    // fallback or show the key.
    if (!wayonKeyStillWanted(generation)) return;
  }
  if (!copied) copied = wayonLegacyCopy(result.key);
  if (copied) {
    hideWayonKey();
    wayonKeyText("wayonKeyMessage", "키를 복사했습니다. 붙여 넣은 뒤에는 다른 글을 복사해 클립보드를 덮어 두세요.");
    return;
  }
  const area = document.getElementById("wayonKeyReveal");
  const label = document.getElementById("wayonKeyRevealLabel");
  area.value = result.key;
  area.hidden = false;
  label.hidden = false;
  wayonKeyShownArea = area;
  area.focus();
  area.select();
  wayonKeyText("wayonKeyMessage", "자동 복사가 막혀 있어 아래 칸에 표시했습니다.");
  if (wayonKeyHideTimer) clearTimeout(wayonKeyHideTimer);
  wayonKeyHideTimer = setTimeout(() => {
    hideWayonKey();
    wayonKeyText("wayonKeyMessage", "표시한 키를 지웠습니다.");
  }, WAYON_KEY_REVEAL_MS);
  wayonKeyWatchTimer = setInterval(() => {
    if (!wayonKeyPanelVisible()) hideWayonKey();
  }, WAYON_KEY_WATCH_MS);
}

function bindWayonKeyPanel(container) {
  hideWayonKey();
  container.querySelector("#wayonKeyCopyId").onclick = async () => {
    const copied = wayonKeyState?.device_id ? await wayonCopyText(wayonKeyState.device_id) : false;
    wayonKeyText("wayonKeyMessage", copied ? "기기 ID를 복사했습니다." : "기기 ID를 길게 눌러 복사하세요.");
  };
  container.querySelector("#wayonKeyCopy").onclick = async () => {
    try {
      await copyWayonKey();
    } catch (error) {
      wayonKeyText("wayonKeyError", error.message);
    }
  };
  refreshWayonKey();
}

document.addEventListener("visibilitychange", () => { if (document.hidden) hideWayonKey(); });
window.addEventListener("pagehide", hideWayonKey);
window.addEventListener("carrot:pagechange", hideWayonKey);
window.addEventListener("carrot:settingscreenchange", (event) => {
  if (event?.detail?.screen !== "items") hideWayonKey();
});
