# 全类型证据盘点（已退出当前主线）

2026-09-04产品负责人纠正范围：用户入口只有客户需求文档与内部知识，格式与入口正交；当前目标是逐份完成
主动减振器PDS、F06、E01、QC/T、PEF、QJLY六份典型案例。本页面保留17类探索、问题和反馈作为参考，
不再代表目标态完成度，也不要求补齐23项决定。

读取私有`type-review.json`，让产品负责人逐项检查六个跨类型决策与十七类文档代理候选。
它只显示已经形成的来源、结果、问题和真实渲染预览，不进行提取或模型调用。

## 启动

```bash
python3 prototype/type_review/server.py \
  --campaign-dir /absolute/path/to/private-campaign \
  --port 8780
```

服务只绑定`127.0.0.1`。代理建议和人工决定分开；接受一个对象不会确认其它对象。
`需要调整`、`暂缓决定`和`仅记录问题`必须填写原因。所有事件追加到私有
`owner-feedback/decisions.jsonl`并绑定候选revision；模拟交互请使用独立目录和`--reviewer-kind qa`。

预览只允许读取工件中登记、位于campaign目录内的PNG；不能通过URL读取其它本机文件。
完整报告位于`/report.md`，`/api/export`导出人工状态与事件，不包含写入令牌。

## 验证

```bash
python3 -m unittest discover -s prototype/type_review -p 'test_*.py'
node --test prototype/type_review/test_review_core.mjs
ruff check prototype/type_review
git diff --check
```

真实浏览器还需检查1280px和1024px、搜索/筛选、键盘队列、预览切换、接受/调整/暂缓、刷新和撤销。
QA目录不能记作产品负责人决定；自动测试也不证明各类型业务结论已经接受。
