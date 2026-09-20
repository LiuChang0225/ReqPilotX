"""Application knowledge catalogs backed by persisted project source records.

Pure capture is re-exported for existing callers. The shared implementation also
serves portable hosts; only this module selects application-owned project files.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.application.knowledge_evidence_capture import (
    KnowledgeFactsError,
    _digest,
    capture_knowledge_facts,
)


def build_project_knowledge_catalog(project_id: str, *, document_ids=None,
                                    as_of: date | None = None) -> dict:
    """Resolve actual uploaded files through the application's source store."""
    from app import db
    from app.application.projects import _require_project
    from app.application.source_runtime import resolve_source_record_path

    project = _require_project(project_id)
    records = db.list_knowledge_documents(project_id)
    if document_ids is not None:
        selected = set(document_ids)
        available = {r["id"] for r in records}
        if selected - available:
            raise KnowledgeFactsError("Selected knowledge document is outside this project")
        records = [r for r in records if r["id"] in selected]
    catalog: dict[str, Any] = {"schema_version": "project-knowledge-facts/v1",
        "project_id": project_id,
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "evidence_catalog": {}, "documents": [], "gaps": [],
        "selection": "EXPLICIT_DOCUMENTS" if document_ids is not None else "ALL_PROJECT_KNOWLEDGE"}
    for record in sorted(records, key=lambda r: r["id"]):
        captured = capture_knowledge_facts(record, resolve_source_record_path(record),
                                           project_id=project_id, as_of=as_of)
        catalog["documents"].append(captured["document"])
        catalog["evidence_catalog"].update(captured["facts"])
        catalog["gaps"].extend({"document_id": record["id"], **gap} for gap in captured["gaps"])
    catalog["catalog_sha256"] = _digest(catalog)
    return catalog


def get_project_knowledge_facts(project_id: str) -> dict:
    """Read-only API; embedding/index availability does not govern source access."""
    from app.application.errors import ApplicationError
    try:
        return build_project_knowledge_catalog(project_id)
    except KnowledgeFactsError as exc:
        raise ApplicationError(status_code=409, detail=str(exc)) from exc
