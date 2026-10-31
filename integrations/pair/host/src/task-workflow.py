"""Run a bound task's fixed Host actions from authenticated, pinned package bytes.

The workspace `.reqpilot` directory is treated as an untrusted bytes source. Its
declared inventory and every file hash are verified before a private Host-owned
read-only cache is populated. Only that cache is imported. The task's data root
remains the workspace; no workspace code or cached bytecode is executed. The
allowlist is inspect/analysis/evidence_status; analysis keeps the task's pinned
entrypoint identity while its typed review subprocess runs the cached bytes.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path


COMMANDS = {'inspect', 'analysis', 'evidence_status'}
MUTATIONS = {'analysis', 'evidence_status'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _document(path, name):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(name + '_INVALID')
    value = read(path)
    if not isinstance(value, dict):
        raise ValueError(name + '_INVALID')
    return value


def pinned_file(root, name):
    path = root / name
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
    return path


def _declared(receipt):
    declared = {entry['path'] for entry in receipt['plugin_files']} | {'build-receipt.json'}
    declared |= {'runtime/prototype/' + entry['path'] for entry in receipt['runtime_files']}
    return declared


def _verify_tree(root, receipt):
    declared = _declared(receipt)
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
        relative = path.relative_to(root)
        if path.is_file():
            if '__pycache__' in relative.parts:
                match = re.fullmatch(r'(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc', path.name)
                source = relative.parent.parent / ((match[1] + '.py') if match else '')
                if path.parent.name != '__pycache__' or not match or source.as_posix() not in declared:
                    raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
                continue
            actual.add(relative.as_posix())
    if actual != declared:
        raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
    for entries, base in ((receipt['plugin_files'], root), (receipt['runtime_files'], root / 'runtime/prototype')):
        for entry in entries:
            path = pinned_file(base, entry['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
    return declared


def _cache(fixed, receipt, cache_root):
    target = cache_root / receipt['bundle_sha256']
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError('TASK_WORKFLOW_CACHE_INVALID')
        try:
            _verify_tree(target, receipt)
            return target
        except (ValueError, OSError, KeyError, TypeError):
            raise ValueError('TASK_WORKFLOW_CACHE_INVALID')
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = cache_root / (receipt['bundle_sha256'] + '.tmp-' + os.urandom(8).hex())
    try:
        temporary.mkdir(mode=0o700)
        sources = [*((entry, fixed, '') for entry in receipt['plugin_files']),
                   *((entry, fixed / 'runtime/prototype', 'runtime/prototype/') for entry in receipt['runtime_files']),
                   ({'path': 'build-receipt.json', 'sha256': digest(receipt)}, fixed, '')]
        for entry, base, prefix in sources:
            source = pinned_file(base, entry['path'])
            destination = temporary / (prefix + entry['path'])
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            destination.chmod(0o400)
        if _verify_tree(temporary, receipt) != _declared(receipt):
            raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
        os.replace(temporary, target)
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _verified_package(payload, workspace, cache_root):
    fixed = pinned_file(workspace, '.reqpilot')
    receipt = read(pinned_file(fixed, 'build-receipt.json'))
    if receipt.get('bundle_sha256') != payload['bundle_sha256'] or digest({k: v for k, v in receipt.items() if k != 'bundle_sha256'}) != payload['bundle_sha256']:
        raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
    _verify_tree(fixed, receipt)
    return receipt, _cache(fixed, receipt, cache_root)


def _task_context(result, workspace):
    root = Path(result['task_root']).resolve(strict=True)
    if not root.is_relative_to(workspace):
        raise ValueError('TASK_WORKFLOW_WORKSPACE_MISMATCH')
    return root, read(pinned_file(root, 'task-context.json'))


def _check_work_ref(root, context, payload):
    work_ref = payload.get('work_ref')
    if not isinstance(work_ref, str) or not work_ref:
        raise ValueError('TASK_WORKFLOW_WORK_REF_MISMATCH')
    declared = Path(context.get('binding_file') or '')
    path = (declared if declared.is_absolute() else root / declared)
    if path.is_symlink() or not path.is_file():
        raise ValueError('TASK_WORKFLOW_WORK_REF_MISMATCH')
    path = path.resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError('TASK_WORKFLOW_WORK_REF_MISMATCH')
    binding = read(path)
    if (binding.get('work_id') not in (None, context['work_id'])
            or binding.get('session_id') != work_ref
            or binding.get('source_sha256') != payload['source_sha256']
            or binding.get('project_id') != payload.get('project_id')
            or binding.get('document_id') != payload.get('document_id')):
        raise ValueError('TASK_WORKFLOW_WORK_REF_MISMATCH')


def _check_identity(result, payload, action, workspace, cache):
    if result.get('status') != 'READY' or result.get('task_id') != payload.get('task_id'):
        raise ValueError('TASK_WORKFLOW_TASK_MISMATCH')
    if (result.get('source') or {}).get('sha256') != payload.get('source_sha256'):
        raise ValueError('TASK_WORKFLOW_SOURCE_CHANGED')
    expected = payload.get('workflow')
    if expected is not None:
        actual = (result.get('workflow') or {}).get('workflow') or {}
        for key in ('id', 'version', 'definition_sha256', 'brief_sha256'):
            if expected.get(key) != actual.get(key):
                raise ValueError('TASK_WORKFLOW_BINDING_MISMATCH')
    if action not in MUTATIONS:
        return None, None
    runtime = result.get('runtime') or {}
    cache_entry = cache / 'scripts/reqpilot.py'
    if (runtime.get('kind') != 'BUNDLED' or runtime.get('bundle_sha256') != payload['bundle_sha256']
            or runtime.get('entrypoint_sha256') != hashlib.sha256(cache_entry.read_bytes()).hexdigest()):
        raise ValueError('TASK_WORKFLOW_RUNTIME_MISMATCH')
    root, context = _task_context(result, workspace)
    if context.get('work_id') != payload['task_id'] or (context.get('source') or {}).get('sha256') != payload['source_sha256']:
        raise ValueError('TASK_WORKFLOW_TASK_MISMATCH')
    for key in ('project_id', 'document_id'):
        if not payload.get(key) or context.get(key) != payload[key]:
            raise ValueError('TASK_WORKFLOW_BINDING_MISMATCH')
    _check_work_ref(root, context, payload)
    return context, root


CHILD_BOOTSTRAP = """import json
import os
import runpy
import sys
from pathlib import Path

entry = Path(sys.argv[1]).resolve(strict=True)
root = entry.parent.parent
sys.path[:0] = [str(root / 'scripts'), str(root / 'runtime/prototype')]
sys.argv = [str(entry), *sys.argv[2:]]
receipt = os.environ.get('REQPILOT_HOST_CHILD_RECEIPT')
if receipt:
    try:
        target = Path(receipt)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(json.dumps({'isolated': bool(sys.flags.isolated),
            'ignore_environment': bool(sys.flags.ignore_environment),
            'argv': sys.argv, 'cwd': str(Path.cwd()), 'entry': str(entry)}), encoding='utf-8')
        target.chmod(0o600)
    except OSError:
        pass
runpy.run_path(str(entry), run_name='__main__')
"""


def _child_bootstrap(cache_root):
    path = cache_root / 'task-command-child.py'
    data = CHILD_BOOTSTRAP.encode('utf-8')
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(CHILD_BOOTSTRAP)
        path.chmod(0o400)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('TASK_WORKFLOW_CHILD_BOOTSTRAP_INVALID')
    return path


@contextmanager
def _pinned_runtime(cache, receipt, cache_root):
    import subprocess as subprocess_module
    import workspace_tools
    original_runtime = workspace_tools._runtime
    original_subprocess = workspace_tools.subprocess
    entry = cache / 'scripts/reqpilot.py'
    bootstrap = _child_bootstrap(cache_root)
    entry_sha256 = hashlib.sha256(entry.read_bytes()).hexdigest()
    class Launch:
        @staticmethod
        def run(argv, **kwargs):
            value = list(argv)
            if len(value) >= 2 and Path(value[1]) == entry:
                value = [value[0], '-I', str(bootstrap), *value[1:]]
            return subprocess_module.run(value, **kwargs)
    def pinned(context):
        ref = context.get('runtime_ref') or {}
        if (ref.get('kind') != 'BUNDLED' or ref.get('bundle_sha256') != receipt['bundle_sha256']
                or ref.get('entrypoint_sha256') != entry_sha256):
            raise ValueError('TASK_WORKFLOW_RUNTIME_MISMATCH')
        return [sys.executable, str(entry)]
    workspace_tools._runtime = pinned
    workspace_tools.subprocess = Launch
    try:
        yield
    finally:
        workspace_tools._runtime = original_runtime
        workspace_tools.subprocess = original_subprocess


def _check_request(action, request, payload, context):
    if request.get('task_id') != payload['task_id']:
        raise ValueError('TASK_WORKFLOW_TASK_MISMATCH')
    bound = (context.get('request') or {}).get('knowledge_evidence')
    if not isinstance(bound, dict) or not bound.get('binding_sha256'):
        raise ValueError('TASK_WORKFLOW_EVIDENCE_BINDING_MISSING')
    if action == 'evidence_status':
        if request.get('binding_sha256') != bound['binding_sha256']:
            raise ValueError('TASK_WORKFLOW_EVIDENCE_BINDING_MISMATCH')
    else:
        if request.get('expected_revision') != payload['expected_revision']:
            raise ValueError('TASK_WORKFLOW_REVISION_CHANGED')
        attestation = request.get('knowledge_attestation')
        if (not isinstance(attestation, dict) or attestation.get('binding_sha256') != bound['binding_sha256']
                or attestation.get('task_revision') != payload['expected_revision']
                or attestation.get('task_id') != payload['task_id']):
            raise ValueError('TASK_WORKFLOW_ATTESTATION_BINDING_MISMATCH')


def main() -> int:
    payload = _document(sys.argv[1], 'TASK_WORKFLOW_COMMAND')
    action = payload.get('action', 'inspect')
    if action not in COMMANDS:
        raise ValueError('TASK_WORKFLOW_ACTION_UNSUPPORTED')
    if action == 'inspect':
        allowed = {'schema', 'action', 'task_id', 'bundle_sha256', 'source_sha256', 'workflow'}
    else:
        allowed = {'schema', 'action', 'task_id', 'bundle_sha256', 'source_sha256', 'project_id',
                   'document_id', 'work_ref', 'workflow', 'expected_revision', 'request_file'}
    if (set(payload) - allowed or not {'task_id', 'bundle_sha256', 'source_sha256'} <= set(payload)
            or payload.get('schema', 'reqpilot-pair-host-command/v1') != 'reqpilot-pair-host-command/v1'):
        raise ValueError('TASK_WORKFLOW_COMMAND_INVALID')
    workspace = Path(sys.argv[2]).resolve(strict=True)
    cache_root = Path(sys.argv[3]).resolve()
    if workspace.is_symlink():
        raise ValueError('TASK_WORKFLOW_WORKSPACE_MISMATCH')
    receipt, cache = _verified_package(payload, workspace, cache_root)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(cache_root / 'pycache')
    for name in ('PYTHONPATH', 'PYTHONHOME', 'REQPILOT_DEVELOPMENT_ROOT'):
        os.environ.pop(name, None)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(cache / 'scripts'))
    import reqpilot
    reqpilot.load_runtime()
    from task_service import TaskService
    service = TaskService(workspace, cache / 'scripts/reqpilot.py')
    result = service.invoke('inspect', {'task_id': payload['task_id']})
    context, root = _check_identity(result, payload, action, workspace, cache)
    if action != 'inspect':
        from task_service import REQUESTS
        if action not in REQUESTS or not callable(getattr(service, action, None)):
            raise ValueError('TASK_WORKFLOW_ACTION_UNSUPPORTED')
        request = _document(payload.get('request_file'), 'TASK_WORKFLOW_REQUEST')
        _check_request(action, request, payload, context)
        try:
            if action == 'analysis':
                with _pinned_runtime(cache, receipt, cache_root):
                    result = service.invoke(action, request)
            else:
                result = service.invoke(action, request)
        except ImportError as error:
            raise ValueError('TASK_WORKFLOW_ACTION_UNAVAILABLE: pinned task package cannot verify this action: ' + str(error))
        if result.get('task_id') not in (None, payload['task_id']):
            raise ValueError('TASK_WORKFLOW_TASK_MISMATCH')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'status': 'FAILED', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
