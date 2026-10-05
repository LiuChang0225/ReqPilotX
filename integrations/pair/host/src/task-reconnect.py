"""Explicit Pair-owned endpoint migration. Never creates work or replays mutations."""
from __future__ import annotations
from argparse import Namespace
import hashlib
import json
import re
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def pinned_file(root, name):
    path = root / name
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('TASK_RECONNECT_WORKSPACE_MISMATCH')
    return path


def _reconnect(value):
    required = {'workspace', 'task_id', 'task_root', 'source_sha256', 'bundle_sha256', 'application_id',
                'command_id', 'goal', 'require_map_confirmation', 'project_id', 'document_id', 'work_ref',
                'from_origin', 'to_origin', 'known_origins'}
    if set(value) != required:
        raise ValueError('TASK_RECONNECT_INVALID_FIELDS')
    workspace = Path(value['workspace'])
    if workspace.is_symlink() or workspace.resolve(strict=True) != workspace:
        raise ValueError('TASK_RECONNECT_WORKSPACE_MISMATCH')
    if not isinstance(value['known_origins'], list) or any(not isinstance(v, str) for v in value['known_origins']):
        raise ValueError('TASK_RECONNECT_INVALID_FIELDS')
    for origin in (value['from_origin'], value['to_origin'], *value['known_origins']):
        parsed = urlsplit(origin)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('TASK_RECONNECT_ENDPOINT_UNTRUSTED')
    fixed = pinned_file(workspace, '.reqpilot')
    receipt = read(pinned_file(fixed, 'build-receipt.json'))
    if receipt.get('bundle_sha256') != value['bundle_sha256'] or digest({k: v for k, v in receipt.items() if k != 'bundle_sha256'}) != value['bundle_sha256']:
        raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
    declared = {entry['path'] for entry in receipt['plugin_files']} | {'build-receipt.json'}
    declared |= {'runtime/prototype/' + entry['path'] for entry in receipt['runtime_files']}
    actual = set()
    for path in fixed.rglob('*'):
        if path.is_symlink():
            raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
        relative = path.relative_to(fixed)
        if path.is_file():
            if '__pycache__' in relative.parts:
                match = re.fullmatch(r'(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc', path.name)
                source = relative.parent.parent / ((match[1] + '.py') if match else '')
                if path.parent.name != '__pycache__' or not match or source.as_posix() not in declared:
                    raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
                continue
            actual.add(relative.as_posix())
    if actual != declared:
        raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
    # Authenticate code before importing it. The current installation is never substituted.
    for entries, root in ((receipt['plugin_files'], fixed), (receipt['runtime_files'], fixed / 'runtime/prototype')):
        for entry in entries:
            path = pinned_file(root, entry['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
    sys.path.insert(0, str(fixed / 'scripts'))
    import reqpilot
    reqpilot.load_runtime()
    from task_service import OpenRequest, find_task_root
    from reqpilotx.application.agent_workflow import task_workspace as core
    from revision_tools import locked
    from workbench_tools import _validate_binding, _validate_state, workbench_result_url, save, initial_session_id, NoRedirect
    from workspace_tools import _publication_lock
    def request(url):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
            raise ValueError('TASK_RECONNECT_ENDPOINT_UNTRUSTED')
        # Inherited proxy settings must never route an owned-loopback check elsewhere.
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url), timeout=15) as response:
            return json.load(response)
    original_request = OpenRequest.model_validate({'mode': 'create', 'creation_key': value['command_id'],
        'project_key': 'project-' + value['project_id'], 'document_key': 'document-' + value['document_id'],
        'goal': value['goal'], 'require_map_confirmation': value['require_map_confirmation']})
    expected_preparation = digest({'request': original_request.model_dump(exclude={'source', 'workbench_url'}),
        'application_id': value['application_id'], 'source_sha256': value['source_sha256'],
        'bundle_sha256': value['bundle_sha256'],
        'target': {'projectId': [value['project_id']], 'documentId': [value['document_id']]}})
    if read(pinned_file(workspace, 'host-preparation.json')).get('identity') != expected_preparation:
        raise ValueError('TASK_RECONNECT_OWNER_MISMATCH')
    inputs = read(pinned_file(workspace, 'runtime-data/input-manifest.json'))['inputs']
    if len(inputs) != 1 or inputs[0]['sha256'] != value['source_sha256'] or hashlib.sha256(pinned_file(workspace, inputs[0]['path']).read_bytes()).hexdigest() != value['source_sha256']:
        raise ValueError('TASK_RECONNECT_SOURCE_CHANGED')
    root = find_task_root(workspace, value['task_id'])
    if root != Path(value['task_root']) or root.is_symlink():
        raise ValueError('TASK_RECONNECT_TASK_MISMATCH')
    creation = read(pinned_file(workspace, 'runtime-data/workspace-creations/' + digest(value['command_id']) + '.json'))
    if (creation.get('schema_version') != 'reqpilot-workspace-creation/v1' or core._seal(creation, 'creation_sha256') != creation
            or creation.get('creation_key') != value['command_id'] or creation.get('work_id') != value['task_id']
            or creation.get('state') != 'READY' or workspace / creation.get('task_path', '') != root):
        raise ValueError('TASK_RECONNECT_TASK_MISMATCH')
    # Use the same locks as normal publication/binding and local task writes.
    with _publication_lock(root):
        initial = core.workspace_status(root, refresh=False)
        path = Path(initial['binding_file'])
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('TASK_RECONNECT_WORKSPACE_MISMATCH')
        # Binding -> task is the normal pull/sync order. Never acquire it in reverse.
        with locked(path.parent), core._locked(root):
            state = core.workspace_status(root, refresh=False)
            if Path(state['binding_file']) != path:
                raise ValueError('TASK_RECONNECT_BINDING_MISMATCH')
            context = state['context']
            if context['work_id'] != value['task_id'] or context['source']['sha256'] != value['source_sha256'] or any(context.get(k) != value[k] for k in ('project_id', 'document_id')):
                raise ValueError('TASK_RECONNECT_TASK_MISMATCH')
            if context['request']['goal'] != value['goal'] or context['request']['require_map_confirmation'] != value['require_map_confirmation']:
                raise ValueError('TASK_RECONNECT_TASK_MISMATCH')
            if initial_session_id((root, context), value['source_sha256'], value['project_id'], value['document_id']) != value['work_ref']:
                raise ValueError('TASK_RECONNECT_BINDING_MISMATCH')
            runtime = context.get('runtime_ref') or {}
            if runtime.get('kind') != 'BUNDLED' or runtime.get('bundle_sha256') != value['bundle_sha256'] or Path(runtime.get('entrypoint', '')) != fixed / 'scripts/reqpilot.py' or runtime.get('python_executable') != sys.executable:
                raise ValueError('TASK_RECONNECT_PACKAGE_MISMATCH')
            known_operation_states = {'FAILED', 'BLOCKED', 'COMMITTED', 'SAVED_CANDIDATE', 'CANDIDATE_CONFLICT'}
            if any(read(pinned_file(root, str(path.relative_to(root)))).get('state') not in known_operation_states for path in (root / 'operations').glob('op-*.json')):
                raise ValueError('TASK_MUTATION_OUTCOME_UNKNOWN')
            latest_receipts = {item['operation_id']: item for item in state['index']['host_receipts']}
            publishing = {'compile-map', 'compile-items', 'compile-images', 'review-open', 'review-apply'}
            if any(op['state'] == 'COMMITTED' and op['operation'] in publishing and op['operation_id'] not in latest_receipts for op in state['index']['committed']):
                raise ValueError('TASK_MUTATION_OUTCOME_UNKNOWN')
            known_sync = {'WORKBENCH_SYNCHRONIZED', 'WAITING_FOR_MAP_CONFIRMATION', 'CURRENT_REVIEW_SYNCHRONIZED',
                          'SYNCHRONIZED_LOCAL_ADVANCED', 'SKIPPED_SUPERSEDED', 'NOT_BOUND'}
            for item in latest_receipts.values():
                host = read(pinned_file(root, item['path']))['receipt']
                if 'status' in host:
                    established = host['status'] in known_sync
                else:
                    established = (isinstance(host.get('sync'), dict) and host['sync'].get('status') in known_sync
                                   and isinstance(host.get('verification'), dict)
                                   and host['verification'].get('status') in {'REVIEW_VERIFIED', 'HISTORICAL_SUCCESS_NOT_CURRENT'})
                if not established:
                    raise ValueError('TASK_MUTATION_OUTCOME_UNKNOWN')
            path = Path(state['binding_file'])
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('TASK_RECONNECT_WORKSPACE_MISMATCH')
            binding = read(path)
            _validate_binding(binding, value['source_sha256'], (root, context))
            if binding.get('work_id') != value['task_id'] or binding['session_id'] != value['work_ref'] or binding['base_url'] not in {value['from_origin'], value['to_origin'], *value['known_origins']}:
                raise ValueError('TASK_RECONNECT_BINDING_MISMATCH')
            documents = request(value['to_origin'] + '/api/projects/' + value['project_id'] + '/sor-documents')
            source = next((d for d in documents if d.get('id') == value['document_id']), None)
            if not source or source.get('source_sha256') != value['source_sha256']:
                raise ValueError('TASK_RECONNECT_SOURCE_CHANGED')
            from task_workbench import _api
            rebound = {**binding, 'base_url': value['to_origin']}
            remote = request(_api(rebound))
            _validate_state(remote, rebound)
            if remote.get('revision_id') != binding['revision_id']:
                raise ValueError('TASK_MUTATION_OUTCOME_UNKNOWN')
            # Preserve revision/feedback/command evidence. Only the transport origin changes.
            url = workbench_result_url(rebound, binding['revision_id'])
            rebound.update(url=url, workbench_url=url, workbench_path=urlsplit(url).path + '?' + urlsplit(url).query)
            save(path, rebound)
    return {'schema': 'reqpilot-pair-reconnect/v1', 'task_id': value['task_id'], 'work_ref': value['work_ref'],
            'source_sha256': value['source_sha256'], 'bundle_sha256': value['bundle_sha256'],
            'workspace': str(workspace), 'service_origin': value['to_origin'], 'mutation_replayed': False}


def reconnect(value):
    # Do not read a workspace's cached bytecode, even for verified source files.
    with TemporaryDirectory(prefix='reqpilot-reconnect-cache-') as cache:
        prior_prefix, prior_write = sys.pycache_prefix, sys.dont_write_bytecode
        sys.pycache_prefix, sys.dont_write_bytecode = cache, True
        try:
            return _reconnect(value)
        finally:
            sys.pycache_prefix, sys.dont_write_bytecode = prior_prefix, prior_write


if __name__ == '__main__':
    try:
        print(json.dumps(reconnect(read(sys.argv[1])), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'FAILED', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
