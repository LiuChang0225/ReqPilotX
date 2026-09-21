import { strict as assert } from "node:assert";
import { test } from "node:test";
import postcss from "postcss";
import { scopePanelCss, scopePanelSelector } from "./scope-panel-css.mjs";

test("all selector branches stay under the plugin root, including nested selector lists", () => {
  assert.equal(scopePanelSelector("button, input:not([type=checkbox]), .row:is(.a, .b)"), ".reqpilotx-panel-root button,.reqpilotx-panel-root  input:not([type=checkbox]),.reqpilotx-panel-root  .row:is(.a, .b)");
});
test("global theme and app-root rules target the panel rather than its host", () => {
  assert.equal(scopePanelSelector(":root[data-theme=dark]"),".reqpilotx-panel-root[data-theme=dark]");
  assert.equal(scopePanelSelector("[data-theme=dark] .workspace-app .button"),".reqpilotx-panel-root[data-theme=dark] .button");
  assert.equal(scopePanelSelector("body"),".reqpilotx-panel-root");
});
test("animation names are isolated without treating keyframes as ordinary selectors", async () => {
  const {css} = await postcss([scopePanelCss()]).process("@keyframes spin {from {opacity:0} to {opacity:1}} .x {animation:spin 1s linear, pulse 2s} @keyframes pulse {to {opacity:.5}}",{from:undefined});
  assert.match(css, /@keyframes reqpilotx-spin/); assert.match(css,/animation:reqpilotx-spin 1s linear, reqpilotx-pulse 2s/);
  assert.match(css,/from \{opacity:0\}/); assert.match(css,/\.reqpilotx-panel-root \.x/);
});
test("references to keyframes defined in another shared stylesheet are also scoped",async()=>{
  const {css}=await postcss([scopePanelCss(["spin"])]).process(".loading {animation:spin 1s linear infinite}",{from:undefined});
  assert.match(css,/animation:reqpilotx-spin 1s/);
});
