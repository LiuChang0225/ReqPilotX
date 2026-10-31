"""Validation for Host-recorded PAIR selected-source evidence catalogs."""
from __future__ import annotations

import hashlib
import re

from reqpilotx.application.agent_workflow.source_item_analysis import ROLES, validate_knowledge_catalog

PAIR_EVIDENCE_SCHEMA = 'reqpilot-pair-evidence-registration/v1'
PAIR_FACT_ORIGINS = {'PAIR_SELECTED_SOURCE', 'PAIR_RETAINED_TOPIC'}


def _sha(value):
    return hashlib.sha256(__import__('json').dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


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
