"""Fixed-capability subprocess transport, shared by MCP tests and host adapters."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from portable_process import isolation_kwargs, stop_timed_out_process


def _needs_generated_item_keys(decisions):
    if not isinstance(decisions,dict):return False
    for field,items,key in [('text_decisions','items','item_key'),('table_decisions','records','record_key')]:
        scopes=decisions.get(field,[])
        if not isinstance(scopes,(list,tuple)):continue
        for scope in scopes:
            records=scope.get(items,[]) if isinstance(scope,dict) else []
            if not isinstance(records,(list,tuple)):continue
            for record in records:
                if isinstance(record,dict) and record.get(key,record.get('item_key') if key=='record_key' else None) is None:
                    return True
    return False


def _uses_item_number_refs(value):
    if isinstance(value,dict):
        return value.get('kind') in {'ITEM_SOURCE','ITEM_HEADING'} or any(_uses_item_number_refs(v) for v in value.values())
    return isinstance(value,(list,tuple)) and any(_uses_item_number_refs(v) for v in value)


class TaskBridge:
    def __init__(self, working_root, entrypoint, *, project_root=None, timeout=120, source_roots=None):
        self.working_root = Path(working_root).expanduser().resolve(strict=True)
        self.entrypoint = Path(entrypoint).resolve(strict=True)
        self.project_root = project_root
        self.timeout = timeout
        self.source_roots = [Path(p).expanduser().resolve(strict=True) for p in (source_roots or [self.working_root])]

    def command_revision(self, task_id, command_id):
        """Recover only this command's original CAS token, never the current head.

        The fixed CLI still authenticates the complete request digest and receipt.
        No new journal or mutable binding sidecar is introduced.
        """
        from task_service import find_task_root
        from reqpilotx.application.agent_workflow import task_workspace as core
        root, context = core._resolve_task(find_task_root(self.working_root, task_id))
        identifier = core._command_operation_id(context, command_id)
        if not (root/'operations'/(identifier+'.json')).is_file():
            return None
        operation = core._operation(root, identifier)
        if operation.get('command_id') != command_id or not operation.get('expected_revision'):
            raise ValueError('COMMAND_CONTEXT_INVALID: original command revision is unavailable; supply the original explicit request')
        return operation['expected_revision']

    def invoke(self, action, request):
        from task_service import find_task_root
        if action == 'open' and request.get('mode') == 'create':
            source = Path(request.get('source') or '').expanduser().resolve(strict=True)
            if not source.is_file() or not any(source.is_relative_to(root) for root in self.source_roots):
                return {'status':'FAILED','error_code':'SOURCE_OUTSIDE_CONFIGURED_ROOT',
                    'message':'Use a source supplied under this host\'s configured source roots.'}
            command = [sys.executable, str(self.entrypoint)]
            if self.project_root: command += ['--project-root', str(self.project_root)]
        elif action=='describe' and not request.get('task_id'):
            command=[sys.executable,str(self.entrypoint)]
            if self.project_root:command+=['--project-root',str(self.project_root)]
        else:
            root = find_task_root(self.working_root, request.get('task_id', ''))
            # Validate the source and sealed context BEFORE it can select an
            # executable. Validation inside the selected child would be too late.
            from reqpilotx.application.agent_workflow.task_workspace import _resolve_task
            root, context = _resolve_task(root)
            from workspace_tools import _runtime
            command = _runtime(context)
        capability = Path(command[1]).parent/'task_service.py'
        if not capability.is_file():
            return {'status':'FAILED','error_code':'UNSUPPORTED_CAPABILITY',
                'message':'The task\'s fixed package has no typed task API. Resume with its existing CLI; no package upgrade was substituted.'}
        catalog=capability.with_name('task-api-capabilities.json')
        supported=json.loads(catalog.read_text()) if catalog.is_file() else {
            'actions':['open','inspect','prepare','read','map','items','review','check','deliver','sync','recover'],
            'read_views':['source','part','scope','page','image']}
        template_unsupported=(action=='read' and request.get('view')=='scope' and request.get('part')=='template'
            and 'scope_template' not in supported.get('read_features',[]))
        outline_unsupported=(action=='map' and 'outline' in (request.get('decisions') or {}) and 'map_outline' not in supported.get('write_features',[]))
        handle_unsupported=(action=='read' and isinstance(request.get('ref'),str) and 'source_handles' not in supported.get('read_features',[]))
        retention_unsupported=(action=='items' and 'retain_map_structure' in (request.get('decisions') or {}) and 'items_retain_map_structure' not in supported.get('write_features',[]))
        context_part=str(request.get('part',''))
        context_unsupported=(action=='read' and request.get('view')=='item' and (context_part.split('@',1)[0] in {'context_candidates','linked_context'} or context_part.startswith('decomposition_packet@')) and 'source_context_candidates' not in supported.get('read_features',[]))
        region_unsupported=(action=='read' and any(request.get(k) is not None for k in ('bbox','scale')) and 'pdf_region_images' not in supported.get('read_features',[]))
        generated_keys_unsupported=(action=='items'
            and 'items_generated_local_keys' not in supported.get('write_features',[])
            and _needs_generated_item_keys(request.get('decisions')))
        number_refs_unsupported=(action in {'annotations','revise'} and _uses_item_number_refs(request.get('changes'))
            and 'item_number_refs' not in supported.get('write_features',[]))
        numbering_unsupported=(action=='read' and request.get('view')=='item' and request.get('part') in {'numbering','numbering_review'}
            and 'source_numbering_review' not in supported.get('read_features',[]))
        if number_refs_unsupported or numbering_unsupported or generated_keys_unsupported or region_unsupported or context_unsupported or retention_unsupported or handle_unsupported or action not in supported['actions'] or (action=='read' and request.get('view','source') not in supported['read_views']) or template_unsupported or outline_unsupported:
            return {'status':'FAILED','error_code':'UNSUPPORTED_CAPABILITY',
                'message':'This operation is not provided by the task\'s fixed package. Its package was not replaced.',
                'supported':supported}
        with tempfile.TemporaryDirectory(prefix='reqpilot-request-') as temporary:
            path = Path(temporary)/'request.json'
            path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8'); path.chmod(0o600)
            command += ['task', action, '--working-root', str(self.working_root), '--input', str(path)]
            process = subprocess.Popen(command, cwd=self.working_root, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, **isolation_kwargs())
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                stop_timed_out_process(process, force=False)
                try: process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_timed_out_process(process, force=True)
                    process.communicate()
                return {'status':'UNKNOWN','error_code':'OPERATION_TIMEOUT',
                    'task_id':request.get('task_id'), 'command_id':request.get('command_id'),
                    'message':'Execution was stopped. It may already have committed. Inspect the persistent task and replay the same request; do not create a replacement command.',
                    'rollback_claimed':False}
            try:
                value = json.loads(stdout if stdout.strip() else stderr)
            except (TypeError, ValueError):
                return {'status':'UNKNOWN','error_code':'INVALID_TRANSPORT_RESULT',
                    'task_id':request.get('task_id'),'exit_code':process.returncode,
                    'message':'The fixed CLI returned no valid result object. Inspect the task before retrying; no success is inferred from exit code.'}
            if not isinstance(value, dict):
                return {'status':'UNKNOWN','error_code':'INVALID_TRANSPORT_RESULT'}
            if process.returncode and value.get('status') not in {'FAILED','NOT_READY','UNKNOWN'}:
                return {'status':'UNKNOWN','error_code':'EXIT_RESULT_MISMATCH','exit_code':process.returncode}
            return value

    def image_bytes(self, value):
        path = Path(value['path']).resolve(strict=True)
        if not path.is_relative_to(self.working_root):
            raise ValueError('Image does not belong to this configured task root')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != value['sha256']:
            raise ValueError('Image changed after source verification')
        return data
