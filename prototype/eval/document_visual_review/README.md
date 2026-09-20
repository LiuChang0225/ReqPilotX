# 跨格式整文视觉 review v1

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

本目录定义 `docx`、`xlsx`、`xlsb`、`tif` 的受控整文视觉输入与
AI 预审账本。PDF 继续使用既有
`pdf_visual_review_bundle.py` / `pdf_visual_ai_pre_review.py`，本模块不改变
PDF 契约、产物或验收语义。

## 契约边界

- `render_profile.schema.json` 描述由受控 renderer 已生成的完整、有序页
  PNG。profile 必须绑定原 source SHA-256、格式、renderer
  id/version/config hash、页数、页定位器、PNG hash 和像素尺寸。
- `pipeline_profile.schema.json` 描述当前 pipeline 已生成的
  SourceIR、Structure Tree、Semantics、RequirementView JSON 以及
  adapter/resolver/classifier/projector 的显式 identity、status、snapshot 和
  fingerprint。profile 不是 pipeline 成功证明；`COMPLETE` 只允许四类真实
  JSON 产物和四个 COMPLETE component 同时存在。
- `bundle.schema.json` 描述 corpus 外的离线、整文 review 输入。bundle 只复制
  已 pin 的输入，用通用文件名发布，重算 source/profile/artifact hash、PNG
  尺寸和页清单，并通过 sibling staging + no-replace rename 原子发布。
- `ai_pre_review.schema.json` 描述 AI-only ledger。每页只允许
  `page_number`、`verdict` 和稳定 `issue_codes`；不允许客户正文、自由文本或
  human/formal/serving 声明。只有完整页集全部 `PASS` 时 summary 才能为
`PASS`。

v1 issue-code taxonomy 冻结为：

- source render：`SOURCE_RENDER_CLIPPED`、`SOURCE_RENDER_MISSING`、
  `SOURCE_RENDER_UNREADABLE`、`SOURCE_RENDER_LAYOUT_SHIFT`
- pipeline 对照：`PIPELINE_TEXT_OMISSION`、
  `PIPELINE_TABLE_TOPOLOGY_MISMATCH`、`PIPELINE_IMAGE_OMISSION`、
  `PIPELINE_ORDER_MISMATCH`、`PIPELINE_DUPLICATE_REQUIREMENT`、
  `PIPELINE_FALSE_REQUIREMENT`
- 未完成检查：`PIPELINE_NOT_RUN`、`REMOTE_VISUAL_REQUIRED`

前两组只能用于 `ISSUE`；最后一组只能用于 `NOT_EVALUATED`。未实际检查的页
不得填 `PASS` 或 `ISSUE`。

所有 bundle 都是 `CUSTOMER_SENSITIVE` /
`CONTROLLED_LOCAL_REVIEW_ONLY`。manifest 和离线 index 不记录原 source 的
绝对路径或文件名。AI ledger 始终是 `AI_PRE_REVIEW`，不是 Human Gold、
formal approval 或 serving activation gate。

## 代码入口

- `scripts/docx_visual_render_profile.py`
  - `LibreOfficeDocxPdfRenderer(...)`
  - `produce_docx_visual_render_profile(...)`
- `scripts/document_visual_pipeline_profile.py`
  - `build_document_visual_pipeline_profile(...)`
  - `verify_document_visual_pipeline_profile(...)`
- `scripts/document_visual_pipeline_export.py`
  - `export_document_visual_pipeline(...)`
  - CLI：`python -m scripts.document_visual_pipeline_export`
- `scripts/document_visual_tif_render_profile.py`
  - `export_tif_render_profile(...)`
  - `verify_tif_render_profile(...)`
- `scripts/document_visual_review_orchestrator.py`
  - `export_document_visual_review_plan(...)`
  - `execute_tif_not_evaluated_review(...)`
  - `execute_office_pipeline_profile_batch(...)`
  - `execute_docx_review_bundle_batch(...)`
- `scripts/document_visual_review_bundle.py`
  - `export_document_visual_review_bundle(...)`
  - `verify_document_visual_review_bundle(...)`
- `scripts/document_visual_ai_pre_review.py`
  - `prepare_document_visual_ai_pre_review(...)`
  - `validate_document_visual_ai_pre_review(...)`

pipeline-profile builder 不调用 adapter、resolver、classifier 或 projector。
调用者必须先在受控的 corpus-external 目录生成真实 JSON artifact，并为每个
artifact 传入相对 profile 路径、外部预期 SHA-256、
`media_type=application/json` 和 `customer_sensitive=true`。builder 在发布
前后检查 regular file、path containment、symlink、hash 和 inode，发现输入
漂移时不会发布 profile。

pipeline exporter 则显式运行当前产品函数。DOCX/XLSX/XLSB 严格按
`build_source_ir` → `build_structure_tree` → `classify_content` →
`project_requirements` 的顺序执行，并把四个对象各自的
`model_dump(mode="json")` 保存为通用命名 JSON 后调用 profile builder。
XLSB 的受控转换 SourceIR 按产品契约保持 `PARTIAL`，exporter 不将其改写为
`COMPLETE`。TIF 只运行正式 `LocalDocumentPreflight`；未提供并执行真实
attachment store 时只发布 `PREFLIGHT_REPORT`，pipeline 为 `BLOCKED`，
resolver/classifier/projector 为 `NOT_RUN`，不会伪造 opaque preservation、
OCR 或四阶段产物。整个目录使用 sibling staging 和 no-replace rename 发布，
任一阶段失败或 source inode/hash 漂移都不会留下目标目录。

TIF renderer 按 preflight 的完整 frame inventory 逐帧输出 PNG，profile 绑定
Pillow 版本和固定 render-config hash，PNG 不保留源元数据。orchestrator 从
受 pin 的 extraction manifest 解析 `external://training-corpus/` refs，逐个
重算 source hash，以 source SHA-256 去重并验证 `duplicate_of`；持久化 plan
不包含 external ref、源路径或文件名。TIF 闭环原子生成 render profile、
BLOCKED pipeline profile、bundle 和全页 `NOT_EVALUATED` ledger。Office
batch 显式接收 format allowlist，因此可只运行 DOCX。

当前 manifest 中 6 份已解析 DOCX 已用受控 producer 生成并完成发布后复验，
合计 137 页；逐页 PNG 的 hash、尺寸、完整页域、profile 内外 hash、source
绑定和统一 renderer config 均一致。6 份真实 `PARTIAL` pipeline profiles
也已逐一绑定并生成整文 bundle；整批使用 no-replace 原子发布，发布后再次
严格复验 bundle、artifact inventory 和全部外部 pins。SourceIR diagnostics
随 pipeline artifacts 完整保留，batch index 仅保存稳定 diagnostic code
计数，不含源路径或文件名。这只表示整文视觉审阅输入已就绪，不表示 137 页
已经获得 AI 或人工视觉结论。

## 进入 24 个唯一源文件审阅前仍需完成

这些 schema、builder、bundle verifier 和 ledger validator 只提供验真与
防篡改基础设施，不代表 24 个源文件已经运行 pipeline 或完成视觉确认。开始
真实整文 review 还需要：

1. 为其余非 PDF 唯一源文件受控生成 render profile 和完整页 PNG。DOCX
   已完成 6 份真实输入；其他格式的 producer 也必须记录真实
   id/version/config hash，不能由 bundle 脚本代跑或猜测。
2. 对 DOCX/XLSX/XLSB 调用 pipeline exporter，保存每次真实运行的四类
   corpus-external JSON artifacts 和 profile；TIF 保留诚实的
   BLOCKED/NOT_RUN profile，直到未来真实 attachment/deep-recognition 路径
   获得授权并实现。
3. 对每个 source 同时提供外部 source、render-profile、pipeline-profile
   file hash，生成并严格复验 bundle；6 份 DOCX 已完成。
4. AI reviewer 检查 bundle 的每一页并写入无正文 ledger；随后再次严格复验
   bundle、artifact inventory 和 ledger 自 hash。
5. 用 orchestration plan 核对 24 个唯一 source 的去重和格式分流；只有实际
   生成 bundle/ledger 的 source 才能声明全页覆盖。PDF 必须分流到既有 PDF
   工具。

DOCX renderer producer、6 份真实 DOCX render、TIF renderer 和 24-source
去重/分流 orchestrator 已完成；6 份 DOCX 的真实 pipeline profiles 也已
受控生成并绑定到 6 个严格复验的 bundle，但产品 SourceIR 状态均为
`PARTIAL`，不得改写为 `COMPLETE`。XLSX/XLSB 等其余
render/pipeline/bundle 输入仍未齐备，DOCX 137 页也尚未逐页审阅。
在每个唯一 source 都有受 pin 的全页 bundle 和逐页 ledger 之前，不得声称已
完成全量整文视觉确认或端到端验收。
