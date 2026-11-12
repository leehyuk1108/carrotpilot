"use strict";

// My Traverse (Wayon) device key. The status call returns only a masked key; the full key is fetched
// once per explicit copy and, when the browser cannot copy on plain http, shown for 60 s and cleared.
// Hiding the tab, leaving the page, going back to the settings group list or re-rendering the panel bumps
// the generation, so a reveal or copy that is still in flight is dropped instead of putting the key back
// on screen or onto the clipboard. While the key is shown, a short watch clears it as soon as the panel
// stops being visible by any other route.
//
// A registered comma can also make a one-time code (30 minutes, one use) that registers another comma to
// the same cloud. The code is shown the same guarded way, for at most five minutes. A comma without any
// key file shows a field to paste such a code; the server makes this comma's own key and registers it.

const WAYON_KEY_REVEAL_MS = 60000;
const WAYON_INVITE_REVEAL_MS = 300000;
const WAYON_KEY_WATCH_MS = 250;
const WAYON_INVITE_PREFIX = "mtinv1.";
let wayonKeyState = null;
let wayonKeyHideTimer = null;
let wayonKeyWatchTimer = null;
let wayonKeyShownAreas = [];
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
    <div id="wayonInviteBox" hidden>
      <div class="remote-support__actions">
        <button type="button" class="smallBtn" id="wayonKeyInvite" disabled>다른 콤마 등록 코드 만들기</button>
      </div>
      <label class="remote-support__field" for="wayonInviteCode" id="wayonInviteLabel" hidden>등록할 콤마의 당근웹에서 '설정 → 디바이스 → My Traverse 키'를 열고 이 코드를 붙여 넣으세요. 5분 뒤 화면에서 지워집니다.</label>
      <textarea id="wayonInviteCode" rows="3" readonly spellcheck="false" autocapitalize="off" autocomplete="off" hidden></textarea>
    </div>
    <div id="wayonRegisterBox" hidden>
      <label class="remote-support__field" for="wayonRegisterInput">이미 등록된 콤마의 당근웹에서 만든 등록 코드를 붙여 넣으면 이 콤마를 같은 클라우드에 등록합니다.</label>
      <textarea id="wayonRegisterInput" rows="3" spellcheck="false" autocapitalize="off" autocomplete="off" placeholder="mtinv1.…"></textarea>
      <div class="remote-support__actions">
        <button type="button" class="smallBtn btn--filled" id="wayonKeyRegister">이 콤마 등록</button>
      </div>
    </div>
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
  // The panel may already be gone from the page (another settings group was rendered); clear the fields
  // we filled even if they are detached.
  wayonKeyShownAreas.forEach((shown) => { shown.value = ""; shown.hidden = true; });
  wayonKeyShownAreas = [];
  ["wayonKeyReveal", "wayonInviteCode"].forEach((id) => {
    const area = document.getElementById(id);
    if (area) { area.value = ""; area.hidden = true; }
  });
  ["wayonKeyRevealLabel", "wayonInviteLabel"].forEach((id) => {
    const label = document.getElementById(id);
    if (label) label.hidden = true;
  });
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
  const inviteBox = document.getElementById("wayonInviteBox");
  if (inviteBox) inviteBox.hidden = !configured;
  const invite = document.getElementById("wayonKeyInvite");
  if (invite) invite.disabled = !configured;
  const canRegister = !configured && Boolean(value?.can_register);
  const registerBox = document.getElementById("wayonRegisterBox");
  if (registerBox) registerBox.hidden = !canRegister;
  if (!configured) wayonKeyText("wayonKeyDeviceId", value?.local_device_id || "-");
  let reason = configured ? " " : wayonKeyReasonText(value?.reason);
  if (canRegister) reason += " 다른 콤마에서 만든 등록 코드로 이 콤마를 등록할 수 있습니다.";
  else if (!configured && value?.reason === "missing" && !value?.local_device_id) reason += " 기기 ID(DongleId)가 없어 등록 코드로도 등록할 수 없습니다.";
  wayonKeyText("wayonKeyError", reason);
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
  wayonKeyShownAreas.push(area);
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

function wayonInviteTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

// Only to show the cloud address before asking; the server checks the code again.
function wayonInviteInfo(text) {
  const compact = String(text || "").replace(/\s+/g, "");
  if (!compact.startsWith(WAYON_INVITE_PREFIX) || compact.length > 600) return null;
  try {
    let encoded = compact.slice(WAYON_INVITE_PREFIX.length).replace(/-/g, "+").replace(/_/g, "/");
    while (encoded.length % 4) encoded += "=";
    const data = JSON.parse(atob(encoded));
    const url = new URL(data.e);
    if (data.v !== 1 || url.protocol !== "https:" || !/^[A-Z2-7]{32}$/.test(String(data.c || ""))) return null;
    return { host: url.hostname, expires: wayonInviteTime(data.x) };
  } catch {
    return null;
  }
}

async function createWayonInvite() {
  wayonKeyText("wayonKeyError", " ");
  if (!wayonKeyState?.csrf) throw new Error("화면을 새로 고친 뒤 다시 시도해 주세요.");
  if (wayonKeyFramed()) throw new Error("다른 화면 안에 열린 당근웹에서는 등록 코드를 만들 수 없습니다.");
  hideWayonKey();
  const generation = wayonKeyGeneration;
  const confirmed = await appConfirm("다른 콤마를 이 클라우드에 새 기기로 등록하는 1회용 코드를 만듭니다. 30분 안에 한 번만 쓸 수 있고, 코드가 있으면 누구든 콤마 한 대를 등록할 수 있으니 등록할 콤마의 당근웹에만 붙여 넣으세요. 만들까요?", { title: "다른 콤마 등록 코드", confirmLabel: "만들기" });
  if (!confirmed || !wayonKeyStillWanted(generation)) return;
  const result = await requestJson("/api/wayon_key/invite", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Carrot-CSRF": wayonKeyState.csrf },
    body: JSON.stringify({ confirmed: true }),
  });
  if (!wayonKeyStillWanted(generation)) return;
  const area = document.getElementById("wayonInviteCode");
  const label = document.getElementById("wayonInviteLabel");
  area.value = result.invite;
  area.hidden = false;
  label.hidden = false;
  wayonKeyShownAreas.push(area);
  const until = wayonInviteTime(result.expires_at);
  let copied = false;
  if (wayonCanUseClipboardApi()) {
    copied = await wayonClipboardApiCopy(result.invite);
    // As with the key: a cancelled copy must not start the fallback or keep the code on screen.
    if (!wayonKeyStillWanted(generation)) { hideWayonKey(); return; }
  }
  if (!copied) copied = wayonLegacyCopy(result.invite);
  if (!copied) { area.focus(); area.select(); }
  wayonKeyText("wayonKeyMessage", `${copied ? "등록 코드를 복사했습니다." : "아래 등록 코드를 길게 눌러 복사하세요."}${until ? ` ${until}까지` : " 30분 동안"} 한 번 쓸 수 있습니다.`);
  if (wayonKeyHideTimer) clearTimeout(wayonKeyHideTimer);
  wayonKeyHideTimer = setTimeout(() => {
    hideWayonKey();
    wayonKeyText("wayonKeyMessage", "표시한 등록 코드를 지웠습니다.");
  }, WAYON_INVITE_REVEAL_MS);
  wayonKeyWatchTimer = setInterval(() => {
    if (!wayonKeyPanelVisible()) hideWayonKey();
  }, WAYON_KEY_WATCH_MS);
}

async function registerWayonDevice() {
  wayonKeyText("wayonKeyError", " ");
  if (!wayonKeyState?.csrf) throw new Error("화면을 새로 고친 뒤 다시 시도해 주세요.");
  if (wayonKeyFramed()) throw new Error("다른 화면 안에 열린 당근웹에서는 등록할 수 없습니다.");
  const input = document.getElementById("wayonRegisterInput");
  const text = input?.value || "";
  const info = wayonInviteInfo(text);
  if (!info) throw new Error("등록 코드 형식이 아닙니다. 이미 등록된 콤마의 당근웹에서 만든 코드를 그대로 붙여 넣으세요.");
  const device = wayonKeyState.local_device_id || "-";
  const generation = wayonKeyGeneration;
  const confirmed = await appConfirm(`이 콤마(기기 ID ${device})를 ${info.host} 클라우드에 새 기기로 등록합니다. 등록하면 이 콤마가 주행 기록, 위치, 사진을 그 클라우드에 올립니다. 등록할까요?`, { title: "이 콤마 등록", confirmLabel: "등록" });
  if (!confirmed || !wayonKeyStillWanted(generation)) return;
  const button = document.getElementById("wayonKeyRegister");
  if (button) button.disabled = true;
  try {
    await requestJson("/api/wayon_key/register", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Carrot-CSRF": wayonKeyState.csrf },
      body: JSON.stringify({ confirmed: true, invite: text }),
    });
  } finally {
    if (button) button.disabled = false;
  }
  if (input) input.value = "";
  wayonKeyText("wayonKeyMessage", "등록했습니다. 잠시 뒤 이 콤마가 클라우드에 접속합니다. '키 복사'로 키를 복사해 이 차의 My Traverse 앱에 넣으세요.");
  await refreshWayonKey();
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
  const invite = container.querySelector("#wayonKeyInvite");
  if (invite) invite.onclick = async () => {
    try {
      await createWayonInvite();
    } catch (error) {
      wayonKeyText("wayonKeyError", error.message);
    }
  };
  const register = container.querySelector("#wayonKeyRegister");
  if (register) register.onclick = async () => {
    try {
      await registerWayonDevice();
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
