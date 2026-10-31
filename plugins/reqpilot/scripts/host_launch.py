"""Host-neutral, no-model preparation using the existing bootstrap and typed task API."""
from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import reqpilot


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def prepare(payload):
    if set(payload) != {'workspace', 'expected_source_sha256', 'bundle_sha256', 'application_id', 'request'}:
        raise ValueError('HOST_PREPARE_INVALID_FIELDS')
    reqpilot.load_runtime()
    from task_service import OpenRequest, TaskService
    request = OpenRequest.model_validate(payload['request'])
    if request.mode != 'create' or not request.source or not request.goal or not request.creation_key:
        raise ValueError('HOST_PREPARE_REQUIRES_CREATE')
    source = Path(request.source).resolve(strict=True)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if sha != payload['expected_source_sha256']:
        raise ValueError('SOURCE_CHANGED')
    receipt = reqpilot.read_json(reqpilot.PLUGIN_ROOT / 'build-receipt.json')
    if receipt['bundle_sha256'] != payload['bundle_sha256']:
        raise ValueError('CAPABILITY_VERSION_CONFLICT')
    check = reqpilot.doctor(Namespace(project_root=None))
    if check['status'] != 'READY':
        raise ValueError('CAPABILITY_NOT_READY: ' + json.dumps(check.get('integrity_mismatches', [])))
    target = parse_qs(urlsplit(request.workbench_url or '').query)
    semantic = request.model_dump(exclude={'source', 'workbench_url'})
    identity = digest({'request': semantic, 'application_id': payload['application_id'],
                       'source_sha256': sha, 'bundle_sha256': receipt['bundle_sha256'],
                       'target': {k: target.get(k) for k in ('projectId', 'documentId')}})
    workspace = Path(payload['workspace']).resolve()
    marker = workspace / 'host-preparation.json'
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=workspace.parent))
        try:
            reqpilot.bootstrap(Namespace(workspace=stage, source=[source], project_root=None))
            reqpilot.write_json(stage / marker.name, {'schema': 'reqpilot-host-preparation/v1', 'identity': identity})
            os.rename(stage, workspace)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if workspace.is_symlink() or not marker.is_file() or reqpilot.read_json(marker).get('identity') != identity:
        raise ValueError('HOST_PREPARATION_CONFLICT')
    fixed = workspace / '.reqpilot'
    if reqpilot.read_json(fixed / 'build-receipt.json')['bundle_sha256'] != payload['bundle_sha256']:
        raise ValueError('CAPABILITY_VERSION_CONFLICT')
    inputs = reqpilot.read_json(workspace / 'runtime-data/input-manifest.json')['inputs']
    if len(inputs) != 1 or inputs[0]['sha256'] != sha:
        raise ValueError('HOST_SOURCE_CONFLICT')
    original = (workspace / inputs[0]['path']).resolve(strict=True)
    if not original.is_relative_to(workspace) or hashlib.sha256(original.read_bytes()).hexdigest() != sha:
        raise ValueError('SOURCE_CHANGED')
    entry = fixed / 'scripts/reqpilot.py'
    service = TaskService(workspace, entry)
    result = service.invoke('open', request.model_copy(update={'source': str(original)}).model_dump())
    task_id = result['task_id']
    task_root = Path(result['task_root']).resolve(strict=True)
    if not task_root.is_relative_to(workspace):
        raise ValueError('HOST_TASK_OUTSIDE_WORKSPACE')
    # Resume an interrupted initial bind using the same typed/core task and deterministic work identity.
    if request.workbench_url and not result['workbench_link'].get('session_id'):
        from workbench_tools import sync_workbench
        sync_workbench(Namespace(workspace=task_root, url=request.workbench_url, project_id=None,
                                document_id=None, goal=request.goal, open=False))
        result = service.invoke('inspect', {'task_id': task_id})
    url = result['workbench_link'].get('url')
    if request.workbench_url and urlsplit(url or '').netloc != urlsplit(request.workbench_url).netloc:
        raise ValueError('HOST_ENDPOINT_RECONNECT_REQUIRED')
    task_path = str((task_root / 'TASK.md').relative_to(workspace))
    cli = shlex.join([sys.executable, str(entry.relative_to(workspace))])
    instruction = ('# 本轮委托\n\n' + request.goal + '\n\n# 已准备的任务\n\n'
                   + '先读取 AGENTS.md 和 `' + task_path + '`，沿用已有任务，不另建任务。\n'
                   + '任务标识：`' + task_id + '`。\n'
                   + '本机工具入口：`' + cli + '`；typed task 命令的 --working-root 为当前工作目录。\n'
                   + '业务规则、角色技能和阶段事实以该固定能力包及 TASK.md 为准。\n')
    (workspace / 'request.md').write_text(instruction, encoding='utf-8')
    return {'schema': 'reqpilot-host-launch/v1', 'workspace': str(workspace), 'task_id': task_id,
            'task_root': str(task_root), 'source_sha256': sha, 'bundle_sha256': payload['bundle_sha256'],
            'work_ref': result['workbench_link'].get('session_id'), 'revision': result['revision'],
            'prompt': '请执行 request.md 中的本轮委托，沿用其中已准备的任务。',
            'execution': 'NOT_STARTED', 'semantic_acceptance': 'NOT_CLAIMED',
            'tools': {'schema': 'reqpilot-host-tools/v1', 'mcp': {
                'command': sys.executable,
                'args': [str(fixed / 'scripts/mcp_server.py'), '--working-root', str(workspace), '--source-root', str(workspace)],
                'cwd': str(workspace)}}}


if __name__ == '__main__':
    if sys.argv[1:] == ['--describe']:
        print(json.dumps({'schema': 'reqpilot-host-launch/v1', 'tools_schema': 'reqpilot-host-tools/v1'}))
        raise SystemExit(0)
    try:
        value = prepare(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'FAILED', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
