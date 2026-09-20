# Document Map 同源修订与原文条目继承

更新：2026-09-08。实现位于 `prototype/app/application/agent_workflow/document_map_revision.py`。
这是供插件与应用共用的纯编译模块，不执行模型调用、文件写入、发布或人工接受。

## 接口

```python
compile_document_map_revision(previous_map, candidate_map)
# {document_map, native_reference, receipt}

rebind_baseline_for_document_map_revision(
    previous_map, new_map, baseline, validation_receipt, table_previews,
    text_scope_recovery=None, feedback_ids=()
)
# {baseline, validation_receipt, receipt, table_previews}
```

工件参数接受现有 typed model 或 JSON 字典，返回 JSON 字典；不会原地修改输入。
候选 Map 应由已有 `compile-map` 路径生成。调用者须保留原 Map、基线、预览、候选和修订回执，
不得用返回值覆盖不可变前驱文件。

第一步返回真实后继版本及明确的前驱绑定。`native_reference` 是已通过守恒检查的候选 typed
结构的原生投影，满足：

```python
compile_document_map_artifact(native_reference, revision=document_map.revision)
# 精确等于返回的 document_map，包括 artifact SHA
```

它不伪造 D2 receipt。revision-binding receipt、旧/候选/新 Map 的身份与SHA、实际节点父级/顺序/
标题/内容归属差异，以及整表 scope 的对应关系均保留。候选原生 reference 中未进入 typed Map
的可选展示字段不被当作来源事实；应用应消费此处返回的精确 reference。

第二步先核验旧基线及校验回执，再通过现有 SourceItem compiler 复演文字跨度、表格行组、
共享单元格、标题上下文和完整分区。复演产生的临时身份不进入结果；只有绑定精确相同的原条目
可以保留原 SRC/REQ。最终校验回执继续使用现有基线 validator。

### 误判标题中正文的原子恢复

实际E01任务暴露：Map把有义务的完整正文误当纯标题时，先改Map会令旧`STRUCTURE_ONLY`分区失效，
先改条目又受旧标题角色约束。因此新增有界的联合修订：`text_scope_recovery`使用`REPLACE`、修正后Map的
`affected_scope_ids`与完整`text_decisions`，并绑定真实`feedback_ids`；插件通过同一`review-revise`中的
`map_revision`与`scope_revision`组合调用。已有条目的拆分/合并/分类调整仍在独立scope/classification操作完成。

联合编译保留私有Map投影，仅把原先结构分区中的文字恢复为新条目。现有完整SRC/REQ、分类、来源绑定和人工
决定必须保持，新增身份高于历史水位；原文字符位置集合、整表和原图仍守恒。最终仅返回一次合法直接后继，
不发布尚未完成正文恢复的投影。下游依据旧身份保留集和新增身份的scope编译证明核对继承；新条目没有旧分析，
实际上下文变化继续标为待重评。语义正确仍需独立原文核对。

可选`text_scope_recovery`回执包含投影SHA、原文字符位置SHA、真实scope revision与receipt、新增SRC/REQ身份，
外层记录最终号码水位。`classification_unchanged`、`original_bindings_unchanged`针对保留的旧条目；新增条目
明确单列。关闭review时重放原文件/SourceIR/Map决策及联合修订，比较完整最终工件，不接受手工重签结果。
共享继承验证还从最终基线反演内部scope compiler产物的revision/predecessor/可选水位包装，核对其实际ID/SHA，
不接受与最终业务字段无关的自签scope输出证明。该联合操作的分析继承调用须同时传`next_baseline`；普通Map
身份迁移保持原可选参数行为。

## 守恒与允许变化

- 原文全文、content ID、source handles、页码、几何、source bounds、原图 anchor/asset/placement
  及完整逻辑表物理片段按实际事实和出现次数守恒。不能通过重新签名接纳来源删除、重复或改写。
- 可以更正父级与同级顺序，移动完整来源对象，更正有原文依据的标题与编号，重命名展示节点或整表
  scope。节点/表名称变化不等于来源身份变化。
- `is_title` 是结构角色，可以纠正；新标为标题的完整文本必须与该节点实际标题/编号对应。
  `SOURCE_VISIBLE` 的新标题编号必须通过原文标题核验。Agent 显示标签不允许新造客户编号。
- 原 SRC/REQ、退休号码水位、原文跨度/单元格/图片绑定、分类、说明、人工覆盖来源及审阅状态保留。
  原件物理位置未变，因此基线 `source_order` 保持；更正后的结构顺序由 Map 表达。
- `covered_scope_ids`、标题上下文、表格上下文及表内原图关系按新归属重绑。完整旧 SRC 若横跨多个
  新 owner，必须先做明确的条目边界修订；模块不会猜测、拆分、复制或丢掉它。
- 只有实际祖先/所属正文、标题或共享上下文变化的条目进入 `context_changed_source_item_ids`。
  纯节点ID或整表scope改名不会仅因身份字符串变化而使分析失效。

Map rebind receipt 提供前后 Map/基线ID与SHA、前后校验回执SHA、`changed_node_ids`、
`rebound_source_item_ids`、`context_changed_source_item_ids`、保留的 SRC/REQ、原表预览SHA及scope映射。
应用/插件据此继续既有派生、释读和分析继承；这些外部工件不在本模块参数内，模块不会替其宣称
已继承、已复核或已获人工接受。

派生继承现通过 `rebind_source_item_decomposition(..., table_previews=new_previews,
previous_table_previews=old_previews, map_revision_receipt=map_rebind["receipt"])` 接入。跨Map回执须匹配
旧/新来源、Map、基线、保留身份、原表预览SHA及scope映射，并通过canonical hash校验。先用旧表
核验旧派生，再用新表重新构建准确证据；真实上下文变化强制待重评。纯source_form、展示node ID或
整表scope改名不虚假失效。原Snn注册表、保留号码和连续stale状态继续保留，新继承不冒充人工接受。

## 明确边界

- 当前支持同一 capture 身份下的完整对象修订。旧 native canonical ID 与新 SourceIR capture ID
  之间没有已证明的对应关系时拒绝迁移，不靠文字相似度猜匹配。
- 当前不合并/拆分完整逻辑表，不切割已有来源块，也不把分散到多个新owner的旧SRC隐式重写。
- 必须提供旧 Map 的全部、已绑定原件的原表预览。typed Map 只保存表片段身份与范围，不含所有
  cell.text；调用者须从已验证的来源存储提供预览。模块记录预览SHA、核验Map片段并复演原绑定，
  不宣称只凭Map SHA能证明任意外来cell.text就是原PDF。
- 原版已接受的外部 native document-unit ID可以保留；新归属不能虚构未能对应实际根文档的unit。
  `order` 是父级范围内的同级顺序，不要求全图全局唯一。
- 存量来源冲突须保留为显式失败。只读PDS v28兼容探针发现 `lt-vehicle-milestones/T0002/P2`
  在旧 Map 与现有预览中的行范围分别是 `[2,3]` 和 `[0,2]`，因此拒绝未证明的片段迁移；未修改
  当前PDS数据。进一步用原PDF重放现有恢复器，已证明旧范围指捕获器合并单元格，新范围指恢复后
  的独立表头/数据两行，完整14格及恢复回执与现有预览一致。需要显式保留这两个坐标空间与恢复
  映射，不能靠行偏移或重新签名接纳；私有证明在`prototype/runtime-data/agent-loop-20260908/document-map-revision-verification/`。
  该探针不代表当前PDS业务修订或新Map发布已经执行。

## 验证

`prototype/tests/test_document_map_revision.py` 使用明确的合成来源 handles、文字SHA、几何与表格
cells，覆盖真实父级/来源owner纠正、标题编号修订、原图与表内图片关系、稳定SRC/REQ、退休水位、
source保持、跨owner拒绝、缺表/几何漂移拒绝及自签错误绑定的compiler复演拒绝。

`test_document_map_text_recovery.py`复现旧标题角色阻断、合法恢复、历史水位、字符遗漏/旧分类更改拒绝，
以及新增条目未分析、旧上下文变更过期、伪造新增身份回执拒绝。插件`test_review_loop.py`另以实际合成PDF
验证“错误标题→联合修订→Excel→关闭反馈→完整重放”的公开入口。

运行：

```bash
PYTHONPATH=prototype prototype/.venv/bin/python -m unittest \
  prototype.tests.test_document_map_revision \
  prototype.tests.test_source_requirement_baseline -q
```

本轮15项新模块检查与31项既有基线检查通过。插件/应用接入及真实模型结构反馈回合需由对应执行
回执另行验证，不能由纯模块测试替代。
