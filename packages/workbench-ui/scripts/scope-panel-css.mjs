import selectorParser from "postcss-selector-parser";

const rootClass = "reqpilotx-panel-root";
const isRoot = node => node.type === "class" && node.value === rootClass;
export function scopePanelSelector(value) {
  return selectorParser(root => root.each(selector => {
    selector.walkPseudos(node => { if (node.value === ":root") node.replaceWith(selectorParser.className({value:rootClass})); });
    selector.walkTags(node => { if (["html","body"].includes(node.value)) node.replaceWith(selectorParser.className({value:rootClass})); });
    selector.walkIds(node => { if (node.value === "root") node.replaceWith(selectorParser.className({value:rootClass})); });
    selector.walkClasses(node => { if (node.value === "workspace-app") node.value = rootClass; });
    let theme;
    if (selector.first?.type === "attribute" && selector.first.attribute === "data-theme") {
      theme = selector.first.clone(); selector.first.remove();
      if (selector.first?.type === "combinator" && !selector.first.value.trim()) selector.first.remove();
    }
    if (!isRoot(selector.first)) {
      selector.prepend(selectorParser.combinator({value:" "}));
      selector.prepend(selectorParser.className({value:rootClass}));
    }
    if (theme) selector.insertAfter(selector.first, theme);
  })).processSync(value);
}

/** Build-time projection from the incumbent stylesheet, never a second editable UI theme. */
export function scopePanelCss(animationNames = []) {
  return { postcssPlugin: "reqpilotx-panel-scope", Once(root) {
    const animations = new Map(animationNames.map(name=>[name,`reqpilotx-${name}`]));
    root.walkAtRules(/keyframes$/, rule => { const name=rule.params.trim(); const scoped=`reqpilotx-${name}`; animations.set(name,scoped); rule.params=scoped; });
    root.walkRules(rule => {
      let parent=rule.parent;
      while(parent) { if(parent.type === "atrule" && /keyframes$/.test(parent.name)) return; parent=parent.parent; }
      rule.selector=scopePanelSelector(rule.selector);
    });
    root.walkDecls(/^(animation|animation-name)$/, declaration => {
      for (const [name,scoped] of animations) declaration.value=declaration.value.replace(new RegExp(`(^|[\\s,])${name.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")}(?=$|[\\s,])`,"g"),`$1${scoped}`);
    });
  } };
}
