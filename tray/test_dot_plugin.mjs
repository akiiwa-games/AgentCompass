import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { actionExpression, findControl } from "./dot_plugin.mjs";

const button = (text, attrs = {}) => ({
  innerText: text, getAttribute: (key) => attrs[key] ?? null,
});
const root = (controls, selectors = {}) => ({
  querySelectorAll: (selector) => selectors[selector] ?? (selector === "button, [role=button]" ? controls : []),
});

test("stable attributes work without an English or Chinese label", () => {
  const add = button("Ajouter");
  const document = root([button("添加"), add], {"button[data-testid=add-plugin]": [add]});
  assert.equal(vm.runInNewContext(actionExpression("add"), {document}), add);
});

test("accessible English and Chinese names are explicit fallbacks", () => {
  for (const label of ["Add", "添加"]) {
    const add = button("", {"aria-label": label});
    assert.equal(vm.runInNewContext(actionExpression("add"), {document: root([add])}), add);
  }
  const send = button("", {"aria-label": "Send message"});
  assert.equal(vm.runInNewContext(actionExpression("send"), {document: root([send])}), send);
});

test("a named connect action stays within the dialog", () => {
  const connect = button("Connect Example");
  const dialog = root([connect]);
  const document = {querySelector: () => dialog};
  assert.equal(vm.runInNewContext(actionExpression("connect", 'document.querySelector("[role=dialog]")', "Example"), {document}), connect);
});

test("ambiguous and disabled actions are refused", () => {
  assert.equal(findControl(root([button("Delete"), button("Delete")]), [], ["Delete"]), null);
  assert.equal(findControl(root([button("Delete", {"aria-disabled": "true"})]), [], ["Delete"]), null);
  assert.equal(findControl(null, [], ["Delete"]), null);
});
