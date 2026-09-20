from app.application.agent_workflow import source_item_delivery_bridge as delivery
from app.application.agent_workflow import source_item_review_context as review_context
from app.application.agent_workflow import source_item_loop_jobs as loop
from app.application.agent_workflow import source_document_loop_jobs as source_loop
from app.application.agent_workflow import native_loop_capabilities as capabilities
from app.application.agent_workflow import source_item_manual_decision_api as engineer
from app.application.agent_workflow import native_loop_finalization as finalization
from app.application.agent_workflow import source_item_delivery_versions as versions
from app.application.agent_workflow import plugin_workbench as plugin
from app.application.agent_workflow import plugin_workbench_reader as plugin_reader
from ._factory import RouteSpec, build_router

ROOT = "/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery"
SPECS = (
    RouteSpec(f"{ROOT}/plugin-workbench", plugin.sync_plugin_draft, "POST"),
    RouteSpec(f"{ROOT}/plugin-workbench/latest", plugin.get_latest_plugin_workbench, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/sessions", plugin.list_plugin_workbench_sessions, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}", plugin.get_plugin_workbench, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/snapshot", plugin.get_plugin_snapshot, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/reader", plugin_reader.get_plugin_reader, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/records/{{source_record_id}}", plugin_reader.get_plugin_record, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/stages/{{stage}}", plugin.get_plugin_stage, "GET"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/confirm-map", plugin.confirm_plugin_map, "POST"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/feedback", plugin.post_plugin_feedback, "POST"),
    RouteSpec(f"{ROOT}/plugin-workbench/{{session_id}}/export", plugin.export_plugin_draft, "GET"),
    RouteSpec(f"{ROOT}/versions/preview", versions.preview_delivery_version, "GET"),
    RouteSpec(f"{ROOT}/versions", versions.list_delivery_versions, "GET"),
    RouteSpec(f"{ROOT}/versions", versions.save_delivery_version, "POST"),
    RouteSpec(f"{ROOT}/versions/{{revision}}", versions.get_delivery_version, "GET"),
    RouteSpec(f"{ROOT}/versions/{{revision}}/export", versions.download_delivery_version, "GET"),
    RouteSpec(f"{ROOT}/engineer-responses", engineer.get_engineer_responses, "GET"),
    RouteSpec(f"{ROOT}/engineer-responses", engineer.post_engineer_responses, "POST"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/capability", capabilities.upgrade_native_loop_capability, "POST"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/finalize", finalization.finalize_source_item_loop, "POST", status_code=202),
    RouteSpec(f"{ROOT}/extraction-loop", source_loop.start_source_document_loop, "POST", status_code=202),
    RouteSpec(f"{ROOT}/extraction-loop/latest", source_loop.get_latest_source_document_loop, "GET"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}", source_loop.get_source_document_loop, "GET"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/stages/{{stage}}", source_loop.get_source_document_stage, "GET"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/confirm-map", source_loop.confirm_source_document_map, "POST"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/events", source_loop.get_source_document_loop_events, "GET"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/steer", source_loop.steer_source_document_loop, "POST"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/interrupt", source_loop.interrupt_source_document_loop, "POST"),
    RouteSpec(f"{ROOT}/extraction-loop/{{task_id}}/resume", source_loop.resume_source_document_loop, "POST", status_code=202),
    RouteSpec(f"{ROOT}/loop", loop.start_source_item_loop, "POST", status_code=202),
    RouteSpec(f"{ROOT}/loop/latest", loop.get_latest_source_item_loop, "GET"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}", loop.get_source_item_loop, "GET"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/events", loop.get_source_item_loop_events, "GET"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/steer", loop.steer_source_item_loop, "POST"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/interrupt", loop.interrupt_source_item_loop, "POST"),
    RouteSpec(f"{ROOT}/loop/{{task_id}}/resume", loop.resume_source_item_loop, "POST", status_code=202),
    RouteSpec(f"{ROOT}/jobs/{{job_id}}", delivery.get_document_agent_job, "GET"),
    RouteSpec(ROOT, delivery.get_source_item_delivery, "GET"),
    RouteSpec(f"{ROOT}/state", delivery.get_source_item_delivery_state, "GET"),
    RouteSpec(f"{ROOT}/review-context", review_context.get_source_item_review_context, "GET"),
    RouteSpec(f"{ROOT}/export", delivery.export_source_item_delivery, "GET"),
)


def build():
    from .source_item_stream import stream_source_item_events
    router = build_router(tag="source-item-delivery", specs=SPECS)
    for mode in ("loop", "extraction-loop"):
        router.add_api_route(f"{ROOT}/{mode}/{{task_id}}/stream", stream_source_item_events, methods=["GET"])
    return router
