"""Program import/carry of explicitly user-supplied engineering work opinions.

This is not a model judgment operation. Native Agent tools cannot invoke imports
or supply manual artifacts; application publication uses the user repository.
"""
from datetime import datetime, timezone
import hashlib
import os
import shutil

from artifact_tools import read, save, required, new_output
from reqpilotx.application.agent_workflow.source_item_manual_decisions import (
    build_manual_decision_context, compile_manual_decisions, rebind_manual_decisions, validate_manual_decisions,
)

NAME = 'manual-decisions.json'
RECEIPT = 'manual-decision-import-receipt.json'


def context_for(directory, *, decomposition=None, annotations=None):
    from revision_tools import bundle
    document_map, baseline, _, tables = bundle(directory)
    decomp = decomposition or directory / 'source-item-decomposition.json'
    annotation = annotations or directory / 'image-annotations.json'
    return build_manual_decision_context(document_map, baseline, tables,
        decomposition=read(decomp) if decomp.is_file() else None,
        annotations=read(annotation) if annotation.is_file() else None)


def _user_input(value, context):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'expected_source_sha256', 'expected_baseline_sha256', 'user_input_reference', 'decisions'}
            or value.get('schema_version') != 'source-item-user-decisions/v1'
            or value.get('expected_source_sha256') != context['source_sha256']
            or value.get('expected_baseline_sha256') != context['baseline_sha256']
            or not str(value.get('user_input_reference', '')).strip()):
        raise ValueError('Manual import requires the explicit user-provided decisions/reference and exact source/baseline')
    return value['decisions']


def run_manual(args):
    directory = required(args.baseline_dir, '--baseline-dir')
    context = context_for(directory, decomposition=getattr(args, 'decomposition', None), annotations=getattr(args, 'annotations', None))
    if args.command == 'manual-context':
        if args.output_dir:
            output = new_output(args.output_dir); save(output, 'manual-context.json', context)
            return {'status': 'MANUAL_CONTEXT_READY', 'context_path': str(output / 'manual-context.json'), 'unit_count': len(context['units'])}
        return {'status': 'MANUAL_CONTEXT_READY', 'context': context}
    if os.environ.get('REQPILOT_NATIVE_AGENT_TOOLS') == '1':
        raise ValueError('A native Agent cannot create or withdraw engineer opinions')
    path = required(args.input, '--input'); value = read(path)
    explicit_previous = getattr(args, 'previous_manual_decisions', None)
    previous_path = (explicit_previous or directory / NAME).expanduser().resolve()
    if explicit_previous and not previous_path.is_file():
        raise ValueError('The declared previous engineer-opinion artifact is missing')
    previous = read(previous_path) if previous_path.is_file() else None
    imported_at = datetime.now(timezone.utc).isoformat()
    artifact = compile_manual_decisions(context, _user_input(value, context), previous=previous,
        origin='USER_PROVIDED_FILE', decided_at=imported_at)
    output = new_output(args.output_dir)
    save(output, NAME, artifact); save(output, 'manual-context.json', context)
    shutil.copyfile(path, output / 'user-decisions.json')
    if previous is not None: save(output, 'previous-manual-decisions.json', previous)
    save(output, RECEIPT, {'schema_version': 'source-item-user-decision-import/v1', 'context_sha256': context['context_sha256'],
        'artifact_sha256': artifact['artifact_sha256'], 'previous_artifact_sha256': (previous or {}).get('artifact_sha256', ''),
        'user_input_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'imported_at': imported_at,
        'origin': 'USER_PROVIDED_FILE', 'human_acceptance': 'NOT_CLAIMED'})
    return {'status': 'USER_WORKING_OPINIONS_IMPORTED', 'artifact': str(output / NAME), 'artifact_sha256': artifact['artifact_sha256'],
        'response_revision': artifact['revision'], 'human_acceptance': 'NOT_CLAIMED'}


def verify_import(path, context, previous):
    receipt = read(path.parent / RECEIPT); source = path.parent / 'user-decisions.json'
    if (receipt.get('schema_version') != 'source-item-user-decision-import/v1'
            or receipt.get('context_sha256') != context['context_sha256']
            or receipt.get('previous_artifact_sha256') != (previous or {}).get('artifact_sha256', '')
            or receipt.get('user_input_sha256') != hashlib.sha256(source.read_bytes()).hexdigest()
            or receipt.get('human_acceptance') != 'NOT_CLAIMED' or receipt.get('origin') != 'USER_PROVIDED_FILE'):
        raise ValueError('Manual user-import provenance or predecessor changed')
    expected = compile_manual_decisions(context, _user_input(read(source), context), previous=previous,
        origin='USER_PROVIDED_FILE', decided_at=receipt['imported_at'])
    artifact = read(path)
    if expected != artifact or receipt.get('artifact_sha256') != artifact['artifact_sha256']:
        raise ValueError('Manual artifact differs from replaying the explicit user import')
    return artifact


def verify_manual_transition(previous_dir, current_dir):
    old_path, new_path = previous_dir / NAME, current_dir / NAME
    if not old_path.is_file() and not new_path.is_file(): return
    previous = read(old_path) if old_path.is_file() else None
    context = context_for(current_dir)
    if (current_dir / RECEIPT).is_file():
        expected = verify_import(new_path, context, previous)
    else:
        expected = rebind_manual_decisions(previous, context, previous_context=context_for(previous_dir), require_previous_context=True)
    if not new_path.is_file() or read(new_path) != expected:
        raise ValueError('Review changed or discarded existing user working opinions outside their validated transition')
