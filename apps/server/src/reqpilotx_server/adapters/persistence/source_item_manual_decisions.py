"""Append-only user working opinions under the common document/source fence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock

from reqpilotx_server import db
from reqpilotx_server.application.agent_workflow.source_item_manual_decisions import (
    compile_manual_decisions, rebind_manual_decisions, validate_manual_decisions, validate_manual_context,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

_LOCK = RLock()
TABLE = 'source_item_manual_decision_versions'


class ManualDecisionConflict(ValueError):
    pass


def _execute(conn, sql, params=()):
    # Application connections translate '?'; the publication hook also accepts
    # the raw psycopg connection already holding its transaction/source lock.
    if type(conn).__module__.startswith('psycopg'):
        sql = sql.replace('?', '%s')
    return conn.execute(sql, params)


def ensure_manual_schema(conn):
    if not isinstance(conn, sqlite3.Connection):
        return  # Production DDL is exclusively migration 0036.
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        project_id TEXT NOT NULL, sor_document_id TEXT NOT NULL, revision INTEGER NOT NULL,
        artifact_sha256 TEXT NOT NULL, artifact_json TEXT NOT NULL, command_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, operation TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(project_id,sor_document_id,revision), UNIQUE(project_id,sor_document_id,command_id))''')


def manual_storage_available(conn):
    if isinstance(conn, sqlite3.Connection):
        ensure_manual_schema(conn)
        return True
    row = _execute(conn, "SELECT to_regclass('source_item_manual_decision_versions') AS name").fetchone()
    return bool(row and row['name'])


def lock_manual_document(conn, project_id, document_id, source_sha256):
    if not isinstance(conn, sqlite3.Connection):
        _execute(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(?, 0))',
            ('\x1f'.join(('agent-structure-loop/v1', project_id, document_id, source_sha256)),))
        row = _execute(conn, 'SELECT source_sha256 FROM sor_documents WHERE id = ? AND project_id = ? FOR UPDATE',
            (document_id, project_id)).fetchone()
    else:
        _execute(conn, 'UPDATE sor_documents SET source_sha256 = source_sha256 WHERE id = ? AND project_id = ?',
            (document_id, project_id))
        row = _execute(conn, 'SELECT source_sha256 FROM sor_documents WHERE id = ? AND project_id = ?',
            (document_id, project_id)).fetchone()
    if row is None or row['source_sha256'] != source_sha256:
        raise ManualDecisionConflict('MANUAL_SOURCE_CHANGED')


def current_manual_decisions_in_transaction(conn, project_id, document_id):
    if not manual_storage_available(conn):
        return None
    row = _execute(conn, f'SELECT artifact_json FROM {TABLE} WHERE project_id = ? AND sor_document_id = ? ORDER BY revision DESC LIMIT 1',
        (project_id, document_id)).fetchone()
    if row is None:
        return None
    return validate_manual_decisions(json.loads(row['artifact_json']))


def current_manual_decisions(project_id, document_id):
    with db.get_conn() as conn:
        return current_manual_decisions_in_transaction(conn, project_id, document_id)


def _existing_command(conn, project_id, document_id, command_id, request_sha256):
    row = _execute(conn, f'SELECT request_sha256 FROM {TABLE} WHERE project_id = ? AND sor_document_id = ? AND command_id = ?',
        (project_id, document_id, command_id)).fetchone()
    if row and row['request_sha256'] != request_sha256:
        raise ManualDecisionConflict('MANUAL_COMMAND_REUSED_WITH_DIFFERENT_INPUT')
    return row is not None


def _append(conn, project_id, document_id, artifact, *, command_id, request_sha256):
    _execute(conn, f'''INSERT INTO {TABLE} (project_id,sor_document_id,revision,artifact_sha256,artifact_json,
        command_id,request_sha256,operation,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
        (project_id, document_id, artifact['revision'], artifact['artifact_sha256'],
         json.dumps(artifact, ensure_ascii=False, sort_keys=True), command_id, request_sha256,
         artifact['operation'], datetime.now(timezone.utc).isoformat()))


def save_manual_decisions(project_id, document_id, *, source_sha256, expected_baseline_sha256,
        expected_response_revision, command_id, decisions, context_resolver):
    request_sha = canonical_json_sha256({'source_sha256': source_sha256, 'baseline_sha256': expected_baseline_sha256,
        'expected_response_revision': expected_response_revision, 'decisions': decisions})
    with _LOCK, db.get_conn() as conn:
        if not manual_storage_available(conn):
            raise ManualDecisionConflict('MANUAL_STORAGE_MIGRATION_REQUIRED')
        lock_manual_document(conn, project_id, document_id, source_sha256)
        stored = current_manual_decisions_in_transaction(conn, project_id, document_id)
        if _existing_command(conn, project_id, document_id, 'USER/' + command_id, request_sha):
            return stored
        context = validate_manual_context(context_resolver())
        if context['source_sha256'] != source_sha256 or context['baseline_sha256'] != expected_baseline_sha256:
            raise ManualDecisionConflict('MANUAL_BASELINE_CHANGED')
        if (stored or {}).get('revision', 0) != expected_response_revision:
            raise ManualDecisionConflict('MANUAL_RESPONSE_CHANGED')
        choices = []
        for decision in decisions:
            unit = context['units'].get(decision.get('unit_id'))
            if unit is None or decision.get('expected_unit_context_sha256') != unit['unit_context_sha256']:
                raise ManualDecisionConflict('MANUAL_UNIT_CONTEXT_CHANGED')
            choices.append({k: v for k, v in decision.items() if k != 'expected_unit_context_sha256'})
        artifact = compile_manual_decisions(context, choices, previous=stored, origin='USER_INPUT',
            decided_at=datetime.now(timezone.utc).isoformat())
        _append(conn, project_id, document_id, artifact, command_id='USER/' + command_id, request_sha256=request_sha)
        return artifact


def rebind_manual_decisions_in_transaction(conn, *, project_id, document_id, previous_context,
        current_context, publication_id):
    """Called by a trusted host under the shared source lock, in publication tx.

    Agent outputs are deliberately not accepted as a parameter. Always use the
    latest user row, including edits made while a model task was running.
    """
    if not manual_storage_available(conn):
        return {'status': 'NO_MANUAL_STORAGE'}
    stored = current_manual_decisions_in_transaction(conn, project_id, document_id)
    if stored is None:
        return {'status': 'NO_MANUAL_DECISIONS'}
    validate_manual_context(current_context)
    request_sha = canonical_json_sha256({'previous': previous_context, 'current': current_context})
    if _existing_command(conn, project_id, document_id, 'PUBLICATION/' + publication_id, request_sha):
        return {'status': 'ALREADY_REBOUND', 'response_revision': stored['revision']}
    artifact = rebind_manual_decisions(stored, current_context, previous_context=previous_context,
        require_previous_context=True)
    if artifact['artifact_sha256'] == stored['artifact_sha256']:
        return {'status': 'UNCHANGED', 'response_revision': stored['revision']}
    _append(conn, project_id, document_id, artifact, command_id='PUBLICATION/' + publication_id, request_sha256=request_sha)
    return {'status': 'REBOUND', 'response_revision': artifact['revision'], 'artifact_sha256': artifact['artifact_sha256'],
        'stale_unit_ids': [uid for uid, record in artifact['records'].items() if record['state'] == 'STALE'],
        'current_unit_ids': [uid for uid, record in artifact['records'].items() if record['state'] == 'CURRENT']}
