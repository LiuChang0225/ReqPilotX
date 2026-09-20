from app.application import requirements
from app.application.extraction import enrichment_workspace, review_sessions

from ._factory import RouteSpec, build_router

DOCUMENT = "/api/projects/{project_id}/sor-documents/{document_id}"
REVIEW = f"{DOCUMENT}/extraction/llm-reviews/{{session_id}}"
REQUIREMENT_SET = f"{DOCUMENT}/requirement-sets/{{requirement_set_id}}"
PROJECT_REQUIREMENTS = "/api/projects/{project_id}/requirements"
SPECS = (
    RouteSpec(
        REVIEW,
        review_sessions.get_llm_requirement_review_session,
        "GET",
    ),
    RouteSpec(
        REVIEW,
        review_sessions.update_llm_requirement_review_session,
        "PUT",
    ),
    RouteSpec(
        f"{REVIEW}/confirmation",
        review_sessions.update_llm_requirement_review_confirmation,
        "PATCH",
    ),
    RouteSpec(
        f"{REVIEW}/atomization-runs",
        enrichment_workspace.create_requirement_atomization_run,
        "POST",
    ),
    RouteSpec(
        f"{REVIEW}/refinement-suggestions",
        enrichment_workspace.create_requirement_refinement_suggestion,
        "POST",
    ),
    RouteSpec(
        f"{REVIEW}/refinement-suggestions/{{suggestion_id}}",
        enrichment_workspace.decide_requirement_refinement_suggestion,
        "PATCH",
    ),
    RouteSpec(
        f"{REVIEW}/publication-preview",
        enrichment_workspace.create_requirement_publication_preview,
        "POST",
    ),
    RouteSpec(
        f"{REVIEW}/publish-draft",
        enrichment_workspace.publish_requirement_enrichment_draft,
        "POST",
    ),
    RouteSpec(
        f"{REVIEW}/enrichment",
        enrichment_workspace.get_requirement_enrichment_state,
        "GET",
    ),
    RouteSpec(
        f"{DOCUMENT}/requirement-sets",
        requirements.list_document_requirement_sets,
        "GET",
    ),
    RouteSpec(
        f"{REQUIREMENT_SET}/activate",
        requirements.activate_document_requirement_set,
        "POST",
    ),
    RouteSpec(
        f"{REQUIREMENT_SET}/restore",
        requirements.restore_document_requirement_set,
        "POST",
    ),
    RouteSpec(
        f"{REQUIREMENT_SET}/derive",
        requirements.derive_document_requirement_set,
        "POST",
    ),
    RouteSpec(
        f"{DOCUMENT}/workspace",
        requirements.get_document_workspace,
        "GET",
    ),
    RouteSpec(
        f"{DOCUMENT}/lock-readiness",
        requirements.get_document_lock_readiness,
        "GET",
    ),
    RouteSpec(
        f"{DOCUMENT}/lock-extraction",
        requirements.lock_document_extraction,
        "POST",
    ),
    RouteSpec(PROJECT_REQUIREMENTS, requirements.list_requirements, "GET"),
    RouteSpec(
        "/api/requirements/{requirement_id}/review-logs",
        requirements.get_review_logs,
        "GET",
    ),
)


def build():
    return build_router(tag="review", specs=SPECS)


__all__ = ["build"]
