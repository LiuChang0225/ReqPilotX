# RAG Quality Active Context

更新时间：2026-07-15 CST
分支：`codex/rag-quality-loop`

## 分支目的

- 建立 requirement -> evidence 的标注与评测闭环
- 在不破坏现有 Azure 基线的前提下调优召回与判断质量

## 当前关注

- control tower / trackctl / skill 已就位，等待该支线真正开工
- `sample10` 基线
- labeled evidence set
- evidence hit / wrong evidence / missing evidence 指标
- IRPS 产品真实语料的本地 preflight 与隔离 Azure baseline 已完成：客户 SOR、内部 Test Spec、Application Note、Drawing、Specification 已完成文件级盘点；每轮 Azure 运行均以 run-specific source-doc ID 上传并在结束后清理。
- 初步分流：客户技术要求 Excel / 项目 SOR 作为 requirement source；内部 Test Spec 和产品信息规格书作为权威 evidence source；合规矩阵与供应商偏离表仅作评测标注真相源，不得检索；图纸先单列为视觉/OCR 专项；同名 AE 通用信息 PDF 在 Application Note 与 Specification 中完全重复，必须仅保留一份。
- `prototype/scripts/irps_rag_eval.py` 已提供 `--preflight-only`：对提供的 `--source-root` 生成本地 `irps_rag_corpus_preflight.json`，核验文件存在性、SHA-256 duplicate、ingestion scope 和 source-root containment；不会加载 Azure profile、解析文档或上传。正常 evaluation 将在 Azure profile 选择前执行同一 preflight 并 fail closed。
- 真实 corpus preflight：11/11 文件存在，Application Note 与 Specification 下同名 AE PDF 的 SHA-256 已验证一致；仅产品 DOCX 与两份跨项目 Test Spec 参与检索。
- Azure baseline：`recall_at_1 = recall_at_3 = mrr = 0.857143`（6/7 current gold cases），cleanup 已完成。首轮 $0.0$ 由 evaluator 忽略 shared chunk builder 所需的 `extracted_text` 引起；修复后建立非空 chunk 回归测试。
- `IRPS-DEEPAL-003` 需要修正标注：5 V 与 4.5-5.5 V tolerance 只存在于 CSP31/TQ250 跨项目 Test Spec，产品 DOCX 不提供这个项目数值，因此不能要求 generic product DOCX 作为 direct gold。`IRPS-DEEPAL-008` 的图纸洁净度要求是当前 visual-only false positive，应由视觉/图纸路由截获，不能作为文本 RAG 结论。

## 当前 blocker

- 对应 worktree 还没创建，控制塔当前会把这条 active track 保持在 `TODO / ON_PLAN / not_started`
- `IRPS-DEEPAL-003` 的 gold 标注与 `IRPS-DEEPAL-008` 的 visual-only 路由规则尚待人工/产品确认；当前 baseline 的 top-3 中仍会出现跨项目 hard-negative，不能直接用于产品事实判定。

## 下一步

- 复核 `IRPS-DEEPAL-003` 的权威项目来源，调整为 unsupported/clarification case 或补充已批准的 Deepal 项目资料，随后更新 manifest gold。
- 为 visual-only/unsupported 条目建立文本 RAG 前的路由保护，避免图纸事实被 generic/Test Spec evidence 覆盖。
- 确认第一轮基线采用单项目闭环还是跨项目泛化；不把合规矩阵/偏离表作为可检索知识。
- 以唯一 source-doc ID 上传选定 SOR 与知识文档，运行隔离 Azure score sampling，采集 gold / hard-negative 的 Recall@k、MRR、wrong-evidence rate 和按 score_source 分组的分布。
- 创建 `codex/rag-quality-loop` worktree 后将测试资产和调优实现迁入该 track。
