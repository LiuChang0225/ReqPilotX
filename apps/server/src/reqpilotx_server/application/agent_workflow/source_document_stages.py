"""Source-bound intermediate results, independent of final graph publication.

The compatibility index contains only the current Map and its matching Items.
Valid out-of-order outputs are retained as immutable stage versions, not failures.
"""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import fcntl
import hashlib
import json
import os

from reqpilotx_server.application.agent_workflow import source_requirement_baseline as core

STAGES = {'compile-map': ('MAP', 'document-map.json'),
          'compile-items': ('ITEMS', 'source-requirement-baseline.json'),
          'compile-images': ('ITEMS', 'source-requirement-baseline.json')}
_FILENAMES = dict(STAGES.values())
_POINTER_FIELDS = {'stage', 'artifact_sha256', 'source_sha256', 'count', 'map_sha256',
                   'stage_version_id', 'current', 'pairing_status', 'retention_reason'}


@contextmanager
def _stage_lock(root):
    # Distinct from task.lock: the native host already holds that lock at publish.
    # Only short local registration/read operations hold this lock, never a model.
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'stage.lock').open('a+b') as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _version_id(pointer):
    return core.canonical_json_sha256({key: pointer.get(key) for key in
        ('stage', 'source_sha256', 'artifact_sha256', 'file_sha256')} |
        {'table_previews_sha256': pointer.get('table_previews', {}).get('sha256')})


def _paired(mapping, items):
    return bool(mapping and items and mapping.get('source_sha256')
        and mapping.get('source_sha256') == items.get('source_sha256')
        and mapping.get('artifact_sha256') == mapping.get('map_sha256')
        and mapping.get('artifact_sha256') == items.get('map_sha256'))


def _current(raw, source_sha256=None):
    result = {stage: {'stage': stage, **raw[stage]} for stage in ('MAP', 'ITEMS')
        if isinstance(raw.get(stage), dict)
        and (not source_sha256 or raw[stage].get('source_sha256') == source_sha256)}
    if not _paired(result.get('MAP'), result.get('ITEMS')):
        result.pop('ITEMS', None)
    return result


def _status(pointer, current):
    pointer = dict(pointer)
    selected = current.get(pointer['stage'])
    is_current = bool(selected and _version_id(selected) == _version_id(pointer))
    if is_current:
        status = 'PAIRED' if pointer['stage'] == 'ITEMS' else 'MAP_CURRENT'
        reason = None
    elif pointer['stage'] == 'MAP':
        status, reason = 'RETAINED', 'SUPERSEDED'
    elif not current.get('MAP'):
        status, reason = 'RETAINED', 'MAP_NOT_REGISTERED'
    elif not _paired(current['MAP'], pointer):
        status, reason = 'RETAINED', 'MAP_MISMATCH'
    else:
        status = 'RETAINED'
        reason = 'SUPERSEDED' if current.get('ITEMS') else 'UNSELECTED'
    return {**pointer, 'stage_version_id': _version_id(pointer), 'current': is_current,
            'pairing_status': status, 'retention_reason': reason}


def _state(root):
    raw = _read_json(root / 'stage-results.json', {})
    history = _read_json(root / 'stage-history.json', {})
    task = _read_json(root / 'task.json', {})
    source_sha256 = task.get('source_sha256') or history.get('source_sha256')
    current = _current(raw, source_sha256)
    entries = {_version_id(p): p for p in history.get('entries', [])}
    # Older indexes have no history. Keep mismatched pointers visible as retained.
    for stage in ('MAP', 'ITEMS'):
        if isinstance(raw.get(stage), dict):
            pointer = {'stage': stage, **raw[stage]}
            entries.setdefault(_version_id(pointer), pointer)
    return current, list(entries.values()), source_sha256


def stage_inventory(root):
    """One consistent projection; current and retained never imply acceptance."""
    root = Path(root)
    with _stage_lock(root):
        current, entries, _ = _state(root)
        projected = [_status(p, current) for p in entries]
        return {'current': {stage: _status({'stage': stage, **p}, current) for stage, p in current.items()},
                'retained': [p for p in projected if not p['current']]}


def public_stage(pointer):
    return {key: value for key, value in pointer.items() if key in _POINTER_FIELDS}


def stage_index(root):
    return stage_inventory(root)['current']


def _safe_file(workspace, relative):
    path = (workspace / relative).resolve(strict=True)
    if not path.is_relative_to(workspace):
        raise ValueError('Stage artifact escaped its workspace')
    return path


def _validate_raw(stage, raw):
    cls = core.DocumentMapArtifactV1 if stage == 'MAP' else core.SourceRequirementBaselineArtifactV1
    return cls.model_validate_json(raw)


def _load_pointer(workspace, pointer):
    raw = _safe_file(workspace, pointer['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['file_sha256']:
        raise ValueError('Stage artifact changed after publication')
    parsed = _validate_raw(pointer['stage'], raw)
    map_sha256 = parsed.artifact_sha256 if pointer['stage'] == 'MAP' else parsed.document_map_artifact_sha256
    if (parsed.source_sha256, parsed.artifact_sha256, map_sha256) != (
            pointer['source_sha256'], pointer['artifact_sha256'], pointer['map_sha256']):
        raise ValueError('Stage pointer does not match its artifact')
    tables_raw = None
    if pointer.get('table_previews'):
        tables = pointer['table_previews']
        tables_raw = _safe_file(workspace, tables['path']).read_bytes()
        if hashlib.sha256(tables_raw).hexdigest() != tables['sha256']:
            raise ValueError('Stage tables changed after publication')
    return raw, tables_raw


def _retain(workspace, pointer, raw, tables_raw):
    """Atomically keep the exact bytes before selecting a new current stage."""
    from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import _write_json
    version_id = _version_id(pointer)
    store = workspace / '.stage-artifacts'
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = store.resolve(strict=True)
    if not store.is_relative_to(workspace):
        raise ValueError('Stage versions escaped task workspace')
    target = store / version_id
    filename = _FILENAMES[pointer['stage']]
    retained = {key: value for key, value in pointer.items()
                if key not in {'current', 'pairing_status', 'retention_reason'}}
    retained.update(stage_version_id=version_id,
        producer_path=pointer.get('producer_path', pointer['path']),
        path=str((target / filename).relative_to(workspace)))
    if tables_raw is not None:
        retained['table_previews'] = {'path': str((target / 'table-previews.json').relative_to(workspace)),
            'sha256': hashlib.sha256(tables_raw).hexdigest()}
    if not target.exists():
        with TemporaryDirectory(prefix='.pending-', dir=store) as temporary:
            pending = Path(temporary)
            for name, data in ((filename, raw), ('table-previews.json', tables_raw)):
                if data is None:
                    continue
                with (pending / name).open('xb') as stream:
                    os.chmod(stream.name, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            _write_json(pending / 'stage.json', retained)
            pending.rename(target)
    _load_pointer(workspace, retained)
    return retained


def publish_stage(task_root, workspace, operation, arguments, source_sha256):
    if operation not in STAGES or not arguments.get('output_dir'):
        return None
    stage, filename = STAGES[operation]
    workspace = Path(workspace).resolve(strict=True)
    path = _safe_file(workspace, Path(arguments['output_dir']) / filename)
    raw = path.read_bytes()
    parsed = _validate_raw(stage, raw)
    if parsed.source_sha256 != source_sha256:
        raise ValueError('Stage artifact belongs to another original')
    pointer = {'stage': stage, 'artifact_sha256': parsed.artifact_sha256,
        'source_sha256': source_sha256, 'path': str(path.relative_to(workspace)),
        'file_sha256': hashlib.sha256(raw).hexdigest(),
        'count': len(parsed.nodes) if stage == 'MAP' else len(parsed.records),
        'map_sha256': parsed.artifact_sha256 if stage == 'MAP' else parsed.document_map_artifact_sha256}
    tables_raw = None
    if (path.parent / 'table-previews.json').exists():
        tables = _safe_file(workspace, path.parent / 'table-previews.json')
        tables_raw = tables.read_bytes()
        pointer['table_previews'] = {'path': str(tables.relative_to(workspace)),
            'sha256': hashlib.sha256(tables_raw).hexdigest()}
    from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import _write_json
    root = Path(task_root)
    with _stage_lock(root):
        current, entries, pinned_source = _state(root)
        known_sources = {p.get('source_sha256') for p in entries if p.get('source_sha256')}
        if (pinned_source and pinned_source != source_sha256) or (not pinned_source and known_sources - {source_sha256}):
            raise ValueError('Stage task is bound to another original')
        history = {}
        for old in entries:
            if not old.get('stage_version_id'):
                try:
                    old = _retain(workspace, old, *_load_pointer(workspace, old))
                except (ValueError, OSError, KeyError):
                    # An already broken legacy pointer remains discoverable; do
                    # not discard a new valid local output because of that damage.
                    pass
            history[_version_id(old)] = old
        current = {key: history.get(_version_id(value), value) for key, value in current.items()}
        pointer = _retain(workspace, pointer, raw, tables_raw)
        history[_version_id(pointer)] = pointer
        if stage == 'MAP':
            current['MAP'] = pointer
            if not _paired(pointer, current.get('ITEMS')):
                current.pop('ITEMS', None)
                candidates = [p for p in history.values() if p['stage'] == 'ITEMS' and _paired(pointer, p)]
                # Multiple matching candidates require an explicit Items publish;
                # neither filesystem mtime nor an arbitrary hash picks a winner.
                if len(candidates) == 1:
                    try:
                        _load_pointer(workspace, candidates[0])
                    except (ValueError, OSError, KeyError):
                        pass
                    else:
                        current['ITEMS'] = candidates[0]
        elif _paired(current.get('MAP'), pointer):
            current['ITEMS'] = pointer
        _write_json(root / 'stage-history.json', {'source_sha256': source_sha256, 'entries': list(history.values())})
        # The old two-key file remains compatible with evaluators and maintenance.
        _write_json(root / 'stage-results.json', current)
        return _status(pointer, current)


def read_stage(root, stage, *, artifact_sha256=None, stage_version_id=None):
    if stage not in _FILENAMES:
        raise ValueError('Unknown source document stage')
    inventory = stage_inventory(root)
    current = inventory['current']
    if artifact_sha256 is None and stage_version_id is None:
        pointer = current.get(stage)
    else:
        candidates = [p for p in [*current.values(), *inventory['retained']]
            if p['stage'] == stage
            and (artifact_sha256 is None or p['artifact_sha256'] == artifact_sha256)
            and (stage_version_id is None or p['stage_version_id'] == stage_version_id)]
        if len(candidates) > 1:
            raise ValueError('Multiple retained versions match; select a stage_version_id')
        pointer = candidates[0] if candidates else None
    if pointer is None:
        raise ValueError('This stage has no matching current artifact; retained versions may be available')
    workspace = (Path(root) / 'workspace').resolve(strict=True)
    raw, tables_raw = _load_pointer(workspace, pointer)
    if pointer['current'] and stage == 'ITEMS':
        _load_pointer(workspace, current['MAP'])
    artifact = json.loads(raw)
    if stage == 'ITEMS' and tables_raw is not None:
        previews = json.loads(tables_raw)
        for record in artifact['records']:
            if record.get('table_fragments'):
                wanted = {i for part in record['table_fragments'] for i in part['cell_ids']}
                wanted.update(record.get('shared_header_cell_ids', []))
                wanted.update(record.get('shared_table_cell_ids', []))
                cells = [cell for fragment in previews[record['logical_table_scope_id']]['fragments']
                         for cell in fragment['cells'] if cell['cell_id'] in wanted]
                record['source_text'] = '\n'.join(cell['text'] for cell in cells)
    return pointer, artifact


def analysis_policy(catalog, mode='AUTO'):
    available = bool(catalog.get('documents') or catalog.get('evidence_catalog') or catalog.get('evidence'))
    return {'mode': mode, 'status': 'AVAILABLE' if available and mode != 'SKIP' else 'SKIPPED',
        'reason': '' if available and mode != 'SKIP' else ('USER_SKIPPED' if mode == 'SKIP' else 'NO_INTERNAL_EVIDENCE')}
