# Pair工具Schema发现能力核对

更新：2026-09-14。只读核对当前Pair源码与包说明；未改变harness、eval配置、工具集合或运行模型。评测线提供的16个MCP工具、51986紧凑JSON字符为待进一步核对请求投影的体积观察，不是token数或耗时归因。

## 已有能力

| 入口 | 实际行为 | 对本需求的限制 |
|---|---|---|
| `packages/mcp/mcp-client/src/tools.ts`的`listToolsUncached`及发现/注册循环 | 分页获取MCP tools/list，将每项inputSchema注册到ctx.tools；list_changed触发全量代际同步 | 这是运行时发现，不是模型按需搜索/展开schema |
| `packages/core/tools/src/index.ts`的`schemas(scope)` | 从当前scope全部visible工具投影name/description/parameters | native模式不延迟schema投影 |
| 同文件`presentAs`、`sdkSection`、`sdkSchemas`；`agent-tool-presentation` preset插件 | native / ptc / both；PTC只暴露run_code函数，完整可见工具转为生成SDK文本；内层调用仍走统一执行与校验 | 需要codeRuntime；仍有完整SDK说明成本，不能承诺比native更省，也不是检索式加载 |
| `ctx.tools.restrict(filter)`及scope注册/dispose | 按作用域限制继承的可见工具，限制可随dispose解除 | 可见性与执行解析关联；不是只隐藏说明、保持全部工具随时可调用。不能直接当成本任务的无损优化 |

关键依据：`tools/src/index.ts:1224`逐一投影visible schemas；`:1229`从全部可见工具生成SDK；`:867`把完整SDK renderer结果放入system prompt。`mcp-client/README.md`说明注册工具的描述/schema持续进入请求，恢复同一工具集合可保持前缀稳定。`tools/README.md`明确PTC以SDK文本替代终端工具schema，不承诺普遍压缩。

## 当前结论

在已检查的core/tools、MCP桥、工具展示preset及LLM接入中，未发现现成的模型侧tool-search/deferred-schema机制。不能将MCP发现、Client bundle惰性加载、技能按需阅读混同为工具schema按需加载。若后续需要新机制，另作明确设计；本轮不新建。

优先后续证据应为同一scope实际native工具投影、PTC生成SDK及请求token统计，分别看固定输入、缓存命中、首输出等待、流式时间及调用轮数。先做离线投影核对，不能凭51986字符断言模型输入或耗时由schema主导；本次也没有运行新的投影实验或模型比较。
