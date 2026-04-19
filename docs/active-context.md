# Active Context

更新时间：2026-04-19 09:19 CST
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 当前活跃 extraction 工作仍在 `codex/extraction-quality-loop`，重点是把 `J02.311` 的 mixed alpha/numeric clause 漂移做成 benchmark-safe 通用规则，而不是继续样本级补丁。
- 长期 SERES 规则账本继续收敛到 `prototype/docs/seres-mda-rule-fix-context.md`。
- 仓库治理继续执行“最新代码 + 最小必要 batch/diagnose 摘要 + 长期规则台账”；runtime export、截图、日志、旧 fidelity/regression 中间产物不再保留在 Git。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/New project`
- extraction 回归入口：`codex/extraction-quality-loop` -> `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop`

## 当前样本 / 保留工件

- `prototype/eval/extraction_batches/seres_mda_20260418_j02311_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260419_fix21`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- `prototype/app/services/extractor.py`
  - 显式 `图/表` 标题不再被 title-only main-body recovery 误恢复成 `1.1` 这类伪 child section。
- `prototype/app/services/extraction_preview.py`
  - mixed alpha/numeric clause 现在会先拆 numeric row 内嵌的 alpha tail，再把父节 inline alpha 前缀 canonicalize 成同序 alpha row，并对同 anchor 重复 alpha label 去重。
  - 修复生成的 alpha row 现在记为 `HYBRID`，不再抬高 requirement benchmark；显式 visual asset repair 也已放宽到 `HYBRID`。
- `J02.311` 真实本地重建已确认：
  - `section 1` 不再生成 `1.1`，`c）` 已重新挂回 `图1平台需求与ECU 需求的区别` source snapshot。
  - `5.11.3` 仍保持 inline alpha，不会被无故拆成独立行。
  - `section 6.2.2` 已稳定为 `父节引导 -> a/b/c/d -> 1~6 -> e/f/g/h`。
  - `section 8.10.4` 已稳定为 `父节引导 -> a -> numeric -> b -> numeric -> ...`，不再把下一组 alpha 吞进上一条 numeric 尾巴。
  - 修后扫描 `residual_embedded_alpha_tail = 0`，focused diagnose `fix21` 为 `requirements_count = 332`、`requirements_drift = -1`。
- 回归验证：
  - `python3 -m unittest prototype.tests.test_extractor_sections -q` -> `69 tests / OK`
  - `python3 -m unittest prototype.tests.test_extraction_preview -q` -> `47 tests / OK`
- 仓库保留工件已收口到当前 batch/diagnose 摘要；`prototype/eval/fidelity/`、`prototype/eval/regression_suite/`、`prototype/eval/iterations/`、本地 DB/log/export 等旧中间产物从 HEAD 移除。
- live 复验：
  - `5555` 已从 `codex/extraction-quality-loop` worktree 重启，当前 PID `98277`。
  - `J02.311` 已重新 `refresh-preview`，live `preview_generation_id = 921d2e83-6ee3-4b2d-adc0-520b866646d6`，`preview_generated_at = 2026-04-19T01:18:51.851267+00:00`，API 返回 `READY` 且 `review_entries_count = 720`。

## 当前 blockers

- `J02.311` 的 `7.3 表10` 仍然只是“正确文字 + 原文截图”，尚未恢复成结构表。
- `J02.311` 仍保留 `parser_low_confidence_hotspot` 页：`1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`。
- 如果前端仍显示旧态，优先怀疑浏览器页面/本地会话缓存，而不是旧后端进程；当前 live generation 已切到 `921d2e83-6ee3-4b2d-adc0-520b866646d6`。

## 下一步

- 如果继续 extraction 复核，直接基于当前 `5555` live generation 做前端 spot-check，确认 `section 1 / 5.11.3 / 6.2.2 / 8.10.4` 与 `fix21` 本地重建一致。
- 然后只盯 `7.3 表10` 与 parser 低置信页，不再回退到 `6.2.2 / 5.11.3 / section 1` 这组已收口问题。

## 活跃端口

- `5555` 当前正在运行，来源 worktree：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype`
