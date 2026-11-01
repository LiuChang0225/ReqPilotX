"""Explicit same-task analysis policy changes using the existing review owner.

No retrieval result can select a policy. The caller's typed review request is
the durable intent; existing compiler, CAS, close and export contracts own the
successor. User opinions and feedback are carried, never manufactured/resolved.
"""
from pathlib import Path
from contextlib import contextmanager
from threading import local

from reqpilotx.application.agent_workflow import task_workspace as core
from task_review import apply_review

_COMMAND_SCOPE = local()


def _command_identity(state, action, request):
    from task_service import digest

    explicit = {'action': action, 'request': request.model_dump(
        mode='json', exclude={'command_id'}, exclude_unset=True)}
    command = request.command_id or 'task-api-' + digest(explicit)
    return explicit, command, core._command_operation_id(state['context'], command)


def _preserved_command(root, identifier, explicit):
    from task_service import digest

    path = root / 'inputs' / 'review-prepare-commands' / (identifier + '.json')
    if not path.is_file():
        return None
    observed = core.read_json(path)
    body = {key: value for key, value in observed.items() if key != 'receipt_sha256'}
    if observed.get('receipt_sha256') != digest(body):
        raise ValueError('IMMUTABLE_INPUT_CHANGED: preserved review command receipt changed')
    if body.get('request') != explicit:
        raise ValueError('COMMAND_ID_CONFLICT: task command intent changed')
    return body


@contextmanager
def guard_write_command(root, state, action, request):
    """Reserve one task-wide key before any public writer binds or compiles.

    The public dispatcher and lower initial-write/review paths share this guard.
    Same-thread nested calls retain the outer OS lock; other threads/processes
    must acquire it. A review no-op cannot be reused by another write action.
    """
    from portable_lock import exclusive_file

    explicit, _, identifier = _command_identity(state, action, request)
    key = (str(root.resolve()), identifier)
    active = getattr(_COMMAND_SCOPE, 'active', None)
    if active is None:
        active = _COMMAND_SCOPE.active = {}
    if key in active:
        if active[key] != explicit:
            raise ValueError('COMMAND_ID_CONFLICT: nested task command intent changed')
        yield
        return
    lock = root / 'operations' / 'review-prepare-command-locks' / (identifier + '.lock')
    with exclusive_file(lock):
        _preserved_command(root, identifier, explicit)
        active[key] = explicit
        try:
            yield
        finally:
            del active[key]


def prepare_review(service, root, state, request):
    explicit, command, identifier = _command_identity(state, 'review', request)
    with guard_write_command(root, state, 'review', request):
        return _prepare_review(service, root, state, request, explicit, command, identifier)


def _prepare_review(service, root, state, request, explicit, command, identifier):
    from task_service import digest, task_summary
    import revision_tools as review

    descriptor = root / 'operations' / (identifier + '.json')
    preserved = root / 'inputs' / 'review-prepare-commands' / (identifier + '.json')
    if preserved.is_file():
        body = _preserved_command(root, identifier, explicit)
        latest = core.workspace_status(root, refresh=False)
        return _preserved_result(latest, body, command, replayed=True)
    operation = None
    if descriptor.is_file():
        operation = core._operation(root, identifier)['operation']
        if operation not in {'review-open', 'review-apply'}:
            raise ValueError('COMMAND_ID_CONFLICT: command belongs to another action')
        core.lookup_operation_command(root, command, operation, explicit)
    reviewed = state['current_working'].get('owner') == 'portable_review'
    # A committed initial request always replays its initial route, even after
    # later review/policy versions. Never rebind a retry to the current head.
    if operation == 'review-open' or operation is None and not reviewed:
        return service.write(root, state, 'review', request)
    if not reviewed:
        raise ValueError('CURRENT_REVIEW_REQUIRED: resume the original working review')

    revision, replacements = None, None
    if operation is None:
        if state['revision'] != request.expected_revision:
            raise ValueError('REVISION_CONFLICT: refresh the task before updating its policy')
        head = review.load_head(Path(state['current_working']['review_root']))
        current, (_, baseline, _, _) = review.current_bundle(root / 'review', head)
        existing_policy = review.state(current).get('analysis_policy')
        policy = {'source_sha256': baseline.source_sha256,
                  'status': 'SKIPPED', 'reason': 'NO_INTERNAL_EVIDENCE'}
        if request.analysis_policy == 'preserve' or existing_policy == policy:
            latest = core.workspace_status(root, refresh=False)
            if (latest['revision'] != request.expected_revision
                    or latest['current_working'].get('review_sha256') != head['review_sha256']):
                raise ValueError('REVISION_CONFLICT: review changed while preserving its policy')
            observed = {'schema_version': 'reqpilot-review-prepare-noop-receipt/v1',
                'request': explicit, 'review_sha256': head['review_sha256'],
                'source_sha256': baseline.source_sha256,
                'baseline_sha256': baseline.artifact_sha256,
                'analysis_policy': existing_policy}
            from task_review import _once
            _once(preserved, {**observed, 'receipt_sha256': digest(observed)})
            return _preserved_result(latest, observed, command, replayed=False)
        # This branch is reached only for the explicit authorized request. It
        # does not claim a user opinion or that a search proved evidence absent.
        directory = root / 'inputs' / 'task-api'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (digest(policy) + '.json')
        from task_review import _once
        _once(path, policy)
        replacements = {'analysis_policy': path}
        revision = {'schema_version': 'reqpilot-review-revision/v1',
                    'expected_review_sha256': head['review_sha256'],
                    'expected_baseline_sha256': baseline.artifact_sha256}
    value = apply_review(root, expected_revision=request.expected_revision,
        command_id=command, revision=revision, replacements=replacements,
        request_payload=explicit)
    try:
        summary = task_summary(core.workspace_status(root, refresh=False))
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        # The durable receipt already describes saved success/current-state
        # failure. Do not lose it because its convenience summary cannot reload.
        summary = {'task_id': request.task_id, 'task_root': str(root)}
    return {**summary, **value, 'human_acceptance': 'NOT_CLAIMED',
            'handoff_ready': False}


def _preserved_result(state, observed, command, *, replayed):
    from task_service import task_summary

    return {'status': 'REVIEW_ALREADY_PREPARED', **task_summary(state),
        'operation_state': 'NOT_NEEDED', 'command_id': command, 'replayed': replayed,
        'business_revision_created': False, 'analysis_policy': observed['analysis_policy'],
        'review_sha256': observed['review_sha256'],
        'review_still_current': state['current_working'].get('review_sha256') == observed['review_sha256'],
        'human_acceptance': 'NOT_CLAIMED'}


__all__ = ['prepare_review', 'guard_write_command']
