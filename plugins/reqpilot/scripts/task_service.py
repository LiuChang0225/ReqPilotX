"""Typed application entry shared by CLI and MCP; workspaces retain all authority."""
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from task_contracts import MapEditRequest, ItemDecisionsRequest, bind_item_decisions
from task_change_contracts import AnnotationChangesRequest, DecompositionRequest, ExistingItemsChangeRequest, ImageItemsRequest


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')


def decoded_json_container(value, expected_type):
    """Accept one losslessly JSON-encoded container; retain all member validation.

    Some function callers stringify optional structured values. This does not
    invent fields, change names, coerce members or unwrap a second encoding.
    Duplicate keys and non-JSON constants must never be silently normalized.
    """
    if not isinstance(value, str):
        return value

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError('non-JSON constant')

    try:
        decoded = json.loads(value, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError):
        return value
    return decoded if type(decoded) is expected_type else value


class OpenRequest(Request):
    mode: Literal['create', 'resume']
    task_id: str | None = None
    source: str | None = None
    goal: str | None = None
    creation_key: str | None = None
    project_key: str | None = None
    document_key: str | None = None
    workbench_url: str | None = None
    require_map_confirmation: bool = False


class TaskRequest(Request):
    task_id: str


class InspectRequest(TaskRequest):
    include_history: bool = False


class DescribeRequest(Request):
    task_id: str | None = None
    action: str | None = None
    scope_id: str | None = None
    part: str | None = None
    expected_revision: str | None = None
    expected_schema_sha256: str | None = None
    offset: int = Field(default=0,ge=0)
    max_chars: int = Field(default=12000,ge=2048,description='Requested text budget; capped at 24000 with explicit continuation.')


class WriteRequest(TaskRequest):
    expected_revision: str
    command_id: str | None = None


class VisualTableRegion(Request):
    page: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[float,float,float,float] | None = None
    source_ref: StrictInt | StrictStr | None = None
    header_row_count: StrictInt = Field(default=0, ge=0)

    @model_validator(mode='after')
    def one_locator(self):
        if self.source_ref is not None:
            if self.page is not None or self.bbox is not None:raise ValueError('Use a captured table source_ref or page/bbox')
        elif self.page is None or self.bbox is None:raise ValueError('Table region needs source_ref or page/bbox')
        return self


class PrepareRequest(WriteRequest):
    mode: Literal['capture', 'pages', 'ocr'] = 'capture'
    pages: list[StrictInt] | None = Field(default=None, description='Original page numbers. Required OCR scope; with mode=pages, requested pages are validated and included in the shared full-document Markdown/HD views.')
    profile: Literal['page','lines','numbers','tables'] = 'page'
    table_regions: list[VisualTableRegion] = Field(default_factory=list)

    @field_validator('pages', 'table_regions', mode='before')
    @classmethod
    def json_encoded_arrays(cls, value):
        return decoded_json_container(value, list)

    @model_validator(mode='after')
    def ocr_scope(self):
        if self.pages is not None and (not self.pages or len(set(self.pages))!=len(self.pages) or any(p<1 for p in self.pages)):
            raise ValueError('Use unique positive original page numbers')
        if self.mode=='ocr':
            if not self.pages:
                raise ValueError('OCR requires unique positive original page numbers')
            if self.profile=='tables' and not self.table_regions:raise ValueError('Table OCR requires explicit regions or captured table refs')
        elif self.table_regions or self.profile!='page' or (self.mode=='capture' and self.pages is not None):
            raise ValueError('OCR profiles/table_regions require mode=ocr; page selection requires mode=pages or ocr')
        return self


class VisualCellCorrection(Request):
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    text: StrictStr


class VisualCorrection(Request):
    ref: StrictInt | StrictStr | None = None
    refs: list[StrictInt | StrictStr] | None = None
    text: StrictStr | None = None
    cells: list[VisualCellCorrection] | None = None
    reason: StrictStr = Field(min_length=1)

    @field_validator('refs', 'cells', mode='before')
    @classmethod
    def json_encoded_arrays(cls, value):
        return decoded_json_container(value, list)

    @model_validator(mode='after')
    def source_and_value(self):
        if (self.ref is None)==(self.refs is None):raise ValueError('Use one ref or a same-page refs group')
        if self.refs is not None and not self.refs:raise ValueError('refs cannot be empty')
        if (self.text is None)==(self.cells is None):raise ValueError('Use corrected text or cell changes')
        if self.cells is not None and not self.cells:raise ValueError('cells cannot be empty')
        return self


class VisualRevisionRequest(WriteRequest):
    changes: list[VisualCorrection] = Field(min_length=1)

    @field_validator('changes', mode='before')
    @classmethod
    def json_encoded_changes(cls, value):
        return decoded_json_container(value, list)


class ReadRequest(TaskRequest):
    view: Literal['source', 'part', 'scope', 'page', 'image', 'items', 'item', 'item_image', 'image_scope', 'image_scope_image', 'feedback'] = 'source'
    pages: list[StrictInt] | None = Field(default=None, description='Page filter for view=source only. To read one page Markdown or its pixels, use view=page/image with singular page (or a located source ref).')
    refs: list[StrictInt | StrictStr] | None = None
    query: str | None = None
    page: int | None = Field(default=None, description='One original page number for view=page (Markdown) or view=image (pixels); alternatively use a located source ref. For multi-page source text use view=source with pages.')
    bbox: tuple[float,float,float,float] | None = Field(default=None, description='view=image only: [x0,y0,x1,y1] in the visible original PDF page coordinates, not image pixels. Use for a table, line or numeric detail.')
    scale: float | None = Field(default=None, gt=0, le=4, description='view=image only: optional PDF render scale, at most 4. Region images default to 2 and reuse the native renderer pixel budget.')
    ref: StrictInt | StrictStr | None = Field(default=None, description="Capture ref or opaque src1 handle returned by source_read; handles keep the capture version.")
    capture_sha256: str | None = None
    scope_id: str | None = None
    expected_map_sha256: str | None = None
    expected_baseline_sha256: str | None = None
    source_item_id: str | None = None
    image_anchor_id: str | None = None
    node_id: str | None = None
    feedback_id: str | None = None
    part: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    max_chars: int = Field(default=12000, ge=2048, description='Requested text budget; capped at 24000 with explicit continuation, never silent content loss.')

    @field_validator('pages', mode='before')
    @classmethod
    def json_encoded_page_filter(cls, value):
        return decoded_json_container(value, list)

    @field_validator('bbox', mode='before')
    @classmethod
    def visible_pdf_coordinates(cls, value):
        if value is None:return value
        if not isinstance(value,(list,tuple)) or len(value)!=4 or any(type(v) not in {int,float} or not math.isfinite(v) for v in value):
            raise ValueError('bbox requires four finite numeric visible PDF coordinates')
        if value[2]<=value[0] or value[3]<=value[1]:raise ValueError('bbox must have positive area')
        return value

    @field_validator('scale', mode='before')
    @classmethod
    def numeric_scale(cls, value):
        if value is not None and (type(value) not in {int,float} or not math.isfinite(value)):
            raise ValueError('scale must be a finite number')
        return value

    @model_validator(mode='after')
    def page_selector_matches_view(self):
        if (self.bbox is not None or self.scale is not None) and self.view!='image':
            raise ValueError('bbox/scale apply only to view=image')
        if self.view in {'page', 'image'} and self.pages is not None:
            raise ValueError('SOURCE_READ_SELECTOR_CONFLICT: view=page/image reads one page; use singular page or a located ref. For source text across pages, use view=source with pages. The pages filter is not a page/image selector.')
        return self


class MapRequest(WriteRequest):
    decisions: MapEditRequest


class ItemsRequest(WriteRequest):
    decisions: ItemDecisionsRequest


class ReviewRequest(WriteRequest):
    analysis_policy: Literal['preserve', 'skip_no_internal_evidence'] = 'preserve'


class SelfReviewFinding(Request):
    text: str = Field(min_length=1)
    source_item_ids: list[str] = Field(default_factory=list)
    rationale: str = ''


class FeedbackResolution(Request):
    feedback_id: str
    disposition: Literal['ADDRESSED','UNRESOLVED']
    reason: str = Field(min_length=1)


class ChangeRequest(WriteRequest):
    origin: Literal['AGENT_SELF_REVIEW','USER'] = 'AGENT_SELF_REVIEW'
    resolutions: list[FeedbackResolution] = Field(default_factory=list)
    self_review_finding: SelfReviewFinding | None = None

    @field_validator('self_review_finding', mode='before')
    @classmethod
    def json_encoded_finding(cls, value):
        return decoded_json_container(value, dict)


class AnnotationsRequest(ChangeRequest):
    changes: AnnotationChangesRequest


class DecompositionApplyRequest(ChangeRequest):
    changes: DecompositionRequest


class ExistingItemsRequest(ChangeRequest):
    changes: ExistingItemsChangeRequest

    @model_validator(mode='after')
    def recorded_scope_reason(self):
        if self.changes.scope_revision is not None and not (self.changes.addressed_feedback_ids or self.self_review_finding):
            raise ValueError('Scope replacement requires recorded feedback or an explicit self_review_finding')
        return self


class ImagesRequest(WriteRequest):
    changes: ImageItemsRequest


class SyncRequest(TaskRequest):
    direction: Literal['pull', 'retry_publication', 'publish_current_review'] = 'pull'
    operation_id: str | None = None
    expected_revision: str | None = None


REQUESTS = {
    'describe':DescribeRequest,
    'open': OpenRequest, 'inspect': InspectRequest, 'prepare': PrepareRequest,
    'read': ReadRequest, 'map': MapRequest, 'items': ItemsRequest,
    'review': ReviewRequest, 'check': TaskRequest, 'deliver': WriteRequest,
    'sync': SyncRequest, 'recover': TaskRequest,
    'annotations':AnnotationsRequest,'decomposition':DecompositionApplyRequest,
    'revise':ExistingItemsRequest,'images':ImagesRequest,
    'visual_revise':VisualRevisionRequest,
}
OPERATIONS = {'map': 'compile-map', 'items': 'compile-items', 'review': 'review-open', 'deliver': 'deliver',
    'images':'compile-images','annotations':'compile-annotations','decomposition':'compile-decomposition',
    'visual_revise':'revise-visual-text'}
CONTRACT_VERSION = 'reqpilot-task-api/v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def find_task_root(working_root, task_id):
    """Resolve existing work_id below a configured root; no latest-task register."""
    if not re.fullmatch(r'task-[a-f0-9]{32}', task_id):
        raise ValueError('INVALID_TASK: use the task_id returned by task_open')
    base = Path(working_root).expanduser().resolve(strict=True)
    matches = list(base.glob('runtime-data/projects/*/documents/*/tasks/' + task_id + '/task-context.json'))
    if len(matches) != 1:
        raise ValueError('TASK_NOT_FOUND: this configured working root does not uniquely own the task')
    path = matches[0]
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise ValueError('INVALID_TASK: task path leaves the configured working root')
    return path.parent


def decomposition_attention(state):
    selected=state['selected']; reference=selected.get('DECOMPOSITION')
    if not reference:return {'remaining_count':0,'items':[],'semantic_review':'NOT_CLAIMED'}
    artifact=json.loads(Path(reference['path']).read_text())
    remaining=artifact.get('review_scope',{}).get('remaining_source_item_ids',[])
    stale=set(artifact.get('carry_forward',{}).get('stale_parent_ids',[]))
    return {'remaining_count':len(remaining),'items':[{'source_item_id':sid,
        'reason':'PARENT_CHANGED_REVIEW_AGAIN' if sid in stale else 'AUTHORIZED_SCOPE_NOT_DECIDED',
        'read':{'tool':'source_read','request':{'task_id':state['context']['work_id'],'view':'item','source_item_id':sid,
            'expected_map_sha256':selected['MAP']['artifact_sha256'],'expected_baseline_sha256':selected['ITEMS']['artifact_sha256']}}}
        for sid in remaining[:20]],'more':len(remaining)>20,'artifact_path':reference['path'],
        'semantic_review':'NOT_CLAIMED'}


def task_summary(state):
    from task_workbench import workbench_link
    link=workbench_link(state)
    context = state['context']
    result = {
        'contract_version': CONTRACT_VERSION, 'task_id': context['work_id'],
        'revision': state['revision'], 'task_root': state['task_root'],
        'source': context['source'], 'goal': context['request']['goal'],
        'require_map_confirmation': context['request']['require_map_confirmation'],
        'runtime': context.get('runtime_ref'), 'current_owner': state['current_working']['owner'],
        'workbench_url': link['url'], 'workbench_link': link, 'brief': state['brief'],
        'selected': state['selected'], 'human_acceptance': 'NOT_CLAIMED',
        'pending_decomposition': decomposition_attention(state),
        'capabilities': {'contract_version': CONTRACT_VERSION, 'actions': list(REQUESTS),
            'map_items_semantics': 'EXISTING_COMPILERS', 'existing_item_revision': 'TYPED_REVIEW_APPLY',
            'assessment_and_engineer_opinion_transport': 'UNSUPPORTED'},
    }
    # The complete brief is an artifact. Keep role, goal and continuation information,
    # without returning its duplicated manifests and full paths on every call.
    result['brief'] = {k:v for k,v in result['brief'].items() if k not in {
        'saved_artifacts', 'retained_stage_artifacts', 'source', 'current_request'}}
    return result


class TaskService:
    def __init__(self, working_root, entrypoint, project_root=None):
        self.working_root = Path(working_root).expanduser().resolve(strict=True)
        self.entrypoint = Path(entrypoint).resolve(strict=True)
        self.project_root = project_root

    def state(self, task_id):
        from reqpilotx.application.agent_workflow.task_workspace import workspace_status
        root = find_task_root(self.working_root, task_id)
        return root, workspace_status(root, refresh=False)

    def invoke(self, action, payload):
        request = REQUESTS[action].model_validate(payload)
        if action=='describe':
            budget=request.max_chars
            result=self.describe(request.model_copy(update={'max_chars':min(budget,24000)}))
            if budget>24000:result['read_budget']={'requested_max_chars':budget,'applied_max_chars':24000,'content_continuation_preserved':True}
            return result
        if action == 'open':
            return self.open(request)
        root, state = self.state(request.task_id)
        if action == 'inspect':
            result = {'status': 'READY', **task_summary(state)}
            if request.include_history:
                result.update(history=state['index']['committed'][-20:],
                              history_total=len(state['index']['committed']),
                              history_path=str(root/'workspace-index.json'))
            return result
        if action == 'read':
            budget=request.max_chars
            request=request.model_copy(update={'max_chars':min(budget,24000)})
            result=self.read(state, request)
            if budget!=request.max_chars:result['read_budget']={'requested_max_chars':budget,'applied_max_chars':request.max_chars,'content_continuation_preserved':True}
            return result
        if action == 'recover':
            from reqpilotx.application.agent_workflow.task_workspace import recover_workspace
            recovered = recover_workspace(root)
            return {'status':'RECOVERED', **task_summary(recovered),
                'incomplete_operations':recovered['incomplete_operations'][:10], 'compiler_reexecuted':False}
        if action == 'check':
            return self.check(root, state)
        if action == 'sync':
            return self.sync(root, state, request)
        if action in {'annotations','decomposition','revise','images'}:
            from task_changes import execute_change
            return execute_change(self,root,state,action,request)
        try:
            return self.write(root, state, action, request)
        except ValueError as exc:
            if action=='items' and str(exc).startswith(('Unknown selected text scope:', 'Unknown selected table scope:')):
                return self.unknown_scope(state,request.task_id,str(exc))
            raise

    @staticmethod
    def scope_link(state,task_id):
        return {'tool':'source_read','request':{'task_id':task_id,'view':'scope',
            'expected_map_sha256':state['selected']['MAP']['artifact_sha256'],'limit':100}}

    @classmethod
    def unknown_scope(cls,state,task_id,message):
        return {'status':'FAILED','error_code':'UNKNOWN_SCOPE','message':message,
            'task_id':task_id,'revision':state['revision'],'preserved_existing_artifacts':True,
            'next_action':cls.scope_link(state,task_id)}

    def describe(self, request):
        from task_schema import describe
        state=self.state(request.task_id)[1] if request.task_id else None
        if request.expected_revision and (state is None or state['revision']!=request.expected_revision):
            raise ValueError('DESCRIBE_CONTEXT_CHANGED: reload the description for the current task')
        scope=None
        if request.scope_id:
            if state is None or not state['selected'].get('MAP'):raise ValueError('MAP_REQUIRED: a scoped template requires an actual task Map')
            from task_reading import _scope_requests
            from task_changes import map_read_dir
            _,choices=_scope_requests(map_read_dir(state),state['context']['source']['sha256'],state['selected']['MAP']['artifact_sha256'])
            chosen=next(((kind,value) for kind,value in choices if (value.scope_id if kind=='TEXT' else value.logical_table_scope_id)==request.scope_id),None)
            if chosen is None:return self.unknown_scope(state,request.task_id,'Select a scope returned by the current Map; no scope was substituted.')
            kind,value=chosen;scope={'scope_kind':kind,'scope_id':request.scope_id,'node_id':value.node_id}
            if kind=='TABLE':scope['logical_table_scope_id']=request.scope_id
        result=describe(request.action,context=state,scope=scope,part=request.part,offset=request.offset,max_chars=request.max_chars)
        if request.expected_schema_sha256 and request.expected_schema_sha256!=result['request_schema_sha256']:
            raise ValueError('DESCRIPTION_SCHEMA_CHANGED: restart this schema part')
        def links(value):
            if isinstance(value,list):return [links(item) for item in value]
            if not isinstance(value,dict):return value
            if value.get('reader')=='task_schema.describe':
                args={k:v for k,v in value['arguments'].items() if k not in {'context','scope'}}
                args['expected_schema_sha256']=result['request_schema_sha256']
                if state:args.update(task_id=request.task_id,expected_revision=state['revision'])
                if request.scope_id:args['scope_id']=request.scope_id
                DescribeRequest.model_validate(args)
                return {'tool':'task_describe','request':args}
            return {key:links(item) for key,item in value.items()}
        result=links(result)
        result['budget_scope']='DESCRIPTION_BEFORE_TASK_TRANSPORT_ENVELOPE'
        if state:result.update(task_id=request.task_id,revision=state['revision'])
        return result

    def open(self, request):
        if request.mode == 'resume':
            if not request.task_id or any((request.source, request.goal, request.creation_key,
                                         request.project_key, request.document_key, request.workbench_url)):
                raise ValueError('INVALID_DECISION: resume takes the existing task_id and retains its policy')
            return {'status': 'RESUMED', **task_summary(self.state(request.task_id)[1])}
        if request.task_id or not all((request.source, request.goal, request.creation_key)):
            raise ValueError('INVALID_DECISION: create requires source, exact goal and a stable creation_key')
        from workspace_tools import initialize
        args = Namespace(working_root=self.working_root, source=Path(request.source), goal=request.goal,
            creation_key=request.creation_key, project_key=request.project_key, document_key=request.document_key,
            project_id=None, document_id=None, workbench_url=request.workbench_url,
            require_map_confirmation=request.require_map_confirmation, no_map_confirmation=None,
            binding_file=None, open=False, project_root=self.project_root, full=False)
        value = initialize(args, self.entrypoint)
        response = {'status': value['status'], **task_summary(value)}
        for key in ('replayed', 'workbench_sync'):
            if key in value: response[key] = value[key]
        return response

    def write(self, root, state, action, request):
        from reqpilotx.application.agent_workflow import task_workspace as core
        from workspace_tools import run
        requested_pages=None
        if action=='prepare' and request.mode=='pages' and request.pages:
            from reqpilotx.adapters.documents.pdf_source_crop_renderer import inspect_pdf_page_geometry
            geometry=inspect_pdf_page_geometry(root/state['context']['source']['path'], expected_source_sha256=state['context']['source']['sha256'])
            if max(request.pages)>len(geometry):raise ValueError('INVALID_PAGE: page is outside this original')
            requested_pages={'requested_pages':request.pages,'prepared_pages':list(range(1,len(geometry)+1)),
                'preparation_scope':'FULL_DOCUMENT_SHARED_VIEWS',
                'note':'Requested pages are included. Shared preparation creates all page views once; OCR remains explicitly scoped.'}
        operation = {'capture':'capture','pages':'reading-workspace','ocr':'read-pdf-visual'}[request.mode] if action == 'prepare' else OPERATIONS[action]
        # Omitted Map fields mean retain; explicit null/[] can mean clear. Preserve
        # that distinction in request identity as well as in the compiler payload.
        explicit = {'action': action, 'request': request.model_dump(mode='json', exclude={'command_id'}, exclude_unset=True)}
        command_id = request.command_id or 'task-api-' + digest(explicit)
        previous = core.lookup_operation_command(root, command_id, operation, explicit)
        input_path = None
        policy_path = None
        retention_path = root/'inputs'/'task-api'/(digest(explicit)+'.structure-binding.json')
        retention_receipt = None
        if previous is None and (action in {'map', 'items','images','annotations','decomposition','visual_revise'} or (action=='prepare' and request.mode=='ocr')):
            if state['revision'] != request.expected_revision:
                raise ValueError('REVISION_CONFLICT: refresh the task; previous decisions are not submitted to a new revision')
            decisions = request.decisions.to_payload() if action == 'map' else request.decisions.model_dump(mode='json', exclude_unset=True) if action=='items' else None
            if action=='prepare':
                decisions={'pages':request.pages,'profile':request.profile,
                           'table_regions':[r.model_dump(mode='json',exclude_none=True) for r in request.table_regions]}
            elif action=='visual_revise':
                decisions={'changes':[r.model_dump(mode='json',exclude_none=True) for r in request.changes]}
            elif action == 'items':
                compiler_directory=state['selected'].get('MAP',{}).get('output_dir')
                if not compiler_directory:raise ValueError('MAP_COMPILER_INPUTS_UNAVAILABLE: current Map is readable, but adding new scopes requires its complete compiler inputs; revise existing scopes through the review')
                map_dir = Path(compiler_directory)
                document_map = json.loads((map_dir/'document-map.json').read_text())
                scopes = json.loads((map_dir/'scope-requests.json').read_text())
                covered=()
                baseline_ref=state['selected'].get('ITEMS')
                if baseline_ref:
                    from reqpilotx.application.agent_workflow import source_requirement_baseline as source_core
                    baseline=source_core.SourceRequirementBaselineArtifactV1.model_validate_json(Path(baseline_ref['path']).read_bytes())
                    if (baseline.source_sha256,baseline.document_map_artifact_sha256)==(document_map['source_sha256'],document_map['artifact_sha256']):
                        covered=baseline.covered_scope_ids
                bound=bind_item_decisions(request.decisions,document_map,scopes,covered_scope_ids=covered,with_receipt=True)
                decisions=bound['decisions'];retention_receipt=bound['structure_retention']
            elif action in {'images','annotations','decomposition'}:
                from task_changes import compiler_decisions
                decisions = compiler_decisions(state,action,request)
            # A deterministic transport-input file, frozen once more by begin_operation.
            directory = root/'inputs'/'task-api'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            input_path = directory/(digest(decisions)+'.json')
            data = canonical(decisions)+'\n'
            try:
                with input_path.open('x', encoding='utf-8') as stream: stream.write(data)
                input_path.chmod(0o400)
            except FileExistsError:
                if input_path.read_text() != data: raise ValueError('IMMUTABLE_INPUT_CHANGED')
            if retention_receipt is not None:
                binding={'schema_version':'reqpilot-structure-retention-binding/v1','request_sha256':digest(explicit),
                    'bound_decisions_sha256':digest(decisions),'receipt':retention_receipt}
                binding['binding_sha256']=digest(binding);binding_data=canonical(binding)+'\n'
                try:
                    with retention_path.open('x',encoding='utf-8') as stream:stream.write(binding_data)
                    retention_path.chmod(0o400)
                except FileExistsError:
                    if retention_path.read_text()!=binding_data:raise ValueError('IMMUTABLE_INPUT_CHANGED: structure binding')
        if previous is None and action == 'review' and request.analysis_policy == 'skip_no_internal_evidence':
            policy = {'source_sha256':state['context']['source']['sha256'], 'status':'SKIPPED', 'reason':'NO_INTERNAL_EVIDENCE'}
            directory = root/'inputs'/'task-api'; directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            policy_path = directory/(digest(policy)+'.json'); data=canonical(policy)+'\n'
            try:
                with policy_path.open('x',encoding='utf-8') as stream: stream.write(data)
                policy_path.chmod(0o400)
            except FileExistsError:
                if policy_path.read_text()!=data: raise ValueError('IMMUTABLE_INPUT_CHANGED')
        args = Namespace(operation=operation, workspace=root, binding_file=None, input=input_path,
            analysis_policy=policy_path,
            expected_revision=request.expected_revision, command_id=command_id, request_payload=explicit,
            full=False, project_root=self.project_root)
        value = run(args, self.entrypoint)
        try:
            current = core.workspace_status(root, refresh=False)
        except Exception as exc:
            return {'status':value.get('status'), 'task_id':request.task_id, 'command_id':command_id,
                'operation_id':value.get('operation_id'), 'operation_state':value.get('state'),
                'result':{k:v for k,v in value.items() if k in {'output_dir','receipt_path','receipt_sha256','compiler_result'}},
                'verification':{'status':'FAILED','message':str(exc)}, 'handoff_ready':False,
                'local_artifact_preserved':True}
        response = {'status': value.get('status'), **task_summary(current), 'command_id': command_id,
            'operation_id': value.get('operation_id'), 'operation_state': value.get('state'),
            'replayed': value.get('replayed', False), 'base_revision': request.expected_revision,
            'result': {k:v for k,v in value.items() if k in {
                'compiler_result','output_dir','receipt_path','receipt_sha256','error','next',
                'execution_state','workbench_sync','publication','delivery_attachment','host_receipt'}},
            'handoff_ready': False}
        if requested_pages:response['page_preparation']=requested_pages
        if value.get('state')=='COMMITTED' and action=='map':
            response['scope_read']=self.scope_link(current,request.task_id)
        if value.get('state')=='COMMITTED' and action=='items':
            response['source_items_read']={'tool':'source_read','request':{'task_id':request.task_id,
                'view':'items','expected_map_sha256':current['selected']['MAP']['artifact_sha256'],
                'expected_baseline_sha256':current['selected']['ITEMS']['artifact_sha256'],'limit':100}}
        if (action=='visual_revise' or (action=='prepare' and request.mode=='ocr')) and value.get('state')=='COMMITTED':
            from task_read_resolution import source_handle
            compiler=response['result'].get('compiler_result',{})
            refs=compiler.get('refs',[])
            capture=compiler.get('source_ir_sha256')
            if refs and capture:
                # Route new observations through the same public reader as native
                # evidence. Agents need not reverse-engineer raw SourceIR fields.
                response['source_authoring_read']={'tool':'source_read','request':{
                    'task_id':request.task_id,'view':'source','refs':[r['ref'] for r in refs],
                    'capture_sha256':capture,'limit':100}}
                response['table_reads']=[{'ref':r['ref'],
                    'cells':{'tool':'source_read','request':{'task_id':request.task_id,'view':'part',
                        'ref':source_handle(capture,r['ref']),'part':'table','limit':100,'max_chars':24000}},
                    'original_image':{'tool':'source_read','request':{'task_id':request.task_id,'view':'image',
                        'page':r['page'],'bbox':r['bbox'],'scale':2}}}
                    for r in refs if r['kind']=='table']
        if action=='items' and request.decisions.retain_map_structure:
            if retention_path.is_file():
                binding=json.loads(retention_path.read_text())
                if binding.get('binding_sha256')!=digest({k:v for k,v in binding.items() if k!='binding_sha256'}) or binding.get('request_sha256')!=digest(explicit):
                    raise ValueError('IMMUTABLE_INPUT_CHANGED: structure binding')
                response['structure_retention']={'binding_path':str(retention_path),**binding}
            else:response['structure_retention']={'status':'BINDING_RECEIPT_UNAVAILABLE','local_artifact_preserved':True}
        if action == 'deliver' and value.get('state') == 'COMMITTED':
            try:
                check = self.check(root, current)
            except Exception as exc:
                check = {'status':'FAILED','message':str(exc), 'current_revision':current['revision'],
                    'local_artifact_preserved':True}
            response['verification'] = check
            response['local_delivery_ready'] = bool(check.get('review_verified') and check.get('current_delivery_registered'))
            response['handoff_ready'] = bool(response['local_delivery_ready'] and check.get('workbench_content_verified')
                and check.get('workbench_is_current',True)
                and not check.get('pending_decomposition',{}).get('remaining_count'))
        return response

    def ensure_pages(self, state, request):
        """Lazily register the established reading-workspace once per task/capture."""
        import fcntl
        from reqpilotx.adapters.documents.pdf_source_crop_renderer import inspect_pdf_page_geometry
        root=Path(state['task_root'])
        # A separate derivative-preparation lock serializes concurrent page reads;
        # existing workspace writes keep their own CAS and publication locks.
        lock=root/'reading'/'.page-preparation.lock'
        lock.parent.mkdir(parents=True,exist_ok=True)
        with lock.open('a') as stream:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            current=self.state(request.task_id)[1]
            from task_read_resolution import resolve_source_request
            resolve_source_request(current,request)  # reject stale refs before writes
            manifest=current['selected']['READING'].get('manifest_path')
            if manifest and Path(manifest).name=='reading-workspace.json':
                return current,{'status':'REUSED','manifest_path':manifest}
            source=root/current['context']['source']['path']
            geometry=inspect_pdf_page_geometry(source,expected_source_sha256=current['context']['source']['sha256'])
            if request.page>len(geometry):raise ValueError('INVALID_PAGE: page is outside this original')
            prepared=self.write(root,current,'prepare',PrepareRequest(task_id=request.task_id,
                expected_revision=current['revision'],mode='pages'))
            if prepared.get('operation_state')!='COMMITTED':
                raise ValueError('READING_PREPARATION_INCOMPLETE: inspect the recorded task operation before retrying')
            ready=self.state(request.task_id)[1]
            resolve_source_request(ready,request)
            return ready,{'status':'PREPARED','operation_id':prepared['operation_id'],
                'manifest_path':ready['selected']['READING']['manifest_path'],
                'base_revision':current['revision'],'revision':ready['revision']}

    def read(self, state, request):
        from task_reading import source_authoring, source_part, scope_read, page_read, page_image
        from task_read_resolution import resolve_source_request,attach_source_handles
        request,binding=resolve_source_request(state,request)
        preparation=None
        region_image=request.view=='image' and (request.bbox is not None or request.scale is not None)
        if request.view in {'page','image'} and not region_image:
            state,preparation=self.ensure_pages(state,request)
        selected = state['selected']; sha = state['context']['source']['sha256']
        if request.view in {'items','item','item_image','image_scope','image_scope_image','feedback'}:
            from task_item_views import read_item_view
            result=read_item_view(state,request)
            result=self.read_links(result,request.task_id,None,selected.get('MAP',{}).get('artifact_sha256'),selected.get('ITEMS',{}).get('artifact_sha256'))
            if 'budget_scope' in result: result['budget_scope']='READER_PAYLOAD_BEFORE_TASK_ENVELOPE'
            return {'status':'READ','task_id':request.task_id,'revision':state['revision'],**result}
        common = {'expected_source_sha256': sha, 'offset': request.offset, 'max_chars': request.max_chars}
        if request.view in {'source', 'part'}:
            ir = Path(selected['READING']['path'])
            if request.view == 'source':
                result = source_authoring(ir, pages=request.pages, refs=request.refs, query=request.query,
                    limit=request.limit, **common)
            else:
                if request.ref is None or request.capture_sha256 is None:
                    raise ValueError('INVALID_DECISION: part requires returned ref and capture_sha256')
                result = source_part(ir, capture_sha256=request.capture_sha256, ref=request.ref,
                    part=request.part or 'text', limit=request.limit, **common)
        elif region_image:
            from task_pdf_images import source_region_image
            picture=source_region_image(state,request)
            result={'image':picture,'_images':[picture]}
        elif request.view == 'scope':
            from task_changes import map_read_dir
            result = scope_read(map_read_dir(state), scope_id=request.scope_id,
                expected_map_sha256=request.expected_map_sha256, part=request.part, limit=request.limit, **common)
        else:
            if request.page is None: raise ValueError('INVALID_DECISION: page/image requires page')
            reading = selected.get('READING', {})
            manifest = reading.get('manifest_path')
            if not manifest or Path(manifest).name != 'reading-workspace.json':
                raise ValueError('READING_NOT_PREPARED: call source_prepare with mode pages')
            directory = Path(manifest).parent
            if request.view == 'page': result = page_read(directory, page=request.page, **common)
            else:
                picture = page_image(directory, expected_source_sha256=sha, page=request.page)
                result = {'image': picture, '_images': [picture]}
        capture = result.get('capture_sha256') or result.get('image', {}).get('capture_sha256')
        if request.capture_sha256 and capture != request.capture_sha256:
            raise ValueError('CAPTURE_VERSION_CONFLICT: the requested source reference belongs to a different reading version')
        result = self.read_links(result, request.task_id, capture,
            state['selected'].get('MAP',{}).get('artifact_sha256'))
        if 'budget_scope' in result: result['budget_scope']='READER_PAYLOAD_BEFORE_TASK_ENVELOPE'
        if capture:result=attach_source_handles(result,capture)
        return {'status':'READ', 'task_id':request.task_id, 'revision':state['revision'], **result,
            **({'source_binding':binding} if binding else {}),
            **({'reading_preparation':preparation} if preparation else {})}

    @staticmethod
    def read_links(value, task_id, capture_sha=None, map_sha=None, baseline_sha=None):
        if isinstance(value, list): return [TaskService.read_links(x,task_id,capture_sha,map_sha,baseline_sha) for x in value]
        if not isinstance(value, dict): return value
        views = {'source_authoring':'source','source_part':'part','scope_read':'scope','page_read':'page','page_image':'image',
            'source_item_read':'item','source_item_image':'item_image','image_scope_read':'image_scope',
            'image_scope_image':'image_scope_image','items_read':'items','feedback_read':'feedback'}
        if value.get('reader') in views and isinstance(value.get('arguments'),dict):
            args = dict(value['arguments'])
            for key in ('source_ir','reading_dir','map_dir','expected_source_sha256','bundle_dir','baseline_dir','source_path','image_dir'): args.pop(key,None)
            if capture_sha and value['reader'] != 'scope_read': args.setdefault('capture_sha256',capture_sha)
            if map_sha and value['reader'] == 'scope_read': args.setdefault('expected_map_sha256',map_sha)
            if value['reader'] in {'source_item_read','source_item_image','items_read','image_scope_read','image_scope_image'}:
                if map_sha:args.setdefault('expected_map_sha256',map_sha)
                if baseline_sha:args.setdefault('expected_baseline_sha256',baseline_sha)
            projected = {'task_id':task_id,'view':views[value['reader']], **args}
            ReadRequest.model_validate(projected)
            return {'tool':'source_read','request':projected}
        return {k:TaskService.read_links(v,task_id,capture_sha,map_sha,baseline_sha) for k,v in value.items()}

    def check(self, root, state):
        result = {'status':'CHECKED', 'task_id':state['context']['work_id'], 'revision':state['revision'],
            'source_verification':state['source_verification'], 'semantic_review':'NOT_CLAIMED',
            'review_verified':False, 'current_delivery_registered':False,
            'remaining':state['brief'], 'incomplete_operations':state['incomplete_operations'][:10],
            'pending_decomposition':decomposition_attention(state)}
        if state['current_working']['owner'] == 'portable_review':
            from revision_tools import verify_review, load_head
            review_root = Path(state['current_working']['review_root'])
            head = load_head(review_root)
            if not head.get('delivery'):
                result['delivery_status'] = 'NOT_REGISTERED'
                return result
            check = verify_review(Namespace(review_dir=review_root))
            result['review_check'] = check
            if check['review_sha256'] != state['current_working']['review_sha256']:
                result.update(status='REVISION_CHANGED_DURING_CHECK', verified_review_sha256=check['review_sha256'])
                return result
            result['review_verified'] = check.get('status') == 'REVIEW_VERIFIED'
            result['current_delivery_registered'] = bool(check.get('delivery'))
            # Use the exact delivery returned by the verifier's locked read,
            # never the preliminary head used to detect an unregistered export.
            local = json.loads((review_root/check['delivery']['path']/'matrix-snapshot.json').read_text())
            result.update(self.workbench_check(state, local))
        return result

    def workbench_check(self, state, local_snapshot):
        from task_workbench import check_workbench
        return check_workbench(state, local_snapshot)

    def sync(self, root, state, request):
        from workbench_tools import pull_workbench
        if request.direction == 'pull':
            review=state['current_working'].get('review_root')
            value = pull_workbench(Namespace(workspace=root, review_dir=Path(review) if review else None, source=None, binding_file=None))
        elif request.direction == 'publish_current_review':
            from task_workbench import sync_current_review
            if not request.expected_revision: raise ValueError('INVALID_DECISION: current review sync requires expected_revision')
            value = sync_current_review(root, request.expected_revision)
        else:
            from workspace_tools import publish_committed
            from reqpilotx.application.agent_workflow.task_workspace import _operation, _verify_inputs
            if not request.operation_id: raise ValueError('INVALID_DECISION: retry_publication needs operation_id')
            operation = _operation(root, request.operation_id)
            receipt_path = root/operation['output_dir']/'operation-receipt.json'
            if not receipt_path.is_file(): raise ValueError('OPERATION_NOT_COMMITTED')
            receipt = json.loads(receipt_path.read_text())
            _verify_inputs(root, operation['inputs'])
            map_input = operation['inputs'].get('map_dir', {})
            original_map = map_input.get('path') if isinstance(map_input, dict) else None
            original_map = (root/original_map).resolve() if original_map else None
            # Resolve only pinned arguments recorded by the original operation.
            args = Namespace(command=operation['operation'], workspace=root, binding_file=Path(state['binding_file']),
                output_dir=receipt_path.parent, review_dir=receipt_path.parent if operation['operation']=='review-open' else None,
                map_dir=original_map)
            value = publish_committed(root, request.operation_id, args, receipt.get('result', {}))
        current=self.state(request.task_id)[1]
        return {'status':'SYNC_CHECKED',**task_summary(current), 'sync':value,
            'source_and_model_not_recompiled':True}


def execute_cli(args):
    from reqpilot import load_runtime
    load_runtime(args.project_root)
    payload = json.loads(sys.stdin.read() if str(args.input)=='-' else args.input.read_text(encoding='utf-8'))
    try:
        return TaskService(args.working_root, Path(__file__).with_name('reqpilot.py'), args.project_root).invoke(args.task_action, payload)
    except (ValueError, KeyError, OSError) as exc:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            return {'status':'FAILED','error_code':'INVALID_DECISION',
                'errors':exc.errors(include_url=False, include_input=False, include_context=False)}
        prefix = str(exc).split(':',1)[0]
        scope=getattr(exc,'scope_context',None)
        return {'status':'FAILED','error_code':prefix if re.fullmatch(r'[A-Z_]+',prefix) else 'INVALID_REQUEST_OR_EVIDENCE',
            'message':str(exc), 'preserved_existing_artifacts':True,
            **({'scope_context':scope,'inspect':{'tool':'source_read','request':{
                'task_id':payload['task_id'],'view':'scope','scope_id':scope['scope_id'],
                'expected_map_sha256':scope['document_map_sha256']}}} if scope and payload.get('task_id') else {}),
            **({'next_action':exc.next_action,'retry_guidance':exc.retry_guidance} if hasattr(exc,'next_action') else {})}
