"""Session convenience over explicit task requests; never selects filesystem latest."""
from copy import deepcopy
import hashlib
import json


class SessionContext:
    def __init__(self):
        self.observed = {}
        self.epochs = {}
        self.requests = {}

    def schema(self, model):
        schema = model.model_json_schema()
        for field in ('task_id', 'expected_revision'):
            if field in schema.get('required', []):
                schema['required'].remove(field)
                schema['properties'][field]['description'] = (
                    'May be omitted in MCP when unambiguous observed session context exists. '
                    'Explicit values remain authoritative; the typed CLI still requires this field.')
        return schema

    def bind(self, action, arguments, model, bridge):
        request = deepcopy(arguments)
        if action not in {'open', 'describe'} and 'task_id' not in request:
            if len(self.observed) != 1:
                raise ValueError('TASK_CONTEXT_REQUIRED: open/inspect one task, or supply task_id when multiple tasks are observed')
            request['task_id'] = next(iter(self.observed))
        task = request.get('task_id')
        # Generic describe stays generic. Explicit describe/revision pins are untouched.
        write = 'expected_revision' in model.model_fields and model.model_fields['expected_revision'].is_required()
        key = None
        if write and 'expected_revision' not in request:
            key = json.dumps([action, request], sort_keys=True, ensure_ascii=False)
            if key in self.requests:
                request = deepcopy(self.requests[key])
            else:
                command = request.get('command_id')
                revision = bridge.command_revision(task, command) if command else None
                revision = revision or self.observed.get(task)
                if not revision:
                    raise ValueError('REVISION_CONTEXT_REQUIRED: inspect/resume this task before writing, or supply expected_revision')
                request['expected_revision'] = revision
                # Expose a stable retry handle even if the caller omits command_id.
                if not command:
                    value = json.dumps([action, request], sort_keys=True, ensure_ascii=False)
                    request['command_id'] = 'mcp-' + hashlib.sha256(value.encode()).hexdigest()
        validated = model.model_validate(request).model_dump(mode='json', exclude_unset=True)
        if key is not None:
            self.requests[key] = deepcopy(validated)
        return validated, (task, self.epochs.get(task, 0))

    def observe(self, result, ticket):
        task, revision = result.get('task_id'), result.get('revision')
        if not task or not revision or result.get('status') in {'FAILED', 'NOT_READY', 'UNKNOWN'}:
            return
        # Out-of-order concurrent replies cannot elect a newer task state. Require
        # a fresh read after overlapping observations instead of guessing order.
        requested_task, epoch = ticket
        if requested_task == task and self.epochs.get(task, 0) != epoch:
            self.observed[task] = None
        else:
            self.observed[task] = revision
        self.epochs[task] = self.epochs.get(task, 0) + 1
