"""Physical portable task workspace around existing immutable compiler bundles.

Only a validated final operation receipt makes a bundle committed. The index is
an atomic navigation pointer; existing review manifests remain the working owner.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from reqpilotx.application.agent_workflow.artifact_kind_catalog import file_sha256, inside, read_json, validate_bundle
from reqpilotx.application.agent_workflow.native_task_brief import build_task_brief
from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

SCHEMA = 'reqpilot-task-workspace/v1'
INDEX_SCHEMA = 'reqpilot-workspace-index/v1'
RECEIPT_SCHEMA = 'reqpilot-operation-receipt/v1'
KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
SHA = re.compile(r'^[a-f0-9]{64}$')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seal(value, key):
    value = {k: v for k, v in value.items() if k != key}
    return {**value, key: canonical_json_sha256(value)}


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.workspace-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _create_receipt_once(path, value):
    # Link a flushed temporary inode into the final name without replacing an
    # already committed receipt when the same logical operation is retried.
    fd, name = tempfile.mkstemp(prefix='.operation-receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        try: os.link(name, path)
        except FileExistsError: pass
    finally:
        if os.path.exists(name): os.unlink(name)


def _atomic_text(path, text):
    fd, name = tempfile.mkstemp(prefix='.workspace-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locked(root):
    # Local filesystem coordination only, not a distributed-lock guarantee.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.task-workspace.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.write(b'0'); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _key(value, prefix):
    value = value or prefix + '-' + uuid4().hex
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise ValueError('Local project/document/work key must be a safe identifier')
    return value


def _read_context(root):
    value = read_json(root / 'task-context.json')
    if value.get('schema_version') != SCHEMA:
        raise ValueError('Unsupported task-context schema')
    if value.get('context_sha256') and _seal(value, 'context_sha256') != value:
        raise ValueError('Task context integrity changed')
    for field in ('work_id', 'project_key', 'document_key'):
        if not isinstance(value.get(field), str) or not KEY.fullmatch(value[field]):
            raise ValueError('Invalid task context identity: ' + field)
    source = value.get('source') or {}
    if not isinstance(source.get('path'), str) or not SHA.fullmatch(str(source.get('sha256', ''))):
        raise ValueError('Task original source binding is invalid')
    path = inside(root, source['path'])
    if not path.is_file() or file_sha256(path) != source['sha256']:
        raise ValueError('Task original source is missing or changed')
    request = value.get('request') or {}
    if not isinstance(request.get('goal'), str) or not request['goal'].strip() or request.get('origin') != 'USER_REQUEST' or type(request.get('require_map_confirmation')) is not bool:
        raise ValueError('Task request facts are invalid')
    binding = value.get('binding_file')
    if not isinstance(binding, str) or not binding:
        raise ValueError('Task binding file reference is invalid')
    if not Path(binding).is_absolute():
        inside(root, binding)
    return value


def find_task_workspace(start: Path | None = None, explicit: Path | None = None) -> tuple[Path, dict] | None:
    """Discover a marked ancestor; a broken marker never means legacy fallback."""
    path = Path(explicit if explicit is not None else start if start is not None else Path.cwd()).expanduser().resolve()
    if explicit is not None and not path.exists():
        raise ValueError('Explicit task workspace does not exist')
    if path.is_file():
        path = path.parent
    for root in (path, *path.parents):
        marker = root / 'task-context.json'
        if marker.exists():
            return root, _read_context(root)
    return None


def _resolve_task(task_root):
    found = find_task_workspace(explicit=Path(task_root))
    if found is None:
        raise ValueError('No task-context.json in task workspace')
    return found


def _empty_index(context):
    return {'schema_version': INDEX_SCHEMA, 'work_id': context['work_id'], 'source_sha256': context['source']['sha256'],
            'generation': 0, 'committed': [], 'selected': {}, 'candidates': [], 'host_receipts': []}


def _index(root, context):
    value = read_json(root / 'workspace-index.json')
    if value.get('schema_version') != INDEX_SCHEMA or value.get('work_id') != context['work_id'] or value.get('source_sha256') != context['source']['sha256'] or _seal(value, 'index_sha256') != value:
        raise ValueError('Workspace index integrity changed')
    return value


def _write_index(root, value):
    _atomic_json(root / 'workspace-index.json', _seal(value, 'index_sha256'))


def _revision(context, index, review):
    """Opaque optimistic-read token; the index/review remain the actual owners."""
    return 'workspace-revision-' + canonical_json_sha256({
        'context': context, 'source_sha256': context['source']['sha256'],
        'generation': index['generation'], 'selected': index['selected'],
        'owner': 'portable_review' if review else 'task_workspace',
        'review_sha256': review['review_sha256'] if review else None,
    })


def _request_value(root, value):
    # Typed business strings are data, not guessed filesystem references.
    if isinstance(value, Path): return _pin_input(root, value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value): raise ValueError('Request keys must be strings')
        return {key: _request_value(root, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_request_value(root, item) for item in value]
    if value is None or type(value) in (str, int, float, bool): return value
    raise ValueError('Request must contain JSON values or explicit Path inputs')


def _request_digest(root, context, operation, request):
    value = _request_value(root, request)
    json.dumps(value, allow_nan=False)
    return canonical_json_sha256({'work_id': context['work_id'], 'source_sha256': context['source']['sha256'],
                                 'operation': operation, 'request': value})


def _command_operation_id(context, command_id):
    if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
        raise ValueError('command_id must be a nonempty string of at most 200 characters')
    return 'op-' + canonical_json_sha256([context['work_id'], command_id])[:32]


def _restore_inputs(root, value):
    if isinstance(value, dict) and value.get('kind') in {'file', 'directory'} and 'path' in value and 'files' in value:
        return str(_resolve_ref(root, value['path']))
    if isinstance(value, dict): return {key: _restore_inputs(root, item) for key, item in value.items()}
    if isinstance(value, list): return [_restore_inputs(root, item) for item in value]
    return value


def _lookup_command(root, context, index, command_id, operation, digest):
    identifier = _command_operation_id(context, command_id)
    if not (root / 'operations' / (identifier + '.json')).is_file(): return None
    value = _operation(root, identifier)
    if value.get('command_id') != command_id or value.get('request_sha256') != digest or value.get('operation') != operation:
        raise ValueError('COMMAND_ID_CONFLICT: command_id already belongs to a different request')
    entry = next((item for item in index['committed'] if item['operation_id'] == identifier), None)
    result = {**value, 'replayed': True, 'task_root': str(root),
              'output_dir': str(inside(root, value['output_dir'])), 'resolved_inputs': _restore_inputs(root, value['inputs'])}
    if entry:
        receipt = _receipt(root, context, value)
        if receipt['receipt_sha256'] != entry['receipt_sha256']: raise ValueError('Indexed operation receipt changed')
        result.update(state=entry['state'], receipt_sha256=receipt['receipt_sha256'],
                      receipt_path=str(inside(root, entry['receipt_path'])), result=receipt['result'])
    elif (inside(root, value['output_dir']) / 'operation-receipt.json').is_file():
        receipt = _receipt(root, context, value)
        result.update(state='SAVED_PENDING_RECOVERY', receipt_sha256=receipt['receipt_sha256'],
                      receipt_path=str(inside(root, value['output_dir']) / 'operation-receipt.json'), result=receipt['result'])
    return result


def lookup_operation_command(task_root, command_id, operation, request):
    """Read an exact prior request before considering a now-stale version token."""
    root, _ = _resolve_task(task_root)
    with _locked(root):
        context = _read_context(root); index = _index(root, context)
        return _lookup_command(root, context, index, command_id, operation,
                               _request_digest(root, context, operation, request))


def _relative_or_absolute(root, path):
    path = Path(path).expanduser().resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def _resolve_ref(root, value):
    return Path(value).resolve() if Path(value).is_absolute() else inside(root, value)


def initialize_workspace(working_root, source, goal, project_key=None, document_key=None,
                         project_id=None, document_id=None, require_map_confirmation=False, runtime_ref=None, creation_key=None):
    arguments = dict(project_key=project_key, document_key=document_key, project_id=project_id, document_id=document_id,
                     require_map_confirmation=require_map_confirmation, runtime_ref=runtime_ref)
    if creation_key is None:
        return _initialize_workspace(working_root, source, goal, **arguments)
    if not isinstance(creation_key, str) or not creation_key.strip() or len(creation_key) > 200:
        raise ValueError('creation_key must be a nonempty string of at most 200 characters')
    working = Path(working_root).expanduser().resolve()
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file() or not isinstance(goal, str) or not goal.strip():
        raise ValueError('Task initialization needs an actual original and exact user request')
    if type(require_map_confirmation) is not bool: raise ValueError('require_map_confirmation must be boolean')
    if runtime_ref is not None and (not isinstance(runtime_ref, dict) or runtime_ref.get('kind') not in {'BUNDLED', 'DEVELOPMENT_CHECKOUT'}):
        raise ValueError('Runtime reference must identify the actual bundle or development checkout')
    request = {'source_sha256': file_sha256(source), 'source_name': source.name, 'goal': goal, **arguments}
    request_digest = canonical_json_sha256(_request_value(working, request))
    control = working / 'runtime-data'
    with _locked(control):
        entry_path = control / 'workspace-creations' / (canonical_json_sha256(creation_key) + '.json')
        if entry_path.is_file():
            entry = read_json(entry_path)
            if entry.get('schema_version') != 'reqpilot-workspace-creation/v1' or _seal(entry, 'creation_sha256') != entry:
                raise ValueError('Creation reference integrity changed')
            if entry.get('creation_key') != creation_key or entry.get('request_sha256') != request_digest:
                raise ValueError('CREATION_KEY_CONFLICT: creation_key already belongs to a different request')
            root = inside(working, entry['task_path'])
            if (root / 'task-context.json').is_file() and (root / 'workspace-index.json').is_file():
                context = _read_context(root)
                if context['work_id'] != entry['work_id'] or context['source']['sha256'] != request['source_sha256']:
                    raise ValueError('Creation reference binds another task or original')
                document = read_json(root.parent.parent / 'document.json')
                project = read_json(root.parent.parent.parent.parent / 'project-index.json')
                if (not any(item['work_id'] == entry['work_id'] for item in document['tasks'])
                        or not any(item['document_key'] == context['document_key'] for item in project['documents'])):
                    raise ValueError('CREATION_INCOMPLETE: reserved task has no complete project/document registration: ' + str(root))
                entry['state'] = 'READY'; _atomic_json(entry_path, _seal(entry, 'creation_sha256'))
                return {**workspace_status(root), 'creation_replayed': True, 'replayed': True, 'creation_key': creation_key,
                        'context_path': str(root / 'task-context.json'), 'index_path': str(root / 'workspace-index.json'), 'task_path': str(root / 'TASK.md')}
            if root.exists():
                raise ValueError('CREATION_INCOMPLETE: resume the reserved task instead of creating another: ' + str(root))
        else:
            selected_project = _key(project_key, 'project'); selected_document = _key(document_key, 'document')
            work_id = 'task-' + uuid4().hex
            root = control / 'projects' / selected_project / 'documents' / selected_document / 'tasks' / work_id
            entry = {'schema_version': 'reqpilot-workspace-creation/v1', 'creation_key': creation_key, 'request_sha256': request_digest,
                     'work_id': work_id, 'project_key': selected_project, 'document_key': selected_document,
                     'task_path': str(root.relative_to(working)), 'state': 'RESERVED'}
            _atomic_json(entry_path, _seal(entry, 'creation_sha256'))
        arguments.update(project_key=entry['project_key'], document_key=entry['document_key'])
        result = _initialize_workspace(working, source, goal, **arguments, _work_id=entry['work_id'])
        entry['state'] = 'READY'; _atomic_json(entry_path, _seal(entry, 'creation_sha256'))
        return {**result, 'creation_replayed': False, 'replayed': False, 'creation_key': creation_key}


def _initialize_workspace(working_root, source, goal, project_key=None, document_key=None,
                          project_id=None, document_id=None, require_map_confirmation=False, runtime_ref=None, _work_id=None):
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file() or not isinstance(goal, str) or not goal.strip():
        raise ValueError('Task initialization needs an actual original and exact user request')
    if type(require_map_confirmation) is not bool:
        raise ValueError('require_map_confirmation must be boolean')
    if runtime_ref is not None and (not isinstance(runtime_ref, dict) or runtime_ref.get('kind') not in {'BUNDLED', 'DEVELOPMENT_CHECKOUT'}):
        raise ValueError('Runtime reference must identify the actual bundle or development checkout')
    project_key, document_key = _key(project_key, 'project'), _key(document_key, 'document')
    project_root = Path(working_root).expanduser().resolve() / 'runtime-data/projects' / project_key
    document_root = project_root / 'documents' / document_key
    source_sha = file_sha256(source)
    with _locked(project_root):
        document_path = document_root / 'document.json'
        if document_path.is_file():
            document = read_json(document_path)
            if document.get('source_sha256') != source_sha:
                raise ValueError('Document key already binds another original; create a new document')
            for key, given in [('project_id', project_id), ('document_id', document_id)]:
                if document.get(key) and given and document[key] != given:
                    raise ValueError('Document key already binds another application identity')
                if given is None:
                    if key == 'project_id': project_id = document.get(key)
                    else: document_id = document.get(key)
        else:
            document = {'schema_version': 'reqpilot-local-document/v1', 'project_key': project_key, 'document_key': document_key,
                        'project_id': project_id, 'document_id': document_id, 'display_name': source.name, 'source_sha256': source_sha, 'tasks': []}
        work_id = _work_id or 'task-' + uuid4().hex
        root = document_root / 'tasks' / work_id
        root.mkdir(parents=True, mode=0o700)
        for name in ('source', 'reading', 'outputs', 'operations', 'notes', 'runtime-data/workbench-bindings'):
            (root / name).mkdir(parents=True, mode=0o700)
        extension = source.suffix.lower()
        source_relative = 'source/original' + extension
        shutil.copyfile(source, root / source_relative)
        if file_sha256(root / source_relative) != source_sha:
            raise ValueError('Original changed during task initialization')
        (root / source_relative).chmod(0o400)
        context = _seal({'schema_version': SCHEMA, 'work_id': work_id, 'project_key': project_key, 'document_key': document_key,
            'project_id': project_id, 'document_id': document_id, 'source': {'path': source_relative, 'sha256': source_sha, 'display_name': source.name},
            'request': {'goal': goal, 'origin': 'USER_REQUEST', 'require_map_confirmation': require_map_confirmation},
            'binding_file': f'runtime-data/workbench-bindings/{source_sha}.json', 'binding_portability': 'TASK_RELATIVE', 'runtime_ref': runtime_ref, 'created_at': _now()}, 'context_sha256')
        _atomic_json(root / 'task-context.json', context)
        _write_index(root, _empty_index(context))
        document['tasks'] = [*document.get('tasks', []), {'work_id': work_id, 'path': f'tasks/{work_id}', 'goal': goal}]
        _atomic_json(document_path, document)
        project_index_path = project_root / 'project-index.json'
        project_index = read_json(project_index_path) if project_index_path.is_file() else {'schema_version': 'reqpilot-local-project-index/v1', 'project_key': project_key, 'documents': []}
        project_index['documents'] = [d for d in project_index['documents'] if d['document_key'] != document_key] + [{'document_key': document_key, 'path': f'documents/{document_key}/document.json', 'source_sha256': source_sha, 'display_name': source.name}]
        _atomic_json(project_index_path, project_index)
    status = workspace_status(root)
    return {**status, 'context_path': str(root / 'task-context.json'), 'index_path': str(root / 'workspace-index.json'), 'task_path': str(root / 'TASK.md')}


def bind_task_workspace(task_root, binding_path, source_sha256, project_id=None, document_id=None):
    root, _ = _resolve_task(task_root)
    binding_path = Path(binding_path).expanduser().resolve()
    with _locked(root):
        context = _read_context(root)
        if source_sha256 != context['source']['sha256']:
            raise ValueError('Workbench binding source differs from task')
        for field, supplied in [('project_id', project_id), ('document_id', document_id)]:
            if context.get(field) and supplied and context[field] != supplied:
                raise ValueError('Workbench binding cannot replace task application identity')
        old_path = _resolve_ref(root, context['binding_file'])
        if old_path.is_file() and binding_path.is_file() and old_path != binding_path:
            old, new = read_json(old_path), read_json(binding_path)
            for field in ('session_id', 'project_id', 'document_id', 'source_sha256'):
                if old.get(field) != new.get(field):
                    raise ValueError('Workbench binding cannot replace an existing task session')
        context['binding_file'] = _relative_or_absolute(root, binding_path)
        context['binding_portability'] = 'TASK_RELATIVE' if binding_path.is_relative_to(root) else 'EXTERNAL_PATH'
        if project_id: context['project_id'] = project_id
        if document_id: context['document_id'] = document_id
        _atomic_json(root / 'task-context.json', _seal(context, 'context_sha256'))
    return workspace_status(root)


def _files(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Operation bundles cannot contain symlinks')
        if path.is_file() and path != directory / 'operation-receipt.json' and not path.name.startswith('.operation-receipt-'):
            result[str(path.relative_to(directory))] = file_sha256(path)
    return result


def _path_exists(value):
    try: return isinstance(value, str) and Path(value).expanduser().exists()
    except (OSError, ValueError): return False


def _pin_input(root, value):
    if isinstance(value, Path) or _path_exists(value):
        path = Path(value).expanduser().resolve(strict=True)
        return {'path': _relative_or_absolute(root, path), 'kind': 'directory' if path.is_dir() else 'file',
                'files': _files(path) if path.is_dir() else {path.name: file_sha256(path)}, 'availability': 'TASK_RELATIVE' if path.is_relative_to(root) else 'EXTERNAL_PATH'}
    if isinstance(value, dict): return {k: _pin_input(root, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_pin_input(root, v) for v in value]
    return value


def _verify_inputs(root, values):
    if isinstance(values, dict) and 'path' in values and values.get('kind') in {'file', 'directory'} and 'files' in values:
        path = _resolve_ref(root, values['path'])
        actual = _files(path) if values['kind'] == 'directory' and path.is_dir() else {path.name: file_sha256(path)} if path.is_file() else None
        if actual != values['files']:
            raise ValueError('Operation input dependency is missing or changed: ' + values['path'])
    elif isinstance(values, dict):
        for value in values.values(): _verify_inputs(root, value)
    elif isinstance(values, list):
        for value in values: _verify_inputs(root, value)


def _dependency_dirs(root, inputs):
    return [_resolve_ref(root, value['path']) for value in inputs.values() if isinstance(value, dict) and value.get('kind') == 'directory']


def _review(root, context):
    review_root = root / 'review'
    path = review_root / 'workspace-manifest.json'
    if not path.exists(): return None
    head = read_json(path)
    if head.get('schema_version') != 'reqpilot-review-workspace/v1' or _seal(head, 'review_sha256') != head or head.get('source', {}).get('sha256') != context['source']['sha256']:
        raise ValueError('Existing review head is invalid or binds another source')
    if file_sha256(inside(review_root, head['source']['path'])) != context['source']['sha256']:
        raise ValueError('Existing review original changed')
    current = head['current']; directory = inside(review_root, current['version_path'])
    for name, digest in current['files'].items():
        if file_sha256(inside(directory, name)) != digest:
            raise ValueError('Existing review artifact changed')
    kinds = ['SOURCE_ITEMS', *[kind for kind, name in [('ANNOTATIONS', 'image-annotations.json'), ('DECOMPOSITION', 'source-item-decomposition.json')] if (directory / name).is_file()]]
    validated = validate_bundle(directory, context['source']['sha256'], operation='review-revise', artifact_kinds=kinds)
    if validated['stages']['ITEMS']['artifact_sha256'] != current['baseline_sha256'] or validated['stages']['MAP']['artifact_sha256'] != current['document_map_sha256']:
        raise ValueError('Existing review current pointer differs from actual bundle')
    return {'owner': 'portable_review', 'review_root': str(review_root), 'review_sha256': head['review_sha256'], **current, 'stages': validated['stages']}


def begin_operation(task_root, operation, inputs=None, *, expected_revision=None, command_id=None, request=None):
    root, _ = _resolve_task(task_root)
    if not KEY.fullmatch(operation): raise ValueError('Invalid operation name')
    with _locked(root):
        context = _read_context(root); index = _index(root, context); review = _review(root, context)
        digest = None
        if command_id is not None:
            logical_request = request if request is not None else {'inputs': inputs or {}, 'expected_revision': expected_revision}
            digest = _request_digest(root, context, operation, logical_request)
            prior = _lookup_command(root, context, index, command_id, operation, digest)
            if prior is not None: return prior
        revision = _revision(context, index, review)
        if expected_revision is not None and expected_revision != revision:
            raise ValueError('REVISION_CONFLICT: selected task state changed before the operation began')
        operation_id = _command_operation_id(context, command_id) if command_id is not None else 'op-' + uuid4().hex
        area = 'reading' if operation in {'capture', 'reading-workspace', 'read-pdf-visual', 'revise-visual-text'} else 'outputs'
        output_dir = root / 'review' if operation == 'review-open' else root / area / operation_id
        if operation == 'review-open' and output_dir.exists(): raise ValueError('Review already exists; use its current head')
        resolved_inputs = dict(inputs or {})
        if operation in {'compile-map', 'compile-items', 'compile-images', 'compile-annotations', 'compile-decomposition', 'review-apply', 'read-pdf-visual', 'revise-visual-text'} and resolved_inputs.get('input'):
            original_input = Path(resolved_inputs['input']).expanduser().resolve(strict=True)
            if not original_input.is_file(): raise ValueError('Semantic decision input must be a file')
            frozen = root / 'inputs' / operation_id / original_input.name
            frozen.parent.mkdir(parents=True, mode=0o700)
            shutil.copyfile(original_input, frozen)
            if file_sha256(frozen) != file_sha256(original_input): raise ValueError('Decision input changed during copy')
            frozen.chmod(0o400)
            resolved_inputs['input'] = frozen
        value = {'schema_version': 'reqpilot-workspace-operation/v1', 'work_id': context['work_id'], 'operation_id': operation_id,
            'operation': operation, 'source_sha256': context['source']['sha256'], 'output_dir': str(output_dir.relative_to(root)),
            'expected_generation': index['generation'], 'expected_review_sha256': review['review_sha256'] if review else None,
            'inputs': _pin_input(root, resolved_inputs), 'state': 'STARTED', 'started_at': _now()}
        if command_id is not None:
            value.update(command_id=command_id, request_sha256=digest)
        if expected_revision is not None: value['expected_revision'] = expected_revision
        _atomic_json(root / 'operations' / (operation_id + '.json'), value)
    return {**value, 'replayed': False, 'output_dir': str(output_dir), 'task_root': str(root), 'resolved_inputs': {k: str(v) if isinstance(v, Path) else v for k, v in resolved_inputs.items()}}


def _operation(root, operation_id):
    if not re.fullmatch(r'op-[a-f0-9]{32}', operation_id): raise ValueError('Invalid operation ID')
    value = read_json(root / 'operations' / (operation_id + '.json'))
    if value.get('operation_id') != operation_id: raise ValueError('Operation identity changed')
    return value


def _receipt_files(directory, operation):
    if operation != 'review-open': return _files(directory)
    head = read_json(directory / 'workspace-manifest.json')
    current = inside(directory, head['current']['version_path'])
    files = {str(path.relative_to(directory)): file_sha256(path) for path in [inside(directory, head['source']['path']), inside(directory, 'reviews/' + head['review_sha256'] + '.json')]}
    files.update({str((current / name).relative_to(directory)): digest for name, digest in _files(current).items()})
    return files


def _review_finding_payload(operation, finding):
    return {'expected_review_sha256': operation['expected_review_sha256'], 'feedback': [{
        'origin': 'AGENT_SELF_REVIEW', 'text': finding['text'], 'source_item_ids': finding['source_item_ids'],
        'rationale': 'Task command ' + operation['operation_id'] + '; ' + finding.get('rationale', '')}]}


def _validate_review_finding(root, operation, finding_sha):
    """Permit exactly this command's one explicit metadata-only finding step."""
    frozen = read_json(Path(_restore_inputs(root, operation['inputs'])['input']))
    finding = frozen.get('self_review_finding')
    if frozen.get('origin') != 'AGENT_SELF_REVIEW' or not finding:
        raise ValueError('Review command did not request an Agent self-review finding')
    review_root = root / 'review'
    base = read_json(inside(review_root, 'reviews/' + operation['expected_review_sha256'] + '.json'))
    intermediate = read_json(inside(review_root, 'reviews/' + finding_sha + '.json'))
    if _seal(base, 'review_sha256') != base or _seal(intermediate, 'review_sha256') != intermediate:
        raise ValueError('Review finding history integrity changed')
    entries = intermediate.get('feedback', [])
    if len(entries) != len(base['feedback']) + 1 or entries[:-1] != base['feedback']:
        raise ValueError('Review finding step changed unrelated feedback')
    expected_item = {**_review_finding_payload(operation, finding)['feedback'][0],
        'bound_baseline_sha256': base['current']['baseline_sha256'], 'status': 'OPEN',
        'created_review_revision': base['review_revision'] + 1}
    expected_item['feedback_id'] = 'feedback-' + canonical_json_sha256(expected_item)[:32]
    expected = deepcopy(base)
    expected.update(feedback=[*base['feedback'], expected_item], review_revision=base['review_revision'] + 1,
                    predecessor_review_sha256=base['review_sha256'])
    if intermediate != _seal(expected, 'review_sha256') or intermediate['review_sha256'] != finding_sha:
        raise ValueError('Review finding is not the exact metadata step requested by this command')
    return intermediate, expected_item['feedback_id']


def _review_application_validation(root, context, operation):
    """Record a completed existing review CAS; never elect a second head."""
    directory = inside(root, operation['output_dir'])
    application = read_json(directory / 'review-application.json')
    if (application.get('schema_version') != 'reqpilot-review-application/v1'
            or application.get('operation_id') != operation['operation_id']
            or application.get('source_sha256') != context['source']['sha256']
            or application.get('old_review_sha256') != operation['expected_review_sha256']):
        raise ValueError('Review application differs from its task operation')
    review = _review(root, context)
    if not review: raise ValueError('Review application has no existing review owner')
    review_root = root / 'review'
    cursor = review['review_sha256']; seen = set(); closed = None
    while cursor and cursor not in seen:
        seen.add(cursor)
        head = read_json(inside(review_root, 'reviews/' + cursor + '.json'))
        if _seal(head, 'review_sha256') != head or head['review_sha256'] != cursor or head['source']['sha256'] != context['source']['sha256']:
            raise ValueError('Review application history changed')
        if cursor == application.get('new_review_sha256'):
            closed = head
            break
        cursor = head.get('predecessor_review_sha256')
    frozen = read_json(Path(_restore_inputs(root, operation['inputs'])['input']))
    predecessor = operation['expected_review_sha256']
    if frozen.get('self_review_finding'):
        intermediate, feedback_id = _validate_review_finding(root, operation, application.get('finding_review_sha256', ''))
        predecessor = intermediate['review_sha256']
        if application.get('finding_feedback_id') != feedback_id or feedback_id not in application.get('addressed_feedback_ids', []):
            raise ValueError('Review application omitted its recorded self-review finding')
    elif application.get('finding_review_sha256') or application.get('finding_feedback_id'):
        raise ValueError('Review application introduced an unrequested intermediate head')
    if not closed or closed.get('predecessor_review_sha256') != predecessor:
        raise ValueError('Review application was not adopted by the actual review head')
    if (closed['current']['version_path'] != application['candidate_path']
            or (closed.get('delivery') or {}).get('path') != application['delivery_path']):
        raise ValueError('Review application candidate or delivery differs from its closed review')
    candidate = inside(review_root, application['candidate_path'])
    for name, digest in closed['current']['files'].items():
        if file_sha256(inside(candidate, name)) != digest: raise ValueError('Closed review artifact changed')
    candidate_receipt = read_json(candidate / 'revision-receipt.json')
    if candidate_receipt.get('review_sha256') != predecessor:
        raise ValueError('Closed candidate belongs to another predecessor')
    for name, digest in candidate_receipt['files'].items():
        if file_sha256(inside(candidate, name)) != digest: raise ValueError('Closed candidate receipt changed')
    delivery = inside(review_root, application['delivery_path'])
    checked = validate_bundle(delivery, context['source']['sha256'], operation='deliver', dependency_dirs=[candidate])
    if (checked['stages']['ITEMS']['artifact_sha256'] != closed['current']['baseline_sha256']
            or checked['stages']['MAP']['artifact_sha256'] != closed['current']['document_map_sha256']
            or file_sha256(delivery / 'matrix.xlsx') != closed['delivery']['xlsx_sha256']):
        raise ValueError('Closed review delivery differs from its registered version')
    from reqpilotx.application.agent_workflow import source_requirement_baseline as source_core
    from reqpilotx.application.agent_workflow.source_item_delivery import build_delivery_snapshot
    optional = {key: read_json(candidate / name) for key, name in (
        ('annotations', 'image-annotations.json'), ('decomposition', 'source-item-decomposition.json'),
        ('assessments', 'source-item-assessments.json'), ('manual_decisions', 'manual-decisions.json'),
        ('analysis_policy', 'analysis-policy.json')) if (candidate / name).is_file()}
    expected = build_delivery_snapshot(source_core.DocumentMapArtifactV1.model_validate(read_json(candidate / 'document-map.json')),
        source_core.SourceRequirementBaselineArtifactV1.model_validate(read_json(candidate / 'source-requirement-baseline.json')),
        read_json(candidate / 'table-previews.json'), **optional)
    if read_json(delivery / 'matrix-snapshot.json') != expected: raise ValueError('Closed delivery omits current review state')
    return {'artifacts': [{'kind': 'REVIEW_APPLICATION', 'path': str(directory / 'review-application.json'),
                          'sha256': file_sha256(directory / 'review-application.json')}],
            'stages': {'DELIVERY': checked['stages']['DELIVERY']},
            'closed_review_sha256': closed['review_sha256']}


def _receipt(root, context, operation):
    directory = inside(root, operation['output_dir'])
    value = read_json(directory / 'operation-receipt.json')
    if value.get('schema_version') != RECEIPT_SCHEMA or _seal(value, 'receipt_sha256') != value or value.get('work_id') != context['work_id'] or value.get('operation_id') != operation['operation_id'] or value.get('source_sha256') != context['source']['sha256']:
        raise ValueError('Operation receipt integrity changed')
    for key in ('operation', 'output_dir', 'expected_generation', 'expected_review_sha256', 'inputs'):
        if value.get(key) != operation.get(key): raise ValueError('Operation receipt no longer matches planned operation')
    for key in ('command_id', 'request_sha256', 'expected_revision'):
        if value.get(key) != operation.get(key): raise ValueError('Operation receipt request identity changed')
    actual = {name: file_sha256(inside(directory, name)) for name in value['files']} if operation['operation'] == 'review-open' else _files(directory)
    if actual != value['files']: raise ValueError('Committed bundle is missing or changed')
    _verify_inputs(root, value['inputs'])
    if operation['operation'] == 'review-apply':
        _review_application_validation(root, context, operation)
    elif operation['operation'] != 'review-open':
        validate_bundle(directory, context['source']['sha256'], operation=value['operation'], artifact_kinds=value['artifact_kinds'], dependency_dirs=_dependency_dirs(root, value['inputs']))
    return value


def _project_validation(root, value):
    result = deepcopy(value)
    for artifact in result['artifacts']:
        artifact['path'] = _relative_or_absolute(root, artifact['path'])
    for stage in result['stages'].values():
        stage['path'] = _relative_or_absolute(root, stage['path'])
        if stage.get('manifest_path'): stage['manifest_path'] = _relative_or_absolute(root, stage['manifest_path'])
    return result


def _advance(root, context, index, receipt, force_conflict=False):
    operation_id = receipt['operation_id']
    prior = next((v for v in index['committed'] if v['operation_id'] == operation_id), None)
    if prior: return prior['state']
    if receipt['operation'] == 'review-apply':
        # The old->new transition already passed the review's own CAS. This
        # acknowledges historical execution, rather than promoting a candidate.
        operation = _operation(root, operation_id)
        checked = _review_application_validation(root, context, operation)
        index['committed'].append({'operation_id': operation_id, 'operation': receipt['operation'],
            'receipt_path': receipt['output_dir'] + '/operation-receipt.json', 'receipt_sha256': receipt['receipt_sha256'],
            'state': 'COMMITTED', 'artifacts': deepcopy(receipt['validation']['artifacts'])})
        if _review(root, context)['review_sha256'] == checked['closed_review_sha256']:
            delivery = receipt['validation']['stages']['DELIVERY']
            index['selected']['DELIVERY'] = {**delivery, 'operation_id': operation_id,
                'output_dir': str(Path(delivery['path']).parent)}
            index['generation'] += 1
        _write_index(root, index)
        return 'COMMITTED'
    review = _review(root, context)
    review_conflict = (review['review_sha256'] if review else None) != receipt['expected_review_sha256'] and receipt['operation'] != 'review-open'
    active = review['stages'] if review else index['selected']
    bound_items = receipt['validation']['stages'].get('ITEMS')
    mismatch = bool(bound_items and active.get('MAP') and bound_items['map_sha256'] != active['MAP']['artifact_sha256'])
    supplemental = receipt['operation'] in {'deliver', 'compile-annotations', 'compile-decomposition'}
    baseline_mismatch = bool(supplemental and bound_items and active.get('ITEMS') and bound_items['artifact_sha256'] != active['ITEMS']['artifact_sha256'])
    # Derived page evidence for the same verified original can be registered
    # during review. It does not author or replace any review business stage.
    state = 'CANDIDATE_CONFLICT' if force_conflict or index['generation'] != receipt['expected_generation'] or review_conflict else 'SAVED_CANDIDATE' if mismatch or baseline_mismatch or (review and receipt['operation'] not in {'review-open', 'deliver', 'reading-workspace', 'read-pdf-visual', 'revise-visual-text'}) else 'COMMITTED'
    entry = {'operation_id': operation_id, 'operation': receipt['operation'], 'receipt_path': receipt['output_dir'] + '/operation-receipt.json', 'receipt_sha256': receipt['receipt_sha256'], 'state': state, 'artifacts': deepcopy(receipt['validation']['artifacts'])}
    index['committed'].append(entry)
    if state != 'COMMITTED':
        index['candidates'].append({**entry, 'reason': 'INDEX_OR_REVIEW_HEAD_CHANGED' if state == 'CANDIDATE_CONFLICT' else 'ITEMS_MAP_IS_NOT_CURRENT' if mismatch else 'BASELINE_IS_NOT_CURRENT' if baseline_mismatch else 'REVIEW_CANDIDATE_NOT_CLOSED'})
    else:
        for stage, value in receipt['validation']['stages'].items():
            if stage in {'MAP', 'ITEMS'} and supplemental:
                continue  # Validation dependencies are not newly authored stages.
            # An ITEMS bundle includes its Map for replay, not a new Map decision.
            if stage == 'MAP' and 'MAP' in index['selected'] and receipt['operation'] != 'compile-map' and value['artifact_sha256'] != index['selected']['MAP']['artifact_sha256']:
                index['candidates'].append({**entry, 'reason': 'ITEMS_MAP_IS_NOT_CURRENT'})
                continue
            if stage == 'ITEMS' and index['selected'].get('MAP', {}).get('artifact_sha256') not in {None, value['map_sha256']}:
                continue
            if stage == 'MAP' and 'MAP' in index['selected'] and receipt['operation'] != 'compile-map':
                continue
            index['selected'][stage] = {**value, 'operation_id': operation_id, 'output_dir': str(Path(value['path']).parent)}
        if 'MAP' in index['selected'] and 'ITEMS' in index['selected'] and index['selected']['MAP']['artifact_sha256'] != index['selected']['ITEMS']['map_sha256']:
            index['selected'].pop('ITEMS')
        if index['selected'].get('SOURCE_FINDINGS') and index['selected']['SOURCE_FINDINGS']['map_sha256'] != index['selected'].get('MAP', {}).get('artifact_sha256'):
            index['selected'].pop('SOURCE_FINDINGS')
        for kind in ('ANNOTATIONS', 'DECOMPOSITION', 'DELIVERY'):
            value = index['selected'].get(kind)
            if value and value.get('baseline_sha256') != (review['stages'] if review else index['selected']).get('ITEMS', {}).get('artifact_sha256'):
                index['selected'].pop(kind)

        index['generation'] += 1
    _write_index(root, index)
    return state


def commit_operation(task_root, operation_id, *, artifact_kinds=None, result=None):
    root, context = _resolve_task(task_root)
    operation = _operation(root, operation_id)
    directory = inside(root, operation['output_dir'])
    if (directory / 'operation-receipt.json').is_file():
        receipt = _receipt(root, context, operation)
    else:
        _verify_inputs(root, operation['inputs'])
        validation = (_review_application_validation(root, context, operation) if operation['operation'] == 'review-apply' else
            validate_bundle(directory, context['source']['sha256'], operation=operation['operation'], artifact_kinds=artifact_kinds, dependency_dirs=_dependency_dirs(root, operation['inputs'])))
        receipt = _seal({'schema_version': RECEIPT_SCHEMA, **{k: operation[k] for k in ('work_id', 'operation_id', 'operation', 'source_sha256', 'output_dir', 'expected_generation', 'expected_review_sha256', 'inputs')},
            **{k: operation[k] for k in ('command_id', 'request_sha256', 'expected_revision') if k in operation},
            'artifact_kinds': [a['kind'] for a in validation['artifacts']], 'files': _receipt_files(directory, operation['operation']), 'validation': _project_validation(root, validation),
            'result': result or {}, 'saved_at': _now()}, 'receipt_sha256')
        _create_receipt_once(directory / 'operation-receipt.json', receipt)
        receipt = _receipt(root, context, operation)
    with _locked(root):
        context = _read_context(root); index = _index(root, context)
        state = _advance(root, context, index, receipt)
        operation.update(state=state, receipt_sha256=receipt['receipt_sha256'])
        _atomic_json(root / 'operations' / (operation_id + '.json'), operation)
    status = workspace_status(root)
    return {'state': state, 'operation_id': operation_id, 'receipt_path': str(directory / 'operation-receipt.json'), 'receipt_sha256': receipt['receipt_sha256'], **status}


def fail_operation(task_root, operation_id, error, state='FAILED'):
    if state not in {'FAILED', 'BLOCKED'}: raise ValueError('Operation failure state must be FAILED or BLOCKED')
    root, context = _resolve_task(task_root)
    with _locked(root):
        operation = _operation(root, operation_id)
        receipt = inside(root, operation['output_dir']) / 'operation-receipt.json'
        operation.update(state='SAVED_PENDING_RECOVERY' if receipt.is_file() else state, error=str(error))
        _atomic_json(root / 'operations' / (operation_id + '.json'), operation)
    return {'operation_id': operation_id, 'state': operation['state'], 'task_root': str(root)}


def _validate_host_receipt(root, context, index, value):
    if value.get('schema_version') != 'reqpilot-host-receipt/v1' or _seal(value, 'host_receipt_sha256') != value:
        raise ValueError('Host receipt integrity changed')
    if value.get('work_id') != context['work_id'] or value.get('source_sha256') != context['source']['sha256']:
        raise ValueError('Host receipt binds another task or original')
    operation = next((v for v in index['committed'] if v['operation_id'] == value.get('operation_id')), None)
    if not operation or operation['state'] != 'COMMITTED':
        raise ValueError('Host receipt requires a committed selected operation')
    for key, expected in [('source_sha256', context['source']['sha256']), ('project_id', context.get('project_id')), ('document_id', context.get('document_id')), ('work_id', context['work_id'])]:
        if value['receipt'].get(key) is not None and expected is not None and value['receipt'][key] != expected:
            raise ValueError('Host receipt scope differs: ' + key)
    identity = {k: value[k] for k in ('work_id', 'operation_id', 'source_sha256', 'receipt')}
    if value.get('host_receipt_id') != 'host-' + canonical_json_sha256(identity):
        raise ValueError('Host receipt identity changed')
    return value


def record_host_receipt(task_root, operation_id, receipt):
    """Record the real adapter return separately, without resigning local output."""
    if not isinstance(receipt, dict): raise ValueError('Host receipt must be an actual structured adapter result')
    root, context = _resolve_task(task_root)
    _operation(root, operation_id)
    with _locked(root):
        index = _index(root, context)
        identity = {'work_id': context['work_id'], 'operation_id': operation_id, 'source_sha256': context['source']['sha256'], 'receipt': receipt}
        identifier = 'host-' + canonical_json_sha256(identity)
        path = root / 'host-receipts' / (operation_id + '-' + identifier + '.json')
        value = read_json(path) if path.exists() else _seal({'schema_version': 'reqpilot-host-receipt/v1', **identity, 'host_receipt_id': identifier, 'recorded_at': _now()}, 'host_receipt_sha256')
        _validate_host_receipt(root, context, index, value)
        if not path.exists():
            path.parent.mkdir(exist_ok=True, mode=0o700)
            _create_receipt_once(path, value)
        entry = {'host_receipt_id': identifier, 'operation_id': operation_id, 'path': str(path.relative_to(root)), 'host_receipt_sha256': value['host_receipt_sha256']}
        if not any(old['host_receipt_id'] == identifier for old in index['host_receipts']):
            index['host_receipts'].append(entry)
            _write_index(root, index)
    return {**entry, 'path': str(path), 'receipt': value['receipt']}


def recover_workspace(task_root):
    root, context = _resolve_task(task_root)
    recovered, incomplete = [], []
    with _locked(root):
        index = _index(root, context)
        known = {v['operation_id'] for v in index['committed']}
        pending = []
        for path in sorted((root / 'operations').glob('op-*.json')):
            operation = _operation(root, path.stem)
            if operation['operation_id'] in known:
                entry = next(v for v in index['committed'] if v['operation_id'] == operation['operation_id'])
                value = _receipt(root, context, operation)
                if value['receipt_sha256'] != entry['receipt_sha256']: raise ValueError('Indexed operation receipt changed')
                if operation.get('state') != entry['state']:
                    operation.update(state=entry['state'], receipt_sha256=value['receipt_sha256'], recovered_at=_now())
                    _atomic_json(path, operation)
                    recovered.append({'operation_id': operation['operation_id'], 'state': entry['state'], 'action': 'RECONCILED_OPERATION_DESCRIPTOR'})
                continue
            if (inside(root, operation['output_dir']) / 'operation-receipt.json').is_file():
                pending.append((operation, _receipt(root, context, operation)))
            else:
                incomplete.append({'operation_id': operation['operation_id'], 'state': operation['state'], 'output_dir': operation['output_dir']})
        for operation, receipt in sorted(pending, key=lambda pair: (pair[1]['expected_generation'], pair[1]['operation_id'])):
            state = _advance(root, context, index, receipt, force_conflict=sum(r['expected_generation'] == receipt['expected_generation'] for _, r in pending) > 1)
            operation.update(state=state, recovered_at=_now())
            _atomic_json(root / 'operations' / (operation['operation_id'] + '.json'), operation)
            recovered.append({'operation_id': operation['operation_id'], 'state': state})
        known_host = {v['host_receipt_id'] for v in index['host_receipts']}
        for path in sorted((root / 'host-receipts').glob('op-*-host-*.json')):
            value = _validate_host_receipt(root, context, index, read_json(path))
            if value['host_receipt_id'] not in known_host:
                index['host_receipts'].append({'host_receipt_id': value['host_receipt_id'], 'operation_id': value['operation_id'], 'path': str(path.relative_to(root)), 'host_receipt_sha256': value['host_receipt_sha256']})
                known_host.add(value['host_receipt_id'])
                _write_index(root, index)
    return {**workspace_status(root), 'recovered': recovered, 'incomplete_operations': incomplete}


def _map_compiler_navigation(root, context, index, selected):
    """Separate the review owner's readable Map from its complete compiler input."""
    mapping = selected.get('MAP')
    if not mapping: return
    owner_directory = Path(mapping['path']).parent
    choices = []
    preferred = index['selected'].get('MAP')
    candidates = ([preferred] if preferred and preferred.get('artifact_sha256') == mapping['artifact_sha256'] else [])
    for entry in index['committed']:
        if entry['operation'] != 'compile-map': continue
        receipt = read_json(inside(root, entry['receipt_path']))
        value = receipt['validation']['stages'].get('MAP')
        if value and value['artifact_sha256'] == mapping['artifact_sha256']:
            candidates.append(value)
    # Explicit imported Map inputs may already be complete even without a new
    # local compile operation. Validate their replay contract, not their name.
    candidates.append({'path': str(owner_directory / 'document-map.json')})
    revision_input = owner_directory / 'revision-input.json'
    if revision_input.is_file() and owner_directory.is_relative_to(root / 'review'):
        declared = (read_json(revision_input).get('map_revision') or {}).get('candidate_map_path')
        if declared:
            declared_root = inside(root / 'review', declared)
            candidates.append({'path': str(declared_root / 'document-map.json')})
    seen = set()
    for candidate in candidates:
        path = _resolve_ref(root, candidate['path'])
        directory = path.parent
        if str(directory) in seen: continue
        seen.add(str(directory))
        try:
            if not (directory / 'scope-requests.json').is_file(): continue
            checked = validate_bundle(directory, context['source']['sha256'], operation='existing-map', artifact_kinds=['DOCUMENT_MAP'])
            if checked['stages']['MAP']['artifact_sha256'] != mapping['artifact_sha256']: continue
        except (ValueError, OSError, KeyError):
            continue
        choices.append(str(directory))
    preferred_directory = str(_resolve_ref(root, preferred['path']).parent) if preferred else None
    chosen = preferred_directory if preferred_directory in choices else choices[0] if len(choices) == 1 else None
    mapping.update(read_bundle_dir=str(owner_directory), output_dir=chosen, compiler_output_dir=chosen,
        producer_path=str(Path(chosen) / 'document-map.json') if chosen else None,
        compiler_bundle_availability='AVAILABLE' if chosen else 'EXPLICIT_SELECTION_REQUIRED' if choices else 'UNAVAILABLE',
        compiler_candidates=choices, path_role='CURRENT_OWNER_MAP')


def workspace_status(task_root, refresh=True):
    root, context = _resolve_task(task_root)
    index = _index(root, context)
    for entry in index['committed']:
        receipt = _receipt(root, context, _operation(root, entry['operation_id']))
        if receipt['receipt_sha256'] != entry['receipt_sha256']:
            raise ValueError('Indexed operation receipt changed')
    for entry in index['host_receipts']:
        value = _validate_host_receipt(root, context, index, read_json(inside(root, entry['path'])))
        if value['host_receipt_sha256'] != entry['host_receipt_sha256']:
            raise ValueError('Indexed host receipt changed')
    selected = deepcopy(index['selected'])
    review = _review(root, context)
    for value in selected.values():
        value['path'] = str(_resolve_ref(root, value['path']))
        if value.get('manifest_path'): value['manifest_path'] = str(_resolve_ref(root, value['manifest_path']))
        value['output_dir'] = str(_resolve_ref(root, value['output_dir']))
    if review:
        # The same source baseline can acquire revised/removed overlays. The
        # real review bundle owns them; matching only baseline SHA is insufficient.
        for kind in ('ANNOTATIONS', 'DECOMPOSITION'): selected.pop(kind, None)
        for stage, value in review['stages'].items():
            selected[stage] = {**value, 'output_dir': str(Path(value['path']).parent), 'owner': 'portable_review'}
    _map_compiler_navigation(root, context, index, selected)
    for kind in ('ANNOTATIONS', 'DECOMPOSITION', 'DELIVERY'):
        if selected.get(kind) and selected[kind].get('baseline_sha256') != selected.get('ITEMS', {}).get('artifact_sha256'):
            selected.pop(kind)
    binding_path = _resolve_ref(root, context['binding_file'])
    binding = read_json(binding_path) if binding_path.is_file() else {}
    inbox_path = binding_path.with_name(binding_path.stem + '-inbox.json')
    inbox = read_json(inbox_path) if inbox_path.is_file() else {}
    valid_inbox = bool(binding and inbox) and all(inbox.get(key) == binding.get(key) for key in ('session_id', 'project_id', 'document_id', 'source_sha256'))
    confirmed = inbox.get('confirmed_map_sha256') if valid_inbox else None
    analysis_policy = None
    if selected.get('ITEMS'):
        # The validated current owner supplies policy, including an explicit
        # skip. Never infer it from the goal or reuse a previous review bundle.
        policy_path = Path(selected['ITEMS']['path']).parent / 'analysis-policy.json'
        if policy_path.is_file() and (not review or 'analysis-policy.json' in review['files']):
            analysis_policy = read_json(policy_path)
    brief = build_task_brief(goal=context['request']['goal'], origin=context['request']['origin'], source={**context['source'], 'path': str(inside(root, context['source']['path']))},
        stages=selected, review=review, require_map_confirmation=context['request']['require_map_confirmation'], confirmed_map_sha256=confirmed,
        project_id=context.get('project_id'), document_id=context.get('document_id'), analysis_policy=analysis_policy)
    status = {'schema_version': 'reqpilot-workspace-status/v1', 'task_root': str(root), 'revision': _revision(context, index, review), 'context': context, 'index': index, 'selected': selected, 'brief': brief,
        'current_working': review or {'owner': 'task_workspace', 'generation': index['generation'], 'selected': selected},
        'binding_file': str(binding_path), 'workbench_url': binding.get('url') or binding.get('workbench_url') or (binding.get('base_url', '') + binding['workbench_path'] if binding.get('workbench_path') else None),
        'candidates': deepcopy(index['candidates']), 'committed_operations': deepcopy(index['committed']),
        'running_state': 'UNREPORTED', 'human_acceptance': 'NOT_CLAIMED', 'source_verification': 'BYTES_MATCH',
        'incomplete_operations': [read_json(p) for p in sorted((root / 'operations').glob('op-*.json')) if read_json(p).get('state') in {'STARTED', 'FAILED', 'BLOCKED', 'SAVED_PENDING_RECOVERY'}]}
    if refresh:
        lines = ['# ReqPilotX 文档任务', '', '## 本轮委托', '', context['request']['goal'], '', '来源：USER_REQUEST', '',
                 '## 程序核对的当前状态', '', brief['work_state_label'], '', '原件：`' + context['source']['path'] + '`',
                 '原件 SHA256：`' + context['source']['sha256'] + '`', '结构确认：' + brief['map_confirmation'],
                 '内部依据策略：' + brief['analysis_policy']['status'], '人工接受：未声称；程序保存不代表语义正确。', '', '## 可接续工件', '']
        lines += ['- ' + key + '：`' + _relative_or_absolute(root, value['path']) + '`' for key, value in selected.items()] or ['尚无已提交工件；从原件开始。']
        if selected.get('MAP'):
            lines += ['', 'Map编制包：' + str(selected['MAP'].get('compiler_output_dir') or selected['MAP']['compiler_bundle_availability']) + '；可读Map快照与完整重编依赖分别列出。']
        items = selected.get('ITEMS') or {}
        if 'unprocessed_scope_ids' in items: lines += ['', '未处理范围：' + json.dumps(items['unprocessed_scope_ids'], ensure_ascii=False)]
        if context.get('runtime_ref'): lines += ['', '能力包引用：`' + json.dumps(context['runtime_ref'], ensure_ascii=False) + '`；接续不自动替换已冻结能力包。']
        if review: lines += ['', '当前工作版由 `review/workspace-manifest.json` 指定；未 close 的编制结果仍是候选。']
        if index['candidates']: lines += ['', f"另有 {len(index['candidates'])} 个候选/冲突引用，详见 workspace-index.json；未自动合并。"]
        if status['incomplete_operations']: lines += ['', '存在未完成操作；恢复只读取最后提交回执，不将半目录当成成果。']
        lines += ['', '## 接续入口', '', '先运行 `workspace status` 核对来源与精确版本；`workspace recover` 处理已保存回执未推进索引的中断。',
                  '工具按需选择，阅读/回查不受阶段目录限制。TASK.md 是事实导航，不增加用户授权或固定阅读步骤。',
                  '工件和展示角色见 workspace-index.json；前端绑定：`' + context['binding_file'] + '`。',
                  '工作台：' + str(status['workbench_url'] or brief['workbench_path'] or '尚未绑定应用；不代表已打开前端。'), '']
        _atomic_text(root / 'TASK.md', '\n'.join(lines))
    return status
