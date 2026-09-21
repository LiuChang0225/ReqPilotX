from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from collections.abc import Iterable
from typing import Protocol

from reqpilotx_server.application.extraction.build_semantic_unit_plan import (
    SemanticUnitPlanRejected,
    build_semantic_unit_plan_from_owner_spans,
)
from reqpilotx_server.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
    validate_structure_snapshot_against_source_ir,
)
from reqpilotx_server.domain.extraction.explicit_peer_items import (
    parse_explicit_numeric_peer_items,
)
from reqpilotx_server.domain.extraction.semantic_units import SemanticUnitPlan
from reqpilotx_server.domain.extraction.semantics import (
    GroundedSemanticInformationFacet,
    RequirementSubtype,
    SemanticAnnotation,
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
    SemanticClassificationSnapshot,
    SemanticGroundingSpan,
    SemanticInformationKind,
    stable_semantic_annotation_id,
    stable_semantic_grounding_span_id,
    stable_semantic_information_facet_id,
    stable_semantic_snapshot_id,
)
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)
from reqpilotx_server.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from reqpilotx_server.domain.extraction.visual_text import VisualTextObservation


DEFAULT_SEMANTIC_CLASSIFIER_ID = "deterministic-semantic-baseline"
DEFAULT_SEMANTIC_CLASSIFIER_VERSION = "1.15.1"


class SemanticTextResolver(Protocol):
    """Optional versioned source-text view, for example OCR/vision output.

    Returning ``None`` retains the native SourceIR text. Raising, or returning
    a non-string value, produces an explicit UNRESOLVED annotation rather than
    an untracked fallback decision.
    """

    def __call__(
        self,
        source_ref: StructureSourceRef,
        source_ir: SourceIRSnapshot,
    ) -> str | None: ...


class SemanticClassificationRejectionCode(str, Enum):
    INPUT_MISMATCH = "input_mismatch"
    INVALID_SCOPE = "invalid_scope"
    INVALID_GROUNDING = "invalid_grounding"


class SemanticClassificationRejected(RuntimeError):
    def __init__(
        self,
        code: SemanticClassificationRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _ResolvedText:
    text: str
    failed: bool = False


@dataclass(frozen=True)
class _SemanticWindow:
    refs: tuple[StructureSourceRef, ...]
    texts: tuple[str, ...]
    offsets: tuple[tuple[int, int], ...]
    own_text: str
    ancestor_text: str
    direct_body_text: str
    resolution_failed: bool


@dataclass(frozen=True)
class _Decision:
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None
    status: SemanticAnnotationStatus
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class _InformationSignal:
    information_kind: SemanticInformationKind
    semantic_class: SemanticClass
    source_ref: StructureSourceRef
    resolved_text: str


@dataclass(frozen=True)
class _ExplicitPeerItemView:
    source_ref: StructureSourceRef
    start_offset: int
    end_offset: int
    text: str


_MODAL_REQUIREMENT_PATTERNS = (
    re.compile(
        r"(?:必须|不得|严禁|不允许|"
        r"(?<![响适效反对供])应(?:当|为|是|有|无|按|与|由|在|将|可|能|"
        r"保证|确保|满足|符合|达到|提供|具备|采用|使用|记录|保持|进行|"
        r"安装|支持|覆盖|包括|不|耐受|承受|通过|完成|避免|处于|设置|"
        r"标识|考虑|定义|完整|清晰|平整|光滑|圆滑|丰满|均匀|牢固|"
        r"可靠|连续|一致|正常))"
    ),
    re.compile(r"需(?:要|满足|符合|达到|提供|具备|采用|使用|保证)"),
    re.compile(r"\b(?:shall|must)\b|\b(?:is|are)\s+required\s+to\b", re.I),
    re.compile(r"\b(?:muss|müssen|soll|sollen)\b", re.I),
)
_IMPLICIT_CONSTRAINT_PATTERNS = (
    re.compile(
        r"(?:至少|至多|不得超过|不超过|不少于|不大于|不小于|"
        r"(?:温度|电压|电流|压力|时间|数量|尺寸|精度|频率|速度|"
        r"载荷|力|扭矩).{0,24}(?:为|按|达到|低于|高于)|"
        r"按.{1,80}(?:执行|进行|试验|检验|验证)|"
        r"以.{1,80}为准)"
    ),
    re.compile(
        r"\b(?:at\s+least|at\s+most|no\s+(?:less|more)\s+than|"
        r"not\s+(?:less|greater)\s+than)\b",
        re.I,
    ),
)
_EXPLICIT_TABLE_PEER_REQUIREMENT_PATTERNS = (
    re.compile(
        r"(?:要求|必须|不得|需(?:要|提供|提交|整改|改进|确认|接受))"
    ),
    re.compile(
        r"(?:供应商|供方|厂商).{0,50}"
        r"(?:提供|提交|整改|改进|接受|确认|执行|完成)"
    ),
    re.compile(
        r"\b(?:supplier|vendor)\b.{0,80}"
        r"\b(?:provide|submit|improve|rectify|accept|confirm|complete)\b",
        re.I,
    ),
)
_TECHNICAL_EXPLANATION_PATTERNS = (
    re.compile(r"\b(?:consists?\s+of|is\s+composed\s+of)\b", re.I),
    re.compile(
        r"\b(?:calculation|measurement|conversion|derivation)\b.{0,120}"
        r"\b(?:is|are)\s+(?:done|performed|calculated|derived)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:sensor|product|system|device|module|unit|board|controller|"
        r"software|firmware|versions?)\b.{0,120}"
        r"\b(?:provides?|generates?|converts?|measures?|calculates?|"
        r"transmits?|detects?|controls?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:is|are)\s+(?:given|defined|calculated|derived|determined)"
        r"\s+(?:by|as)\b",
        re.I,
    ),
    re.compile(
        r"(?:由.{1,80}(?:组成|构成)|"
        r"(?:计算|测量|转换|推导).{0,60}(?:完成|实现|得出)|"
        r"(?:传感器|产品|系统|设备|模块|单元|控制器|软件|固件).{0,80}"
        r"(?:提供|产生|转换|测量|计算|传输|检测|控制))"
    ),
)
_TABLE_NOTE_PREFIX = re.compile(r"^\s*(?:注|备注|说明|note)\s*[:：]", re.I)
_EQUIPMENT_PATTERNS = (
    re.compile(
        r"(?:试验|测试|检测|测量|检验|校准)(?:用)?"
        r"(?:仪|仪器|设备|装置|系统|工装|台架|试验台|机)"
    ),
    re.compile(r"(?:仪器|设备|装置|工装|台架).{0,10}(?:要求|规定|规格|精度)"),
    re.compile(
        r"\b(?:(?:force|load|torque|pressure)\s+)?"
        r"(?:test(?:ing)?|measur(?:ing|ement))\s+"
        r"(?:equipment|instrument|device|machine|rig|gauge)\b",
        re.I,
    ),
    re.compile(r"\b(?:tester|test\s+equipment|test\s+rig)\b", re.I),
    re.compile(r"\b(?:prüfgerät|prüfeinrichtung|messgerät)\b", re.I),
)
_OBJECTIVE_PATTERNS = (
    re.compile(r"(?:试验|测试|验证|检验)(?:的)?(?:目的|目标)"),
    re.compile(r"(?:目的|目标)(?:是|为|在于|[:：])"),
    re.compile(r"(?:旨在|用于)\s*(?:确定|验证|评价|评估|确认|检查)"),
    re.compile(
        r"\b(?:test(?:ing)?\s+(?:objective|purpose)|"
        r"(?:objective|purpose)\s+of\s+(?:the\s+)?test(?:ing)?)\b",
        re.I,
    ),
    re.compile(r"\b(?:prüfziel|prüfzweck|zweck\s+der\s+prüfung)\b", re.I),
)
_OBJECTIVE_HEADING_PATTERN = re.compile(
    r"(?:^|\s)(?:(?:试验|测试|验证|检验)(?:的)?)?(?:目的|目标)\s*$"
)
_OBJECTIVE_ACTION_PATTERN = re.compile(
    r"^\s*(?:(?:用于|旨在)\s*)?(?:确定|验证|评价|评估|确认|检查)"
)
_NUMBERED_TEST_METHOD_CONTAINER_PATTERN = re.compile(
    r"(?:试验|测试|验证|检验)?(?:顺序|步骤|方法)(?:如下)?\s*[:：。.]?$|"
    r"\b(?:test(?:ing)?\s+(?:sequence|steps?|method|procedure)|"
    r"(?:method|procedure)\s+of\s+(?:the\s+)?test(?:ing)?)\b",
    re.I,
)
_NUMBERED_REQUIREMENT_CONTAINER_PATTERN = re.compile(
    r"(?:^|\s)(?:要求|规定)(?:如下)?\s*[:：。.]?$|"
    r"\b(?:requirements?|acceptance\s+criteria)(?:\s+are\s+as\s+follows)?\s*[:.]?$",
    re.I,
)
_TABULAR_REQUIREMENT_CONTAINER_PATTERN = re.compile(
    r"(?:要求|规定|\brequirements?\b|\bspecifications?\b)",
    re.I,
)
_PERFORMANCE_PARAMETER_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "parameter",
        re.compile(
            r"^(?:参数(?:名称)?|特性(?:名称)?|项目(?:名称)?|"
            r"parameters?|characteristics?|item(?:\s+name)?)$",
            re.I,
        ),
    ),
    (
        "unit",
        re.compile(r"^(?:单位|units?)$", re.I),
    ),
    (
        "lower",
        re.compile(
            r"^(?:最小(?:值|限值)?|下限(?:值)?|"
            r"minimum(?:\s+(?:value|limit))?|min\.?(?:\s+value)?|"
            r"lower(?:\s+limit)?)$",
            re.I,
        ),
    ),
    (
        "nominal",
        re.compile(
            r"^(?:典型(?:值)?|标称(?:值)?|额定(?:值)?|目标(?:值)?|"
            r"typ\.?(?:\s+value)?|typical(?:\s+value)?|"
            r"nominal(?:\s+value)?|"
            r"rated(?:\s+value)?|target(?:\s+value)?)$",
            re.I,
        ),
    ),
    (
        "upper",
        re.compile(
            r"^(?:最大(?:值|限值)?|上限(?:值)?|"
            r"maximum(?:\s+(?:value|limit))?|max\.?(?:\s+value)?|"
            r"upper(?:\s+limit)?)$",
            re.I,
        ),
    ),
)
_PERFORMANCE_PARAMETER_VALUE_ROLES = frozenset({"lower", "nominal", "upper"})
_PERFORMANCE_PARAMETER_EMPTY_VALUES = frozenset(
    {"", "-", "--", "/", "—", "–", "n/a", "na", "not applicable", "不适用"}
)
_MATERIAL_HEADER_PATTERN = re.compile(r"(?:材料|材质|\bmaterial\b)", re.I)
_MATERIAL_REQUIREMENT_HEADER_PATTERN = re.compile(
    r"(?:(?:材料|材质|\bmaterial\b).{0,48}"
    r"(?:要求|需求|\brequirements?\b)|"
    r"(?:要求|需求|\brequirements?\b).{0,48}"
    r"(?:材料|材质|\bmaterial\b))",
    re.I | re.S,
)
_STANDARD_HEADER_PATTERN = re.compile(
    r"(?:标准|规范|\bstandards?\b|\bspecifications?\b)",
    re.I,
)
_ACCEPTANCE_HEADER_PATTERN = re.compile(
    r"(?:验收|接受|接收|判定|合格|\bacceptance\b|\bcriteria\b|\bcriterion\b)",
    re.I,
)
_TEST_CRITERIA_HEADER_PATTERN = re.compile(
    r"(?:(?:测试|试验|\btests?\b).{0,32}"
    r"(?:标准|准则|\bcriteria\b|\bcriterion\b))",
    re.I | re.S,
)
_MATERIAL_TABLE_FORBIDDEN_HEADER_PATTERN = re.compile(
    r"(?:\bbom\b|\bbill\s+of\s+materials?\b|"
    r"\binventory\b|\bstock(?:\s+(?:record|ledger|quantity))?\b|"
    r"\bwarehouse\b|\border\s+quantity\b|"
    r"\brevision(?:\s+history)?\b|\bversion\s+history\b|"
    r"\bchange\s+history\b|"
    r"物料清单|库存|存货|仓储|订单数量|采购数量|"
    r"修订(?:历史|记录)?|版本历史|变更记录)",
    re.I,
)
_COMPOUND_TEST_ROW_ORDINAL_PATTERN = re.compile(
    r"^(?:[1-9][0-9]{0,3}|[一二三四五六七八九十百两]{1,4})"
    r"(?:\.[0-9]{1,3})*$"
)
_COMPOUND_TEST_ROW_EVIDENCE_PATTERNS = (
    re.compile(r"(?:试验|测试|验证|检验|测量|高低温|温度循环|耐久|性能)"),
    re.compile(
        r"\b(?:test(?:ing)?|verification|validation|measurement|"
        r"temperature\s+cycling|durability|performance)\b",
        re.I,
    ),
    re.compile(r"\b(?:prüfung|prüfverfahren|messung|dauerlauf)\b", re.I),
)
_SCHEDULE_DURATION_VALUE_PATTERN = re.compile(
    r"^(?:约|大约|预计|approx(?:imately)?\.?\s*)?"
    r"(?:[1-9][0-9]{0,3}(?:\.[0-9]+)?|"
    r"[一二三四五六七八九十百两]{1,5})\s*"
    r"(?:个?\s*)?(?:工作日|自然日|天|日|"
    r"working\s+days?|calendar\s+days?|days?)$",
    re.I,
)
_SAMPLE_AND_PHASE_VALUE_PATTERN = re.compile(
    r"^(?:约|大约|approx(?:imately)?\.?\s*)?"
    r"(?:[1-9][0-9]{0,3}|[一二三四五六七八九十百两]{1,5})\s*"
    r"(?:件|个|套|台|辆|只|组|pcs?\.?|pieces?|units?|samples?)"
    r"\s*(?:[/／|,+&-]\s*|\s+)"
    r"(?:ots|dv|pv|dvp|pvp|evt|dvt|pvt|ppap|sop)"
    r"(?:\s*(?:阶段|phase))?$",
    re.I,
)
_COMPOUND_TEST_ROW_DISALLOWED_CLASSES = frozenset(
    {
        SemanticClass.BACKGROUND,
        SemanticClass.REFERENCE,
        SemanticClass.PROJECT_RISK,
        SemanticClass.VERSION_METADATA,
        SemanticClass.ATTACHMENT,
    }
)
_NORMATIVE_APPLICABILITY_PATTERNS = (
    re.compile(r"(?:必不可少|不可或缺|适用于本文件|构成本文件|应(?:当)?遵守)"),
    re.compile(
        r"\b(?:shall\s+apply|indispensable\s+for|constitute(?:s)?\s+"
        r"(?:provisions|requirements))\b",
        re.I,
    ),
)
_REGULATORY_OPTION_CONTAINER_PATTERN = re.compile(
    r"(?:(?:法规|标准).{0,8}(?:认证|适用|区域|市场)|"
    r"认证.{0,8}(?:区域|市场|要求))"
)
_REVISION_DATA_ROW_PATTERN = re.compile(
    r"^(?:v(?:ersion)?\s*)?(?:"
    r"\d+(?:\s*\.\s*\d+){1,3}|"
    r"\d{2,4}\s*\."
    r")(?=\s|$)",
    re.I,
)
_CLASS_PATTERNS: tuple[tuple[SemanticClass, tuple[re.Pattern[str], ...]], ...] = (
    (
        SemanticClass.BACKGROUND,
        (
            re.compile(r"(?:项目|技术|产品)?背景(?:介绍|说明)?"),
            re.compile(r"(?:概述|引言|前言)"),
            re.compile(
                r"\b(?:background|introduction|overview|general\s+information)\b", re.I
            ),
        ),
    ),
    (
        SemanticClass.TECHNICAL_INFORMATION,
        (
            re.compile(r"(?:技术|原理|产品)(?:说明|介绍|信息)"),
            re.compile(r"(?:工作|技术)(?:原理|描述)"),
            re.compile(
                r"\b(?:technical\s+(?:information|description)|"
                r"product\s+information|operating\s+principle)\b",
                re.I,
            ),
        ),
    ),
    (
        SemanticClass.SCHEDULE,
        (
            re.compile(
                r"(?:(?:项目|交付|开发|试验)(?:进度|计划|周期|里程碑)|"
                r"(?:项目)?时间节点|进度(?:计划|要求|安排|节点)?|"
                r"交付周期|里程碑(?:计划|节点)?)"
            ),
            re.compile(
                r"\b(?:schedule|timeline|milestone|delivery\s+plan|terminplan)\b", re.I
            ),
        ),
    ),
    (
        SemanticClass.REFERENCE,
        (
            re.compile(r"(?:规范性|适用|参考|引用)(?:文件|标准|资料)"),
            re.compile(
                r"\b(?:normative\s+references?|reference\s+documents?|applicable\s+standards?)\b",
                re.I,
            ),
        ),
    ),
    (
        SemanticClass.PROJECT_RISK,
        (
            re.compile(r"(?:项目|技术|质量)?风险"),
            re.compile(r"\b(?:project|technical|quality)?\s*risk(?:s)?\b", re.I),
        ),
    ),
    (
        SemanticClass.VERSION_METADATA,
        (
            re.compile(r"(?:版本|修订|变更)(?:记录|历史|说明|状态)"),
            re.compile(
                r"(?:(?:中文(?:版本|原文)|chinese\s+original).{0,160}"
                r"(?:为准|权威|authoritative)|"
                r"(?:英文(?:版本|译文)|english\s+translation).{0,160}"
                r"(?:控制|翻译|负责|in\s+charge))",
                re.I | re.S,
            ),
            re.compile(
                r"^\s*[—–-]{1,3}\s*[^\\n]{0,100}"
                r"(?:重新定义|修改|增加|删除|调整|更正|变更)|"
                r"^\s*(?:modified|added|deleted|redefined|changed)\b",
                re.I,
            ),
            re.compile(
                r"\b(?:revision|version|change)\s+(?:history|record|status)\b", re.I
            ),
        ),
    ),
    (
        SemanticClass.ATTACHMENT,
        (
            re.compile(r"(?:附件|附录)(?:清单|列表|说明)?"),
            re.compile(r"\b(?:attachment|appendix|annex)(?:es)?\b", re.I),
        ),
    ),
)
_NOISE_SOURCE_KINDS = {
    SourceNodeKind.HEADER,
    SourceNodeKind.FOOTER,
    SourceNodeKind.WATERMARK,
    SourceNodeKind.TOC_ENTRY,
}


def classify_content(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    *,
    scope_structure_node_ids: tuple[str, ...] | None = None,
    classifier_id: str = DEFAULT_SEMANTIC_CLASSIFIER_ID,
    classifier_version: str = DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    text_resolver: SemanticTextResolver | None = None,
) -> SemanticClassificationSnapshot:
    """Classify a complete ordered StructureTree scope without copying text.

    The deterministic baseline uses only SourceIR text and tree relationships.
    It never consults file names or fixed page numbers. Strong test-equipment
    and test-objective evidence is sufficient for requirement classification;
    a modal word is deliberately not required.
    """

    _validate_input_types_and_binding(source_ir, tree)
    scope = _normalize_scope(tree, scope_structure_node_ids)
    context = _ClassificationContext(
        source_ir=source_ir,
        tree=tree,
        text_resolver=text_resolver,
    )

    decisions: list[tuple[str, _Decision, tuple[SemanticGroundingSpan, ...]]] = []
    for target_id in scope:
        node = context.structure_nodes[target_id]
        window = context.window_for(node)
        decision = _classify_window(node, window, context)
        spans = tuple(
            SemanticGroundingSpan(
                grounding_span_id=stable_semantic_grounding_span_id(
                    source_ref=source_ref,
                    start_offset=start_offset,
                    end_offset=end_offset,
                ),
                source_ref=source_ref,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            for source_ref, (start_offset, end_offset) in zip(
                window.refs,
                window.offsets,
                strict=True,
            )
        )
        decisions.append((target_id, decision, spans))

    unit_plan = build_semantic_unit_plan_from_owner_spans(
        source_ir,
        tree,
        scope_owner_structure_node_ids=scope,
        owner_source_spans_by_id={
            target_id: tuple(
                span
                for span in spans
                if span.source_ref
                in set(
                    context.source_refs_for(
                        context.structure_nodes[target_id]
                    )
                )
            )
            for target_id, _decision, spans in decisions
        },
    )
    semantic_snapshot_id = stable_semantic_snapshot_id(
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        classifier_id=classifier_id,
        classifier_version=classifier_version,
        semantic_unit_plan_id=unit_plan.semantic_unit_plan_id,
        unitizer_version=unit_plan.unitizer_version,
        scope_semantic_unit_ids=unit_plan.semantic_unit_ids,
    )
    units_by_owner = {unit.owner_structure_node_id: unit for unit in unit_plan.units}
    annotations = tuple(
        SemanticAnnotation(
            semantic_annotation_id=stable_semantic_annotation_id(
                semantic_snapshot_id=semantic_snapshot_id,
                semantic_unit_id=units_by_owner[target_id].semantic_unit_id,
            ),
            semantic_unit_id=units_by_owner[target_id].semantic_unit_id,
            owner_structure_node_id=target_id,
            semantic_class=decision.semantic_class,
            requirement_subtype=decision.requirement_subtype,
            candidate_source=SemanticCandidateSource.RULE,
            grounding_spans=spans,
            status=decision.status,
            reason_codes=decision.reason_codes,
        )
        for target_id, decision, spans in decisions
    )

    result = SemanticClassificationSnapshot(
        semantic_snapshot_id=semantic_snapshot_id,
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        classifier_id=classifier_id,
        classifier_version=classifier_version,
        semantic_unit_plan_id=unit_plan.semantic_unit_plan_id,
        unitizer_version=unit_plan.unitizer_version,
        scope_semantic_unit_ids=unit_plan.semantic_unit_ids,
        scope_owner_structure_node_ids=scope,
        annotations=annotations,
    )
    validate_semantic_classification_against_inputs(
        result,
        source_ir,
        tree,
        text_resolver=text_resolver,
    )
    return result


def classify_table_row_information(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    owner_structure_node_id: str,
    text_resolver: SemanticTextResolver | None = None,
) -> tuple[GroundedSemanticInformationFacet, ...]:
    """Return exact-cell information facets below one row-level requirement.

    Facets are deliberately separate from ``SemanticAnnotation``: the
    Structure owner retains one PRODUCT_REQUIREMENT annotation and therefore
    one RequirementView item. Schedule and sample/phase values remain
    independently source-bound without becoming top-level requirements.
    """

    validate_semantic_classification_against_inputs(
        semantics,
        source_ir,
        tree,
        text_resolver=text_resolver,
    )
    if not isinstance(owner_structure_node_id, str) or not owner_structure_node_id:
        _reject_invalid_scope(
            "Table-row information requires one non-blank Structure owner."
        )
    context = _ClassificationContext(
        source_ir=source_ir,
        tree=tree,
        text_resolver=text_resolver,
    )
    owner = context.structure_nodes.get(owner_structure_node_id)
    if owner is None:
        _reject_invalid_scope(
            "Table-row information must reference a node in this StructureTree."
        )
    matching_annotations = tuple(
        annotation
        for annotation in semantics.annotations
        if annotation.owner_structure_node_id == owner_structure_node_id
    )
    if len(matching_annotations) != 1:
        _reject_invalid_scope(
            "Table-row information requires exactly one row-owner annotation."
        )
    annotation = matching_annotations[0]
    expected_decision = _classify_window(owner, context.window_for(owner), context)
    observed_decision = (
        annotation.semantic_class,
        annotation.requirement_subtype,
        annotation.status,
        annotation.reason_codes,
    )
    if observed_decision != (
        expected_decision.semantic_class,
        expected_decision.requirement_subtype,
        expected_decision.status,
        expected_decision.reason_codes,
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INPUT_MISMATCH,
            "Table-row information requires the matching deterministic row decision.",
        )
    if (
        annotation.semantic_class is not SemanticClass.REQUIREMENT
        or annotation.requirement_subtype is not RequirementSubtype.PRODUCT_REQUIREMENT
        or "compound_test_requirement_row" not in annotation.reason_codes
    ):
        return ()

    signals = context.compound_test_requirement_information(owner)
    return tuple(
        GroundedSemanticInformationFacet(
            information_facet_id=stable_semantic_information_facet_id(
                semantic_snapshot_id=semantics.semantic_snapshot_id,
                owner_semantic_unit_id=annotation.semantic_unit_id,
                owner_structure_node_id=owner_structure_node_id,
                information_kind=signal.information_kind,
                semantic_class=signal.semantic_class,
                source_ref=signal.source_ref,
                evidence_text_sha256=hashlib.sha256(
                    signal.resolved_text.encode("utf-8")
                ).hexdigest(),
            ),
            semantic_snapshot_id=semantics.semantic_snapshot_id,
            owner_semantic_unit_id=annotation.semantic_unit_id,
            owner_structure_node_id=owner_structure_node_id,
            information_kind=signal.information_kind,
            semantic_class=signal.semantic_class,
            source_ref=signal.source_ref,
            evidence_text_sha256=hashlib.sha256(
                signal.resolved_text.encode("utf-8")
            ).hexdigest(),
            reason_codes=(f"{signal.information_kind.value}_signal",),
        )
        for signal in signals
    )


def validate_semantic_classification_against_inputs(
    semantics: SemanticClassificationSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    *,
    text_resolver: SemanticTextResolver | None = None,
    semantic_unit_plan: SemanticUnitPlan | None = None,
) -> None:
    """Fail closed unless scope, order, and full grounding match both inputs."""

    if not isinstance(semantics, SemanticClassificationSnapshot):
        raise TypeError("semantics must be a SemanticClassificationSnapshot")
    _validate_input_types_and_binding(source_ir, tree)
    if (
        semantics.source_ir_id != source_ir.source_ir_id
        or semantics.structure_snapshot_id != structure_snapshot_id(tree)
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INPUT_MISMATCH,
            "Semantic classification is not bound to the supplied inputs.",
        )
    if semantic_unit_plan is not None:
        _validate_semantic_classification_against_unit_plan(
            semantics,
            source_ir,
            tree,
            semantic_unit_plan,
            text_resolver=text_resolver,
        )
        return
    scope = _normalize_scope(tree, semantics.scope_owner_structure_node_ids)
    if scope != semantics.scope_owner_structure_node_ids:
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INVALID_SCOPE,
            "Semantic scope does not preserve StructureTree source order.",
        )

    context = _ClassificationContext(
        source_ir=source_ir,
        tree=tree,
        text_resolver=text_resolver,
    )
    annotations_by_target = {
        annotation.owner_structure_node_id: annotation
        for annotation in semantics.annotations
    }
    if tuple(annotations_by_target) != scope or len(annotations_by_target) != len(
        semantics.annotations
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INVALID_SCOPE,
            "Semantic annotations must cover the scoped nodes exactly once and in order.",
        )

    for target_id in scope:
        annotation = annotations_by_target[target_id]
        window = context.window_for(context.structure_nodes[target_id])
        expected_spans = tuple(
            (
                source_ref,
                start_offset,
                end_offset,
            )
            for source_ref, (start_offset, end_offset) in zip(
                window.refs,
                window.offsets,
                strict=True,
            )
        )
        observed_spans = tuple(
            (
                span.source_ref,
                span.start_offset,
                span.end_offset,
            )
            for span in annotation.grounding_spans
        )
        if observed_spans != expected_spans:
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Semantic grounding must cover the complete verified context in source order.",
            )
        selected_refs = annotation.selected_evidence_source_refs
        allowed_selected_refs = set(
            context.source_refs_for(context.structure_nodes[target_id])
        )
        if context.structure_nodes[target_id].kind is StructureNodeKind.SECTION:
            allowed_selected_refs.update(
                _section_provision_source_refs(target_id, context=context)
            )
        if not set(selected_refs).issubset(allowed_selected_refs):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Selected model evidence must belong to the target provision.",
            )
        if annotation.candidate_source is SemanticCandidateSource.MODEL:
            if (
                annotation.status is not SemanticAnnotationStatus.UNRESOLVED
                and not selected_refs
            ):
                raise SemanticClassificationRejected(
                    SemanticClassificationRejectionCode.INVALID_GROUNDING,
                    "Resolved model annotations require selected evidence.",
                )
        elif selected_refs:
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Only model annotations may carry selected evidence.",
            )
        if annotation.is_continuation_fragment and (
            annotation.candidate_source is not SemanticCandidateSource.MODEL
            or context.structure_nodes[target_id].kind
            not in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
            or context.structure_nodes[target_id].parent_structure_node_id is None
            or context.structure_nodes[target_id].explicit_label is not None
            or context.structure_nodes[target_id].list_ordinal is not None
        ):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Continuation fragments require a model-backed non-Section parent.",
            )
        if window.resolution_failed and (
            annotation.semantic_class is not SemanticClass.UNRESOLVED
            or annotation.status is not SemanticAnnotationStatus.UNRESOLVED
            or "text_resolution_failed" not in annotation.reason_codes
        ):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "A failed text view must remain explicitly UNRESOLVED.",
            )

    try:
        expected_plan = build_semantic_unit_plan_from_owner_spans(
            source_ir,
            tree,
            scope_owner_structure_node_ids=scope,
            owner_source_spans_by_id={
                annotation.owner_structure_node_id: tuple(
                    span
                    for span in annotation.grounding_spans
                    if span.source_ref
                    in set(
                        context.source_refs_for(
                            context.structure_nodes[
                                annotation.owner_structure_node_id
                            ]
                        )
                    )
                )
                for annotation in semantics.annotations
            },
            unitizer_version=semantics.unitizer_version,
        )
    except SemanticUnitPlanRejected as exc:
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INVALID_GROUNDING,
            "Semantic unit identity is not grounded by the supplied inputs.",
        ) from exc
    if (
        semantics.semantic_unit_plan_id != expected_plan.semantic_unit_plan_id
        or semantics.scope_semantic_unit_ids != expected_plan.semantic_unit_ids
        or tuple(annotation.semantic_unit_id for annotation in semantics.annotations)
        != expected_plan.semantic_unit_ids
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INVALID_SCOPE,
            "Semantic unit identity does not match the declared owner scope.",
        )


def _validate_semantic_classification_against_unit_plan(
    semantics: SemanticClassificationSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
    *,
    text_resolver: SemanticTextResolver | None,
) -> None:
    if not isinstance(semantic_unit_plan, SemanticUnitPlan):
        raise TypeError("semantic_unit_plan must be a SemanticUnitPlan")
    if (
        semantic_unit_plan.source_ir_id != source_ir.source_ir_id
        or semantic_unit_plan.structure_snapshot_id != structure_snapshot_id(tree)
        or semantics.semantic_unit_plan_id != semantic_unit_plan.semantic_unit_plan_id
        or semantics.unitizer_version != semantic_unit_plan.unitizer_version
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INPUT_MISMATCH,
            "Semantic classification does not belong to the supplied unit plan.",
        )

    owner_scope = _normalize_scope(
        tree,
        semantic_unit_plan.scope_owner_structure_node_ids,
    )
    unit_owner_ids = tuple(
        item.owner_structure_node_id for item in semantic_unit_plan.units
    )
    if (
        owner_scope != semantic_unit_plan.scope_owner_structure_node_ids
        or semantics.scope_semantic_unit_ids != semantic_unit_plan.semantic_unit_ids
        or semantics.scope_owner_structure_node_ids != unit_owner_ids
        or tuple(item.semantic_unit_id for item in semantics.annotations)
        != semantic_unit_plan.semantic_unit_ids
        or tuple(item.owner_structure_node_id for item in semantics.annotations)
        != unit_owner_ids
    ):
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INVALID_SCOPE,
            "Semantic classification must exactly preserve unit-plan order and ownership.",
        )

    context = _ClassificationContext(
        source_ir=source_ir,
        tree=tree,
        text_resolver=text_resolver,
    )
    units_by_owner: dict[str, list[tuple[SemanticGroundingSpan, ...]]] = {}
    for unit, annotation in zip(
        semantic_unit_plan.units,
        semantics.annotations,
        strict=True,
    ):
        owner = context.structure_nodes.get(unit.owner_structure_node_id)
        if (
            owner is None
            or owner.source_order != unit.source_order
            or owner.parent_structure_node_id != unit.parent_owner_structure_node_id
            or annotation.grounding_spans != unit.source_spans
        ):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Semantic annotation grounding does not match its declared unit.",
            )
        allowed_refs = {span.source_ref for span in unit.source_spans}
        if not set(annotation.selected_evidence_source_refs).issubset(allowed_refs):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Selected evidence must remain inside its semantic unit.",
            )
        if annotation.candidate_source is SemanticCandidateSource.MODEL:
            if (
                annotation.status is not SemanticAnnotationStatus.UNRESOLVED
                and not annotation.selected_evidence_source_refs
            ):
                raise SemanticClassificationRejected(
                    SemanticClassificationRejectionCode.INVALID_GROUNDING,
                    "Resolved model annotations require selected unit evidence.",
                )
        elif annotation.selected_evidence_source_refs:
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Only model annotations may carry selected evidence.",
            )
        if annotation.is_continuation_fragment and (
            annotation.candidate_source is not SemanticCandidateSource.MODEL
            or owner.kind
            not in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
            or owner.parent_structure_node_id is None
            or owner.explicit_label is not None
            or owner.list_ordinal is not None
        ):
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Continuation fragments require a model-backed non-Section owner.",
            )

        resolved_by_ref: dict[StructureSourceRef, _ResolvedText] = {}
        for span in unit.source_spans:
            resolved = resolved_by_ref.setdefault(
                span.source_ref,
                context.resolve(span.source_ref),
            )
            if (
                resolved.failed
                or span.end_offset > len(resolved.text)
                or span.end_offset <= span.start_offset
            ):
                raise SemanticClassificationRejected(
                    SemanticClassificationRejectionCode.INVALID_GROUNDING,
                    "Semantic unit spans must resolve to non-empty source text.",
                )
        units_by_owner.setdefault(unit.owner_structure_node_id, []).append(
            unit.source_spans
        )

    for owner_id in owner_scope:
        owner = context.structure_nodes[owner_id]
        observed_spans = tuple(
            span
            for unit_spans in units_by_owner.get(owner_id, ())
            for span in unit_spans
        )
        observed_refs = tuple(dict.fromkeys(span.source_ref for span in observed_spans))
        owner_source_refs = context.source_refs_for(owner)
        coverage_is_exact = observed_refs == owner_source_refs
        expected_bounds_by_ref = {
            source_ref: context.semantic_interval_for(owner, source_ref)
            for source_ref in owner_source_refs
        }
        previous_end_by_ref: dict[StructureSourceRef, int] = {
            source_ref: bounds[0]
            for source_ref, bounds in expected_bounds_by_ref.items()
        }
        for span in observed_spans:
            previous_end = previous_end_by_ref.get(span.source_ref)
            if previous_end is None:
                coverage_is_exact = False
                break
            if span.start_offset != previous_end:
                coverage_is_exact = False
                break
            previous_end_by_ref[span.source_ref] = span.end_offset
        if coverage_is_exact and any(
            previous_end_by_ref.get(source_ref) != bounds[1]
            for source_ref, bounds in expected_bounds_by_ref.items()
        ):
            coverage_is_exact = False
        if not coverage_is_exact:
            raise SemanticClassificationRejected(
                SemanticClassificationRejectionCode.INVALID_GROUNDING,
                "Semantic units must exactly partition every owner source view.",
            )


def _section_provision_source_refs(
    section_id: str,
    *,
    context: _ClassificationContext,
) -> set[StructureSourceRef]:
    """Return descendant refs owned by one Section, excluding nested Sections."""

    refs: set[StructureSourceRef] = set()
    pending = list(context.children.get(section_id, ()))
    while pending:
        node = pending.pop()
        if node.kind is StructureNodeKind.SECTION:
            continue
        refs.update(context.source_refs_for(node))
        pending.extend(context.children.get(node.structure_node_id, ()))
    return refs


class _ClassificationContext:
    def __init__(
        self,
        *,
        source_ir: SourceIRSnapshot,
        tree: StructureSnapshot,
        text_resolver: SemanticTextResolver | None,
    ) -> None:
        self.source_ir = source_ir
        self.tree = tree
        self.text_resolver = text_resolver
        self.source_nodes = {node.source_node_id: node for node in source_ir.nodes}
        self.source_cells: dict[str, TableCellIR] = {
            cell.cell_id: cell
            for source_node in source_ir.nodes
            if source_node.table_payload is not None
            for cell in source_node.table_payload.cells
        }
        self.visual_observations: dict[str, VisualTextObservation] = {
            observation.observation_id: observation
            for visual_snapshot in source_ir.visual_text_snapshots
            for observation in visual_snapshot.observations
        }
        self.structure_nodes = {node.structure_node_id: node for node in tree.nodes}
        self._noise_source_node_ids = _native_noise_source_node_ids(source_ir)
        reconciled_pages = {
            diagnostic.native_locator.page_number
            for diagnostic in source_ir.diagnostics
            if diagnostic.code
            in {
                "pdf_full_page_image_with_invisible_text_layer",
                "pdf_vector_text_with_low_native_unicode_coverage",
            }
            and diagnostic.native_locator is not None
            and diagnostic.native_locator.page_number is not None
        }
        self._noise_source_node_ids.update(
            node.source_node_id
            for node in source_ir.nodes
            if node.kind is SourceNodeKind.PARAGRAPH
            and node.native_locator.page_number in reconciled_pages
        )
        self.children: dict[str, list[StructureNode]] = {}
        for node in tree.nodes:
            if node.parent_structure_node_id is not None:
                self.children.setdefault(node.parent_structure_node_id, []).append(node)
        for children in self.children.values():
            children.sort(key=lambda node: node.source_order)
        self.continuations: dict[str, set[str]] = {}
        self.incoming_applies_to: dict[str, list[str]] = {}
        self.option_states: dict[str, StructureOptionState] = {}
        for relation in tree.relations:
            if relation.kind is StructureRelationKind.CONTINUES:
                source_node = self.structure_nodes[relation.source_structure_node_id]
                target_node = self.structure_nodes[relation.target_structure_node_id]
                if not _is_same_logical_continuation(source_node, target_node):
                    continue
                self.continuations.setdefault(
                    relation.source_structure_node_id, set()
                ).add(relation.target_structure_node_id)
                self.continuations.setdefault(
                    relation.target_structure_node_id, set()
                ).add(relation.source_structure_node_id)
            elif relation.kind is StructureRelationKind.APPLIES_TO:
                self.incoming_applies_to.setdefault(
                    relation.target_structure_node_id,
                    [],
                ).append(relation.source_structure_node_id)
            elif relation.kind is StructureRelationKind.OPTION_STATE:
                assert relation.option_state is not None
                self.option_states[relation.target_structure_node_id] = (
                    relation.option_state
                )
        self.source_rank = _source_ref_ranks(source_ir)
        self._text_cache: dict[StructureSourceRef, _ResolvedText] = {}
        self._window_cache: dict[str, _SemanticWindow] = {}

    def resolve(self, source_ref: StructureSourceRef) -> _ResolvedText:
        cached = self._text_cache.get(source_ref)
        if cached is not None:
            return cached
        native_text = self._native_text(source_ref)
        if self.text_resolver is None:
            resolved = _ResolvedText(native_text)
        else:
            try:
                override = self.text_resolver(source_ref, self.source_ir)
                if override is None:
                    resolved = _ResolvedText(native_text)
                elif isinstance(override, str):
                    resolved = _ResolvedText(override)
                else:
                    resolved = _ResolvedText("", failed=True)
            except Exception:
                resolved = _ResolvedText("", failed=True)
        self._text_cache[source_ref] = resolved
        return resolved

    def _native_text(self, source_ref: StructureSourceRef) -> str:
        if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
            source_node = self.source_nodes.get(source_ref.source_id)
            if source_node is None:
                _reject_invalid_grounding()
            return source_node.raw_text
        if source_ref.kind is StructureSourceRefKind.TABLE_CELL:
            cell = self.source_cells.get(source_ref.source_id)
            if cell is None:
                _reject_invalid_grounding()
            return cell.raw_text
        observation = self.visual_observations.get(source_ref.source_id)
        if observation is None:
            _reject_invalid_grounding()
        return observation.raw_text

    def window_for(self, target: StructureNode) -> _SemanticWindow:
        cached = self._window_cache.get(target.structure_node_id)
        if cached is not None:
            return cached

        ancestor_nodes = self._ancestors(target)
        component_nodes = self._continuation_component(target)
        direct_body_nodes = self._direct_body_nodes(target, component_nodes)
        own_refs = _deduplicated_sorted_refs(
            (
                source_ref
                for node in component_nodes
                for source_ref in self.source_refs_for(node)
            ),
            source_rank=self.source_rank,
        )
        ancestor_refs = _deduplicated_sorted_refs(
            (
                source_ref
                for node in ancestor_nodes
                for source_ref in self.source_refs_for(node)
            ),
            source_rank=self.source_rank,
        )
        direct_body_refs = _deduplicated_sorted_refs(
            (
                source_ref
                for node in direct_body_nodes
                for source_ref in self.source_refs_for(node)
            ),
            source_rank=self.source_rank,
        )

        declared_peer_item = self.is_declared_explicit_table_peer_item(target)
        node_by_ref: dict[StructureSourceRef, StructureNode] = {}
        for node in ancestor_nodes:
            for source_ref in self.source_refs_for(node):
                node_by_ref[source_ref] = node
        for node in direct_body_nodes:
            for source_ref in self.source_refs_for(node):
                node_by_ref[source_ref] = node
        for node in component_nodes:
            for source_ref in self.source_refs_for(node):
                node_by_ref[source_ref] = node
        refs = _deduplicated_sorted_refs(
            iter((*ancestor_refs, *direct_body_refs, *own_refs)),
            source_rank=self.source_rank,
        )
        resolved = tuple(self.resolve(source_ref) for source_ref in refs)
        resolved_by_ref = dict(zip(refs, resolved, strict=True))
        intervals = tuple(
            self.semantic_interval_for(node_by_ref[source_ref], source_ref)
            for source_ref in refs
        )
        texts = tuple(
            resolved_by_ref[source_ref].text[start_offset:end_offset]
            for source_ref, (start_offset, end_offset) in zip(
                refs,
                intervals,
                strict=True,
            )
        )
        window = _SemanticWindow(
            refs=refs,
            texts=texts,
            offsets=intervals,
            own_text=self._semantic_text_for_nodes(component_nodes),
            ancestor_text=self._semantic_text_for_nodes(ancestor_nodes),
            direct_body_text=self._semantic_text_for_nodes(
                direct_body_nodes
            ),
            resolution_failed=(
                any(item.failed for item in resolved)
                or any(
                    start_offset < 0
                    or end_offset < start_offset
                    or end_offset > len(resolved_by_ref[source_ref].text)
                    for source_ref, (start_offset, end_offset) in zip(
                        refs,
                        intervals,
                        strict=True,
                    )
                )
                or (
                    declared_peer_item
                    and self.explicit_table_peer_item(target) is None
                )
            ),
        )
        self._window_cache[target.structure_node_id] = window
        return window

    def _semantic_text_for_nodes(
        self,
        nodes: Iterable[StructureNode],
    ) -> str:
        segments: list[_ResolvedText] = []
        for node in nodes:
            for source_ref in self.source_refs_for(node):
                resolved = self.resolve(source_ref)
                start_offset, end_offset = self.semantic_interval_for(
                    node,
                    source_ref,
                )
                if (
                    resolved.failed
                    or start_offset < 0
                    or end_offset < start_offset
                    or end_offset > len(resolved.text)
                ):
                    segments.append(_ResolvedText("", failed=True))
                    continue
                segments.append(
                    _ResolvedText(resolved.text[start_offset:end_offset])
                )
        return _join_resolved_text(segments)

    def is_declared_explicit_table_peer_item(self, node: StructureNode) -> bool:
        if (
            node.kind is not StructureNodeKind.CLAUSE
            or node.boundary_basis is not StructureBoundaryBasis.EXPLICIT_NUMBER
            or node.list_ordinal is None
            or node.parent_structure_node_id is None
        ):
            return False
        parent = self.structure_nodes.get(node.parent_structure_node_id)
        source_refs = self.source_refs_for(node)
        return bool(
            parent is not None
            and parent.kind is StructureNodeKind.TABLE_ROW
            and len(source_refs) == 1
            and source_refs[0].kind is StructureSourceRefKind.TABLE_CELL
        )

    def explicit_table_peer_item(
        self,
        node: StructureNode,
    ) -> _ExplicitPeerItemView | None:
        if not self.is_declared_explicit_table_peer_item(node):
            return None
        source_ref = self.source_refs_for(node)[0]
        resolved = self.resolve(source_ref)
        if resolved.failed:
            return None
        matching_items = tuple(
            item
            for item in parse_explicit_numeric_peer_items(resolved.text)
            if item.ordinal == node.list_ordinal
        )
        if len(matching_items) != 1:
            return None
        item = matching_items[0]
        return _ExplicitPeerItemView(
            source_ref=source_ref,
            start_offset=item.start_offset,
            end_offset=item.end_offset,
            text=resolved.text[item.start_offset : item.end_offset],
        )

    def semantic_interval_for(
        self,
        node: StructureNode,
        source_ref: StructureSourceRef,
    ) -> tuple[int, int]:
        matching_spans = tuple(
            span
            for span in node.semantic_source_spans
            if span.source_ref == source_ref
        )
        if matching_spans:
            span = matching_spans[0]
            return (span.start_offset, span.end_offset)
        peer_item = self.explicit_table_peer_item(node)
        if peer_item is not None and peer_item.source_ref == source_ref:
            return (peer_item.start_offset, peer_item.end_offset)
        resolved = self.resolve(source_ref)
        return (0, len(resolved.text))

    def _ancestors(self, target: StructureNode) -> list[StructureNode]:
        ancestors: list[StructureNode] = []
        parent_id = target.parent_structure_node_id
        while parent_id is not None:
            parent = self.structure_nodes[parent_id]
            if parent.kind is not StructureNodeKind.DOCUMENT:
                ancestors.append(parent)
            parent_id = parent.parent_structure_node_id
        ancestors.reverse()
        return ancestors

    def _continuation_component(self, target: StructureNode) -> list[StructureNode]:
        pending = [target.structure_node_id]
        observed: set[str] = set()
        while pending:
            node_id = pending.pop()
            if node_id in observed:
                continue
            observed.add(node_id)
            pending.extend(self.continuations.get(node_id, ()))
        return sorted(
            (self.structure_nodes[node_id] for node_id in observed),
            key=lambda node: node.source_order,
        )

    def _first_paragraph(
        self,
        target: StructureNode,
        component_nodes: list[StructureNode],
    ) -> StructureNode | None:
        candidates = self._direct_body_nodes(target, component_nodes)
        return candidates[0] if candidates else None

    def _direct_body_nodes(
        self,
        target: StructureNode,
        component_nodes: list[StructureNode],
    ) -> list[StructureNode]:
        if target.kind not in {
            StructureNodeKind.SECTION,
            StructureNodeKind.CLAUSE,
            StructureNodeKind.GROUP,
            StructureNodeKind.LIST_GROUP,
        }:
            return []
        component_ids = {node.structure_node_id for node in component_nodes}
        candidates = [
            child
            for component_id in component_ids
            for child in self.children.get(component_id, ())
            if child.kind in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
        ]
        return sorted(
            candidates,
            key=lambda node: (
                node.source_order,
                node.structure_node_id,
            ),
        )

    def source_kinds_for(self, node: StructureNode) -> set[SourceNodeKind]:
        return {
            self.source_nodes[source_ref.source_id].kind
            for source_ref in self.source_refs_for(node)
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
            and source_ref.source_id in self.source_nodes
        }

    @staticmethod
    def source_refs_for(
        node: StructureNode,
    ) -> tuple[StructureSourceRef, ...]:
        return node.effective_semantic_source_refs

    def has_direct_numbered_children(self, node: StructureNode) -> bool:
        return any(
            child.kind is StructureNodeKind.CLAUSE and child.list_ordinal is not None
            for child in self.children.get(node.structure_node_id, ())
        )

    def has_explicit_table_peer_children(self, node: StructureNode) -> bool:
        return bool(
            node.kind is StructureNodeKind.TABLE_ROW
            and any(
                self.is_declared_explicit_table_peer_item(child)
                for child in self.children.get(node.structure_node_id, ())
            )
        )

    def is_reconciled_noise(self, node: StructureNode) -> bool:
        if node.parent_structure_node_id is None:
            return False
        parent = self.structure_nodes[node.parent_structure_node_id]
        if parent.kind is not StructureNodeKind.DOCUMENT:
            return False
        source_refs = self.source_refs_for(node)
        return bool(source_refs) and all(
            (
                source_ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_ref.source_id in self._noise_source_node_ids
            )
            for source_ref in source_refs
        )

    def has_direct_table_children(self, node: StructureNode) -> bool:
        return any(
            child.kind is StructureNodeKind.TABLE
            for child in self.children.get(node.structure_node_id, ())
        )

    def has_tabular_requirement_parent(self, node: StructureNode) -> bool:
        if node.kind is StructureNodeKind.TABLE:
            table = node
        elif node.kind is StructureNodeKind.TABLE_ROW:
            if node.parent_structure_node_id is None:
                return False
            table = self.structure_nodes[node.parent_structure_node_id]
            if table.kind is not StructureNodeKind.TABLE:
                return False
        else:
            return False
        if table.parent_structure_node_id is None:
            return False
        section = self.structure_nodes[table.parent_structure_node_id]
        if section.kind is not StructureNodeKind.SECTION:
            return False
        context_nodes = [section]
        first_paragraph = self._first_paragraph(section, [section])
        if first_paragraph is not None:
            context_nodes.append(first_paragraph)
        refs = _deduplicated_sorted_refs(
            (
                source_ref
                for context_node in context_nodes
                for source_ref in self.source_refs_for(context_node)
            ),
            source_rank=self.source_rank,
        )
        section_text = _normalize(
            _join_resolved_text(self.resolve(ref) for ref in refs)
        )
        return bool(
            _TABULAR_REQUIREMENT_CONTAINER_PATTERN.search(section_text)
            and not _matching_non_requirement_classes(section_text)
        )

    def compound_test_requirement_information(
        self,
        node: StructureNode,
    ) -> tuple[_InformationSignal, ...]:
        """Recognize structured test-row facets without a corpus-specific key.

        The row must keep an ordinal and at least five populated context cells,
        including two substantive cells and explicit test/verification
        evidence. Exactly one sample/phase value followed immediately by one
        schedule duration must form the populated tail. This rejects isolated
        quantities, narrative mentions, switched column roles, duplicate
        signals, merged/partial rows, and revision-history records.
        """

        if (
            node.kind is not StructureNodeKind.TABLE_ROW
            or node.table_row_index is None
            or node.table_row_index < 1
            or node.parent_structure_node_id is None
            or self.is_revision_history_data_row(node)
        ):
            return ()
        table_node = self.structure_nodes[node.parent_structure_node_id]
        if table_node.kind is not StructureNodeKind.TABLE:
            return ()

        source_table_nodes: dict[str, SourceNode] = {}
        for source_ref in self.source_refs_for(table_node):
            if source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
                continue
            source_node = self.source_nodes.get(source_ref.source_id)
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if table_node.identity_key.startswith("source:"):
            source_node = self.source_nodes.get(
                table_node.identity_key.removeprefix("source:")
            )
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if len(source_table_nodes) != 1:
            return ()
        table = next(iter(source_table_nodes.values())).table_payload
        if table is None or not 8 <= table.column_count <= 20:
            return ()

        row_cells = sorted(
            (
                cell
                for cell in table.cells
                if cell.row == node.table_row_index
            ),
            key=lambda cell: cell.column,
        )
        if (
            len(row_cells) != table.column_count
            or [cell.column for cell in row_cells]
            != list(range(table.column_count))
            or any(cell.row_span != 1 or cell.column_span != 1 for cell in row_cells)
        ):
            return ()
        row_source_ids = {
            source_ref.source_id
            for source_ref in self.source_refs_for(node)
            if source_ref.kind is StructureSourceRefKind.TABLE_CELL
        }
        if row_source_ids != {cell.cell_id for cell in row_cells}:
            return ()

        resolved_by_column = {
            cell.column: self.resolve(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            )
            for cell in row_cells
        }
        if any(item.failed for item in resolved_by_column.values()):
            return ()
        normalized_by_column = {
            column: _normalize(item.text)
            for column, item in resolved_by_column.items()
        }
        schedule_cells = tuple(
            cell
            for cell in row_cells
            if _SCHEDULE_DURATION_VALUE_PATTERN.fullmatch(
                normalized_by_column[cell.column]
            )
        )
        sample_phase_cells = tuple(
            cell
            for cell in row_cells
            if _SAMPLE_AND_PHASE_VALUE_PATTERN.fullmatch(
                normalized_by_column[cell.column]
            )
        )
        if len(schedule_cells) != 1 or len(sample_phase_cells) != 1:
            return ()
        schedule_cell = schedule_cells[0]
        sample_phase_cell = sample_phase_cells[0]
        if (
            sample_phase_cell.column < 5
            or schedule_cell.column != sample_phase_cell.column + 1
            or any(
                normalized_by_column[cell.column]
                for cell in row_cells
                if cell.column > schedule_cell.column
            )
        ):
            return ()

        context_cells = tuple(
            cell for cell in row_cells if cell.column < sample_phase_cell.column
        )
        populated_context = tuple(
            cell
            for cell in context_cells
            if normalized_by_column[cell.column]
        )
        if (
            len(populated_context) < 5
            or not context_cells
            or context_cells[0].column != 0
            or _COMPOUND_TEST_ROW_ORDINAL_PATTERN.fullmatch(
                normalized_by_column[0]
            )
            is None
            or sum(
                len(normalized_by_column[cell.column]) >= 16
                for cell in populated_context
            )
            < 2
        ):
            return ()
        context_text = "\n".join(
            normalized_by_column[cell.column] for cell in populated_context
        )
        if (
            not _matches_any(context_text, _COMPOUND_TEST_ROW_EVIDENCE_PATTERNS)
            or _matching_non_requirement_classes(context_text).intersection(
                _COMPOUND_TEST_ROW_DISALLOWED_CLASSES
            )
        ):
            return ()

        return (
            _InformationSignal(
                information_kind=(
                    SemanticInformationKind.SAMPLE_AND_PHASE_INFORMATION
                ),
                semantic_class=SemanticClass.TECHNICAL_INFORMATION,
                source_ref=StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=sample_phase_cell.cell_id,
                ),
                resolved_text=resolved_by_column[sample_phase_cell.column].text,
            ),
            _InformationSignal(
                information_kind=SemanticInformationKind.SCHEDULE_INFORMATION,
                semantic_class=SemanticClass.SCHEDULE,
                source_ref=StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=schedule_cell.cell_id,
                ),
                resolved_text=resolved_by_column[schedule_cell.column].text,
            ),
        )

    def is_material_requirement_data_row(self, node: StructureNode) -> bool:
        """Recognize one row in a conservative root-level material schema.

        The XLSX table must expose four distinct, ordered header roles:
        material identity, standard, acceptance criteria, and material
        requirement.  The latter three values form one aligned vertical
        applicability band covering at least two rows.  Every covered row
        must keep its own non-empty, unique, unmerged identity cell.  This
        lets the anchor row consume a direct value and covered rows consume
        the same value through ``APPLIES_TO`` without treating the shared
        material-requirement cell as an identity.
        """

        if (
            node.kind is not StructureNodeKind.TABLE_ROW
            or node.table_row_index is None
            or node.table_row_index < 1
            or node.parent_structure_node_id is None
            or self.is_revision_history_data_row(node)
        ):
            return False
        table_node = self.structure_nodes[node.parent_structure_node_id]
        if (
            table_node.kind is not StructureNodeKind.TABLE
            or table_node.parent_structure_node_id is None
            or self.structure_nodes[
                table_node.parent_structure_node_id
            ].kind
            is not StructureNodeKind.DOCUMENT
        ):
            return False

        source_table_nodes: dict[str, SourceNode] = {}
        for source_ref in self.source_refs_for(table_node):
            if source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
                continue
            source_node = self.source_nodes.get(source_ref.source_id)
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if table_node.identity_key.startswith("source:"):
            source_node = self.source_nodes.get(
                table_node.identity_key.removeprefix("source:")
            )
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if len(source_table_nodes) != 1:
            return False
        source_table_node = next(iter(source_table_nodes.values()))
        table = source_table_node.table_payload
        if (
            table is None
            or source_table_node.native_locator.format
            is not NativeDocumentFormat.XLSX
            or not 4 <= table.column_count <= 30
            or table.row_count < 3
        ):
            return False

        header_cells = tuple(
            sorted(
                (cell for cell in table.cells if cell.row == 0),
                key=lambda cell: cell.column,
            )
        )
        if not header_cells:
            return False
        role_columns: dict[str, set[int]] = {
            "identity": set(),
            "standard": set(),
            "acceptance": set(),
            "material_requirement": set(),
        }
        normalized_headers: list[str] = []
        for cell in header_cells:
            resolved = self.resolve(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            )
            if resolved.failed:
                return False
            header_text = _normalize(resolved.text)
            normalized_headers.append(header_text)
            roles = _material_header_roles(header_text)
            if len(roles) > 1:
                return False
            if roles:
                if cell.row_span != 1 or cell.column_span != 1:
                    return False
                role_columns[roles[0]].add(cell.column)
        if _MATERIAL_TABLE_FORBIDDEN_HEADER_PATTERN.search(
            "\n".join(normalized_headers)
        ):
            return False
        if any(len(columns) != 1 for columns in role_columns.values()):
            return False
        identity_column = next(iter(role_columns["identity"]))
        standard_column = next(iter(role_columns["standard"]))
        acceptance_column = next(iter(role_columns["acceptance"]))
        material_requirement_column = next(
            iter(role_columns["material_requirement"])
        )
        if not (
            identity_column
            < standard_column
            < acceptance_column
            < material_requirement_column
        ):
            return False

        role_cells = tuple(
            _cell_covering_position(
                table.cells,
                row=node.table_row_index,
                column=column,
            )
            for column in (
                standard_column,
                acceptance_column,
                material_requirement_column,
            )
        )
        if any(cell is None for cell in role_cells):
            return False
        shared_cells = tuple(cell for cell in role_cells if cell is not None)
        shared_band = (
            shared_cells[0].row,
            shared_cells[0].row_span,
        )
        if (
            shared_band[1] < 2
            or any(
                cell.column_span != 1
                or cell.row_span < 2
                or (cell.row, cell.row_span) != shared_band
                for cell in shared_cells
            )
        ):
            return False

        row_nodes = {
            child.table_row_index: child
            for child in self.children.get(table_node.structure_node_id, ())
            if child.kind is StructureNodeKind.TABLE_ROW
            and child.table_row_index is not None
        }
        covered_rows = range(shared_band[0], sum(shared_band))
        normalized_identities: list[str] = []
        for covered_row_index in covered_rows:
            covered_row_node = row_nodes.get(covered_row_index)
            identity_cell = _cell_covering_position(
                table.cells,
                row=covered_row_index,
                column=identity_column,
            )
            if (
                covered_row_node is None
                or identity_cell is None
                or identity_cell.row != covered_row_index
                or identity_cell.column != identity_column
                or identity_cell.row_span != 1
                or identity_cell.column_span != 1
            ):
                return False
            identity_ref = StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=identity_cell.cell_id,
            )
            if identity_ref not in self.source_refs_for(covered_row_node):
                return False
            resolved_identity = self.resolve(identity_ref)
            if resolved_identity.failed:
                return False
            normalized_identity = _normalize(resolved_identity.text)
            if (
                normalized_identity in _PERFORMANCE_PARAMETER_EMPTY_VALUES
                or _TABLE_NOTE_PREFIX.search(normalized_identity)
            ):
                return False
            normalized_identities.append(normalized_identity)

            if any(
                not self._row_receives_material_role(
                    table_node=table_node,
                    row_node=covered_row_node,
                    cell=cell,
                )
                for cell in shared_cells
            ):
                return False
        if (
            len(normalized_identities) != len(set(normalized_identities))
            or not normalized_identities
        ):
            return False

        for cell in shared_cells:
            resolved = self.resolve(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            )
            if (
                resolved.failed
                or _normalize(resolved.text)
                in _PERFORMANCE_PARAMETER_EMPTY_VALUES
            ):
                return False
        return True

    def _row_receives_material_role(
        self,
        *,
        table_node: StructureNode,
        row_node: StructureNode,
        cell: TableCellIR,
    ) -> bool:
        cell_ref = StructureSourceRef(
            kind=StructureSourceRefKind.TABLE_CELL,
            source_id=cell.cell_id,
        )
        if cell.row == row_node.table_row_index:
            return cell_ref in self.source_refs_for(row_node)
        return any(
            source.parent_structure_node_id == table_node.structure_node_id
            and source.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
            and cell_ref in self.source_refs_for(source)
            for source_id in self.incoming_applies_to.get(
                row_node.structure_node_id,
                (),
            )
            if (source := self.structure_nodes[source_id])
        )

    def is_performance_parameter_data_row(self, node: StructureNode) -> bool:
        """Recognize a conservative parameter-table schema without corpus IDs.

        The table must expose one complete, unmerged header row with distinct
        parameter and unit columns plus distinct lower, nominal and upper
        value columns. Only non-header rows with a named parameter and at
        least one non-placeholder value are eligible. A lone keyword, an
        inventory/order header, or a row containing only name/unit metadata is
        insufficient.
        """

        if (
            node.kind is not StructureNodeKind.TABLE_ROW
            or node.table_row_index is None
            or node.table_row_index < 1
            or node.parent_structure_node_id is None
        ):
            return False
        table_node = self.structure_nodes[node.parent_structure_node_id]
        if table_node.kind is not StructureNodeKind.TABLE:
            return False

        source_table_nodes: dict[str, SourceNode] = {}
        for source_ref in self.source_refs_for(table_node):
            if source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
                continue
            source_node = self.source_nodes.get(source_ref.source_id)
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if table_node.identity_key.startswith("source:"):
            source_node = self.source_nodes.get(
                table_node.identity_key.removeprefix("source:")
            )
            if source_node is not None and source_node.table_payload is not None:
                source_table_nodes[source_node.source_node_id] = source_node
        if len(source_table_nodes) != 1:
            return False
        table = next(iter(source_table_nodes.values())).table_payload
        if table is None or not 5 <= table.column_count <= 8:
            return False

        header_cells = sorted(
            (cell for cell in table.cells if cell.row == 0),
            key=lambda cell: cell.column,
        )
        if (
            len(header_cells) != table.column_count
            or [cell.column for cell in header_cells]
            != list(range(table.column_count))
            or any(
                cell.row_span != 1 or cell.column_span != 1
                for cell in header_cells
            )
        ):
            return False
        role_columns: dict[str, set[int]] = {
            role: set() for role, _ in _PERFORMANCE_PARAMETER_HEADER_PATTERNS
        }
        for cell in header_cells:
            resolved = self.resolve(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            )
            if resolved.failed:
                return False
            header_text = _normalize(resolved.text)
            if not header_text:
                return False
            for role, pattern in _PERFORMANCE_PARAMETER_HEADER_PATTERNS:
                if pattern.fullmatch(header_text):
                    role_columns[role].add(cell.column)
        if (
            len(role_columns["parameter"]) != 1
            or len(role_columns["unit"]) != 1
            or any(
                len(role_columns[role]) != 1
                for role in _PERFORMANCE_PARAMETER_VALUE_ROLES
            )
            or len(
                {
                    next(iter(role_columns[role]))
                    for role in role_columns
                }
            )
            != len(role_columns)
        ):
            return False

        row_cells = sorted(
            (
                cell
                for cell in table.cells
                if cell.row == node.table_row_index
            ),
            key=lambda cell: cell.column,
        )
        if (
            len(row_cells) != table.column_count
            or [cell.column for cell in row_cells]
            != list(range(table.column_count))
            or any(cell.row_span != 1 or cell.column_span != 1 for cell in row_cells)
        ):
            return False
        resolved_row = {
            cell.column: self.resolve(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            )
            for cell in row_cells
        }
        if any(item.failed for item in resolved_row.values()):
            return False
        parameter_column = next(iter(role_columns["parameter"]))
        parameter_text = _normalize(resolved_row[parameter_column].text)
        if (
            _PERFORMANCE_PARAMETER_HEADER_PATTERNS[0][1].fullmatch(parameter_text)
            or _TABLE_NOTE_PREFIX.search(parameter_text)
        ):
            return False
        value_columns = {
            next(iter(role_columns[role]))
            for role in _PERFORMANCE_PARAMETER_VALUE_ROLES
        }
        return bool(
            parameter_text
            and any(
                _normalize(resolved_row[column].text)
                not in _PERFORMANCE_PARAMETER_EMPTY_VALUES
                for column in value_columns
            )
        )

    def has_applicable_table_note(self, node: StructureNode) -> bool:
        if node.kind is not StructureNodeKind.TABLE_ROW:
            return False
        for source_id in self.incoming_applies_to.get(node.structure_node_id, ()):
            source = self.structure_nodes[source_id]
            if (
                source.kind is not StructureNodeKind.TABLE_ROW
                or source.parent_structure_node_id != node.parent_structure_node_id
            ):
                continue
            note_text = _normalize(
                _join_resolved_text(
                    self.resolve(ref)
                    for ref in self.source_refs_for(source)
                )
            )
            if _TABLE_NOTE_PREFIX.search(note_text):
                return True
        return False

    def option_state_for(self, node: StructureNode) -> StructureOptionState | None:
        return self.option_states.get(node.structure_node_id)

    def has_option_state_descendant(self, node: StructureNode) -> bool:
        pending = list(self.children.get(node.structure_node_id, ()))
        while pending:
            candidate = pending.pop()
            if candidate.structure_node_id in self.option_states:
                return True
            pending.extend(self.children.get(candidate.structure_node_id, ()))
        return False

    def is_revision_history_data_row(self, node: StructureNode) -> bool:
        """Recognize data rows only inside an explicit revision-history band.

        A version-looking token is not enough on its own.  The row must be a
        sibling of an earlier, explicit revision-history marker in the same
        native table, remain before the first blank separator, and appear in
        the small header/data band immediately following that marker.
        """

        if (
            node.kind is not StructureNodeKind.TABLE_ROW
            or node.parent_structure_node_id is None
        ):
            return False
        parent = self.structure_nodes[node.parent_structure_node_id]
        if parent.kind is not StructureNodeKind.TABLE:
            return False
        rows = tuple(
            child
            for child in self.children.get(parent.structure_node_id, ())
            if child.kind is StructureNodeKind.TABLE_ROW
            and child.table_row_index is not None
        )
        try:
            target_position = rows.index(node)
        except ValueError:
            return False

        row_texts = tuple(
            _normalize(
                _join_resolved_text(
                    self.resolve(ref)
                    for ref in self.source_refs_for(row)
                )
            )
            for row in rows
        )
        marker_positions = tuple(
            index
            for index, text in enumerate(row_texts[:target_position])
            if SemanticClass.VERSION_METADATA in _matching_non_requirement_classes(text)
        )
        if not marker_positions:
            return False
        marker_position = marker_positions[-1]
        if not 2 <= target_position - marker_position <= 5:
            return False
        if any(
            not row_texts[index]
            for index in range(marker_position + 1, target_position)
        ):
            return False
        return bool(_REVISION_DATA_ROW_PATTERN.match(row_texts[target_position]))


def _classify_window(
    node: StructureNode,
    window: _SemanticWindow,
    context: _ClassificationContext,
) -> _Decision:
    if window.resolution_failed:
        return _unresolved("text_resolution_failed")

    if context.is_reconciled_noise(node):
        return _candidate(SemanticClass.NOISE, reason="reconciled_page_noise")

    if node.boundary_basis is StructureBoundaryBasis.PAGE_FURNITURE:
        return _candidate(SemanticClass.NOISE, reason="page_furniture")

    if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK:
        return _candidate(SemanticClass.NOISE, reason="toc_block")

    source_kinds = context.source_kinds_for(node)
    if source_kinds and source_kinds.issubset(_NOISE_SOURCE_KINDS):
        return _candidate(SemanticClass.NOISE, reason="noise_source_kind")
    if SourceNodeKind.ATTACHMENT in source_kinds:
        return _candidate(SemanticClass.ATTACHMENT, reason="attachment_source_kind")

    if context.is_revision_history_data_row(node):
        return _candidate(
            SemanticClass.VERSION_METADATA,
            reason="revision_history_data_row",
        )

    own = _normalize(window.own_text)
    ancestors = _normalize(window.ancestor_text)
    direct_body = _normalize(window.direct_body_text)
    section_evidence = "\n".join(part for part in (own, direct_body) if part)

    explicit_peer_item = context.explicit_table_peer_item(node)
    if explicit_peer_item is not None:
        if _matches_any(own, _EXPLICIT_TABLE_PEER_REQUIREMENT_PATTERNS):
            return _candidate(
                SemanticClass.REQUIREMENT,
                subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                reason="explicit_table_cell_peer_requirement",
            )
        return _unresolved("explicit_table_cell_peer_without_requirement_signal")

    if context.has_explicit_table_peer_children(node):
        return _candidate(
            SemanticClass.TECHNICAL_INFORMATION,
            reason="explicit_table_cell_peer_container",
        )

    if (
        node.kind is StructureNodeKind.SECTION
        and context.has_option_state_descendant(node)
        and _REGULATORY_OPTION_CONTAINER_PATTERN.search(section_evidence)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.REGULATION_REQUIREMENT,
            reason="regulatory_certification_option_container_signal",
        )

    option_state = context.option_state_for(node)
    if option_state is not None:
        inherited_classes = _matching_non_requirement_classes(ancestors)
        if len(inherited_classes) > 1:
            return _unresolved("conflicting_ancestor_semantic_signals")
        if inherited_classes:
            inherited_class = next(iter(inherited_classes))
            return _candidate(
                inherited_class,
                reason=f"ancestor_{inherited_class.value.lower()}_context",
            )
        return _candidate(
            SemanticClass.TECHNICAL_INFORMATION,
            reason=f"option_state_{option_state.value}",
        )

    compound_test_information = context.compound_test_requirement_information(node)
    if compound_test_information:
        return _Decision(
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            status=SemanticAnnotationStatus.CANDIDATE,
            reason_codes=(
                "compound_test_requirement_row",
                "sample_and_phase_information_signal",
                "schedule_information_signal",
            ),
        )

    if (
        node.kind is StructureNodeKind.TABLE_ROW
        and context.is_material_requirement_data_row(node)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            reason="material_requirement_table_row",
        )

    if (
        node.kind is StructureNodeKind.TABLE_ROW
        and context.is_performance_parameter_data_row(node)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            reason="explicit_performance_parameter_schema",
        )

    if (
        node.kind is StructureNodeKind.TABLE_ROW
        and context.has_tabular_requirement_parent(node)
        and context.has_applicable_table_note(node)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            reason="applicable_table_note_parameter_row",
        )

    if node.kind in {
        StructureNodeKind.TABLE,
        StructureNodeKind.TABLE_ROW,
    } and context.has_tabular_requirement_parent(node):
        return _candidate(
            SemanticClass.TECHNICAL_INFORMATION,
            reason="tabular_requirement_content",
        )

    numbered_clause_context = _numbered_clause_context_decision(
        node,
        ancestors,
    )
    if numbered_clause_context is not None:
        return numbered_clause_context

    direct_equipment = _matches_any(
        section_evidence if _is_container(node) else own,
        _EQUIPMENT_PATTERNS,
    )
    direct_objective = _matches_any(
        section_evidence if _is_container(node) else own,
        _OBJECTIVE_PATTERNS,
    )
    ancestor_equipment = _matches_any(ancestors, _EQUIPMENT_PATTERNS)
    ancestor_objective = _matches_any(ancestors, _OBJECTIVE_PATTERNS)
    if _is_container(node) and _OBJECTIVE_HEADING_PATTERN.search(own):
        direct_objective = bool(
            _OBJECTIVE_ACTION_PATTERN.search(direct_body)
            or _matches_any(ancestors, _OBJECTIVE_PATTERNS)
            or re.search(r"(?:试验|测试|验证|检验)", ancestors)
        )
    elif ancestor_objective and _OBJECTIVE_ACTION_PATTERN.search(own):
        direct_objective = True
    if direct_equipment and direct_objective:
        return _unresolved("conflicting_requirement_subtype_signals")

    direct_class_evidence = (
        section_evidence
        if node.kind is StructureNodeKind.SECTION
        and context.has_direct_table_children(node)
        else own
    )
    direct_classes = _matching_non_requirement_classes(direct_class_evidence)
    if len(direct_classes) > 1:
        return _unresolved("conflicting_semantic_signals")
    if (direct_equipment or direct_objective) and direct_classes:
        return _unresolved("conflicting_semantic_signals")

    if direct_equipment:
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
            reason="test_equipment_signal",
        )
    if direct_objective:
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.TEST_OBJECTIVE_REQUIREMENT,
            reason="test_objective_signal",
        )

    if (
        node.kind is StructureNodeKind.SECTION
        and context.has_direct_table_children(node)
        and SemanticClass.REFERENCE in direct_classes
        and _matches_any(section_evidence, _NORMATIVE_APPLICABILITY_PATTERNS)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.REGULATION_REQUIREMENT,
            reason="normative_applicability_container_signal",
        )

    modal_evidence = section_evidence if _is_container(node) else own
    has_modal_requirement = _matches_any(
        modal_evidence,
        _MODAL_REQUIREMENT_PATTERNS,
    )
    has_implicit_constraint = _matches_any(
        modal_evidence,
        _IMPLICIT_CONSTRAINT_PATTERNS,
    )
    if (
        not _is_container(node)
        and has_modal_requirement
        and SemanticClass.REFERENCE in direct_classes
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.REGULATION_REQUIREMENT,
            reason="normative_applicability_signal",
        )

    if direct_classes:
        direct_class = next(iter(direct_classes))
        return _candidate(
            direct_class,
            reason=f"{direct_class.value.lower()}_signal",
        )

    if (
        node.kind is StructureNodeKind.SECTION
        and context.has_direct_table_children(node)
        and _TABULAR_REQUIREMENT_CONTAINER_PATTERN.search(section_evidence)
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            reason="tabular_requirement_container_signal",
        )

    if node.kind is StructureNodeKind.SECTION and context.has_direct_numbered_children(
        node
    ):
        container_evidence = "\n".join(
            part for part in (own, direct_body, ancestors) if part
        )
        if _NUMBERED_TEST_METHOD_CONTAINER_PATTERN.search(container_evidence):
            return _candidate(
                SemanticClass.REQUIREMENT,
                subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
                reason="numbered_test_method_container_signal",
            )
        if _NUMBERED_REQUIREMENT_CONTAINER_PATTERN.search(section_evidence):
            return _candidate(
                SemanticClass.REQUIREMENT,
                subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                reason="numbered_requirement_container_signal",
            )

    if has_modal_requirement or has_implicit_constraint:
        subtype = RequirementSubtype.PRODUCT_REQUIREMENT
        reason_codes = [
            (
                "modal_requirement_signal"
                if has_modal_requirement
                else "implicit_constraint_signal"
            )
        ]
        ancestor_classes = _matching_non_requirement_classes(ancestors)
        if len(ancestor_classes) > 1:
            return _unresolved("conflicting_ancestor_semantic_signals")
        if ancestor_equipment and ancestor_objective:
            return _unresolved("conflicting_requirement_subtype_signals")
        if SemanticClass.REFERENCE in ancestor_classes:
            subtype = RequirementSubtype.REGULATION_REQUIREMENT
            reason_codes.append("ancestor_reference_context")
        elif ancestor_equipment:
            subtype = RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
            reason_codes.append("ancestor_test_equipment_context")
        elif ancestor_objective:
            subtype = RequirementSubtype.TEST_OBJECTIVE_REQUIREMENT
            reason_codes.append("ancestor_test_objective_context")
        return _Decision(
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=subtype,
            status=SemanticAnnotationStatus.CANDIDATE,
            reason_codes=tuple(sorted(reason_codes)),
        )

    if (
        not _is_container(node)
        and _matches_any(own, _TECHNICAL_EXPLANATION_PATTERNS)
    ):
        return _candidate(
            SemanticClass.TECHNICAL_INFORMATION,
            reason="technical_explanation_signal",
        )

    if _is_container(node):
        return _candidate(SemanticClass.HEADING, reason="structure_heading")

    inherited_classes = _matching_non_requirement_classes(ancestors)
    if len(inherited_classes) > 1:
        return _unresolved("conflicting_ancestor_semantic_signals")
    if inherited_classes:
        inherited_class = next(iter(inherited_classes))
        return _candidate(
            inherited_class,
            reason=f"ancestor_{inherited_class.value.lower()}_context",
        )

    return _unresolved("no_confident_rule")


def _candidate(
    semantic_class: SemanticClass,
    *,
    subtype: RequirementSubtype | None = None,
    reason: str,
) -> _Decision:
    return _Decision(
        semantic_class=semantic_class,
        requirement_subtype=subtype,
        status=SemanticAnnotationStatus.CANDIDATE,
        reason_codes=(reason,),
    )


def _numbered_clause_context_decision(
    node: StructureNode,
    ancestor_text: str,
) -> _Decision | None:
    if node.kind is not StructureNodeKind.CLAUSE or node.list_ordinal is None:
        return None
    ancestor_lines = tuple(
        line.strip() for line in ancestor_text.splitlines() if line.strip()
    )[-2:]
    if any(
        _NUMBERED_TEST_METHOD_CONTAINER_PATTERN.search(line) for line in ancestor_lines
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
            reason="ancestor_numbered_test_method_context",
        )
    if any(
        _NUMBERED_REQUIREMENT_CONTAINER_PATTERN.search(line) for line in ancestor_lines
    ):
        return _candidate(
            SemanticClass.REQUIREMENT,
            subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            reason="ancestor_numbered_requirement_context",
        )
    return None


def _unresolved(reason: str) -> _Decision:
    return _Decision(
        semantic_class=SemanticClass.UNRESOLVED,
        requirement_subtype=None,
        status=SemanticAnnotationStatus.UNRESOLVED,
        reason_codes=(reason,),
    )


def _matching_non_requirement_classes(text: str) -> set[SemanticClass]:
    return {
        semantic_class
        for semantic_class, patterns in _CLASS_PATTERNS
        if _matches_any(text, patterns)
    }


def _native_noise_source_node_ids(source_ir: SourceIRSnapshot) -> set[str]:
    return {
        node.source_node_id
        for node in source_ir.nodes
        if node.kind in _NOISE_SOURCE_KINDS
    }


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return bool(text) and any(pattern.search(text) for pattern in patterns)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _material_header_roles(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    is_material_requirement = bool(
        _MATERIAL_REQUIREMENT_HEADER_PATTERN.search(text)
    )
    is_acceptance = bool(
        _ACCEPTANCE_HEADER_PATTERN.search(text)
        or _TEST_CRITERIA_HEADER_PATTERN.search(text)
    )
    is_standard = bool(
        _STANDARD_HEADER_PATTERN.search(text)
        and not is_acceptance
        and not is_material_requirement
    )
    is_identity = bool(
        _MATERIAL_HEADER_PATTERN.search(text)
        and not is_material_requirement
        and not is_acceptance
        and not is_standard
    )
    roles: list[str] = []
    if is_identity:
        roles.append("identity")
    if is_standard:
        roles.append("standard")
    if is_acceptance:
        roles.append("acceptance")
    if is_material_requirement:
        roles.append("material_requirement")
    return tuple(roles)


def _cell_covering_position(
    cells: tuple[TableCellIR, ...],
    *,
    row: int,
    column: int,
) -> TableCellIR | None:
    matches = tuple(
        cell
        for cell in cells
        if cell.row <= row < cell.row + cell.row_span
        and cell.column <= column < cell.column + cell.column_span
    )
    return matches[0] if len(matches) == 1 else None


def _is_container(node: StructureNode) -> bool:
    return (
        node.kind
        in {
            StructureNodeKind.SECTION,
            StructureNodeKind.CLAUSE,
            StructureNodeKind.GROUP,
            StructureNodeKind.LIST_GROUP,
        }
        or node.boundary_basis is StructureBoundaryBasis.EXPLICIT_NUMBER
    )


def _is_same_logical_continuation(
    source: StructureNode,
    target: StructureNode,
) -> bool:
    # The structure relation can mean either a logical fragment or merely that
    # a section's sequence continues on the next page.  Only like-kind siblings
    # (or an explicit CONTINUATION node) are safe to merge semantically.
    if StructureNodeKind.CONTINUATION in {source.kind, target.kind}:
        return (
            source.parent_structure_node_id == target.parent_structure_node_id
            or source.parent_structure_node_id == target.structure_node_id
            or target.parent_structure_node_id == source.structure_node_id
        )
    if source.kind is not target.kind:
        return False
    if source.parent_structure_node_id != target.parent_structure_node_id:
        return False
    if source.kind is StructureNodeKind.CLAUSE:
        return source.list_ordinal == target.list_ordinal
    if source.kind is StructureNodeKind.SECTION:
        return source.explicit_label == target.explicit_label
    return True


def _normalize_scope(
    tree: StructureSnapshot,
    requested_scope: tuple[str, ...] | None,
) -> tuple[str, ...]:
    ordered_nodes = tuple(
        node for node in tree.nodes if node.kind is not StructureNodeKind.DOCUMENT
    )
    ordered_ids = tuple(node.structure_node_id for node in ordered_nodes)
    if requested_scope is None:
        return ordered_ids
    if len(requested_scope) != len(set(requested_scope)):
        _reject_invalid_scope("Semantic scope contains duplicate structure nodes.")
    valid_ids = set(ordered_ids)
    if any(node_id not in valid_ids for node_id in requested_scope):
        _reject_invalid_scope(
            "Semantic scope must reference non-document nodes in this StructureTree."
        )
    requested_set = set(requested_scope)
    canonical = tuple(node_id for node_id in ordered_ids if node_id in requested_set)
    if canonical != requested_scope:
        _reject_invalid_scope(
            "Semantic scope must preserve StructureTree source order."
        )
    return canonical


def _validate_input_types_and_binding(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
) -> None:
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if tree.source_ir_id != source_ir.source_ir_id:
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INPUT_MISMATCH,
            "StructureTree is not bound to the supplied SourceIR.",
        )
    try:
        validate_structure_snapshot_against_source_ir(tree, source_ir)
    except Exception as exc:
        raise SemanticClassificationRejected(
            SemanticClassificationRejectionCode.INPUT_MISMATCH,
            "StructureTree grounding does not match the supplied SourceIR.",
        ) from exc


def _source_ref_ranks(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, tuple[int, int, int, int, str]]:
    ranks: dict[StructureSourceRef, tuple[int, int, int, int, str]] = {}
    for source_node in source_ir.nodes:
        source_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=source_node.source_node_id,
        )
        ranks[source_ref] = (
            source_node.source_order,
            0,
            0,
            0,
            source_node.source_node_id,
        )
        if source_node.table_payload is None:
            continue
        for cell in source_node.table_payload.cells:
            cell_ref = StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=cell.cell_id,
            )
            ranks[cell_ref] = (
                source_node.source_order,
                1,
                cell.row,
                cell.column,
                cell.cell_id,
            )
    visual_order = 0
    visual_base_order = (
        max(
            (node.source_order for node in source_ir.nodes),
            default=-1,
        )
        + 1
    )
    for visual_snapshot in sorted(
        source_ir.visual_text_snapshots,
        key=lambda item: (
            item.page_number,
            item.provider_id,
            item.provider_version,
            item.visual_text_snapshot_id,
        ),
    ):
        for observation in sorted(
            visual_snapshot.observations,
            key=lambda item: (
                item.observation_index,
                item.bbox[1],
                item.bbox[0],
                item.observation_id,
            ),
        ):
            visual_ref = StructureSourceRef(
                kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                source_id=observation.observation_id,
            )
            ranks[visual_ref] = (
                visual_base_order + visual_order,
                2,
                observation.page_number,
                observation.observation_index,
                observation.observation_id,
            )
            visual_order += 1
    return ranks


def _deduplicated_sorted_refs(
    refs: Iterable[StructureSourceRef],
    *,
    source_rank: dict[StructureSourceRef, tuple[int, int, int, int, str]],
) -> tuple[StructureSourceRef, ...]:
    unique: set[StructureSourceRef] = set()
    for source_ref in refs:
        if source_ref not in source_rank:
            _reject_invalid_grounding()
        unique.add(source_ref)
    return tuple(sorted(unique, key=source_rank.__getitem__))


def _join_resolved_text(items: Iterable[_ResolvedText]) -> str:
    return "\n".join(item.text.strip() for item in items if item.text.strip())


def _reject_invalid_scope(message: str) -> None:
    raise SemanticClassificationRejected(
        SemanticClassificationRejectionCode.INVALID_SCOPE,
        message,
    )


def _reject_invalid_grounding() -> None:
    raise SemanticClassificationRejected(
        SemanticClassificationRejectionCode.INVALID_GROUNDING,
        "Semantic grounding references a source outside the supplied SourceIR.",
    )


__all__ = [
    "DEFAULT_SEMANTIC_CLASSIFIER_ID",
    "DEFAULT_SEMANTIC_CLASSIFIER_VERSION",
    "SemanticClassificationRejected",
    "SemanticClassificationRejectionCode",
    "SemanticTextResolver",
    "classify_content",
    "classify_table_row_information",
    "validate_semantic_classification_against_inputs",
]
