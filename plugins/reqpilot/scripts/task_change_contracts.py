"""Typed semantic choices for the existing annotation/decomposition/review core.

Binders resolve display IDs and exact quotations only. They do not select task
versions, create authorization, fill source gaps, or assign source/Snn identity.
"""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, StrictInt, StrictStr, model_validator
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_decomposition import (
    build_source_item_decomposition_packet, compile_source_item_decomposition,
)
from app.application.agent_workflow.source_item_visual import SourceItemImageDecisionV1
from task_contracts import (
    InputModel, Nonempty, ItemDecisionsRequest, TextDecisionInput, TableDecisionInput,
    TextItemInput, TableRecordInput, LocalItemKey,
    _canonical_sets, _unique, bind_item_decisions,
)


def source_item_record(baseline, reference):
    """Resolve an exact SRC display ID or stable source-record ID, never REQ/index."""
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    matches = [r for r in baseline.records if reference in {r.source_record_id, r.source_display_id}]
    if len(matches) != 1:
        raise ValueError('SourceItem reference must identify exactly one current SRC or source-record ID')
    return matches[0]


class VisualReadingInput(InputModel):
    source_item_id: Nonempty = Field(description='Current SRC display ID or stable source-record ID')
    image_anchor_ids: tuple[Nonempty, ...] = Field(min_length=1, max_length=512)
    text: Nonempty = Field(description='Agent visual interpretation, separate from original source text')
    association_reason: Nonempty
    unresolved: tuple[Nonempty, ...] = ()

    @model_validator(mode='after')
    def unique_anchors(self):
        if len(self.image_anchor_ids) != len(set(self.image_anchor_ids)):
            raise ValueError('Visual reading image anchors must be unique')
        return self


class TextNumberReference(InputModel):
    kind: Literal['TEXT', 'MAP_HEADING']
    content_id: Nonempty
    start_offset: Annotated[StrictInt, Field(ge=0)]
    end_offset: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode='after')
    def nonempty_range(self):
        if self.end_offset <= self.start_offset:
            raise ValueError('Source reference must be a nonempty half-open range')
        return self


class TextExtentReference(TextNumberReference):
    kind: Literal['TEXT']


class CellNumberReference(InputModel):
    kind: Literal['TABLE_CELL']
    fragment_id: Nonempty
    cell_id: Nonempty
    start_offset: Annotated[StrictInt, Field(ge=0)]
    end_offset: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode='after')
    def nonempty_range(self):
        if self.end_offset <= self.start_offset:
            raise ValueError('Source reference must be a nonempty half-open range')
        return self


NumberReference = TextNumberReference | CellNumberReference
ExtentReference = TextExtentReference | CellNumberReference


class SourceNumberInput(InputModel):
    value: Nonempty
    source_number_ref: NumberReference
    source_extent: tuple[ExtentReference, ...] = Field(default=None, min_length=1)


class AnnotationUpdateInput(InputModel):
    source_item_id: Nonempty
    source_number: StrictStr = None
    source_number_ref: NumberReference = None
    source_numbers: tuple[SourceNumberInput, ...] = None
    topics: tuple[Literal['REGULATORY', 'VERIFICATION', 'LOGISTICS_PACKAGING', 'STANDARDS'], ...] = None
    context_source_ids: tuple[Nonempty, ...] = None
    reason: Nonempty

    @model_validator(mode='after')
    def explicit_update(self):
        fields = self.model_fields_set
        if not fields & {'source_number', 'source_numbers', 'topics', 'context_source_ids'}:
            raise ValueError('Annotation update needs an actual changed field')
        if 'source_numbers' in fields and fields & {'source_number', 'source_number_ref'}:
            raise ValueError('Use source_numbers or the scalar source_number pair')
        if self.source_number:
            if self.source_number_ref is None:
                raise ValueError('A nonempty original number needs its actual original reference')
        elif self.source_number_ref is not None:
            raise ValueError('A number reference requires a nonempty source_number')
        for field in ('topics', 'context_source_ids'):
            values = getattr(self, field)
            if values is not None and len(values) != len(set(values)):
                raise ValueError(field + ' must be unique')
        return self


class AnnotationChangesRequest(InputModel):
    visual_readings: tuple[VisualReadingInput, ...] = ()
    annotation_updates: tuple[AnnotationUpdateInput, ...] = ()

    @model_validator(mode='after')
    def actual_changes(self):
        if not self.visual_readings and not self.annotation_updates:
            raise ValueError('Supply explicit visual readings or annotation updates')
        return self


class ImageScopeInput(InputModel):
    node_id: Nonempty
    items: tuple[SourceItemImageDecisionV1, ...] = Field(max_length=4096)


class ImageItemsRequest(InputModel):
    scopes: tuple[ImageScopeInput, ...] = Field(min_length=1)

    @model_validator(mode='before')
    @classmethod
    def metadata(cls, value):
        return _canonical_sets(value)


def bind_image_items(value, document_map, baseline=None, *, table_previews=None):
    from app.application.agent_workflow.source_item_visual import build_source_item_image_scope_packet
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    if baseline is not None:
        document_map, baseline = _pair(document_map, baseline)
    value = ImageItemsRequest.model_validate(value)
    seen = set(); scopes = []
    for decision in value.scopes:
        if decision.node_id in seen:
            raise ValueError('Image scope decisions must be unique')
        seen.add(decision.node_id)
        packet = build_source_item_image_scope_packet(document_map, node_id=decision.node_id,
            baseline=baseline, table_previews=table_previews)
        standalone = {image['anchor_id'] for image in packet['images']}
        selected = [anchor for item in decision.items for anchor in item.image_anchor_ids]
        if len(selected) != len(set(selected)) or set(selected) != standalone:
            raise ValueError('Image items must cover exactly the standalone anchors; table-owned images cannot become separate SRCs')
        scopes.append({'node_id': decision.node_id, 'packet_sha256': packet['packet_sha256'],
            'items': [item.model_dump(mode='json') for item in decision.items]})
    return {'schema_version': 'reqpilot-image-decisions/v1', 'source_sha256': document_map.source_sha256,
        'document_map_artifact_sha256': document_map.artifact_sha256, 'scopes': scopes}


class DecompositionRange(InputModel):
    segment_index: Annotated[StrictInt, Field(ge=0)]
    quote: Nonempty = Field(default=None, description='Exact unique quote within this parent packet segment')
    start_offset: Annotated[StrictInt, Field(ge=0)] = None
    end_offset: Annotated[StrictInt, Field(gt=0)] = None

    @model_validator(mode='after')
    def exact_selection(self):
        offsets = self.model_fields_set & {'start_offset', 'end_offset'}
        if self.quote is not None:
            if offsets:
                raise ValueError('Select a packet range using quote or offsets, not both')
        elif offsets != {'start_offset', 'end_offset'} or self.end_offset <= self.start_offset:
            raise ValueError('Supply an exact quote or a nonempty half-open packet range')
        return self


class DecompositionChild(InputModel):
    text: Nonempty
    derivation_type: Literal['VERBATIM_SPAN', 'NORMALIZED_DERIVATION']
    source_ranges: tuple[DecompositionRange, ...] = Field(min_length=1)
    requirement_kinds: tuple[core.RequirementKind, ...] = Field(default=None, min_length=1, max_length=5)


class DecompositionContext(InputModel):
    source_range: DecompositionRange
    disposition: Literal['CONTEXT', 'HEADING', 'METHOD', 'PRESENTATION', 'REFERENCE', 'UNRESOLVED']


class DecompositionDecision(InputModel):
    source_item_id: Nonempty
    action: Literal['SPLIT', 'KEEP', 'UNDO']
    reason: Nonempty
    children: tuple[DecompositionChild, ...] = ()
    context_ranges: tuple[DecompositionContext, ...] = ()
    logic: Literal['ALL_OF', 'ANY_OF'] = 'ALL_OF'

    @model_validator(mode='after')
    def action_shape(self):
        if self.action == 'SPLIT' and len(self.children) < 2:
            raise ValueError('SPLIT needs at least two useful children')
        if self.action != 'SPLIT' and (self.children or self.context_ranges or 'logic' in self.model_fields_set):
            raise ValueError('KEEP/UNDO cannot silently discard supplied split ranges or logic')
        return self


class DecompositionAuthorization(InputModel):
    request: Nonempty = Field(default=None, description='Actual user authorization; service may supply the recorded task goal')
    source_item_ids: tuple[Nonempty, ...] = Field(min_length=1)


class DecompositionRequest(InputModel):
    authorization: DecompositionAuthorization
    decisions: tuple[DecompositionDecision, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def metadata(cls, value):
        return _canonical_sets(value)


class ClassificationChangeInput(core.SourceRequirementClassificationCoordinationDecisionV1):
    source_record_id: Nonempty = Field(description='Current SRC display ID or stable source-record ID; field remains source_record_id')


class ExistingTextItemInput(TextItemInput):
    item_key: LocalItemKey = Field(description='Required: preserve the existing item_key returned by the affected scope template.')


class ExistingTableRecordInput(TableRecordInput):
    record_key: LocalItemKey = Field(description='Required: preserve the existing record_key returned by the affected scope template.')


class ExistingTextDecisionInput(TextDecisionInput):
    items: tuple[ExistingTextItemInput,...] = Field(default=(),max_length=8192)


class ExistingTableDecisionInput(TableDecisionInput):
    records: tuple[ExistingTableRecordInput,...] = Field(max_length=8192)


class ScopeReplacementInput(InputModel):
    operation: Literal['REPLACE'] = 'REPLACE'
    affected_scope_ids: tuple[Nonempty, ...] = Field(min_length=1)
    text_decisions: tuple[ExistingTextDecisionInput, ...] = ()
    table_decisions: tuple[ExistingTableDecisionInput, ...] = ()
    comparison_scope_ids: tuple[Nonempty, ...] = ()


class ExistingItemsChangeRequest(InputModel):
    decisions: tuple[ClassificationChangeInput, ...] = ()
    model_id: Nonempty = None
    visual_readings: tuple[VisualReadingInput, ...] = ()
    annotation_updates: tuple[AnnotationUpdateInput, ...] = ()
    scope_revision: ScopeReplacementInput = None
    addressed_feedback_ids: tuple[Nonempty, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def metadata(cls, value):
        return _canonical_sets(value)

    @model_validator(mode='after')
    def compatible_changes(self):
        if self.decisions and not self.model_id:
            raise ValueError('Classification needs the actual model_id')
        if self.decisions and self.scope_revision is not None:
            raise ValueError('Scope replacement already contains classification; do not combine coordination')
        if len(self.addressed_feedback_ids) != len(set(self.addressed_feedback_ids)):
            raise ValueError('Feedback references must be unique')
        if not (self.decisions or self.visual_readings or self.annotation_updates or self.scope_revision):
            raise ValueError('Supply explicit existing-item changes')
        return self


def _pair(document_map, baseline):
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if baseline.source_sha256 != document_map.source_sha256 or baseline.document_map_artifact_sha256 != document_map.artifact_sha256:
        raise ValueError('Changes require one exact source/Map/baseline pair')
    return document_map, baseline


def _resolved_changes(value, document_map, baseline):
    result = value.model_dump(mode='json', exclude_unset=True)
    for key in ('visual_readings', 'annotation_updates'):
        seen = set()
        for change in result.get(key, []):
            record = source_item_record(baseline, change['source_item_id'])
            change['source_item_id'] = record.source_record_id
            if record.source_record_id in seen:
                raise ValueError('Duplicate SourceItem decisions after resolving SRC references')
            seen.add(record.source_record_id)
            if key == 'annotation_updates' and 'context_source_ids' in change:
                change['context_source_ids'] = [source_item_record(baseline, sid).source_record_id for sid in change['context_source_ids']]
            if key == 'visual_readings':
                owner = next(n for n in document_map.nodes if n.node_id == record.node_id)
                anchors = {a.anchor_id for a in owner.source_anchors if a.anchor_kind in {'IMAGE', 'FIGURE'}}
                if set(change['image_anchor_ids']) - anchors:
                    raise ValueError('Visual reading must cite actual image anchors owned by this SourceItem Map node')
    return result


def bind_annotation_changes(value, document_map, baseline, *, table_previews=None):
    document_map, baseline = _pair(document_map, baseline)
    value = AnnotationChangesRequest.model_validate(value)
    result = _resolved_changes(value, document_map, baseline)
    # Reuse source-number/location validation; no artifact is saved by this binder.
    from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
    if result.get('annotation_updates'):
        compile_source_item_annotation_updates(document_map, baseline, result['annotation_updates'], table_previews=table_previews)
    return {'schema_version': 'reqpilot-annotation-decisions/v1', 'source_sha256': baseline.source_sha256,
        'document_map_artifact_sha256': document_map.artifact_sha256, 'baseline_sha256': baseline.artifact_sha256, **result}


def _packet_range(value, packet):
    if not 0 <= value.segment_index < len(packet['segments']):
        raise ValueError('segment_index is outside the selected parent packet')
    text = packet['segments'][value.segment_index]['text']
    if value.quote is not None:
        start = _unique(text, value.quote); end = start + len(value.quote)
    else:
        start, end = value.start_offset, value.end_offset
    if not 0 <= start < end <= len(text):
        raise ValueError('Range exceeds the selected packet segment')
    return {'segment_index': value.segment_index, 'start_offset': start, 'end_offset': end}


def bind_decomposition_decisions(value, baseline, *, table_previews=None, authorization_request=None):
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    value = DecompositionRequest.model_validate(value)
    request = value.authorization.request or authorization_request
    if not isinstance(request, str) or not request.strip():
        raise ValueError('Use the actual recorded user authorization; it cannot be inferred from split decisions')
    authorized = [source_item_record(baseline, sid).source_record_id for sid in value.authorization.source_item_ids]
    if len(authorized) != len(set(authorized)):
        raise ValueError('Authorized SourceItems repeat after resolving SRC references')
    decisions = []
    for decision in value.decisions:
        sid = source_item_record(baseline, decision.source_item_id).source_record_id
        packet = build_source_item_decomposition_packet(baseline, sid, table_previews=table_previews)
        result = decision.model_dump(mode='json', exclude_unset=True)
        result['source_item_id'] = sid
        if decision.action == 'SPLIT':
            result['children'] = [{**child.model_dump(mode='json', exclude_unset=True),
                'source_ranges': [_packet_range(r, packet) for r in child.source_ranges]} for child in decision.children]
            result['context_ranges'] = [{'source_range': _packet_range(c.source_range, packet), 'disposition': c.disposition}
                for c in decision.context_ranges]
        decisions.append(result)
    # Preserve the exact-cover/alternative/numeric guards. Missing non-whitespace
    # source is an error, never automatically classified as shared context.
    compile_source_item_decomposition(baseline, decisions, table_previews=table_previews, authorized_source_item_ids=authorized)
    return {'schema_version': 'reqpilot-decomposition-decisions/v1', 'baseline_sha256': baseline.artifact_sha256,
        'authorization': {'request': request, 'source_item_ids': authorized}, 'decisions': decisions}


def bind_existing_item_changes(value, document_map, baseline, *, table_previews=None):
    from scope_tools import scope_template
    document_map, baseline = _pair(document_map, baseline)
    value = ExistingItemsChangeRequest.model_validate(value)
    result = _resolved_changes(value, document_map, baseline)
    seen = set()
    for decision in result.get('decisions', []):
        sid = source_item_record(baseline, decision['source_record_id']).source_record_id
        if sid in seen:
            raise ValueError('Duplicate classification target after resolving SRC references')
        seen.add(sid); decision['source_record_id'] = sid
        core.SourceRequirementClassificationCoordinationDecisionV1.model_validate(decision)
    if value.scope_revision is not None:
        change = value.scope_revision
        affected = list(change.affected_scope_ids)
        if len(affected) != len(set(affected)) or set(affected) - set(baseline.covered_scope_ids):
            raise ValueError('Replacement requires unique already-covered source scopes')
        candidates = ItemDecisionsRequest.model_validate({'text_decisions': [d.model_dump(mode='json', exclude_unset=True) for d in change.text_decisions],
            'table_decisions': [d.model_dump(mode='json', exclude_unset=True) for d in change.table_decisions]})
        selected = {d.scope_id for d in candidates.text_decisions} | {d.logical_table_scope_id for d in candidates.table_decisions}
        if selected != set(affected):
            raise ValueError('Replacement must supply the complete explicit affected scopes')
        requests = {'text_requests': [], 'table_requests': []}
        for scope in affected:
            template = scope_template(document_map, baseline, table_previews or {}, scope)
            requests['text_requests' if template['scope_kind'] == 'TEXT' else 'table_requests'].append(template['request'])
        bound = bind_item_decisions(candidates, document_map, requests, allow_generated_keys=False)
        result['scope_revision'] = {'operation': 'REPLACE', 'affected_scope_ids': affected,
            'text_decisions': bound['text_decisions'], 'table_decisions': bound['table_decisions'],
            'comparison_scope_ids': list(change.comparison_scope_ids)}
    return result
