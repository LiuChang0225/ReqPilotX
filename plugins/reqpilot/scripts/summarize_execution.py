#!/usr/bin/env python3
"""Summarize actual public events and verified task artifacts, without reasoning."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

EXCLUDED_DIRECTORIES = {'.reqpilot', '.agents', '.venv', 'venv', 'node_modules', '.git', '__pycache__'}
ARTIFACT_NAMES = {'document-map.json', 'source-requirement-baseline.json', 'map-compilation-receipt.json',
    'item-compilation-receipt.json', 'matrix-snapshot.json', 'matrix.xlsx', 'delivery-receipt.json',
    'verification-report.json', 'image-annotations.json', 'source-item-decomposition.json', 'source-findings.json'}
USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _optional_hash(path, workspace):
    if path.is_symlink() or not path.resolve().is_relative_to(workspace): return None
    return sha(path) if path.is_file() else None


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool): return float(value)
    if not isinstance(value, str): return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def _time(path, recorded_at=None, start=None, event=None):
    instant = _epoch(recorded_at)
    source = 'OPERATION_RECEIPT_SAVED_AT'
    if event and _epoch(event.get('timestamp')) is not None:
        instant = _epoch(event['timestamp']); source = 'PUBLIC_OPERATION_RESULT_TIMESTAMP'
    if instant is None:
        instant = path.stat().st_mtime; source = 'FILE_MTIME_ESTIMATE'
    return {'at': datetime.fromtimestamp(instant, timezone.utc).isoformat(),
        'epoch_seconds': instant, 'elapsed_seconds': round(instant-start, 3) if start is not None else None,
        'source': source, 'estimated': source == 'FILE_MTIME_ESTIMATE',
        'meaning': 'Observed tool result' if source.startswith('PUBLIC') else 'Receipt saved; not proof of when the index advanced' if source.startswith('OPERATION') else 'Filesystem estimate only'}


def _files(workspace):
    found, skipped = [], []
    for root, directories, filenames in os.walk(workspace, followlinks=False):
        parent = Path(root)
        kept = []
        for name in directories:
            path = parent/name
            if name in EXCLUDED_DIRECTORIES: continue
            if path.is_symlink(): skipped.append({'path': str(path.relative_to(workspace)), 'reason': 'SYMLINK_NOT_FOLLOWED'})
            else: kept.append(name)
        directories[:] = sorted(kept)
        for name in sorted(filenames):
            path = parent/name
            if path.is_symlink(): skipped.append({'path': str(path.relative_to(workspace)), 'reason': 'SYMLINK_NOT_FOLLOWED'})
            elif path.is_file(): found.append(path)
    return sorted(found), skipped


def _safe_ref(workspace, root, value):
    path = Path(value)
    path = path if path.is_absolute() else root/path
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError('Evaluator will not read a task dependency outside the cold workspace')
    return resolved


def _check_inputs(workspace, root, values):
    if isinstance(values, dict):
        if values.get('kind') in {'file', 'directory'} and 'path' in values:
            _safe_ref(workspace, root, values['path'])
        else:
            for value in values.values(): _check_inputs(workspace, root, value)
    elif isinstance(values, list):
        for value in values: _check_inputs(workspace, root, value)


def _preflight(workspace, root, context, index, operations, files, skipped):
    # The core accepts explicitly supplied external dependencies. The evaluator
    # deliberately has the narrower read boundary of the cold workspace.
    if any((workspace/entry['path']).is_relative_to(root) for entry in skipped):
        raise ValueError('Task contains a symlink; evaluator did not follow it')
    _safe_ref(workspace, root, context['source']['path'])
    _safe_ref(workspace, root, context['binding_file'])
    for value in index.get('selected', {}).values():
        for key in ('path', 'output_dir', 'manifest_path'):
            if value.get(key): _safe_ref(workspace, root, value[key])
    for group in ('committed', 'host_receipts'):
        for value in index.get(group, []):
            for key in ('path', 'receipt_path'):
                if value.get(key): _safe_ref(workspace, root, value[key])
    for operation in operations:
        _safe_ref(workspace, root, operation['output_dir'])
        _check_inputs(workspace, root, operation.get('inputs', {}))
    for path in files:
        if path.name != 'operation-receipt.json' or not path.is_relative_to(root): continue
        receipt = _json(path)
        _check_inputs(workspace, root, receipt.get('inputs', {}))
        for value in [*receipt.get('validation', {}).get('stages', {}).values(),
                      *receipt.get('validation', {}).get('artifacts', [])]:
            if value.get('path'): _safe_ref(workspace, root, value['path'])


def _core():
    try:
        from reqpilotx.application.agent_workflow import task_workspace
    except ModuleNotFoundError:
        from reqpilot import load_runtime
        load_runtime(None)  # The accompanying frozen bundle, never a guessed checkout.
        from reqpilotx.application.agent_workflow import task_workspace
    return task_workspace


def _stage_ref(workspace, value):
    keys = ('artifact_sha256', 'map_sha256', 'baseline_sha256', 'count', 'covered_scope_ids',
        'unprocessed_scope_ids', 'owner', 'compiler_bundle_availability')
    result = {key:value[key] for key in keys if key in value}
    for key in ('path', 'output_dir', 'producer_path', 'compiler_output_dir'):
        if value.get(key): result[key] = str(_safe_ref(workspace, workspace, value[key]).relative_to(workspace))
    return result


def _task_summary(workspace, marker, files, skipped, start, public_operations):
    root = marker.parent
    result = {'task_root': str(root.relative_to(workspace)), 'context_path': str(marker.relative_to(workspace)),
        'validation': 'UNAVAILABLE', 'current_owner': None, 'selected': {}, 'operations': [],
        'first_saved_artifacts': {}, 'first_committed_artifacts': {}}
    try:
        context = _json(marker); index = _json(root/'workspace-index.json')
        result.update(work_id=context.get('work_id'), source_sha256=context.get('source', {}).get('sha256'),
            project_key=context.get('project_key'), document_key=context.get('document_key'),
            require_map_confirmation=context.get('request', {}).get('require_map_confirmation'))
        operations = [_json(path) for path in files if path.parent == root/'operations' and path.name.startswith('op-') and path.suffix == '.json']
        _preflight(workspace, root, context, index, operations, files, skipped)
        core = _core()
        context = core._read_context(root); index = core._index(root, context)
        result['index_generation'] = index['generation']
        indexed = {entry['operation_id']:entry for entry in index['committed']}
        for operation in operations:
            entry = indexed.get(operation['operation_id'])
            directory = _safe_ref(workspace, root, operation['output_dir'])
            receipt_path = directory/'operation-receipt.json'
            item = {'operation_id': operation['operation_id'], 'operation': operation['operation'],
                'reported_operation_state': operation.get('state'), 'commit_state': entry['state'] if entry else 'NOT_COMMITTED',
                'output_dir': str(directory.relative_to(workspace)), 'validation': 'NOT_VALIDATED', 'artifacts': []}
            if receipt_path.is_file():
                try:
                    receipt = core._receipt(root, context, operation)
                    if entry and entry['receipt_sha256'] != receipt['receipt_sha256']:
                        raise ValueError('Index references a different operation receipt')
                    item.update(validation='PASSED', commit_state=entry['state'] if entry else 'SAVED_UNINDEXED',
                        receipt_path=str(receipt_path.relative_to(workspace)), receipt_sha256=receipt['receipt_sha256'],
                        time=_time(receipt_path, receipt.get('saved_at'), start, public_operations.get(operation['operation_id'])))
                    for artifact in receipt['validation']['artifacts']:
                        path = _safe_ref(workspace, root, artifact['path'])
                        item['artifacts'].append({key:value for key,value in artifact.items() if key != 'path'} |
                            {'path':str(path.relative_to(workspace))})
                        milestone = {'operation_id': operation['operation_id'], 'path': str(path.relative_to(workspace)),
                            'commit_state': item['commit_state'], 'time': item['time']}
                        for key in ('first_saved_artifacts', 'first_committed_artifacts'):
                            if key == 'first_committed_artifacts' and item['commit_state'] != 'COMMITTED': continue
                            old = result[key].get(artifact['kind'])
                            if old is None or item['time']['epoch_seconds'] < old['time']['epoch_seconds']:
                                result[key][artifact['kind']] = milestone
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    item.update(validation='FAILED', error=str(exc))
            else:
                item['output_state'] = 'PARTIAL_OR_UNCOMMITTED' if directory.exists() else 'NO_OUTPUT_DIRECTORY'
            result['operations'].append(item)
        status = core.workspace_status(root, refresh=False)
        result.update(validation='PASSED', current_owner=status['current_working']['owner'],
            selected={key:_stage_ref(workspace, value) for key,value in status['selected'].items()},
            work_state=status['brief']['work_state'], map_confirmation=status['brief']['map_confirmation'],
            analysis_policy=status['brief']['analysis_policy'], candidates=status['candidates'])
        if status['current_working']['owner'] == 'portable_review':
            result['review'] = {key:status['current_working'].get(key) for key in
                ('review_sha256', 'version_path', 'baseline_sha256', 'document_map_sha256')}
            result['review']['path'] = str(Path(status['current_working']['review_root']).relative_to(workspace))
        result['host_receipts'] = []
        for entry in index.get('host_receipts', []):
            path = _safe_ref(workspace, root, entry['path'])
            value = core._validate_host_receipt(root, context, index, _json(path))
            result['host_receipts'].append({'path': str(path.relative_to(workspace)), 'operation_id': value['operation_id'],
                'recorded_at': value.get('recorded_at'), 'receipt': value['receipt']})
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        result['error'] = str(exc)
    return result


def _events(path):
    commands, collaborations, errors, thread_ids, usage_reports, public_operations = [], [], [], [], [], {}
    completed_turns = 0
    for position, line in enumerate(path.read_text(encoding='utf-8').splitlines()):
        try: event = json.loads(line)
        except json.JSONDecodeError:
            errors.append({'type':'invalid_jsonl_event', 'event_index':position}); continue
        event_type = event.get('type')
        if event_type == 'thread.started': thread_ids.append(event.get('thread_id'))
        if event_type == 'turn.completed':
            completed_turns += 1
            usage_reports.append(event.get('usage') if isinstance(event.get('usage'), dict) else None)
        item = event.get('item', {})
        if event_type == 'item.completed' and item.get('type') == 'command_execution':
            command = {key:item.get(key) for key in ('command', 'exit_code', 'status')}
            command['event_index'] = position
            output = item.get('aggregated_output')
            if isinstance(output, str):
                command.update(output_excerpt=output[:16000], output_truncated=len(output)>16000,
                    output_sha256=hashlib.sha256(output.encode()).hexdigest())
                try: public = json.loads(output)
                except json.JSONDecodeError: public = None
                if isinstance(public, dict):
                    command['public_result'] = {key:public[key] for key in
                        ('status', 'state', 'operation_id', 'task_root', 'workbench_sync', 'publication') if key in public}
                    if public.get('operation_id'):
                        public_operations[public['operation_id']] = {'event_index': position,
                            'timestamp': event.get('timestamp') or event.get('observed_at')}
            commands.append(command)
        elif event_type == 'item.completed' and item.get('type') in {'collab_tool_call','collaboration_tool_call'}:
            collaborations.append({key:value for key,value in item.items() if key in {'id','type','tool','status','sender_thread_id','receiver_thread_ids','agents_states'}})
        elif event_type == 'item.completed' and item.get('type') == 'error':
            errors.append({'type':'tool_error','message':item.get('message')})
        elif event_type in {'error','turn.failed'}:
            errors.append({'type':event_type,'message':event.get('message',event.get('error'))})
    usage = None
    if any(report is not None for report in usage_reports):
        usage = {'coverage':'COMPLETED_TURNS_ONLY', 'reported_turns':sum(report is not None for report in usage_reports),
            'completed_turns':completed_turns, 'turn_reports':usage_reports,
            'totals':{key:sum(report[key] for report in usage_reports) if usage_reports and all(
                report is not None and type(report.get(key)) is int for report in usage_reports) else None for key in USAGE_KEYS},
            'reasoning_output_included_in_output':True}
    return commands, collaborations, errors, thread_ids, completed_turns, usage, public_operations


def summarize(workspace: Path, events: Path, exit_code: int | None) -> dict:
    workspace = workspace.resolve(strict=True)
    commands, collaborations, errors, thread_ids, completed_turns, usage, public_operations = _events(events)
    files, skipped = _files(workspace)
    start_path = events.parent/'execution-start.json'
    start_data = _json(start_path) if start_path.is_file() else {}
    start = _epoch(start_data.get('started_epoch_seconds')) or _epoch(start_data.get('started_at'))
    tasks = [_task_summary(workspace, path, files, skipped, start, public_operations)
             for path in files if path.name == 'task-context.json']
    artifacts = []
    for path in files:
        if path.name not in ARTIFACT_NAMES: continue
        task = next((t for t in tasks if path.is_relative_to(workspace/t['task_root'])), None)
        if task is None and not path.is_relative_to(workspace/'runtime-data'): continue
        observed = {'path':str(path.relative_to(workspace)), 'bytes':path.stat().st_size, 'sha256':sha(path),
            'evidence_role':'TASK_FILE_OBSERVATION' if task else 'LEGACY_FILE_OBSERVATION',
            'semantic_validation':'NOT_CLAIMED', 'file_time':_time(path, start=start)}
        if task:
            observed.update(task_root=task['task_root'], work_id=task.get('work_id'), source_sha256=task.get('source_sha256'))
            owners = [operation for operation in task['operations'] if any(a['path'] == observed['path'] for a in operation['artifacts'])]
            observed['operation_refs'] = [{key:operation[key] for key in ('operation_id','commit_state','validation')} for operation in owners]
            observed['current_stages'] = [stage for stage,value in task['selected'].items() if value.get('path') == observed['path']]
        artifacts.append(observed)
    build = workspace/'.reqpilot/build-receipt.json'; manifest = workspace/'runtime-data/input-manifest.json'
    stderr = events.parent/'stderr.log'
    if stderr.is_file():
        for line in stderr.read_text(encoding='utf-8',errors='replace').splitlines():
            if 'collab spawn failed' in line or 'failed to spawn' in line: errors.append({'type':'host_spawn_error','message':line})
    return {'schema_version':'reqpilot-cold-execution-evidence/v1', 'execution':'RUNNING' if exit_code is None else 'EXITED',
        'exit_code':exit_code, 'completed_turns':completed_turns, 'thread_ids':thread_ids,
        'bundle_receipt_sha256':_optional_hash(build, workspace),
        'input_manifest_sha256':_optional_hash(manifest, workspace), 'events_sha256':sha(events),
        'completed_commands':commands, 'collaboration_calls':collaborations, 'errors':errors, 'usage':usage,
        'observed_artifacts':artifacts, 'task_workspaces':tasks, 'skipped_paths':skipped,
        'work_selection':'EXPLICIT_TASKS_AND_CURRENT_OWNER; no mtime winner',
        'semantic_pass':'REQUIRES_SOURCE_AND_OUTPUT_REVIEW',
        'interpretation':'Public execution and file/receipt evidence only. Validated commits, retained candidates, uncommitted files, semantic fidelity and human acceptance are separate; no stage or usage is inferred from absence.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('--exit-code', type=int)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); output = args.output.expanduser().resolve()
    if output.exists(): raise ValueError('Execution evidence output must be a new file')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = summarize(args.workspace.expanduser().resolve(), args.events.expanduser().resolve(), args.exit_code)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False)+'\n', encoding='utf-8'); output.chmod(0o600)
    print(json.dumps({'execution':payload['execution'], 'completed_commands':len(payload['completed_commands']),
        'collaboration_calls':len(payload['collaboration_calls']), 'observed_artifacts':len(payload['observed_artifacts']),
        'task_workspaces':len(payload['task_workspaces']), 'output':str(output)},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
