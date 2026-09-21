"""Adapt immutable plugin versions to the existing source workbench read model.

No application baseline publication or native Agent job is performed here.
"""
from copy import deepcopy

from reqpilotx_server.application.agent_workflow import plugin_workbench as transport
from reqpilotx_server.application.agent_workflow import source_requirement_baseline as core
from reqpilotx_server.application.agent_workflow.native_structure_bridge import _public_continuation
from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
    project_source_requirement_outline, project_source_requirement_record,
)
from reqpilotx_server.application.agent_workflow.source_item_review_context import project_review_context
from reqpilotx_server.application.agent_workflow.source_item_workbench_metadata import project_workbench_metadata, numbering_findings
from reqpilotx_server.application.transport import ResponseSink


def _inputs(project_id, document_id, session_id, revision):
    root, head = transport._load(project_id, document_id, session_id)
    directory, version = transport._version(root, head, revision)
    artifacts = {name: transport._artifact(directory, version, name)
                 for name in version['files'] if name in transport.ARTIFACT_NAMES}
    artifacts['matrix-snapshot.json'] = transport._artifact(directory, version, 'matrix-snapshot.json')
    mapping = core.DocumentMapArtifactV1.model_validate(artifacts['document-map.json'])
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(artifacts['source-requirement-baseline.json']) if version['baseline_sha256'] else None
    bundle = {'document_map': mapping, 'baseline': baseline,
              'receipt': core.SourceRequirementBaselineValidationReceiptV1.model_validate(artifacts['validation-receipt.json']) if baseline else None,
              'manifest': {'authority': 'EXTERNAL_PLUGIN'}, 'run_receipts': [],
              'image_annotations': artifacts.get('image-annotations.json')}
    bundle['saved_number_metadata'] = _saved_number_metadata(mapping, baseline, artifacts['matrix-snapshot.json'],
        version, artifacts.get('source-item-decomposition.json'))
    return head, version, artifacts, bundle


def _saved_number_metadata(mapping, baseline, snapshot, version, decomposition):
    """Read numbers from this immutable Matrix version, including old empty IDs.

    Upgrading a projector must not make the source reader show numbers that are
    absent from the saved Matrix/Excel. Never borrow another version or rebuild
    a missing snapshot to satisfy a read request.
    """
    def conflict():
        raise transport.ApplicationError(status_code=409, detail='保存的编号投影与成果版本不一致，请重新同步。')
    identity = snapshot.get('source_item_baseline') or {}
    if (snapshot.get('delivery_sha256') != version.get('snapshot_sha256')
            or snapshot.get('delivery_sha256') != core.canonical_json_sha256({k:v for k,v in snapshot.items() if k!='delivery_sha256'})
            or version.get('source_sha256') != mapping.source_sha256
            or identity.get('source_sha256') != mapping.source_sha256
            or identity.get('document_map_artifact_sha256') != mapping.artifact_sha256
            or baseline and (identity.get('artifact_sha256') != baseline.artifact_sha256
                or version.get('baseline_sha256') != baseline.artifact_sha256)
            or decomposition and (baseline is None or decomposition.get('baseline_sha256') != baseline.artifact_sha256)):
        conflict()
    expected_sources = {r.source_record_id for r in baseline.records} if baseline else set()
    expected_children = {c['singular_requirement_id']:sid for sid,children in (decomposition or {}).get('records',{}).items() for c in children}
    if set(expected_children.values()) - expected_sources: conflict()
    numbers, derived = {}, {}
    for node in snapshot.get('nodes',[]):
        item = node.get('source_item')
        number = node.get('source_number', '')
        if not isinstance(number,str): conflict()
        if item:
            sid = item.get('source_item_id')
            entries = item.get('source_numbers', [])
            if sid not in expected_sources or sid in numbers or not isinstance(entries,list): conflict()
            numbers[sid] = {'source_number':number, 'source_numbers':deepcopy(entries),
                'source_number_role':item.get('source_number_role') or (
                    'SOURCE_NUMBER_COLLECTION' if len(entries)>1 else 'EXACT_SOURCE_NUMBER' if number else 'UNNUMBERED')}
        if node.get('source_kind') == 'SINGULAR_REQUIREMENT':
            child_id = node.get('node_id'); value = node.get('decomposition') or {}
            if child_id not in expected_children or child_id in derived or value.get('source_item_id') != expected_children[child_id]: conflict()
            derived[child_id] = {'source_number':number, 'source_number_projection':deepcopy(value.get('source_number_projection'))}
    if set(numbers) != expected_sources or set(derived) != set(expected_children): conflict()
    return {'numbers':numbers, 'derived_numbers':derived}


def _feedback(head, version, bundle):
    """Retain exact-origin history; a plugin-reported change is pending human review."""
    result = []
    baseline, mapping = bundle['baseline'], bundle['document_map']
    number = next(v['number'] for v in head['versions'] if v['revision_id'] == version['revision_id'])
    visible_versions = {v['revision_id'] for v in head['versions'] if v['number'] <= number}
    for item in head['feedback']:
        if item['expected_revision_id'] not in visible_versions:
            continue
        if item['map_sha256'] != mapping.artifact_sha256:
            continue
        resolution = head.get('feedback_resolutions', {}).get(item['feedback_id'])
        if resolution and resolution['revision_id'] not in visible_versions:
            resolution = None
        common = {'feedback_id': item['feedback_id'], 'project_id': head['project_id'],
                  'document_id': head['document_id'], 'source_sha256': head['source_sha256'],
                  'command_id': item['command_id'], 'message': item['text'],
                  'created_at_utc': item['created_at'], 'origin_revision_id': item['expected_revision_id'],
                  'resolution': resolution, 'human_acceptance': 'NOT_CLAIMED'}
        if baseline and item.get('baseline_sha256') == baseline.artifact_sha256:
            for sid in item['source_item_ids']:
                result.append({**common, 'schema_version': 'source-requirement-feedback/v1',
                    'source_record_id': sid, 'baseline_artifact_id': baseline.artifact_id,
                    'document_map_artifact_sha256': mapping.artifact_sha256,
                    'status': 'CONSUMED' if resolution else 'PENDING_AGENT'})
        if item.get('node_id') and not item['source_item_ids']:
            result.append({**common, 'schema_version': 'native-structure-feedback/v1',
                'artifact_id': mapping.native_artifact_id, 'scope_kind': 'STRUCTURE_NODE',
                'scope_id': item['node_id'], 'status': 'ACKNOWLEDGED' if resolution else 'PENDING_CODEX_APP'})
    return result


def _tables(mapping, artifacts):
    output = deepcopy(artifacts['table-previews.json'])
    for table in mapping.logical_tables:
        preview = output[table.scope_id]
        preview.update(schema_version='native-structure-table-preview/v1', artifact_id=mapping.native_artifact_id,
            title=table.title, presentation=core.document_map_table_presentation(table),
            summary={'fragment_count': len(preview['fragments']),
                     'row_count': sum(f['row_count'] for f in preview['fragments']),
                     'cell_count': sum(len(f['cells']) for f in preview['fragments'])})
    return output


def get_plugin_reader(project_id: str, document_id: str, session_id: str, revision: str, response: ResponseSink):
    response.headers.update(transport._no_store_headers())
    head, version, artifacts, bundle = _inputs(project_id, document_id, session_id, revision)
    mapping, baseline = bundle['document_map'], bundle['baseline']
    feedback = _feedback(head, version, bundle)
    tree = {'schema_version': 'native-document-structure-tree/v1', 'root_ids': list(mapping.root_node_ids),
            'nodes': [n.model_dump(mode='json', exclude={'ancestor_path'}) for n in mapping.nodes]}
    continuation = _public_continuation(feedbacks=[], proposal_decisions=[], handoffs=[], handoff_events=[], handoff_attempts=[], proposals=[])
    continuation.update(can_continue=False, advancement_allowed=False, evidence_state=mapping.evidence_state,
                        message='修改意见保存后，由当前插件任务取回并修订。')
    reference = {'schema_version': 'native-structure-reference-public/v1', 'availability': 'READY',
        'source_sha256': mapping.source_sha256, 'artifact_id': mapping.native_artifact_id,
        'artifact_status': 'WORKING', 'coverage_status': mapping.status, 'boundary_status': mapping.evidence_state,
        'workflow_step': 'DOCUMENT_STRUCTURE_RECOVERY', 'summary': {}, 'document_tree': tree,
        'logical_tables': [t.model_dump(mode='json') for t in mapping.logical_tables],
        'reading_projections': {}, 'page_ownership': [], 'main_document_sections': [],
        'structure_proposals': [], 'physical_table_dispositions': [], 'unresolved': [], 'implementation_findings': [],
        'visual_summary': {'page_count': max((p for n in mapping.nodes for p in n.pages), default=1),
                           'opened_page_count': 0, 'receipts': [], 'semantic_verification': 'NOT_CLAIMED'},
        'structure_projection': {'schema_version': 'native-structure-projection-state/v1', 'state': 'DRAFT',
            'base_artifact_id': mapping.native_artifact_id, 'projection_sha256': core.canonical_json_sha256(tree),
            'attempt_id': None, 'attempt_ordinal': None, 'affected_scope_ids': [], 'pending_rendered_scope_ids': [], 'validation_status': 'PASS'},
        'repeatability': {'status': 'NOT_RUN', 'stable_semantics': {}, 'refinements_applied': [], 'unresolved_not_settled_by_replay': [], 'quality_claim': 'NOT_CLAIMED'},
        'validation': {'status': 'PASS', 'checks': {}, 'quality_claim': 'SOURCE_BOUND_COMPILATION_ONLY'},
        'oracle_diagnostic': {'authority': 'NONE'}, 'activity': [], 'continuation': continuation,
        'feedbacks': [f for f in feedback if 'scope_id' in f],
        'feedback_capability': {'status': 'QUEUE_READY', 'agent_delivery': continuation['status'], 'message': continuation['message']}}
    reference['source_findings'] = numbering_findings(mapping)
    tables = _tables(mapping, artifacts)
    source_feedback = [f for f in feedback if 'source_record_id' in f]
    decomposition = artifacts.get('source-item-decomposition.json')
    metadata = project_workbench_metadata(mapping, baseline, annotations=bundle['image_annotations'],
        decomposition=decomposition, table_previews=artifacts['table-previews.json']) if baseline else None
    if metadata is not None:
        metadata.update(bundle['saved_number_metadata'])
    outline = project_source_requirement_outline(bundle, feedbacks=source_feedback, metadata=metadata) if baseline else None
    displayed_decomposition = deepcopy(decomposition)
    if displayed_decomposition:
        for children in displayed_decomposition['records'].values():
            for child in children:
                child.update(metadata['derived_numbers'].get(child['singular_requirement_id'], {}))
    required = [r for r in baseline.records if r.requirementness.value == 'REQUIREMENT'] if baseline else []
    unit_count = sum(len((decomposition or {}).get('records', {}).get(r.source_record_id) or [r]) for r in required)
    reasoning = {'source_sha256': mapping.source_sha256, 'baseline_sha256': baseline.artifact_sha256,
        'baseline_revision': baseline.revision, 'source_item_count': len(baseline.records), 'requirement_count': len(required),
        'non_requirement_count': len(baseline.records)-len(required), 'analysis_unit_count': unit_count,
        'assessed_unit_count': 0, 'pending_unit_count': unit_count, 'decomposition_job_id': None, 'analysis_job_id': None,
        'active_jobs': [], 'notices': [], 'decomposition': displayed_decomposition, 'assessments': None} if baseline else None
    review = project_review_context(mapping, baseline, artifacts['table-previews.json'],
        project_id=project_id, document_id=document_id, visual_annotations=bundle['image_annotations'],
        decomposition=decomposition, feedbacks=source_feedback) if baseline else None
    return {'revision_id': version['revision_id'], 'source_sha256': head['source_sha256'],
            'revision_number': next(v['number'] for v in head['versions'] if v['revision_id'] == version['revision_id']),
            'reference': reference, 'outline': outline, 'tables': tables, 'reasoning': reasoning, 'review': review,
            'source_findings': metadata['source_findings'] if metadata else reference['source_findings'],
            'scope_summaries': metadata['scope_summaries'] if metadata else None,
            'analysis_policy': artifacts.get('analysis-policy.json')}


def get_plugin_record(project_id: str, document_id: str, session_id: str, source_record_id: str, revision: str, response: ResponseSink):
    response.headers.update(transport._no_store_headers())
    head, version, artifacts, bundle = _inputs(project_id, document_id, session_id, revision)
    if not bundle['baseline']:
        raise transport.ApplicationError(status_code=404, detail='这一版尚无原文条目。')
    document = transport._source(project_id, document_id, head['source_sha256'])
    tables = _tables(bundle['document_map'], artifacts)
    result = project_source_requirement_record(bundle, source_record_id,
        feedbacks=[f for f in _feedback(head, version, bundle) if f.get('source_record_id') == source_record_id],
        source_path=transport.resolve_source_record_path(document), table_preview=lambda scope: tables[scope])
    result['record'].update(deepcopy(bundle['saved_number_metadata']['numbers'][source_record_id]))
    return result
