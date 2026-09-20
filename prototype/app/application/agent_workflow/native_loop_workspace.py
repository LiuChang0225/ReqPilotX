"""Materialize the current application graph for the portable Agent tools."""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
import shutil

from app.application.agent_workflow.native_loop_tools import _save


def export_registered_knowledge(catalog: dict, output_dir: Path) -> None:
    """Copy actual registered originals so the portable host can recapture facts."""
    if not catalog.get('documents') and not catalog.get('evidence_catalog'):
        return
    from app import db
    from app.application.source_runtime import resolve_source_record_path
    from app.application.knowledge_evidence_capture import capture_knowledge_facts
    from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

    project_id = catalog['project_id']
    records = {record['id']: record for record in db.list_knowledge_documents(project_id)}
    registration = {'schema_version': 'reqpilot-evidence-registration/v1', 'project_id': project_id,
        'knowledge_base_version': catalog['knowledge_base_version'], 'as_of': date.today().isoformat(),
        'declaration': {'origin': 'HOST_REGISTERED', 'reference': 'Persisted project internal-knowledge registrations'},
        'documents': [], 'selection': catalog.get('selection', 'ALL_PROJECT_KNOWLEDGE'),
        'authority_boundary': 'Existing project document metadata, not new Agent or human approval.'}
    captured_facts, captured_documents, captured_gaps = {}, [], []
    for number, document in enumerate(sorted(catalog['documents'], key=lambda value: value['id']), 1):
        current = records.get(document['id'])
        if current is None:
            raise ValueError('Registered internal source no longer belongs to this project')
        keys = ('id', 'project_id', 'source_sha256', 'file_name', 'doc_version', 'doc_type', 'status',
                'authority', 'claim_role', 'applicability', 'effective_from', 'effective_to')
        record = {key: current.get(key) for key in keys}
        source_path = resolve_source_record_path(current).resolve(strict=True)
        relative = f'knowledge-sources/{number:03d}{source_path.suffix.lower()}'
        target = output_dir / relative
        target.parent.mkdir(exist_ok=True, mode=0o700)
        shutil.copyfile(source_path, target); target.chmod(0o600)
        captured = capture_knowledge_facts(record, target, project_id=project_id, as_of=date.fromisoformat(registration['as_of']))
        captured_facts.update(captured['facts']); captured_documents.append(captured['document'])
        captured_gaps.extend({'document_id': record['id'], **gap} for gap in captured['gaps'])
        registration['documents'].append({'source_path': relative, 'record': record})
    if (captured_facts != catalog['evidence_catalog'] or captured_documents != catalog['documents'] or captured_gaps != catalog.get('gaps', [])):
        raise ValueError('Internal knowledge changed while freezing the Agent task; reload current evidence')
    registration['catalog_sha256'] = catalog['catalog_sha256']
    registration['registration_sha256'] = canonical_json_sha256(registration)
    _save(output_dir / 'evidence-registration.json', registration)


def export_current_review_input(project_id: str, document_id: str, output_dir: Path,
                                *, expected_baseline_sha256: str) -> dict:
    from app.application.agent_workflow.source_item_agent_jobs import _source_context, _container, _read_private
    from app.application.agent_workflow.source_item_delivery_bridge import _reasoning
    from app.application.agent_workflow.source_requirement_baseline import build_source_item_text_scope_request
    from app.application.knowledge_facts import build_project_knowledge_catalog

    _, seed, _, _ = _source_context(project_id, document_id, ())
    ids = tuple(record.source_record_id for record in seed['baseline'].records)
    _, bundle, source_path, tables = _source_context(project_id, document_id, ids)
    baseline, document_map = bundle['baseline'], bundle['document_map']
    if baseline.artifact_sha256 != expected_baseline_sha256:
        raise ValueError('The application baseline changed before review export')
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != baseline.source_sha256:
        raise ValueError('Original source does not match the application baseline')
    reasoning = _reasoning(project_id, document_id, baseline)
    values = {
        'document-map.json': document_map.model_dump(mode='json'),
        'source-requirement-baseline.json': baseline.model_dump(mode='json'),
        'validation-receipt.json': bundle['receipt'].model_dump(mode='json'),
        'table-previews.json': tables,
    }
    if bundle.get('image_annotations'):
        values['image-annotations.json'] = bundle['image_annotations']
    if reasoning.get('decomposition'):
        values['source-item-decomposition.json'] = reasoning['decomposition']
    catalog = build_project_knowledge_catalog(project_id)
    if reasoning.get('assessments'):
        job = _container().jobs.get(reasoning['analysis_job_id'])
        pointer = job.result['analysis_input_artifact'] if job.kind in {'agent.source-item.loop', 'agent.source-item.extract-loop'} else job.payload['input_artifact']
        inputs = _read_private(_container(), pointer, project_id=project_id,
                              document_id=document_id, namespace=job.payload.get('operation_namespace', 'working'))
        values['source-item-assessments.json'] = reasoning['assessments']
        values['analysis-packet.json'] = inputs['packet']
        catalog = inputs['knowledge_catalog']
    values['knowledge-catalog.json'] = catalog
    # A reading workspace includes every original node, table and image; a
    # selected feedback scope never becomes the Agent's evidence-access limit.
    values['scope-requests.json'] = {
        'text_requests': [build_source_item_text_scope_request(document_map, node_id=node.node_id).model_dump(mode='json')
                          for node in document_map.nodes if node.source_content],
        'table_requests': [],
        'table_request_state': 'READ_FULL_TABLE_PREVIEWS; boundary revision uses the original current Map',
    }
    current = _source_context(project_id, document_id, ())[1]['baseline']
    if current.artifact_sha256 != baseline.artifact_sha256:
        raise ValueError('Application baseline changed while exporting the review workspace')
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in values.items():
        _save(output_dir / name, value)
    export_registered_knowledge(catalog, output_dir)
    target = output_dir / ('original' + source_path.suffix.lower())
    shutil.copyfile(source_path, target)
    target.chmod(0o600)
    receipt = {'schema_version': 'reqpilotx-application-review-export/v1',
        'project_id': project_id, 'document_id': document_id, 'source_sha256': baseline.source_sha256,
        'document_map_sha256': document_map.artifact_sha256, 'baseline_sha256': baseline.artifact_sha256,
        'baseline_revision': baseline.revision, 'source_item_count': len(baseline.records),
        'source_path': str(target), 'baseline_dir': str(output_dir),
        'files': {str(path.relative_to(output_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in output_dir.rglob('*') if path.is_file()},
        'human_acceptance': 'NOT_CLAIMED', 'application_write': False}
    _save(output_dir / 'application-export-receipt.json', receipt)
    return receipt
