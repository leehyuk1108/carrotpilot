import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const catalog = JSON.parse(await readFile(new URL("../../../carrot_settings.json", import.meta.url), "utf8"));
const keys = await readFile(new URL("../../../../common/params_keys.h", import.meta.url), "utf8");

test("Navdy ambient dark-off is an off-by-default on/off select in the external HUD menu", () => {
  const setting = catalog.params.find((item) => item.name === "NavdyAmbientDarkOff");
  assert.ok(setting);
  assert.equal(setting.default, 0);
  assert.equal(setting.min, 0);
  assert.equal(setting.max, 1);
  assert.equal(setting.control, "select");
  for (const lang of ["ko", "en", "zh"]) assert.equal(setting.options[lang].length, 2);
  for (const field of ["title", "descr", "etitle", "edescr", "ctitle", "cdescr"]) assert.ok(setting[field]);
  const findGroup = (groups, id) => groups.find((group) => group.id === id)
    || groups.map((group) => findGroup(group.groups || [], id)).find(Boolean);
  assert.ok(findGroup(catalog.menu, "HUD_BASIC").params.includes(setting.name));
  assert.match(keys, /\{"NavdyAmbientDarkOff", \{PERSISTENT, BOOL, "0"\}\}/);
});
