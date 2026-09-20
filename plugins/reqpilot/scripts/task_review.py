"""Durable execution of the existing review compiler/export/close contracts.

The task command journal records execution only. The review manifest and its
immutable history remain the sole owner of semantic versions.
"""
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

from app.application.agent_workflow import task_workspace as core
from app.application.agent_workflow.artifact_kind_catalog import validate_bundle
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
import artifact_tools
import revision_tools as review

OPERATION = 'review-apply'
REPLACEMENTS = {'annotations': ('image-annotations.json', 'annotation-compilation-receipt.json'),
                'decomposition': ('source-item-decomposition.json', 'decomposition-compilation-receipt.json')}
REVISION_FIELDS = {'schema_version', 'expected_review_sha256', 'expected_baseline_sha256', 'model_id',
                   'decisions', 'scope_revision', 'visual_readings', 'annotation_updates', 'addressed_feedback_ids'}


def _once(path, value):
    if path.is_file():
        if core.read_json(path) != value: raise ValueError('IMMUTABLE_INPUT_CHANGED: saved review command input differs')
    else:
        core._atomic_json(path, value)


def _checkpoint(directory, stage, value):
    """Separate saved substage receipts permit recovery after a lost response."""
    _once(directory / ('stage-' + stage + '.json'), value)


def _closed(review_root, old_sha, candidate, delivery):
    head = review.load_head(review_root); cursor = head; seen = set()
    while cursor['review_sha256'] not in seen:
        seen.add(cursor['review_sha256'])
        if (cursor.get('predecessor_review_sha256') == old_sha
                and cursor['current']['version_path'] == candidate
                and (cursor.get('delivery') or {}).get('path') == delivery):
            return cursor
        previous = cursor.get('predecessor_review_sha256')
        if not previous: return None
        cursor = core.read_json(core.inside(review_root, 'reviews/' + previous + '.json'))
        if review.seal(cursor) != cursor or cursor['review_sha256'] != previous:
            raise ValueError('Review history changed during command recovery')
    raise ValueError('Review history contains a cycle')


def _finding_head(root, operation):
    current = review.load_head(root / 'review'); cursor = current; seen = set()
    while cursor['review_sha256'] not in seen:
        seen.add(cursor['review_sha256'])
        if cursor.get('predecessor_review_sha256') == operation['expected_review_sha256']:
            try:
                head, feedback_id = core._validate_review_finding(root, operation, cursor['review_sha256'])
                return {'review_sha256': head['review_sha256'], 'feedback_id': feedback_id}
            except ValueError:
                return None  # Another writer's metadata step is not adopted.
        previous = cursor.get('predecessor_review_sha256')
        if not previous: return None
        cursor = core.read_json(root / 'review/reviews' / (previous + '.json'))
        if review.seal(cursor) != cursor or cursor['review_sha256'] != previous:
            raise ValueError('Review finding history integrity changed')
    raise ValueError('Review finding history contains a cycle')


def _candidate(review_root, directory, expected, source_sha):
    marker = directory / 'revision-receipt.json'
    if not marker.is_file(): raise ValueError('UNKNOWN_EXECUTION: candidate directory has no completed revision receipt; preserve it for explicit recovery')
    receipt = core.read_json(marker)
    if receipt.get('review_sha256') != expected['expected_review_sha256']:
        raise ValueError('Candidate belongs to another review command')
    submitted = core.read_json(directory / 'revision-input.json')
    if {key: submitted.get(key) for key in expected} != expected:
        raise ValueError('Candidate decisions differ from the frozen command')
    for name, digest in receipt['files'].items():
        if core.file_sha256(core.inside(directory, name)) != digest: raise ValueError('Saved candidate changed')
    validate_bundle(directory, source_sha, operation='review-revise')
    previous = core.read_json(review_root / 'reviews' / (expected['expected_review_sha256'] + '.json'))
    previous_dir = core.inside(review_root, previous['current']['version_path'])
    mapping, baseline, _, tables = review.bundle(directory)
    old = review.bundle(previous_dir)[1]
    review.verify_transition(review_root, previous_dir, directory, mapping, old, baseline, tables,
                             expected_review_sha256=expected['expected_review_sha256'])
    return {'path': str(directory.relative_to(review_root)), 'receipt_sha256': core.file_sha256(marker), 'baseline_sha256': baseline.artifact_sha256}


def _delivery(directory, candidate, source_sha):
    if not (directory / 'delivery-receipt.json').is_file():
        raise ValueError('UNKNOWN_EXECUTION: delivery directory has no completed export receipt; preserve it for explicit recovery')
    validate_bundle(directory, source_sha, operation='deliver', dependency_dirs=[candidate])
    mapping, baseline, _, tables = review.bundle(candidate)
    values = review.state(candidate)
    from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
    expected = build_delivery_snapshot(mapping, baseline, tables, **{key: values[key] for key in
        ('annotations', 'decomposition', 'assessments', 'manual_decisions', 'analysis_policy') if key in values})
    if core.read_json(directory / 'matrix-snapshot.json') != expected:
        raise ValueError('Saved export differs from the complete current candidate')
    return {'path': str(directory.relative_to(candidate.parent.parent)), 'xlsx_sha256': core.file_sha256(directory / 'matrix.xlsx'),
            'snapshot_sha256': canonical_json_sha256(expected)}


def lookup_review_command(root: Path, command_id: str, request_payload: dict):
    """Read before rebinding semantic input; STARTED commands resume via apply."""
    return core.lookup_operation_command(root, command_id, OPERATION, request_payload)


def _present(root, operation, *, replayed, execution_state, host=None):
    directory = Path(operation['output_dir'])
    if not directory.is_absolute(): directory = core.inside(root, operation['output_dir'])
    application = core.read_json(directory / 'review-application.json')
    receipt = core.read_json(directory / 'operation-receipt.json')
    try:
        current = core.workspace_status(root, refresh=False)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return {'status': 'REVIEW_OPERATION_COMMITTED', 'operation_state': 'COMMITTED', 'state': 'COMMITTED',
            'operation_id': operation['operation_id'], 'command_id': operation['command_id'], 'replayed': replayed,
            'execution_state': execution_state, 'historical_success': True, 'review_still_current': None,
            'old_review_sha256': application['old_review_sha256'], 'new_review_sha256': application['new_review_sha256'],
            'finding_review_sha256': application.get('finding_review_sha256'), 'finding_feedback_id': application.get('finding_feedback_id'),
            'source_sha256': application['source_sha256'], 'candidate': str(root / 'review' / application['candidate_path']),
            'delivery': str(root / 'review' / application['delivery_path']), 'diff': receipt['result']['diff'],
            'receipt_path': str(directory / 'operation-receipt.json'), 'receipt_sha256': receipt['receipt_sha256'],
            'sync': (host or {}).get('sync', {'status': 'NOT_REEXECUTED'}),
            'verification': {'status': 'CURRENT_STATE_UNAVAILABLE', 'error': str(exc), 'saved_check': (host or {}).get('verification')},
            'local_artifact_preserved': True, 'human_acceptance': 'NOT_CLAIMED'}
    if host is None:
        records = [entry for entry in current['index']['host_receipts'] if entry['operation_id'] == operation['operation_id']]
        host = core.read_json(core.inside(root, records[-1]['path']))['receipt'] if records else {'sync': {'status': 'NOT_REEXECUTED'}, 'verification': {'status': 'NOT_REEXECUTED'}}
    same = current['current_working'].get('review_sha256') == application['new_review_sha256']
    return {'status': 'REVIEW_OPERATION_COMMITTED', 'operation_state': 'COMMITTED', 'state': 'COMMITTED',
        'task_id': current['context']['work_id'], 'task_root': str(root), 'revision': current['revision'],
        'operation_id': operation['operation_id'], 'command_id': operation['command_id'], 'replayed': replayed,
        'execution_state': execution_state, 'historical_success': True, 'review_still_current': same,
        'source_sha256': application['source_sha256'], 'old_review_sha256': application['old_review_sha256'],
        'new_review_sha256': application['new_review_sha256'], 'candidate': str(root / 'review' / application['candidate_path']),
        'finding_review_sha256': application.get('finding_review_sha256'), 'finding_feedback_id': application.get('finding_feedback_id'),
        'delivery': str(root / 'review' / application['delivery_path']), 'diff': receipt['result']['diff'],
        'origin': application['origin'], 'addressed_feedback_ids': application['addressed_feedback_ids'],
        'receipt_path': str(directory / 'operation-receipt.json'), 'receipt_sha256': receipt['receipt_sha256'],
        'sync': host.get('sync'), 'verification': host.get('verification'),
        **({'host_receipt': host['host_receipt']} if 'host_receipt' in host else {}),
        'selected': current['selected'], 'workbench_url': current['workbench_url'],
        'human_acceptance': 'NOT_CLAIMED'}


def apply_review(root: Path, *, expected_revision: str, command_id: str, revision: dict | None,
                 replacements: dict[str, Path] | None = None, compile_replacements: dict[str, dict] | None = None,
                 origin: str = 'AGENT_SELF_REVIEW', resolutions: list[dict] | None = None,
                 self_review_finding: dict | None = None,
                 request_payload: dict | None = None) -> dict:
    root, context = core._resolve_task(root)
    replacements = replacements or {}; compile_replacements = compile_replacements or {}
    supplied = {'revision': revision, 'origin': origin, 'resolutions': resolutions or [],
                'compile_replacements': compile_replacements,
                **({'self_review_finding': self_review_finding} if self_review_finding is not None else {})}
    request = request_payload if request_payload is not None else {'expected_revision': expected_revision, **supplied,
        'replacements': {key: {'sha256': core.file_sha256(path)} for key, path in replacements.items()}}
    prior = lookup_review_command(root, command_id, request)
    if prior is None:
        state = core.workspace_status(root, refresh=False)
        if state['revision'] != expected_revision: raise ValueError('REVISION_CONFLICT: refresh the task before creating a review command')
        if state['current_working']['owner'] != 'portable_review': raise ValueError('CURRENT_REVIEW_REQUIRED: prepare the review first')
        if not isinstance(revision, dict) or set(revision) - REVISION_FIELDS:
            raise ValueError('INVALID_DECISION: revision supports classification, scope replacement, visual readings and annotation updates only')
        if origin not in {'AGENT_SELF_REVIEW', 'USER'}: raise ValueError('INVALID_DECISION: use an actual review origin')
        if self_review_finding is not None:
            if (origin != 'AGENT_SELF_REVIEW' or not isinstance(self_review_finding, dict)
                    or set(self_review_finding) - {'text', 'source_item_ids', 'rationale'}
                    or not isinstance(self_review_finding.get('text'), str) or not self_review_finding['text'].strip()
                    or not isinstance(self_review_finding.get('source_item_ids'), list)
                    or not isinstance(self_review_finding.get('rationale', ''), str)):
                raise ValueError('INVALID_DECISION: self_review_finding requires explicit Agent text, resolved source_item_ids and optional rationale')
        if set(replacements) - REPLACEMENTS.keys() or set(compile_replacements) - REPLACEMENTS.keys() or set(replacements) & set(compile_replacements):
            raise ValueError('INVALID_DECISION: provide annotations/decomposition once, as a sealed artifact or compiler decisions')
        review_root = root / 'review'; head = review.load_head(review_root, revision['expected_review_sha256'])
        if head['current']['baseline_sha256'] != revision['expected_baseline_sha256']:
            raise ValueError('REVISION_CONFLICT: revision decisions target another baseline')
        feedback = {item['feedback_id']: item for item in head['feedback']}
        if self_review_finding is not None:
            known = {record.source_record_id for record in review.current_bundle(review_root, head)[1][1].records}
            ids = self_review_finding['source_item_ids']
            if any(not isinstance(sid, str) for sid in ids) or len(ids) != len(set(ids)) or set(ids) - known:
                raise ValueError('INVALID_DECISION: self-review finding source_item_ids must resolve to this baseline')
        addressed = revision.get('addressed_feedback_ids', [])
        if origin == 'USER' and not any(feedback.get(fid, {}).get('origin') == 'USER' for fid in addressed):
            raise ValueError('INVALID_DECISION: USER origin requires an actual recorded USER feedback reference')
        if any(resolution.get('feedback_id') not in feedback for resolution in supplied['resolutions']):
            raise ValueError('INVALID_DECISION: resolutions must reference existing feedback')
        # Freeze external replacement bytes once; retries consume these copies.
        inputs = root / 'inputs' / 'review-commands' / canonical_json_sha256(request)
        inputs.mkdir(parents=True, exist_ok=True)
        copied = {}
        for key, path in replacements.items():
            target = inputs / REPLACEMENTS[key][0]
            value = core.read_json(Path(path)); _once(target, value); copied[key] = target
        _once(inputs / 'request.json', supplied)
        planned = core.begin_operation(root, OPERATION, {'input': inputs / 'request.json', 'replacements': copied},
            expected_revision=expected_revision, command_id=command_id, request=request)
    else: planned = prior
    lock_root = root / 'operations' / 'review-command-locks' / planned['operation_id']
    lock_root.mkdir(parents=True, exist_ok=True)
    with review.locked(lock_root):
        operation = lookup_review_command(root, command_id, request)
        if operation['state'] in {'FAILED', 'BLOCKED'}:
            return {'status': 'FAILED', 'operation_state': operation['state'], 'operation_id': operation['operation_id'],
                    'error': operation.get('error'), 'replayed': True, 'execution_state': 'NOT_REEXECUTED',
                    'local_artifact_preserved': True}
        if operation['state'] in {'COMMITTED', 'SAVED_PENDING_RECOVERY'}:
            if operation['state'] != 'COMMITTED': core.commit_operation(root, operation['operation_id'])
            return _present(root, operation, replayed=True, execution_state='NOT_REEXECUTED')
        directory = Path(operation['output_dir']); directory.mkdir(parents=True, exist_ok=True)
        frozen = core.read_json(Path(operation['resolved_inputs']['input']))
        revision = deepcopy(frozen['revision']); original_sha = operation['expected_review_sha256']; old_sha = original_sha; review_root = root / 'review'
        finding_state = None
        resolutions = deepcopy(frozen['resolutions'])
        candidate_rel = 'versions/' + operation['operation_id']; delivery_rel = 'deliveries/' + operation['operation_id']
        candidate = review_root / candidate_rel; delivery = review_root / delivery_rel
        try:
            if frozen.get('self_review_finding'):
                finding_state = _finding_head(root, operation)
                if finding_state is None:
                    if review.load_head(review_root)['review_sha256'] != original_sha:
                        raise ValueError('REVISION_CONFLICT: another writer advanced the original review before this command recorded its finding')
                    finding_input = directory / 'finding-input.json'
                    _once(finding_input, core._review_finding_payload(operation, frozen['self_review_finding']))
                    actual = review.feedback_review(Namespace(review_dir=review_root, input=finding_input))
                    finding_state = _finding_head(root, operation)
                    if finding_state is None or finding_state['review_sha256'] != actual['review_sha256'] or [finding_state['feedback_id']] != actual['added_feedback_ids']:
                        raise ValueError('UNKNOWN_EXECUTION: recorded finding has no matching reachable review step')
                _checkpoint(directory, 'finding', finding_state)
                old_sha = finding_state['review_sha256']
                revision['expected_review_sha256'] = old_sha
                revision['addressed_feedback_ids'] = [*revision.get('addressed_feedback_ids', []), finding_state['feedback_id']]
                resolutions.append({'feedback_id': finding_state['feedback_id'], 'disposition': 'ADDRESSED',
                    'reason': 'Applied the submitted revision for this explicitly recorded Agent self-review finding.'})
            closed = _closed(review_root, old_sha, candidate_rel, delivery_rel)
            if closed is None:
                head = review.load_head(review_root)
                if head['review_sha256'] != old_sha:
                    raise ValueError('REVISION_CONFLICT: current review advanced before this command closed; saved candidate/export remain available')
                baseline_dir = core.inside(review_root, head['current']['version_path'])
                selected_replacements = {key: Path(path) for key, path in operation['resolved_inputs'].get('replacements', {}).items()}
                for key, decisions in frozen['compile_replacements'].items():
                    input_path = directory / (key + '-decisions.json'); _once(input_path, decisions)
                    output = directory / key
                    if not output.exists():
                        args = Namespace(command='compile-' + key, baseline_dir=baseline_dir, input=input_path, output_dir=output,
                            annotations=None, previous_decomposition=baseline_dir / 'source-item-decomposition.json' if (baseline_dir / 'source-item-decomposition.json').is_file() else None)
                        (artifact_tools.annotation_operation if key == 'annotations' else artifact_tools.decomposition_operation)(args)
                    if not (output / REPLACEMENTS[key][1]).is_file(): raise ValueError('UNKNOWN_EXECUTION: replacement compiler has no completed receipt')
                    validate_bundle(output, context['source']['sha256'], operation='compile-' + key, dependency_dirs=[baseline_dir])
                    selected_replacements[key] = output / REPLACEMENTS[key][0]
                    _checkpoint(directory, key, {'path': key, 'sha256': core.file_sha256(selected_replacements[key])})
                revision_path = directory / 'revision-input.json'; _once(revision_path, revision)
                if not candidate.exists():
                    review.revise_review(Namespace(review_dir=review_root, input=revision_path, output_dir=candidate, **selected_replacements))
                _checkpoint(directory, 'compile', _candidate(review_root, candidate, revision, context['source']['sha256']))
                for key, path in selected_replacements.items():
                    if core.read_json(candidate / REPLACEMENTS[key][0]) != core.read_json(path):
                        raise ValueError('Saved candidate omits a selected review replacement')
                if not delivery.exists():
                    artifact_tools.deliver(Namespace(baseline_dir=candidate, source=core.inside(root, context['source']['path']),
                        workspace=root, input=None, output_dir=delivery))
                _checkpoint(directory, 'deliver', _delivery(delivery, candidate, context['source']['sha256']))
                close_input = {'expected_review_sha256': old_sha, 'candidate_path': candidate_rel,
                               'delivery_path': delivery_rel, 'resolutions': resolutions}
                close_path = directory / 'close-input.json'; _once(close_path, close_input)
                review.close_review(Namespace(review_dir=review_root, input=close_path))
                closed = _closed(review_root, old_sha, candidate_rel, delivery_rel)
                if closed is None: raise ValueError('UNKNOWN_EXECUTION: close has no reachable completed review transition')
                _checkpoint(directory, 'close', {'review_sha256': closed['review_sha256']})
            application = {'schema_version': 'reqpilot-review-application/v1', 'operation_id': operation['operation_id'],
                'source_sha256': context['source']['sha256'], 'old_review_sha256': original_sha,
                'new_review_sha256': closed['review_sha256'], 'candidate_path': candidate_rel, 'delivery_path': delivery_rel,
                **({'finding_review_sha256': finding_state['review_sha256'], 'finding_feedback_id': finding_state['feedback_id']} if finding_state else {}),
                'origin': frozen['origin'], 'addressed_feedback_ids': revision.get('addressed_feedback_ids', []),
                'human_acceptance': 'NOT_CLAIMED'}
            _once(directory / 'review-application.json', application)
            core.commit_operation(root, operation['operation_id'], result={'status': 'REVIEW_ADVANCED',
                'diff': core.read_json(candidate / 'revision-diff.json'), **application})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            closed = _closed(review_root, old_sha, candidate_rel, delivery_rel)
            if closed is not None:
                return {'status': 'REVIEW_ADVANCED_REGISTRATION_PENDING', 'operation_state': 'SAVED_PENDING_RECOVERY',
                    'operation_id': operation['operation_id'], 'new_review_sha256': closed['review_sha256'],
                    'candidate': str(candidate), 'delivery': str(delivery), 'error': str(exc), 'local_artifact_preserved': True}
            unknown = isinstance(exc, OSError) or str(exc).startswith('UNKNOWN_EXECUTION:')
            conflict = review.load_head(review_root)['review_sha256'] != old_sha or str(exc).startswith('REVISION_CONFLICT:')
            message = 'REVISION_CONFLICT: ' + str(exc) if conflict and not str(exc).startswith('REVISION_CONFLICT:') else str(exc)
            if not unknown: core.fail_operation(root, operation['operation_id'], message, state='BLOCKED' if conflict else 'FAILED')
            return {'status': 'UNKNOWN' if unknown else 'FAILED', 'operation_state': 'STARTED' if unknown else 'BLOCKED' if conflict else 'FAILED',
                'operation_id': operation['operation_id'], 'error': message, 'candidate': str(candidate), 'delivery': str(delivery),
                'local_artifact_preserved': True, 'automatic_reexecution': 'NOT_PERFORMED'}
        host = {}
        try:
            current = core.workspace_status(root, refresh=False)
            if current['current_working'].get('review_sha256') == closed['review_sha256']:
                from task_workbench import sync_current_review
                host['sync'] = sync_current_review(root, current['revision'])
                host['verification'] = review.verify_review(Namespace(review_dir=review_root))
            else:
                host = {'sync': {'status': 'SKIPPED_SUPERSEDED'}, 'verification': {'status': 'HISTORICAL_SUCCESS_NOT_CURRENT'}}
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            host.setdefault('sync', {'status': 'NOT_SYNCHRONIZED'})
            host['verification'] = {'status': 'POST_COMMIT_CHECK_FAILED', 'error': str(exc), 'local_artifact_preserved': True}
        try:
            core.record_host_receipt(root, operation['operation_id'], host)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            host['host_receipt'] = {'status': 'SAVE_FAILED', 'error': str(exc)}
        return _present(root, operation, replayed=bool(prior), execution_state='RESUMED' if prior else 'EXECUTED', host=host)


__all__ = ['apply_review', 'lookup_review_command']
