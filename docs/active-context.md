# Active Context

更新时间：2026-03-12  
适用仓库：`/Users/liuchang/Documents/New project`

这份文档用于压缩会话上下文，给后续 Codex 新窗口/新线程做快速接续。  
长期背景看：`/Users/liuchang/Documents/New project/docs/项目.md`  
短期状态看：本文件。

## 1. 当前项目目标

当前阶段的最高优先级只有一个：

1. `条目拆分准确`

具体要求：

- 不重复
- 不前后错乱
- 章节归属正确
- 表格识别正常
- 人工审阅时能信任原文/原图

## 2. 当前主线与分支

主仓库：

- `main`

活跃 worktree：

1. `codex/table-assets-mainline`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-assets-mainline`
   - 职责：表格资产化主线，Web/Excel 优先消费同一份表格资产

2. `codex/workspace-stabilization`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/workspace-stabilization`
   - 职责：统一工作台稳定性、性能、虚拟滚动、Windows 卡顿

3. `codex/table-asset-lifecycle`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-asset-lifecycle`
   - 职责：保留原图预生成，但做资产瘦身、去重、保留策略、生命周期管理

4. `codex/extraction-optimization`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization`
   - 职责：文档拆解精度专项，当前重点样本是“油泵 PDS”

## 3. 当前真实重点样本

当前正在修的文档：

- `/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

当前专项重点：

- `3.2.1 产品信息`
- `3.2.2 产品详细描述`
- 后半段内嵌子文档流（重新从 `1/2/3` 编号开始）

## 4. 当前已确认的问题与状态

### 已修

1. 远距离重复编号全局合并
   - 后半段局部 `1/2/3` 不再吞掉主文前半段

2. 扁平化表格行误升章节
   - 如 `2 油口接头接口根据需求进行定义` 不再错误挂到 `2 供应商能力要求及设计责任`

3. 假表头
   - 已禁止通用测试矩阵假表头 fallback
   - 不允许再出现“序号/项目/参考标准/试验方法/DV-PV/责任方...”这类不属于原文的表头

4. `3.2.1` 一张表被切成多张
   - 当前已经恢复为 1 张表
   - 表头已恢复为真实字段

### 正在修

1. `3.2.2` 复杂混排
   - `1）油泵性能要求`
   - `2）控制响应性能要求`
   - `5）油管总成性能要求`
   - `6.6.1 ~ 6.6.5`
   - 这些条目需要继续稳住“标题 + 图/表 + 正文”的边界

2. 后半段 `SUB1` 子文档流里的列表项误升章节

3. 复杂表在 Web/Excel 中的同源展示

## 5. 当前明确的产品/工程决策

1. 人类展示优先信任原文截图/原表切片
2. AI 不读截图，AI 读结构化切片文本
3. 条目集锁定后才能导出和跑 AI
4. 锁定后继续编辑会自动解锁
5. 假表头禁止
6. 当前阶段优先级：拆解准确 > 表格花哨渲染 > AI 判定

## 6. 上下文压缩工作法

建议以后都按两层文档维护：

1. 长期背景文档
   - `docs/项目.md`
   - 放架构、数据流、模块、长期计划

2. 滚动上下文文档
   - `docs/active-context.md`
   - 只放：
     - 当前样本
     - 当前问题
     - 最近已修
     - 下一步
     - 当前分支/端口/路径

控制规则：

- 长期文档：少改，只在架构或主流程发生变化时更新
- 滚动文档：每次阶段性修复后更新，保持在 150 行内
- 新窗口接手时，先看 `docs/active-context.md`，再看对应分支下的专项上下文
- 仓库已加 `$context-sync` skill + pre-commit 门禁，代码/核心文档变更时应同步更新至少一个 active-context 文件

## 7. 新窗口/新线程怎么接续

推荐方式：

1. 打开对应 worktree
2. 先读：
   - `/Users/liuchang/Documents/New project/docs/active-context.md`
   - 对应分支下的专项文档
3. 再看 `git status`
4. 最后启动服务/执行测试

示例：

- 拆解优化窗口：
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization`
  - 先看：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/docs/extraction-active-context.md`

## 8. 当前端口

- `8015`
  - 分支：`codex/extraction-optimization`
  - 用途：油泵 PDS 拆解优化测试

## 9. 下一步

1. 继续收 `3.2.2`
   - 保证直到 `3.3` 前的内容都正确归属到 `3.2.2`
   - 重点压 `1）/2）/5）` 与 `6.6.1~6.6.5`

2. 继续消掉 `SUB1` 作用域内的列表项误升章节

3. 在稳定后，把有效修复回灌到：
   - `codex/table-assets-mainline`
   - 再决定是否回主线
