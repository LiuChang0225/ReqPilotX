from __future__ import annotations

from reqpilotx_server.application import assessment, exports, knowledge, runtime
from reqpilotx_server.application.agent_workflow import (
    document_map_jobs,
    source_item_agent_jobs,
    source_item_classification_jobs,
    source_item_image_jobs,
    source_item_direct_revision,
    source_item_loop_jobs,
    source_document_loop_jobs,
    native_parent_provision_jobs,
    source_requirement_jobs,
    native_structure_baseline_jobs,
    native_structure_handoff_jobs,
    structure_proposal_jobs,
)
from reqpilotx_server.application.extraction import (
    document_jobs,
    restoration,
    structure_optimization_jobs,
)
from reqpilotx_server.infrastructure.runtime_container import RuntimeContainer
from reqpilotx_server.worker.runtime import JobHandler


def build_job_handlers(container: RuntimeContainer) -> dict[str, JobHandler]:
    """Build the production worker registry without starting the HTTP app."""

    runtime.configure_worker_runtime(container)
    return {
        "agent.source-item.extract-loop": source_document_loop_jobs.run_durable_source_document_loop,
        "agent.source-item.loop": source_item_loop_jobs.run_durable_source_item_loop,
        "agent.source-item.direct-revision": source_item_direct_revision.run_direct_revision,
        "agent.source-item.images": source_item_image_jobs.run_durable_source_item_images,
        "agent.source-item.classify": source_item_classification_jobs.run_durable_source_item_classification_review,
        "agent.source-item.decompose": source_item_agent_jobs.run_durable_source_item_decomposition,
        "agent.source-item.analyze": source_item_agent_jobs.run_durable_source_item_analysis,
        "agent.document-map": document_map_jobs.run_durable_document_map,
        "agent.structure.propose": (
            structure_proposal_jobs.run_durable_agent_structure_proposal
        ),
        "agent.structure.handoff": (
            native_structure_handoff_jobs.run_durable_native_structure_handoff
        ),
        "agent.native-baseline.accept": (
            native_structure_baseline_jobs.run_durable_native_structure_baseline_acceptance
        ),
        "agent.parent-provision.recognize": (
            native_parent_provision_jobs.run_durable_native_parent_provision_recognition
        ),
        "agent.source-requirement.baseline": (
            source_requirement_jobs.run_durable_source_requirement_baseline
        ),
        "agent.source-item-text.baseline": (
            source_requirement_jobs.run_durable_source_item_text_baseline
        ),
        "agent.source-item.scope-revision": (
            source_requirement_jobs.run_durable_source_item_scope_revision
        ),
        "document.extract": document_jobs.run_durable_document_extraction,
        "structure.optimize": (
            structure_optimization_jobs.run_durable_structure_optimization
        ),
        "document.restore": restoration.run_durable_document_restoration,
        "document.assess": assessment.run_durable_document_assessment,
        "knowledge.sync": knowledge.run_durable_knowledge_sync,
        "project.export": exports.run_durable_project_export,
    }


__all__ = ["build_job_handlers"]
