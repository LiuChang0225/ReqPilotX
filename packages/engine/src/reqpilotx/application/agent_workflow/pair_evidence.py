"""Validation for Host-recorded PAIR selected-source evidence catalogs."""
from __future__ import annotations

import hashlib
import hmac
import json
import re

from reqpilotx.application.agent_workflow.source_item_analysis import ROLES, validate_knowledge_catalog

PAIR_EVIDENCE_SCHEMA = 'reqpilot-pair-evidence-registration/v1'
PAIR_ATTESTATION_SCHEMA = 'reqpilot-pair-analysis-attestation/v1'
PAIR_FACT_ORIGINS = {'PAIR_SELECTED_SOURCE', 'PAIR_RETAINED_TOPIC'}


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def sign_pair_attestation(payload, key):
    """Sign the exact Host analysis statement with the Host-owned shared key."""
    if not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None:
        raise ValueError('HOST_ATTESTATION_KEY_INVALID')
    body = {name: value for name, value in payload.items() if name != 'signature'}
    return hmac.new(bytes.fromhex(key), _sha(body).encode('ascii'), hashlib.sha256).hexdigest()


def verify_pair_attestation(value, key, *, expected=None, catalog=None, decisions=None):
    """Verify a Host statement; no workspace-contained value is a trust anchor."""
    allowed = {'schema', 'application_id', 'project_id', 'document_id', 'task_id',
               'workbench_ref', 'binding_sha256', 'mode', 'task_revision', 'command_id',
               'unit_ids', 'decisions', 'decisions_sha256', 'catalog_sha256',
               'ledger_sha256', 'consumed_fact_ids', 'checked_at', 'signature'}
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError('HOST_ATTESTATION_INVALID: fields')
    if value.get('schema') != PAIR_ATTESTATION_SCHEMA:
        raise ValueError('HOST_ATTESTATION_INVALID: schema')
    for field in ('application_id', 'project_id', 'task_id', 'mode',
                  'task_revision', 'command_id', 'checked_at'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError('HOST_ATTESTATION_INVALID: ' + field)
    if not isinstance(value.get('document_id'), str):
        raise ValueError('HOST_ATTESTATION_INVALID: document_id')
    for field in ('binding_sha256', 'decisions_sha256', 'catalog_sha256',
                  'ledger_sha256'):
        if re.fullmatch(r'[0-9a-f]{64}', str(value.get(field) or '')) is None:
            raise ValueError('HOST_ATTESTATION_INVALID: ' + field)
    if not isinstance(value.get('workbench_ref'), str):
        raise ValueError('HOST_ATTESTATION_INVALID: workbench_ref')
    if value.get('mode') not in {'NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED'}:
        raise ValueError('HOST_ATTESTATION_INVALID: mode')
    if value.get('unit_ids') is not None and (
            not isinstance(value['unit_ids'], list)
            or any(not isinstance(item, str) or not item for item in value['unit_ids'])
            or len(value['unit_ids']) != len(set(value['unit_ids']))):
        raise ValueError('HOST_ATTESTATION_INVALID: unit_ids')
    if not isinstance(value.get('decisions'), list) or not value['decisions']:
        raise ValueError('HOST_ATTESTATION_INVALID: decisions')
    if value.get('decisions_sha256') != _sha(value['decisions']):
        raise ValueError('HOST_ATTESTATION_INVALID: decisions digest')
    consumed = value.get('consumed_fact_ids')
    if (not isinstance(consumed, list)
            or any(not isinstance(item, str) or not item for item in consumed)
            or len(consumed) != len(set(consumed))):
        raise ValueError('HOST_ATTESTATION_INVALID: consumed facts')
    if not hmac.compare_digest(str(value.get('signature') or ''),
                               sign_pair_attestation(value, key)):
        raise ValueError('HOST_ATTESTATION_INVALID: signature')
    if expected:
        for field in ('application_id', 'project_id', 'document_id', 'task_id', 'binding_sha256',
                      'mode', 'task_revision', 'command_id', 'workbench_ref'):
            if field in expected and value.get(field) != expected.get(field):
                raise ValueError('HOST_ATTESTATION_INVALID: ' + field + ' mismatch')
        if 'unit_ids' in expected and value.get('unit_ids') != expected.get('unit_ids'):
            raise ValueError('HOST_ATTESTATION_INVALID: unit_ids mismatch')
    if catalog is not None:
        if value.get('catalog_sha256') != catalog.get('catalog_sha256'):
            raise ValueError('HOST_ATTESTATION_INVALID: catalog mismatch')
        if set(value.get('consumed_fact_ids', [])) != set((catalog.get('evidence_catalog') or {}).keys()):
            raise ValueError('HOST_ATTESTATION_INVALID: catalog facts were not consumed')
    if decisions is not None:
        if value.get('decisions') != decisions:
            raise ValueError('HOST_ATTESTATION_INVALID: decisions differ from the submitted payload')
        if value.get('decisions_sha256') != _sha(decisions):
            raise ValueError('HOST_ATTESTATION_INVALID: decisions digest')
    return value


def validate_pair_registration(registration, catalog):
    """Verify the frozen Host registration against every consumed fact."""
    allowed = {'schema_version', 'binding_sha256', 'mode', 'application_id',
               'project_id', 'catalog_sha256', 'documents', 'facts',
               'receipts', 'authority', 'human_acceptance', 'registration_sha256'}
    if not isinstance(registration, dict) or set(registration) != allowed:
        raise ValueError('PAIR evidence registration has unknown or missing fields')
    if registration.get('schema_version') != PAIR_EVIDENCE_SCHEMA:
        raise ValueError('PAIR evidence registration schema is unsupported')
    if registration.get('registration_sha256') != _sha({key: item for key, item in registration.items() if key != 'registration_sha256'}):
        raise ValueError('PAIR evidence registration integrity changed')
    if (registration.get('catalog_sha256') != catalog.get('catalog_sha256')
            or registration.get('documents') != catalog.get('documents', [])):
        raise ValueError('PAIR evidence registration and catalog documents differ')
    facts = catalog.get('evidence_catalog')
    if not isinstance(facts, dict):
        raise ValueError('PAIR evidence catalog has no fact map')
    if registration.get('facts') != [facts[key] for key in sorted(facts)]:
        raise ValueError('PAIR evidence registration facts differ from the catalog')
    for fact_id, fact in facts.items():
        text = fact.get('text')
        if (not isinstance(fact, dict) or fact.get('fact_id') != fact_id
                or fact.get('role') not in ROLES
                or fact.get('origin') not in PAIR_FACT_ORIGINS
                or not str(fact.get('kb_id') or '').strip()
                or not str(fact.get('document_id') or '').strip()
                or re.fullmatch(r'[0-9a-f]{64}', str(fact.get('source_sha256') or '')) is None
                or not isinstance(text, str) or not text.strip()
                or hashlib.sha256(text.encode()).hexdigest() != fact.get('text_sha256')
                or type(fact.get('eligible')) is not bool
                or type(fact.get('authority_verified')) is not bool
                or not fact.get('source_ref')
                or not str(fact.get('url') or '').startswith('/reqpilotx/knowledge/')):
            raise ValueError('A PAIR evidence fact failed source/citation integrity validation')
    receipts = registration.get('receipts')
    if not isinstance(receipts, list) or not receipts:
        raise ValueError('PAIR evidence registration has no consumed tool receipts')
    consumed = []
    for receipt in receipts:
        if (not isinstance(receipt, dict)
                or set(receipt) != {'tool', 'request_sha256', 'returned_at', 'fact_ids'}
                or receipt.get('tool') not in {'search', 'read', 'topic_read'}
                or re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('request_sha256') or '')) is None
                or not str(receipt.get('returned_at') or '').strip()
                or not isinstance(receipt.get('fact_ids'), list)
                or any(not isinstance(item, str) for item in receipt['fact_ids'])):
            raise ValueError('A PAIR evidence consumption receipt is invalid')
        consumed.extend(receipt['fact_ids'])
    if set(consumed) != set(facts):
        raise ValueError('PAIR evidence catalog contains facts that were not consumed by a logged task tool')
    if registration.get('authority') != 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE':
        raise ValueError('PAIR evidence authority boundary changed')
    if registration.get('human_acceptance') != 'NOT_CLAIMED':
        raise ValueError('PAIR evidence registration cannot claim human acceptance')
    return validate_knowledge_catalog(catalog)
