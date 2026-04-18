# Active Context

更新时间：2026-04-19 02:06 CST
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 保持 `main` 与 `codex/extraction-quality-loop` 已收敛的稳定基线；只有出现新的 extraction 回归时才重新打开专项修复。
- 当前仓库治理目标是“最新代码 + 最小必要评测资产 + 长期规则台账”；运行产物、截图、日志、临时 staged docs 不再进入版本库。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/New project`
- 对照/回滚：`codex/extraction-quality-loop` -> `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop`
- 其余本地支线/worktree 本轮统一归档后删除，不再作为日常开发入口。

## 当前样本 / 保留工件

- `prototype/eval/extraction_batches/seres_mda_20260418_j02311_fix13`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260418_j02311_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260418_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260418_fix14`
- `prototype/eval/extraction_diagnose/j02_311_20260419_fix15`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- PR `#2` 已将 `codex/extraction-quality-loop` 合入 `main`；本地 `main` 已同步到合并结果。
- `preview_generation_id` 已打通 `db / main / preview_cache / exporter / app.js` 的 preview/export coherence，旧 generation 视觉资产不再跨代回流。
- 仓库瘦身已从 HEAD 删除 tracked `storage/`、`prototype/storage/table_snapshots/`、旧 extraction batch/diagnose 工件，并补强 `.gitignore` 规则。

## 当前 blockers

- 无新的主线 blocker。
- `J02.311` 仍保留 focused residual：`7.3 表10` 目前还是“文字 + 原文截图”，`prototype/eval/extraction_diagnose/j02_311_20260419_fix15` 的 `requirements_drift = 17` 仍未归零。
- 如果 GitHub 仓库总体体积在这轮 cleanup 后仍不可接受，需要单开历史重写（`git filter-repo`）；本轮不做破坏式改历史。

## 下一步

- 只有在发现新的 extraction 回归时，再从 `codex/extraction-quality-loop` 继续做 targeted fix、focused diagnose 和 full batch rerun。
- 若未来重开 `release / rag / product-kb / angular` 轨道，先重建对应 branch/worktree，再把 `track_registry.json` 和 `progress_tracker.json` 切回活跃态。
- 若要继续压缩远端历史体积，单开 history-rewrite 方案，不与当前主线继续混做。

## 活跃端口

- 无固定活跃端口；`5555` 仅在 extraction live review 复核时按需启动。
