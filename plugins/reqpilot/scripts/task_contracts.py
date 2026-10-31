"""Typed transport inputs for the existing Map and source-item compilers.

Semantic choices stay with the caller. Source bindings, hashes and character
arithmetic are resolved against the selected immutable scope, never guessed.
"""
from __future__ import annotations
import re
import hashlib
import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator, field_validator
from reqpilotx.application.agent_workflow import source_requirement_baseline as core

Nonempty = Annotated[StrictStr, Field(min_length=1)]
SourceRef = Annotated[StrictInt, Field(ge=0)] | Nonempty
LocalItemKey = Annotated[StrictStr,Field(pattern=r'^[a-z][a-z0-9._:-]{0,199}$')]
RowRange = tuple[Annotated[StrictInt, Field(ge=0)], Annotated[StrictInt, Field(gt=0)]]


def _integer_text(value):
    # Lossless transport convenience for row coordinates only, never source
    # labels/IDs, booleans, decimals or an out-of-range source selection.
    return int(value) if isinstance(value,str) and re.fullmatch(r'0|[1-9][0-9]{0,8}',value) else value


def _schema_defaults(schema):
    # Omitted patch fields use an internal None default; explicit null is not
    # part of their input type. Do not advertise an invalid default to clients.
    for value in schema.get('properties',{}).values():
        nullable=value.get('type')=='null' or any(part.get('type')=='null' for part in value.get('anyOf',[]))
        if value.get('default','absent') is None and not nullable:value.pop('default')


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid',json_schema_extra=_schema_defaults)


class MapSourceSelector(InputModel):
    source: SourceRef
    quote: Nonempty = None
    start: Nonempty = None
    end_before: Nonempty = None

    @model_validator(mode='after')
    def exclusive_markers(self):
        if 'quote' in self.model_fields_set and self.model_fields_set & {'start','end_before'}:
            raise ValueError('Use quote or start/end_before, not both')
        return self


SourceSelection = SourceRef | MapSourceSelector


class MapSourceRange(InputModel):
    source_node_id: SourceRef
    start_offset: Annotated[StrictInt, Field(ge=0)]
    end_offset: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode='after')
    def half_open(self):
        if self.end_offset <= self.start_offset: raise ValueError('Text range must be nonempty and half-open')
        return self


class MapHeading(InputModel):
    source: SourceSelection
    label: StrictStr = ''


class NumberedBody(InputModel):
    source: SourceSelection
    label: Nonempty
    continuations: tuple[SourceSelection, ...] = ()


class MapEvidenceRef(InputModel):
    source: SourceRef
    row_range: RowRange = None

    @model_validator(mode='after')
    def half_open(self):
        if self.row_range is not None and self.row_range[1] <= self.row_range[0]: raise ValueError('Row range must be nonempty and half-open')
        return self


UnsetField = Literal['title','label','title_source_ids','content_source_ids','title_source_ranges',
                     'content_source_ranges','title_reference_source_ids','evidence_refs']


class MapNodeEdit(InputModel):
    node_id: Annotated[StrictStr, Field(min_length=1,max_length=200)]
    parent_id: Annotated[StrictStr, Field(min_length=1,max_length=200)] | None = None
    kind: Literal['DOCUMENT','ATTACHMENT','SECTION','COVER','TABLE_OF_CONTENTS','DOCUMENT_CONTROL'] = None
    title: Annotated[StrictStr, Field(min_length=1,max_length=1000)] = None
    label: Annotated[StrictStr, Field(max_length=240)] = None
    title_source_ids: tuple[SourceRef,...] = None
    content_source_ids: tuple[SourceRef,...] = None
    title_reference_source_ids: tuple[SourceRef,...] = None
    title_source_ranges: tuple[MapSourceRange,...] = None
    content_source_ranges: tuple[MapSourceRange,...] = None
    evidence_refs: tuple[MapEvidenceRef,...] = None
    heading: MapHeading = None
    content: tuple[SourceSelection,...] = None
    numbered_body: NumberedBody = None
    unset: tuple[UnsetField,...] = ()

    @model_validator(mode='after')
    def exclusive_shorthands(self):
        fields=self.model_fields_set
        if 'heading' in fields and fields & {'title','label','title_source_ids','title_source_ranges','title_reference_source_ids'}:
            raise ValueError('heading replaces title, label and title evidence together')
        if 'content' in fields and fields & {'content_source_ids','content_source_ranges'}:
            raise ValueError('Use content or explicit content sources, not both')
        conflicts={'heading','content','title','label','title_source_ids','title_source_ranges',
                   'title_reference_source_ids','content_source_ids','content_source_ranges'}
        if 'numbered_body' in fields and fields & conflicts:
            raise ValueError('numbered_body replaces heading/content fields; do not combine them')
        return self


class MapTableFragment(InputModel):
    source_node_id: SourceRef
    row_range: RowRange = None

    @field_validator('row_range',mode='before')
    @classmethod
    def integer_rows(cls,value):
        return [_integer_text(v) for v in value] if isinstance(value,(list,tuple)) else value

    @model_validator(mode='after')
    def half_open(self):
        if self.row_range is not None and self.row_range[1] <= self.row_range[0]: raise ValueError('Row range must be nonempty and half-open')
        return self


class MapTableEdit(InputModel):
    scope_id: Annotated[StrictStr, Field(min_length=1,max_length=200)]
    owner_node_id: Annotated[StrictStr, Field(min_length=1,max_length=200)] = None
    title: Annotated[StrictStr, Field(min_length=1,max_length=1000)] = None
    role: Annotated[StrictStr, Field(min_length=1,max_length=120)] = None
    fragments: tuple[MapTableFragment,...] = None
    agent_display_label: Annotated[StrictStr, Field(min_length=1,max_length=1000)] | None = None


class MapSourceDisposition(InputModel):
    source_node_id: SourceRef
    disposition: Literal['PAGE_FURNITURE','DUPLICATE_CAPTURE','NON_BUSINESS','UNRESOLVED']
    reason: Nonempty
    start_offset: Annotated[StrictInt, Field(ge=0)] = None
    end_offset: Annotated[StrictInt, Field(gt=0)] = None

    @model_validator(mode='after')
    def half_open(self):
        if self.start_offset is not None and self.end_offset is not None and self.end_offset <= self.start_offset:
            raise ValueError('Disposition range must be nonempty and half-open')
        return self


from compact_map import OutlineNode


class MapEditRequest(InputModel):
    """Edits merge by ID; supplied lists replace their field, omitted fields remain."""
    schema_version: Literal['reqpilot-map-edit/v1'] = 'reqpilot-map-edit/v1'
    outline: Annotated[tuple[OutlineNode,...],Field(min_length=1)] = None
    nodes: tuple[MapNodeEdit,...] = ()
    logical_tables: tuple[MapTableEdit,...] = ()
    source_dispositions: tuple[MapSourceDisposition,...] = None
    remove_node_ids: tuple[Nonempty,...] = ()
    remove_table_ids: tuple[Nonempty,...] = ()

    @model_validator(mode='after')
    def non_conflicting_edits(self):
        if self.outline is not None and (self.nodes or self.logical_tables):
            raise ValueError('Use outline or explicit nodes/logical_tables in one edit, not both')
        for values,field,removed in ((self.nodes,'node_id',self.remove_node_ids),(self.logical_tables,'scope_id',self.remove_table_ids)):
            keys=[getattr(value,field) for value in values]
            if len(keys)!=len(set(keys)) or set(keys)&set(removed): raise ValueError('Repeated or removed object in the same edit')
        return self

    def to_payload(self):
        # Preserve explicit parent_id=None and list clearing, but never emit
        # absent patch fields as null. The fixed version is transport metadata.
        return {'schema_version':self.schema_version,**self.model_dump(mode='json',exclude_unset=True)}


class ScopeTextSelector(InputModel):
    """A content index/ID in this scope; markers refer to its exact Map text."""
    content_ref: SourceRef
    quote: Nonempty = None
    start: Nonempty = None
    end_before: Nonempty = None

    @model_validator(mode='after')
    def exclusive_markers(self):
        if 'quote' in self.model_fields_set and self.model_fields_set & {'start','end_before'}:
            raise ValueError('Use quote or start/end_before, not both')
        return self


TextSelection = core.SourceItemTextSpanV1 | ScopeTextSelector


class CanonicalReasons(BaseModel):
    reason_codes: tuple[Annotated[StrictStr,Field(pattern=r'^[a-z][a-z0-9_-]{0,79}$')], ...] = Field(
        min_length=1,max_length=16,description='Lowercase machine codes; hyphens normalize to underscores and the program sorts/deduplicates. Put human explanations in decision_summary.')

    @field_validator('reason_codes',mode='before')
    @classmethod
    def normalize_reason_codes(cls,value):
        if isinstance(value,(list,tuple)) and all(isinstance(v,str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,79}',v) for v in value):
            return tuple(sorted({v.replace('-','_') for v in value}))
        return value


class TextItemInput(CanonicalReasons, core.SourceItemTextBoundaryCandidateV1):
    model_config = ConfigDict(json_schema_extra=_schema_defaults)
    item_key: LocalItemKey | None = Field(
        default=None,description='Omit or null for initial items: program derives a stable key from the resolved original range. Preserve existing keys in revisions.')
    source_form: Literal['NUMBERED_CLAUSE','NATIVE_LIST','PARAGRAPH']
    text_spans: tuple[TextSelection,...] = Field(min_length=1,max_length=4096)


class StructureOnlyInput(CanonicalReasons, core.SourceItemStructureOnlySpanV1):
    text_span: TextSelection


class TableOwnedTextInput(InputModel):
    logical_table_scope_id: Nonempty
    record_key: LocalItemKey
    text_span: TextSelection


class TextDecisionInput(InputModel):
    scope_id: Annotated[StrictStr,Field(pattern=r'^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')]
    items: tuple[TextItemInput,...] = Field(default=(),max_length=8192)
    structure_only_spans: tuple[StructureOnlyInput,...] = Field(default=(),max_length=4096)
    table_owned_spans: tuple[TableOwnedTextInput,...] = Field(default=(),max_length=4096)
    diagnostic_codes: tuple[StrictStr,...] = Field(default=(),max_length=64)


class TableRowSelection(InputModel):
    fragment_ref: Annotated[StrictInt,Field(ge=0)] = None
    fragment_id: Annotated[StrictStr,Field(min_length=1,max_length=200)] = None
    row_start: Annotated[StrictInt,Field(ge=0)]
    row_end: Annotated[StrictInt,Field(gt=0)]

    @field_validator('row_start','row_end',mode='before')
    @classmethod
    def integer_rows(cls,value):return _integer_text(value)

    @model_validator(mode='after')
    def exact_selection(self):
        if (self.fragment_ref is None)==(self.fragment_id is None):raise ValueError('Use one fragment_ref or fragment_id')
        if self.row_end<=self.row_start:raise ValueError('Row group must be nonempty and half-open')
        return self


class TableRecordInput(CanonicalReasons, core.SourceRequirementBoundaryCandidateV1):
    model_config = ConfigDict(json_schema_extra=_schema_defaults)
    record_key: LocalItemKey | None = Field(
        default=None,description='Omit or null for initial records: program derives a stable key from resolved table row ranges. Preserve existing keys in revisions.')
    row_groups: tuple[TableRowSelection,...] = Field(min_length=1,max_length=512)
    owning_text_spans: tuple[TextSelection,...] = Field(default=(),max_length=4096)

    @model_validator(mode='before')
    @classmethod
    def key_alias(cls,value):
        if isinstance(value,dict) and 'item_key' in value:
            value=dict(value)
            if 'record_key' in value and value['record_key']!=value['item_key']:raise ValueError('Conflicting item_key and record_key')
            value['record_key']=value.pop('item_key')
        return value


class TableContextInput(CanonicalReasons, core.SourceRequirementContextRowGroupV1):
    row_group: TableRowSelection


class TableDecisionInput(InputModel):
    logical_table_scope_id: Annotated[StrictStr,Field(min_length=1,max_length=200)]
    records: tuple[TableRecordInput,...] = Field(max_length=8192)
    context_row_groups: tuple[TableContextInput,...] = Field(default=(),max_length=1024)
    diagnostic_codes: tuple[StrictStr,...] = Field(default=(),max_length=64)


def _canonical_sets(value):
    if isinstance(value,dict):
        return {key:sorted(set(item)) if key in {'reason_codes','requirement_kinds','diagnostic_codes'} and isinstance(item,list) and all(isinstance(v,str) for v in item)
            else _canonical_sets(item) for key,item in value.items()}
    if isinstance(value,list):return [_canonical_sets(item) for item in value]
    return value


class ItemDecisionsRequest(InputModel):
    schema_version: Literal['reqpilot-item-decisions/v1'] = 'reqpilot-item-decisions/v1'
    retain_map_structure: StrictBool = Field(default=False, description='Let the program retain already declared Map titles and whole front matter, without deciding body or table granularity.')
    text_decisions: tuple[TextDecisionInput,...] = ()
    table_decisions: tuple[TableDecisionInput,...] = ()

    @model_validator(mode='before')
    @classmethod
    def canonical_metadata(cls,value):
        # Never reorder source spans, records, rows or structure decisions.
        return _canonical_sets(value)

    @model_validator(mode='after')
    def unique_scopes(self):
        ids=[scope.scope_id for scope in self.text_decisions]+[scope.logical_table_scope_id for scope in self.table_decisions]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate scope decisions')
        return self


def _unique(raw,marker):
    first=raw.find(marker)
    if first<0 or raw.find(marker,first+1)>=0:raise ValueError('Scope text marker must match exactly once; use a longer exact quote')
    return first


def _text_span(selection,request):
    if isinstance(selection,core.SourceItemTextSpanV1):return selection
    contents=request.source_content
    if type(selection.content_ref) is int:
        if not 0<=selection.content_ref<len(contents):raise ValueError('content_ref is outside this selected scope')
        content=contents[selection.content_ref]
    else:
        content=next((value for value in contents if value.content_id==selection.content_ref),None)
        if content is None:raise ValueError('content_ref is not in this selected scope')
    raw=content.text
    if selection.quote is not None:
        lo=_unique(raw,selection.quote);hi=lo+len(selection.quote)
    else:
        lo=_unique(raw,selection.start) if selection.start is not None else 0
        hi=_unique(raw,selection.end_before) if selection.end_before is not None else len(raw)
    return core.SourceItemTextSpanV1(content_id=content.content_id,start_offset=lo,end_offset=hi)


class ScopeDecisionError(ValueError):
    def __init__(self,error,request,kind,index,candidate):
        identifier=request.scope_id if kind=='TEXT' else request.logical_table_scope_id
        message=str(error)
        super().__init__(message+(': ' if ':' not in message else '; ')+f'scope={identifier}')
        self.scope_context={'scope_id':identifier,'scope_kind':kind,'node_id':request.node_id,
            'structural_role':request.structural_role,'decision_index':index,
            'document_map_sha256':request.document_map_artifact_sha256}
        if kind=='TEXT' and 'STRUCTURE_ONLY_SPAN_NOT_TITLE' in message:
            refs={c.content_id:(i,c) for i,c in enumerate(request.source_content)}
            self.scope_context['structure_only_candidates']=[{'content_ref':refs[s.text_span.content_id][0],
                'start_offset':s.text_span.start_offset,'end_offset':s.text_span.end_offset,
                'text':refs[s.text_span.content_id][1].text[s.text_span.start_offset:s.text_span.end_offset][:240],
                'is_title':False} for s in candidate.structure_only_spans
                if s.text_span.content_id in refs and not refs[s.text_span.content_id][1].is_title]


def _validate_scope(request,candidate,kind,index):
    validator=core.validate_source_item_text_slice_candidate if kind=='TEXT' else core.validate_source_requirement_slice_candidate
    try:validator(request,candidate)
    except ValueError as exc:raise ScopeDecisionError(exc,request,kind,index,candidate) from exc


def _range_key(source_sha,scope_id,ranges,kind):
    """Same resolved coverage has the same local key across selector spellings.

    Coalescing is only for identity; the compiler still validates the untouched
    selections, including overlaps, ordering and completeness.
    """
    merged=[]
    for identifier,start,end in sorted(ranges):
        if merged and merged[-1][0]==identifier and merged[-1][2]==start:
            merged[-1][2]=end
        else:merged.append([identifier,start,end])
    material=json.dumps([source_sha,scope_id,merged],ensure_ascii=False,separators=(',',':'))
    return 'auto-'+kind+'-'+hashlib.sha256(material.encode()).hexdigest()[:32]


def bind_item_decisions(value,document_map,scope_requests,*,covered_scope_ids=(),with_receipt=False,allow_generated_keys=True):
    """Resolve cheap selectors and emit the existing compile-items envelope."""
    typed=value if isinstance(value,ItemDecisionsRequest) else ItemDecisionsRequest.model_validate(value)
    retention=None
    if typed.retain_map_structure:
        from compact_items import expand_structure_retention
        expanded=expand_structure_retention(typed.model_dump(mode='json',exclude_unset=True),document_map,scope_requests,covered_scope_ids=covered_scope_ids)
        typed=ItemDecisionsRequest.model_validate(expanded['decisions'])
        retention=expanded['receipt']
    mapping=core.DocumentMapArtifactV1.model_validate(document_map)
    text_requests={v['scope_id']:core.SourceItemTextScopeRequestV1.model_validate(v) for v in scope_requests['text_requests']}
    table_requests={v['logical_table_scope_id']:core.SourceRequirementScopeRequestV1.model_validate(v) for v in scope_requests['table_requests']}
    def binding(request):
        if (request.source_sha256,request.document_map_artifact_id,request.document_map_artifact_sha256)!=(mapping.source_sha256,mapping.artifact_id,mapping.artifact_sha256):
            raise ValueError('Scope requests belong to a different source or Map')
        return {'source_sha256':mapping.source_sha256,'document_map_artifact_id':mapping.artifact_id,
            'document_map_artifact_sha256':mapping.artifact_sha256,'request_sha256':request.request_sha256}
    texts=[];tables=[]
    for index,item in enumerate(typed.text_decisions):
        request=text_requests.get(item.scope_id)
        if request is None:raise ValueError('Unknown selected text scope: '+item.scope_id)
        envelope=binding(request)
        payload=item.model_dump(mode='json')
        for actual,source in zip(payload['items'],item.items):
            actual['text_spans']=[_text_span(span,request).model_dump(mode='json') for span in source.text_spans]
            if source.item_key is None:
                if not allow_generated_keys:raise ValueError('REVISION_ITEM_KEY_REQUIRED: preserve the existing item_key from the scope template')
                actual['item_key']=_range_key(mapping.source_sha256,item.scope_id,
                    [(s['content_id'],s['start_offset'],s['end_offset']) for s in actual['text_spans']],'text')
        for actual,source in zip(payload['structure_only_spans'],item.structure_only_spans):
            actual['text_span']=_text_span(source.text_span,request).model_dump(mode='json')
        for actual,source in zip(payload['table_owned_spans'],item.table_owned_spans):
            actual['text_span']=_text_span(source.text_span,request).model_dump(mode='json')
        candidate=core.SourceItemTextSliceCandidateV1.model_validate({**payload,**envelope})
        _validate_scope(request,candidate,'TEXT',index)
        texts.append(payload)
    for index,item in enumerate(typed.table_decisions):
        request=table_requests.get(item.logical_table_scope_id)
        if request is None:raise ValueError('Unknown selected table scope: '+item.logical_table_scope_id)
        payload=item.model_dump(mode='json')
        def resolve_rows(selection):
            if selection.fragment_ref is not None:
                if selection.fragment_ref>=len(request.fragments):raise ValueError('fragment_ref is outside this selected table scope')
                identifier=request.fragments[selection.fragment_ref].fragment_id
            else:identifier=selection.fragment_id
            return {'fragment_id':identifier,'row_start':selection.row_start,'row_end':selection.row_end}
        for actual,source in zip(payload['records'],item.records):
            actual['row_groups']=[resolve_rows(v) for v in source.row_groups]
            actual['owning_text_spans']=[_text_span(span,request).model_dump(mode='json') for span in source.owning_text_spans]
            if source.record_key is None:
                if not allow_generated_keys:raise ValueError('REVISION_RECORD_KEY_REQUIRED: preserve the existing record_key from the scope template')
                actual['record_key']=_range_key(mapping.source_sha256,item.logical_table_scope_id,
                    [(r['fragment_id'],r['row_start'],r['row_end']) for r in actual['row_groups']],'table')
        for actual,source in zip(payload['context_row_groups'],item.context_row_groups):actual['row_group']=resolve_rows(source.row_group)
        candidate=core.SourceRequirementSliceCandidateV1.model_validate({**payload,**binding(request)})
        _validate_scope(request,candidate,'TABLE',index)
        tables.append(payload)
    result={'schema_version':'reqpilot-item-decisions/v1','source_sha256':mapping.source_sha256,
        'document_map_artifact_sha256':mapping.artifact_sha256,'text_decisions':texts,'table_decisions':tables}
    return {'decisions':result,'structure_retention':retention} if with_receipt else result
