# Optional OpenKB → ReqPilotX original-evidence bridge

Candidate dated 2026-10-02, based on ReqPilotX `67bb444cdbdb5d2a2e6e39cde45543b8233b8ea4`.
This is a tested isolated integration candidate, not a deployed server or a live
OpenKB model run. OpenKB is optional; source reading/extraction never depends on
it. Only explicit folder/upload sources are supported. No SharePoint connector,
customer fixture, credential, paid call or production merge is included.

## Actual integration

`OpenKBAnalysisService` calls existing portable public operations
`analysis-packet`, `compile-assessments`, and `deliver`. These share the engine's
SourceItem analysis compiler and Matrix/Excel projection used by the main
workbench. It does not rebuild source extraction or introduce a parallel
requirements model.

The host binds one project, KB, current baseline directory and its exact original
before constructing the service. `create_router(service, authorize=...)` can be
mounted explicitly on an existing FastAPI host; the host must supply its real
identity AND project-authorization dependency. There is no anonymous default
application or automatic main-server mounting.

Runtime import paths must include `packages/engine/src`,
`plugins/reqpilot/scripts`, and `integrations/openkb/src`. Reuse the project's
installed dependencies; optional routes require FastAPI/python-multipart, and
the evidence Excel annex uses the already supported openpyxl dependency.

## API sequence

1. POST `/api/v1/openkb-evidence/sources/upload`: multipart files, explicit host
   declaration, JSON metadata. Identity/hash/status/version are host-generated.
   Upload supports TXT, DOCX, XLSX and PDF; current explicit limits are 64 files,
   10 MiB per file, 20 MiB per API batch. Exceeding a limit fails explicitly.
   Folder import is a host method and accepts no client filesystem path.
2. POST `/sessions`: current `expected_generation` and
   `expected_baseline_sha256`, optionally current unit IDs. The actual native
   analysis packet and original evidence registration are frozen.
3. GET `/sessions/{id}/sources`, then `/originals/{document_id}`. The tool
   recaptures native facts/locators from immutable originals. Issued evidence IDs
   are persisted, allowing the same session to survive service reconstruction.
4. POST `/sessions/{id}/assessments`: submit a
   `reqpilotx-openkb-assessment/v1` envelope bound to project/KB/generation/packet.
   Every selected unit must appear exactly once. Each decision has unit_id,
   decision, reason, evidence_ids, conditions, conflicts, missing and
   clarification_question. The host maps only issued IDs to native fact/exact
   quote pairs; the existing compiler rejects unsupported compliance conclusions.
5. GET `/sessions/{id}/assessments/{sha256}/export-receipt` or `/matrix.xlsx`:
   export exactly that native assessment/baseline/knowledge generation. The
   untouched canonical workbook remains `matrix-native.xlsx`; the delivered
   workbook adds `OpenKB原件依据` with original hashes, exact quotes/locators,
   applicability, gaps, version bindings and explicit NOT_CLAIMED human acceptance.
   Both workbook hashes are recorded; source images and native sheet values are
   tested unchanged. Repeat export reuses checked immutable bytes.

Updates/deletions, changed original bytes, baseline/review changes or expired
sources invalidate current use. Historical artifacts remain immutable, but a
stale session cannot be used to export a current judgment. The API contains no
human-acceptance operation. Model judgments remain proposals requiring actual
engineering scope/coverage review, not automatic source-semantic verification.

## Customer context fix

The existing analysis packet could carry `context_source_ids` as IDs without the
referenced non-ancestor source text. The engine now expands declared same-source
context, including exact text/cell/image locators, shared content and indirect
references, into `referenced_customer_context`. A visited graph exposes cycles
without repeated obligations. Unknown/retired/foreign references fail explicitly.
Dependency content is part of unit hashes; changed indirect conditions stale only
dependent assessment units. No-reference unit contracts remain unchanged. An
associated customer response/state is never promoted into a new AI conclusion.

## Upstream contract and honest runtime boundary

Audited OpenKB pin: `ff54396e575ee6feb0113b631a34caa082b441cc`
(https://github.com/VectifyAI/OpenKB/tree/ff54396e575ee6feb0113b631a34caa082b441cc).
Its REST `/api/v1/query` returns only answer/saved_path and has no document filter
or native evidence envelope. Wiki prose remains discovery, never an original
fact. `runtime.build_analysis_query_agent` extends the real upstream query agent
with original-read/customer-unit tools for an approved host runner. It checks the
audited query module hash and the host-owned generation Wiki path.

The actual OpenKB/Agents/LiteLLM runtime is unavailable in this cloud environment.
The pinned upstream schemas were fetched and verified by Git blob identity;
source-contract and native integration tests are offline synthetic tests. No
mock/contract server result is represented as a full live OpenKB pass. Approved
runtime installation, Wiki compilation, authenticated main-host mounting,
UI/Pair/plugin invocation, actual customer semantic validation and deployment
remain separate validation tasks.

## Tests

From repository root: `python -m pytest -q integrations/openkb/tests`.
The regression fixture is invented, uses the real source capture/Map/SourceItem
compiler, and preserves customer-source vs internal-evidence separation.


## Canonical Mac integration verification (2026-10-02)

Library version 1 candidate ZIP was checked at 117,401 bytes, SHA-256
`227cbe4d40d0a9107016e969f759f084a441be2ee1af03c388e41a3008b52597`.
All 16 overlay paths and both existing-file preimages were checked before applying
on the clean Pair commit `881356852959c5b729db5f6830659f759947b4ea`; the paths do
not overlap the Pair transport change. Existing-file backups were retained.

With existing Mac dependencies, 31 new integration tests passed. The combined
OpenKB/analysis/Map/decomposition/portable/knowledge-original/plugin/native
delivery regression passed 116 tests, including those 31. The existing Pair
suite passed 41 tests in the restricted sandbox; its three localhost HTTP tests
failed to listen with EPERM, then all three passed through the normal approved
numeric 127.0.0.1 test run. This was an environment restriction, not a business
regression. The synthetic test servers were closed after the checks.

These checks made no paid model call and used no customer document. They do not
establish company identity, automatic main-server mounting, live OpenKB runtime,
external Pair browser, canonical delivery-version integration or Windows 11
acceptance. The earlier 68-test PoC result is cloud provenance, not a Mac rerun.
The existing repository CI covers its backend/plugin/engine/Pair workflows;
these new opt-in OpenKB pytest checks were run separately as described above.


### Full CI reconciliation

The first integrated-head frontend CI passed. Its full backend run reported two
failures among 4,811 tests (154 skipped): the new engine context module lacked the
required server compatibility alias, and a lease-heartbeat test depended on a
100 ms wall-clock window. The standard alias now reuses the identical engine
module. The test advances a controlled lease clock past the original expiry,
waits for a real database heartbeat and still rejects a competing owner; it no
longer treats CI scheduling delays as lease expiration. Production lease timing
and ownership rules were not changed. Five productization and 58 atomization
scope tests passed locally after these corrections. Final-head remote CI is
tracked separately; the earlier failed run is retained as evidence.

## 2026-10-02 主server / 原生Web有限接入

基于统一提交 `79458fdee2daf5531d96b13e6160c69e502fd5fe` 的后续候选，在真实主server新增原生本地入口。该切片接通**原件登记、事实准备、原件回查、待审引用、现有工程师意见与同版交付**；它没有运行OpenKB模型，不能称为已完成Live OpenKB检索/编译或自动符合性判断。

- 原生Web“项目依据”可登记文件或选择文件夹。浏览器上传相对文件名与字节，不能指定服务器读取路径。API固定source-only，不请求索引、Embedding或模型；部分失败逐文件说明
- `/api/projects/{project_id}/openkb-evidence/status` 明确区分原件/事实数量、索引未请求、runtime是否安装及模型未运行。知识目录和事实复用既有project knowledge registration/capture，不另建知识数据库
- 原生SOR入口为 `.../agent-structure/delivery/openkb-evidence/sessions`、`.../originals/{knowledge_document_id}` 与 `.../prepare-review`。宿主验证project/SOR归属、原文/工作版/关联上下文/知识版本，并保存实际读取签发的引用身份
- 只接受LOCAL/TEST既有单机模式和既有Host/Origin边界，LOCAL还要求实际loopback peer。可信主体由server模式与应用范围确定，不读取caller的X-User、actor或subject。CLOUD不开放此本地写入口；没有新增SSO或网络权限
- 准备动作仅生成 `HOST_PENDING_DRAFT_SAVED`，语义核查 `NOT_PERFORMED`、模型 `NOT_RUN`、人工接受 `NOT_CLAIMED`。逐单元输入回执保留其宿主准备来源；UI/canonical Matrix不把这些行计为实际AI分析，旧的真实分析单元来源保留
- 工程师沿用既有意见接口。canonical snapshot保存精确签发引用回执和分析/知识版本；canonical XLSX增加“内部原件引用”，引用JSON/原文可无损分段回查。它是候选引用记录，不是完整覆盖证明。DRAFT/FORMAL与immutable版本下载仍使用原生delivery API，未修改已冻结文件
- 原件字节、失效期、source/baseline、annotations wrapper、decomposition或knowledge变化使准备会话失效；较新的真实原生分析替代准备。32MiB native工件读取上限在写入前检查，不静默存储不可读结果
- standalone Web明确开启nativeEvidence；Pair/plugin等外部宿主未声明该能力时继续原合同，不自动暴露新操作

本轮使用合成原件测试真实main HTTP、DB/capture、工程意见、canonical XLSX和immutable版本接口；客户原件、生产PostgreSQL宿主、Live模型及Mac实屏仍须单独验收。云浏览器localhost视觉验收被平台拒绝，没有改用其它路径绕过。


### Mac 主入口验证与当前交接边界（2026-10-02）

上述 v4 ZIP 在 Mac 核对为 355,691 bytes，SHA-256
`122951481410bb34a4d1035522f48c75f81cb532c8531dcf91f3dbe552a2185c`。
干净 `79458fd` 上的 30 个路径及 20 个精确前像核对后已备份并应用。
使用已有依赖通过 74 项受影响后端、14 项引擎分发、1 项主路由组合、
1000 项 UI 测试，以及 typecheck、Web/panel build 和 4 项 CSS 检查。

另以受控私有 PostgreSQL 16.14、真实 main API 和真实 durable worker
运行仅 loopback 的脚本模型，生成新的合成 PDF 来源及持久原生 SourceItem；
没有替换来源读取器。新证据入口完成相对名称原件登记、字节/hash 回查、
精确签发引用、待证据准备、既有工程师意见、canonical XLSX 引用/定位核对、
幂等重放、资料变化后的过期拒绝，以及重启后的意见和冻结 Excel 保留。
该准备步骤新增模型请求为 0；来源生成的脚本模型调用不是实际模型认证或
语义验收。所有输入均为合成资料，未使用客户原件。

实际构建的 standalone Web 主入口在 jsdom 中连接同一真实 main API/PG，
完成项目到项目依据导航、文件夹相对名称上传、原文读取与选择、保存待证据
草稿和关闭窗口。DOM 复现器补齐了浏览器 Event/dialog API；这不证明实屏
排版、PDF 视觉保真或浏览器文件夹选择对话框。原生 Chrome 页面脚本不可用，
没有开启其安全选项；Mac 实屏交互验收为 **NOT_RUN**。

本地提交命令未启动：自动审批将父任务传来的 Git 授权视为不可信转述。
因此候选仍为未提交源码，官方 engine release 的已提交/干净输入门禁阻止
发布构建；新的非 editable 安装、推送和新 HEAD CI 为 **NOT_RUN**。
现有远端 `79458fd` CI 不能证明这个未提交候选。
临时 main API、两次限定 worker、脚本模型服务及私有 PG 均已关闭，
合成验证工件及前像备份保留在仓库外。Live OpenKB、公司生产 PG、真实模型
部署认证、客户语义和 Windows 11 验收均未运行。
