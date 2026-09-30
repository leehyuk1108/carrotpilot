'use strict';
const key = document.getElementById('key');
const copy = document.getElementById('copy');
const reissue = document.getElementById('reissue');
const status = document.getElementById('status');
let pending = null;
let checking = false;
let healthRequest = null;
let healthCode = null;
let keyEpoch = 0;

function setText(id, value) {
  const element = document.getElementById(id);
  if (element.textContent !== value) element.textContent = value;
}
function healthUnavailable(message) {
  document.getElementById('health').dataset.level = 'warning';
  setText('health-title', '전송 상태를 확인할 수 없어요');
  setText('health-detail', message);
  document.getElementById('health-action').hidden = true;
  document.getElementById('health-history').hidden = true;
  for (const id of ['success', 'attempt', 'process', 'code', 'error', 'http', 'retry']) setText('health-' + id, '—');
}
function healthTime(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString('ko-KR') : '아직 확인되지 않음';
}
function renderHealth(data) {
  if (!['success', 'waiting', 'warning', 'error'].includes(data.level) || typeof data.title !== 'string') throw new Error('invalid health');
  document.getElementById('health').dataset.level = data.level;
  setText('health-title', data.title);
  setText('health-detail', data.detail || '');
  setText('health-action', data.action || '');
  document.getElementById('health-action').hidden = !data.action;
  setText('health-success', healthTime(data.lastSuccessAt));
  setText('health-attempt', healthTime(data.lastAttemptAt));
  document.getElementById('health-history').hidden = !data.historyOnly || !data.lastSuccessAt;
  setText('health-process', data.process === 'running' ? '최근 동작 확인됨' : '최근 동작 확인 안 됨');
  setText('health-code', data.code || '—');
  setText('health-error', data.lastErrorTitle ? data.lastErrorTitle + ' · ' + healthTime(data.lastFailureAt) : '기록된 오류 없음');
  setText('health-http', data.httpStatus ? String(data.httpStatus) : '—');
  setText('health-retry', typeof data.retryInSeconds === 'number' ? (data.retryInSeconds ? `약 ${data.retryInSeconds}초 후` : '재시도 대기 중') : '—');
  if (data.code !== healthCode) document.getElementById('health-diagnostics').open = data.level === 'error';
  healthCode = data.code;
}
async function refreshHealth() {
  if (healthRequest || document.hidden) return;
  const controller = new AbortController();
  healthRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch('/api/health', {headers: {'X-Hylink-Request': '1'}, cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (healthRequest === controller && !controller.signal.aborted) renderHealth(data);
  } catch {
    if (healthRequest === controller) healthUnavailable('시동을 끈 상태와 콤마의 같은 Wi-Fi 연결을 확인해 주세요.');
  } finally {
    clearTimeout(timeout);
    if (healthRequest === controller) healthRequest = null;
  }
}

function resizeKey() {
  key.style.height = 'auto';
  key.style.height = key.scrollHeight + 'px';
}
function clearKey(message = '') {
  key.value = '';
  copy.disabled = true;
  copy.textContent = '키 복사';
  status.textContent = message;
}
async function connect(replacing = false) {
  if (pending) return;
  keyEpoch++;
  const controller = new AbortController();
  pending = controller;
  reissue.disabled = true;
  reissue.textContent = replacing ? '재발급 중…' : '키 재발급';
  clearKey(replacing ? '이 콤마를 확인하고 새 키로 연결하고 있어요…' : '키를 불러오는 중…');
  const timeout = setTimeout(() => controller.abort(), 25000);
  try {
    // Same-origin POST activates defaults; GET and browser prefetch never do.
    const response = await fetch(replacing ? '/api/reissue' : '/api/connect', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Hylink-Request': '1'},
      cache: 'no-store', body: '{}', signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '연결하지 못했어요. 새로고침해 주세요.');
    if (!data.ready || !data.enabled || !/^wayon_[A-Za-z0-9_-]{32,128}$/.test(data.key)) {
      throw new Error('키를 준비하지 못했어요. 새로고침해 주세요.');
    }
    if (controller.signal.aborted || pending !== controller) return;
    key.value = data.key;
    copy.disabled = false;
    status.textContent = replacing ? '새 키로 교체했어요. 키를 복사해 앱에 다시 입력해 주세요.' : '';
    resizeKey();
  } catch (error) {
    if (pending === controller) clearKey(error.name === 'AbortError' || error instanceof TypeError
      ? '처리 결과를 확인하지 못했어요. 연결을 확인하고 페이지를 다시 열어 주세요.' : error.message);
  } finally {
    clearTimeout(timeout);
    if (pending === controller) {
      pending = null;
      reissue.disabled = false;
      reissue.textContent = '키 재발급';
    }
    refreshHealth();
  }
}
reissue.onclick = () => { if (!reissue.disabled) return connect(true); };
copy.onclick = async () => {
  if (!key.value || copy.disabled) return;
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(key.value);
    else { key.select(); if (!document.execCommand('copy')) throw new Error('copy'); }
    copy.textContent = '복사됨';
    status.textContent = '키를 복사했어요.';
  } catch {
    key.select();
    status.textContent = '키를 길게 눌러 복사해 주세요.';
  }
};
window.addEventListener('resize', resizeKey);
window.addEventListener('pagehide', () => {
  keyEpoch++;
  reissue.disabled = true;
  const request = pending;
  pending = null;
  request?.abort();
  clearKey();
  const health = healthRequest;
  healthRequest = null;
  health?.abort();
  healthUnavailable('페이지를 다시 열면 현재 상태를 확인해요.');
});
window.addEventListener('pageshow', event => { if (event.persisted) { connect(); refreshHealth(); } });
refreshHealth();
connect();
// Read only: a stopped connection stays stopped until the page is reopened.
setInterval(async () => {
  if (document.hidden || pending || checking) return;
  refreshHealth();
  if (!key.value) return;
  checking = true;
  const epoch = keyEpoch;
  try {
    const response = await fetch('/api/key', {
      headers: {'X-Hylink-Request': '1'}, cache: 'no-store', signal: AbortSignal.timeout(2500),
    });
    const data = await response.json();
    if (epoch !== keyEpoch) return;
    if (!response.ok || !data.ready || !data.enabled || data.key !== key.value) throw new Error('closed');
  } catch {
    if (epoch === keyEpoch) clearKey('연결이 종료됐어요. 시동을 끄고 같은 Wi-Fi에서 새로고침해 주세요.');
  } finally { checking = false; }
}, 5000);
