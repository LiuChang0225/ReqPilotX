"""Read a bound task's workflow state from authenticated, pinned package bytes.

The workspace `.reqpilot` directory is treated as an untrusted bytes source. Its
declared inventory and every file hash are verified before a private Host-owned
read-only cache is populated. Only that cache is imported. The task's data root
remains the workspace; no workspace code or cached bytecode is executed.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


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


def main() -> int:
    payload = read(sys.argv[1])
    workspace = Path(sys.argv[2]).resolve(strict=True)
    cache_root = Path(sys.argv[3]).resolve()
    if workspace.is_symlink():
        raise ValueError('TASK_WORKFLOW_WORKSPACE_MISMATCH')
    fixed = pinned_file(workspace, '.reqpilot')
    receipt = read(pinned_file(fixed, 'build-receipt.json'))
    if receipt.get('bundle_sha256') != payload['bundle_sha256'] or digest({k: v for k, v in receipt.items() if k != 'bundle_sha256'}) != payload['bundle_sha256']:
        raise ValueError('TASK_WORKFLOW_PACKAGE_MISMATCH')
    _verify_tree(fixed, receipt)
    cache = _cache(fixed, receipt, cache_root)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(cache_root / 'pycache')
    sys.path.insert(0, str(cache / 'scripts'))
    import reqpilot
    reqpilot.load_runtime()
    from task_service import TaskService
    result = TaskService(workspace, cache / 'scripts/reqpilot.py').invoke('inspect', {'task_id': payload['task_id']})
    if result.get('status') != 'READY' or result.get('task_id') != payload['task_id']:
        raise ValueError('TASK_WORKFLOW_TASK_MISMATCH')
    if (result.get('source') or {}).get('sha256') != payload['source_sha256']:
        raise ValueError('TASK_WORKFLOW_SOURCE_CHANGED')
    expected = payload.get('workflow') or {}
    actual = (result.get('workflow') or {}).get('workflow') or {}
    for key in ('id', 'version', 'definition_sha256', 'brief_sha256'):
        if expected.get(key) != actual.get(key):
            raise ValueError('TASK_WORKFLOW_BINDING_MISMATCH')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'status': 'FAILED', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
