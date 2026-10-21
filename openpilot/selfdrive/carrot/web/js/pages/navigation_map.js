"use strict";

// Scoped port of the navigation UI at 59eb0f82. A saved destination is not
// evidence of a model-input connection, and never changes CarrotNaviSource.
(() => {
  const $ = (id) => document.getElementById(id);
  if (!$("form")) {
    // The main app has history-based settings routes, not /settings URLs.
    const target = new URLSearchParams(window.location.search).get("navigation_settings");
    if (target === "mapbox" || target === "source") {
      let opened = false;
      const openSettings = () => {
        // Bootstrap can be deferred by the first-run intro. Its pagechange
        // event fires before the bootstrap flag is committed, hence the microtask.
        Promise.resolve().then(async () => {
          const names = ["loadSettings", "showPage", "switchSettingTab", "activateSettingGroup", "focusSettingItem"];
          if (target === "source") names.push("findSettingItemByName");
          if (opened || document.readyState === "loading" || window.__CARROT_WEB_HAS_BOOTSTRAPPED_PAGE !== true
              || !names.every((name) => typeof window[name] === "function")) return;
          opened = true;
          window.showPage("setting", false);
          await window.loadSettings();
          let group = "SYS_NET";
          if (target === "source") {
            const entry = window.findSettingItemByName("CarrotNaviSource");
            if (typeof entry?.group !== "string" || !entry.group.trim()) {
              window.showAppToast?.("Navigation Input (CarrotNaviSource) is unavailable in the current settings.", { tone: "error" });
              return;
            }
            group = entry.group;
          }
          await window.switchSettingTab("carrot");
          await window.activateSettingGroup(group, false, { animateGroups: false, animateItems: false });
          if (target === "source") window.focusSettingItem("CarrotNaviSource");
          else $("mapboxTokenCard")?.scrollIntoView({ block: "nearest" });
          history.replaceState({ page: "setting", screen: "items", group }, "", "/");
        }).catch(() => window.showAppToast?.("Could not open navigation settings.", { tone: "error" }));
      };
      window.addEventListener("DOMContentLoaded", openSettings);
      window.addEventListener("carrot:pagechange", openSettings);
      openSettings();
    }
    return;
  }

  let language = navigator.language || "en";
  try { language = localStorage.getItem("carrot_web_lang") || language; } catch (_) {}
  const ko = language.startsWith("ko");
  const tr = (k, e) => ko ? k : e;
  document.documentElement.lang = ko ? "ko" : "en";
  const labels = {
    title: tr("Carrot 내비게이션", "Carrot Navigation"),
    sourceLabel: tr("설정된 입력 소스", "Configured source"),
    sourceSettings: tr("입력 설정", "Source settings"), keySettings: tr("키 설정", "Key settings"),
    searchTitle: tr("목적지 검색", "Find a destination"), search: tr("검색", "Search"),
    currentTitle: tr("저장된 목적지", "Saved destination"), clear: tr("목적지 삭제", "Clear destination"),
    previewTitle: tr("지도 미리보기", "Map preview"), refresh: tr("상태 새로고침", "Refresh status"),
    previewNote: tr("미리보기 전용. N16 경로·지도 입력 미연결.", "Preview only. N16 route/map input not connected."),
  };
  Object.entries(labels).forEach(([id, label]) => { $(id).textContent = label; });
  $("query").setAttribute("aria-label", tr("목적지", "Destination"));
  $("results").setAttribute("aria-label", tr("검색 결과", "Search results"));
  $("map").alt = tr("경로 지도 미리보기", "Route map preview");

  const unavailable = tr("N16 경로·지도 입력 미연결", "N16 route/map input not connected");
  const disconnected = tr("이 빌드에는 N16 경로·지도 입력 연결이 없습니다.", "The N16 route/map input pipeline is not connected in this build.");
  const optionalInput = tr("카메라 추론과 별개의 선택적 내비 입력입니다.", "Camera inference is independent of this optional navigation input.");
  let snapshot = null, alive = true, busy = false;
  let searchGeneration = 0, statusGeneration = 0, mapGeneration = 0, writeGeneration = 0;
  let searchController, statusController, mapController, imageUrl;
  let resultButtons = [];
  const requests = new Set();

  function controls() {
    const editable = snapshot?.destination_editable === true;
    $("query").disabled = busy;
    $("search").disabled = busy;
    $("clear").disabled = busy || !editable || !snapshot?.destination;
    $("refresh").disabled = busy;
    resultButtons.forEach((button) => { button.disabled = busy || !editable; });
  }

  function failure(status, operation) {
    if (status === 409 && operation === "search") return tr("Mapbox Public 키가 없습니다. 키 설정을 확인하세요.", "Mapbox Public key missing. Check key settings.");
    if (status === 409) return tr("선택한 내비 입력 또는 외부 경로가 목적지를 관리합니다. 웹에서 변경할 수 없습니다.", "The selected navigation source or external route owns this destination. Web changes are blocked.");
    if (status === 429) return tr("검색 요청이 많습니다. 잠시 후 다시 시도하세요.", "Search rate limit reached. Try again shortly.");
    if (status === 400 || status === 422) return tr("검색어나 목적지 좌표를 확인하세요.", "Check the query or destination coordinates.");
    if (status === 401 || status === 403) return tr("요청이 거부되었습니다. 연결과 키 설정을 확인하세요.", "Request denied. Check the connection and key settings.");
    return tr("서비스에 연결할 수 없습니다. 잠시 후 다시 시도하세요.", "Service unavailable. Check the connection and try again.");
  }

  async function request(path, options = {}, kind = "json", controller = new AbortController()) {
    requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`/api/navigation/${path}`, {
        ...options, cache: "no-store", credentials: "same-origin", redirect: "error", signal: controller.signal,
      });
      if (!response.ok) throw Object.assign(new Error("request failed"), { status: response.status });
      if (kind === "map") {
        const reason = response.headers.get("X-Nav-Map-Status");
        const type = response.headers.get("Content-Type") || "";
        const blob = response.status === 200 && reason === "ready" && type.split(";")[0] === "image/png"
          ? await response.blob() : null;
        return { reason, blob };
      }
      return await response.json();
    } finally {
      clearTimeout(timeout);
      requests.delete(controller);
    }
  }

  function place(value) {
    if (!value || typeof value.place_name !== "string" || !value.place_name.trim() || value.place_name.length > 256
        || /[\x00-\x1f]/.test(value.place_name) || !Number.isFinite(value.latitude) || !Number.isFinite(value.longitude)
        || Math.abs(value.latitude) > 85.05112878 || Math.abs(value.longitude) > 180) return null;
    // Only these fields cross the write boundary; never forward response metadata.
    return { place_name: value.place_name.trim(), latitude: value.latitude, longitude: value.longitude };
  }

  function renderSnapshot(value) {
    snapshot = {
      destination: place(value.destination), key_configured: value.key_configured === true,
      destination_editable: value.destination_editable === true && value.navigation_source === "network"
        && value.destination?.place_name !== "External Navi",
      navigation_source: value.navigation_source, pipeline_status: value.pipeline_status,
    };
    $("current").textContent = snapshot.destination?.place_name || tr("저장된 목적지 없음", "No saved destination");
    const source = { network: tr("네트워크", "Network"), navdy: "Navdy" }[snapshot.navigation_source] || tr("알 수 없음", "Unknown");
    $("sourceStatus").textContent = `${source} · ${tr("실행 상태 미확인", "runtime unverified")}`;
    $("keyStatus").textContent = snapshot.key_configured ? tr("등록됨", "Configured") : tr("없음", "Missing");
    $("editStatus").textContent = snapshot.destination_editable
      ? tr("목적지 저장은 내비 입력 선택을 바꾸지 않습니다.", "Saving a destination does not change the selected source.")
      : tr("선택된 입력 또는 외부 내비가 목적지를 관리합니다. 검색만 가능합니다.", "Destination managed by the selected source or external navigation. Search remains available.");
    // Never promote model-input readiness from a stored destination or PNG.
    $("pipelineStatus").textContent = unavailable;
    $("pipelineReason").textContent = optionalInput;
    controls();
  }

  function waiting(message) {
    $("map").hidden = true;
    $("map").removeAttribute("src");
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = undefined;
    $("mapStatus").textContent = message;
    $("mapStatus").hidden = false;
    $("attribution").hidden = true;
  }

  function invalidateMap() {
    mapGeneration += 1;
    mapController?.abort();
    waiting(tr("지도 상태 확인 중", "Checking map status"));
  }

  async function preview() {
    invalidateMap();
    const generation = mapGeneration;
    if (snapshot?.pipeline_status === "not_connected") {
      waiting(disconnected);
      return;
    }
    mapController = new AbortController();
    try {
      const { reason, blob } = await request("map.png", {}, "map", mapController);
      if (!alive || generation !== mapGeneration) return;
      if (blob?.size) {
        imageUrl = URL.createObjectURL(blob);
        $("map").src = imageUrl;
        $("map").hidden = false;
        $("mapStatus").hidden = true;
        $("attribution").hidden = false;
      } else {
        const reasons = {
          not_connected: disconnected,
          position: tr("현재 GPS 위치 없음", "Current GPS position unavailable"),
          route: tr("경로 지도 없음", "No route map available"),
          offroad: tr("현재 지도 미리보기 없음", "Map preview currently unavailable"),
          tiles: tr("지도 데이터 없음", "Map data unavailable"),
        };
        waiting(reasons[reason] || tr("지도 미리보기 없음", "Map preview unavailable"));
      }
    } catch (_) {
      if (alive && generation === mapGeneration) waiting(failure(0));
    }
  }

  async function refresh() {
    if (!alive || busy) return;
    const generation = ++statusGeneration;
    statusController?.abort();
    statusController = new AbortController();
    snapshot = null;
    controls();
    invalidateMap();
    $("pipelineStatus").textContent = unavailable;
    try {
      const value = await request("destination", {}, "json", statusController);
      if (!alive || generation !== statusGeneration) return;
      renderSnapshot(value);
      await preview();
    } catch (_) {
      if (!alive || generation !== statusGeneration) return;
      $("sourceStatus").textContent = tr("알 수 없음", "Unknown");
      $("keyStatus").textContent = tr("알 수 없음", "Unknown");
      $("current").textContent = tr("저장 상태 미확인", "Saved destination unverified");
      $("editStatus").textContent = tr("상태 확인 전에는 목적지를 변경할 수 없습니다.", "Destination changes are locked until status is verified.");
      $("pipelineReason").textContent = failure(0);
      waiting(failure(0));
    }
  }

  function invalidateSearch() {
    searchGeneration += 1;
    searchController?.abort();
    resultButtons = [];
    $("results").replaceChildren();
    $("results").setAttribute("aria-busy", "false");
  }

  $("query").addEventListener("input", () => {
    invalidateSearch();
    if (!busy) $("status").textContent = "";
  });
  $("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !alive) return;
    invalidateSearch();
    const generation = searchGeneration;
    const query = $("query").value.trim();
    if (query.length < 2 || query.length > 200) { $("status").textContent = failure(400); return; }
    searchController = new AbortController();
    $("results").setAttribute("aria-busy", "true");
    $("status").textContent = tr("검색 중", "Searching");
    try {
      const value = await request(`search?q=${encodeURIComponent(query)}`, {}, "json", searchController);
      if (!alive || generation !== searchGeneration) return;
      if (!Array.isArray(value.results)) throw new Error("invalid results");
      for (const item of value.results.slice(0, 5)) {
        const destination = place(item);
        if (!destination || destination.place_name === "External Navi") continue;
        const row = document.createElement("li"), button = document.createElement("button");
        button.type = "button";
        button.textContent = destination.place_name;
        button.addEventListener("click", () => mutate("POST", destination, generation));
        resultButtons.push(button);
        row.append(button);
        $("results").append(row);
      }
      controls();
      $("status").textContent = resultButtons.length ? tr("검색 결과", "Search results") : tr("검색 결과 없음", "No results");
    } catch (error) {
      if (alive && generation === searchGeneration) $("status").textContent = failure(error.status, "search");
    } finally {
      if (alive && generation === searchGeneration) $("results").setAttribute("aria-busy", "false");
    }
  });

  async function mutate(method, destination, generation) {
    if (!alive || busy || snapshot?.destination_editable !== true
        || (method === "POST" && generation !== searchGeneration)) return;
    // Serialize writes: aborting fetch does not undo a server-side mutation.
    busy = true;
    const write = ++writeGeneration;
    statusGeneration += 1;
    statusController?.abort();
    invalidateSearch();
    invalidateMap();
    controls();
    $("status").textContent = tr("저장 중", "Saving");
    try {
      await request("destination", method === "POST" ? {
        method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(destination),
      } : { method });
      if (!alive || write !== writeGeneration) return;
      $("status").textContent = method === "POST"
        ? tr("목적지만 저장했습니다. N16 경로·지도 입력 미연결.", "Destination saved only. N16 route/map input not connected.")
        : tr("목적지를 삭제했습니다.", "Destination cleared.");
    } catch (error) {
      if (!alive || write !== writeGeneration) return;
      $("status").textContent = error.status ? failure(error.status)
        : tr("저장 결과를 확인할 수 없습니다. 현재 상태를 다시 확인합니다.", "Write outcome unverified. Rechecking the saved destination.");
    } finally {
      // GET is authoritative even after a timeout: the write may have succeeded.
      if (alive && write === writeGeneration) { busy = false; await refresh(); }
    }
  }

  $("clear").addEventListener("click", () => {
    if (snapshot?.destination) return mutate("DELETE");
  });
  $("refresh").addEventListener("click", refresh);
  $("map").addEventListener("error", () => waiting(tr("지도 이미지를 읽을 수 없습니다.", "Map image could not be decoded.")));
  window.addEventListener("pagehide", () => {
    alive = false;
    writeGeneration += 1;
    searchGeneration += 1; statusGeneration += 1; mapGeneration += 1;
    requests.forEach((controller) => controller.abort());
    waiting(disconnected);
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) { alive = true; busy = false; invalidateSearch(); refresh(); }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && alive && !busy) refresh();
  });
  refresh();
})();
