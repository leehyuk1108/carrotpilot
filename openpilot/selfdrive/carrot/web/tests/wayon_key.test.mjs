import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("the key panel is wired into the device tab before its binder", () => {
  const html = read("index.html");
  assert.ok(html.indexOf("/js/pages/wayon_key.js") > 0);
  assert.ok(html.indexOf("/js/pages/wayon_key.js") < html.indexOf("/js/pages/setting_device_actions.js"));
  assert.match(read("js/pages/setting_device_config.js"), /id: "WayonKey", labelKey: "device_group_wayon_key"/);
  assert.match(read("js/pages/setting_device.js"), /groupId === "WayonKey"\) return renderWayonKeyPanel\(\)/);
  assert.match(read("js/pages/setting_device_actions.js"), /#wayonKeyPanel"\)\) bindWayonKeyPanel\(container\)/);
  for (const lang of ["ko", "en", "zh"]) assert.match(read(`js/translations/${lang}.js`), /device_group_wayon_key: "/);
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function panelContext({ secure = false, clipboardOk = true, execOk = false, reveal = null, clipboard = null, framed = false } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { id, textContent: "", value: "", hidden: true, disabled: false, focus() {}, select() {} });
    return elements.get(id);
  };
  const timers = [];
  const posted = [];
  const copied = [];
  const listeners = { document: {}, window: {} };
  const on = (target) => (type, fn) => { (listeners[target][type] ||= []).push(fn); };
  const self = {};
  const document = {
    hidden: false, addEventListener: on("document"), getElementById: element,
    createElement: () => ({ value: "", style: {}, setAttribute() {}, select() {}, remove() {} }),
    body: { appendChild() {} },
    execCommand: () => execOk,
  };
  const context = vm.createContext({
    window: { isSecureContext: secure, addEventListener: on("window"), self, top: framed ? {} : self },
    navigator: { clipboard: { writeText: async (text) => {
      if (clipboard) await clipboard.promise;
      if (!clipboardOk) throw new Error("denied");
      copied.push(text);
    } } },
    document,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
    appConfirm: async () => true,
    getJson: async () => ({ ok: true, configured: true, device_id: "dev-1", endpoint_host: "cloud.example.test", key_masked: "wyn_…9z9z", key_length: 52, csrf: "tok" }),
    requestJson: async (url, options) => {
      posted.push({ url, options });
      if (reveal) await reveal.promise;
      return { ok: true, key: "SECRET-KEY-VALUE", device_id: "dev-1" };
    },
  });
  vm.runInContext(read("js/pages/wayon_key.js"), context);
  const fire = (target, type) => (listeners[target][type] || []).forEach((fn) => fn({ type }));
  const hideTab = () => { document.hidden = true; fire("document", "visibilitychange"); };
  return { context, element, timers, posted, copied, fire, hideTab };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("status shows only the masked key and enables copying", async () => {
  const { context, element } = panelContext();
  await context.refreshWayonKey();
  assert.equal(element("wayonKeyMasked").textContent, "wyn_…9z9z (52자)");
  assert.equal(element("wayonKeyDeviceId").textContent, "dev-1");
  assert.equal(element("wayonKeyCopy").disabled, false);
  for (const [, el] of [["", element("wayonKeyMasked")], ["", element("wayonKeyReveal")]]) {
    assert.doesNotMatch(`${el.textContent}${el.value}`, /SECRET/);
  }
});

test("a missing key disables the buttons and says why", () => {
  const { context, element } = panelContext();
  context.updateWayonKeyPanel({ ok: true, configured: false, reason: "missing", csrf: "tok" });
  assert.equal(element("wayonKeyCopy").disabled, true);
  assert.equal(element("wayonKeyCopyId").disabled, true);
  assert.match(element("wayonKeyError").textContent, /설정돼 있지 않습니다/);
});

test("copy sends the CSRF token with confirmation and never leaves the key on screen when copying works", async () => {
  const { context, element, posted, copied } = panelContext({ secure: true });
  await context.refreshWayonKey();
  await context.copyWayonKey();
  assert.equal(posted[0].url, "/api/wayon_key/reveal");
  assert.equal(posted[0].options.headers["X-Carrot-CSRF"], "tok");
  assert.deepEqual(JSON.parse(posted[0].options.body), { confirmed: true });
  assert.deepEqual(copied, ["SECRET-KEY-VALUE"]);
  assert.equal(element("wayonKeyReveal").value, "");
  assert.equal(element("wayonKeyReveal").hidden, true);
});

test("on plain http without copy support the key is shown for 60 s, then cleared", async () => {
  const { context, element, timers } = panelContext({ secure: false, execOk: false });
  await context.refreshWayonKey();
  await context.copyWayonKey();
  assert.equal(element("wayonKeyReveal").value, "SECRET-KEY-VALUE");
  assert.equal(element("wayonKeyReveal").hidden, false);
  const hide = timers.find((t) => t.ms === 60000);
  assert.ok(hide);
  hide.fn();
  assert.equal(element("wayonKeyReveal").value, "");
  assert.equal(element("wayonKeyReveal").hidden, true);
});

test("a reveal that answers after the tab was hidden is dropped, not shown", async () => {
  const reveal = deferred();
  const { context, element, copied, hideTab } = panelContext({ secure: false, execOk: false, reveal });
  await context.refreshWayonKey();
  const pending = context.copyWayonKey();
  await settle();
  hideTab();
  reveal.resolve();
  await pending;
  assert.equal(element("wayonKeyReveal").value, "");
  assert.equal(element("wayonKeyReveal").hidden, true);
  assert.deepEqual(copied, []);
});

test("a failed clipboard write that finishes after the tab was hidden does not fall back to showing the key", async () => {
  const clipboard = deferred();
  const { context, element, posted, hideTab } = panelContext({ secure: true, clipboardOk: false, execOk: false, clipboard });
  await context.refreshWayonKey();
  const pending = context.copyWayonKey();
  await settle();
  assert.equal(posted.length, 1);
  hideTab();
  clipboard.resolve();
  await pending;
  assert.equal(element("wayonKeyReveal").value, "");
  assert.equal(element("wayonKeyReveal").hidden, true);
});

test("leaving the page inside the app clears a shown key and drops a reveal in flight", async () => {
  const shown = panelContext({ secure: false, execOk: false });
  await shown.context.refreshWayonKey();
  await shown.context.copyWayonKey();
  assert.equal(shown.element("wayonKeyReveal").value, "SECRET-KEY-VALUE");
  shown.fire("window", "carrot:pagechange");
  assert.equal(shown.element("wayonKeyReveal").value, "");

  const reveal = deferred();
  const late = panelContext({ secure: false, execOk: false, reveal });
  await late.context.refreshWayonKey();
  const pending = late.context.copyWayonKey();
  await settle();
  late.fire("window", "pagehide");
  reveal.resolve();
  await pending;
  assert.equal(late.element("wayonKeyReveal").value, "");
});

test("Carrot Web framed by another page refuses to fetch the key", async () => {
  const { context, posted } = panelContext({ framed: true });
  await context.refreshWayonKey();
  await assert.rejects(context.copyWayonKey(), /다른 화면 안에/);
  assert.equal(posted.length, 0);
});
