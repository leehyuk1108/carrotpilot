// Offline DOM contract tests. No browser, clipboard, network or real key access.
// Run: node openpilot/system/hylink/tests/check_health_page.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const source = fs.readFileSync(path.join(__dirname, '../connect.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../connect.html'), 'utf8');
  const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: '', value: '', disabled: true, hidden: false, open: false, dataset: {}, style: {}, scrollHeight: 100,
    select() { this.selected = true; },
  }]));
  const token = 'wayon_demo_only_' + 'synthetic'.repeat(5);
  const listeners = {}, intervals = [], requests = [], copied = [];
  const normal = {level: 'success', title: '최근 업로드 성공', code: 'ok', detail: '저장 확인', process: 'running',
    lastSuccessAt: '2026-09-30T03:10:00Z', lastAttemptAt: '2026-09-30T03:10:00Z'};
  let health = normal, unavailable = false, enabled = true, connectFails = false;
  const context = vm.createContext({
    document: {hidden: false, getElementById: id => {assert.ok(elements[id], id); return elements[id];},
      execCommand: command => {copied.push(command); return true;}},
    navigator: {clipboard: {writeText: async value => copied.push(value)}},
    window: {isSecureContext: true, addEventListener: (name, fn) => listeners[name] = fn},
    AbortController, AbortSignal, Date, TypeError,
    setTimeout: () => 1, clearTimeout() {}, setInterval: fn => intervals.push(fn),
    fetch: async (url, options) => {
      requests.push({url, options});
      if (url === '/api/health') {
        if (unavailable) throw new TypeError('offline');
        return {ok: true, json: async () => health};
      }
      if (url === '/api/connect' && connectFails) return {ok: false, json: async () => ({error: '등록 실패'})};
      return {ok: true, json: async () => ({ready: true, enabled, key: token})};
    },
  });
  vm.runInContext(source, context);
  const settle = async () => {for (let i = 0; i < 10; i++) await Promise.resolve();};
  const tick = async () => {await intervals[0](); await settle();};
  await settle();
  assert.equal(elements.key.value, token);
  assert.equal(elements.health.dataset.level, 'success');
  assert.equal(elements['health-diagnostics'].open, false);
  assert.equal(requests.filter(r => r.options.method === 'POST').length, 1);
  assert.equal((html.match(/<button/g) || []).length, 1);
  await elements.copy.onclick();
  assert.equal(copied[0], token);
  context.window.isSecureContext = false;
  await elements.copy.onclick();
  assert.equal(copied[1], 'copy');

  health = {...normal, level: 'error', code: 'auth', title: '클라우드 인증 실패', action: '등록 정보 확인',
    httpStatus: 401, retryInSeconds: 30, lastErrorTitle: '인증 실패', lastFailureAt: normal.lastAttemptAt};
  await tick();
  assert.equal(elements.health.dataset.level, 'error');
  assert.equal(elements['health-http'].textContent, '401');
  assert.equal(elements['health-diagnostics'].open, true);
  assert.equal(elements['health-action'].hidden, false);
  elements['health-diagnostics'].open = false; // Respect a user's disclosure choice while same error continues.
  await tick();
  assert.equal(elements['health-diagnostics'].open, false);
  health = {...health, code: 'payload', title: '<img src=x onerror=attack()>', httpStatus: null};
  await tick();
  assert.equal(elements['health-title'].textContent, health.title); // textContent, never HTML injection.
  assert.equal(elements['health-http'].textContent, '—');

  health = {...normal, historyOnly: true};
  await tick();
  assert.equal(elements['health-history'].hidden, false);
  unavailable = true;
  await tick();
  assert.equal(elements.health.dataset.level, 'warning');
  assert.equal(elements['health-success'].textContent, '—'); // Never leave old green success after disconnect.
  assert.equal(elements['health-history'].hidden, true);
  assert.equal(elements['health-action'].hidden, true);
  unavailable = false;
  health = normal;
  await tick();
  assert.equal(elements.health.dataset.level, 'success');

  context.document.hidden = true;
  const count = requests.length;
  await tick();
  assert.equal(requests.length, count);
  context.document.hidden = false;
  enabled = false;
  await tick();
  assert.equal(elements.key.value, '');
  assert.equal(elements.copy.disabled, true);
  await tick();
  assert.equal(requests.filter(r => r.options.method === 'POST').length, 1);
  assert.ok(requests.every(r => r.url.startsWith('/api/') && r.options.cache === 'no-store'));

  enabled = true;
  connectFails = true;
  await vm.runInContext('connect()', context);
  await settle();
  assert.equal(elements.key.value, '');
  assert.equal(elements.copy.disabled, true);
  assert.equal(elements.status.textContent, '등록 실패');
  assert.equal(elements.health.dataset.level, 'success'); // Diagnostics still work when key loading fails.
  listeners.pagehide();
  assert.equal(elements['health-success'].textContent, '—');
  assert.equal(elements.copy.disabled, true);
  console.log('PASS: health success/error/recovery/history, privacy, disclosure, copy, hidden-page pause and read-only polling');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
