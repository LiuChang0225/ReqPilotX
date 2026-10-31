"""A persisted initial graph made by the portable source compilers.

This is a transport envelope over the existing Document Map / SourceItem graph,
not a second semantic model and not a D2-stage receipt. The original native
reference is retained byte-for-byte as JSON data so its typed Map hash and real
SourceIR handles survive application adoption.
"""

from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
import json
import re
import threading
from typing import Any, Mapping

from reqpilotx_server.application.agent_workflow import source_requirement_baseline as core
from reqpilotx_server.application.agent_workflow.source_item_delivery import build_delivery_snapshot


SCHEMA = "reqpilotx-native-source-graph/v1"
EXTRACTION_JOB_KIND = "agent.source-item.extract-loop"
_VALIDATED_GRAPHS = OrderedDict()
_VALIDATED_GRAPHS_LOCK = threading.RLock()
_SOURCE_READ_BUNDLES = OrderedDict()
_SOURCE_READ_BUNDLES_LOCK = threading.RLock()


class _LoadedNativeGraph(dict):
    """Read-side proof only; never add unhashed fields to the stored envelope."""

    published_snapshot = None
    publication = None


def _verify_published_snapshot(value, snapshot, published_snapshot=None):
    expected = value["matrix_snapshot_sha256"]
    supplied = value.get("matrix_snapshot")
    if supplied is None:
        supplied = published_snapshot or getattr(value, "published_snapshot", None)
    if supplied is None:
        if core.canonical_json_sha256(snapshot) != expected:
            raise ValueError(
                "Initial graph needs its exact published Matrix snapshot for legacy ordering verification"
            )
        return
    from reqpilotx_server.application.agent_workflow.delivery_snapshot_compatibility import (
        verify_published_delivery_snapshot,
    )

    verify_published_delivery_snapshot(
        document_map=value.get("document_map"),
        baseline=value.get("baseline"),
        published_snapshot=supplied,
        current_snapshot=snapshot,
        expected_snapshot_sha256=expected,
        annotations=value.get("annotations"),
    )


def _validate_map_history(value, final_map):
    history = value.get("map_history", [])
    if not isinstance(history, list):
        raise ValueError("Native Map history must be an ordered array")
    supplied_initial = (
        "initial_document_map" in value or "initial_native_reference" in value
    )
    if (history or supplied_initial) and not {
        "initial_document_map",
        "initial_native_reference",
    } <= set(value):
        raise ValueError(
            "Map history requires its actual initial Map and native reference"
        )
    initial = core.DocumentMapArtifactV1.model_validate(
        value.get("initial_document_map", value["document_map"])
    )
    initial_reference = value.get("initial_native_reference", value["native_reference"])
    if (
        core.compile_document_map_artifact(
            deepcopy(initial_reference), revision=initial.revision
        )
        != initial
    ):
        raise ValueError(
            "Initial native reference does not compile to the actual initial Map"
        )
    from reqpilotx_server.application.agent_workflow.document_map_revision import (
        compile_document_map_revision,
    )

    previous, final_reference = initial, initial_reference
    for index, entry in enumerate(history):
        if not isinstance(entry, dict) or not {
            "candidate_map",
            "document_map",
            "native_reference",
            "receipt",
        } <= set(entry):
            raise ValueError(
                f"Map history step {index} lacks its candidate and compiler outputs"
            )
        compiled = compile_document_map_revision(previous, entry["candidate_map"])
        if any(
            entry[key] != compiled[key]
            for key in ("document_map", "native_reference", "receipt")
        ):
            raise ValueError(
                f"Map history step {index} differs from shared compiler replay"
            )
        previous = core.DocumentMapArtifactV1.model_validate(compiled["document_map"])
        final_reference = compiled["native_reference"]
    if previous != final_map or final_reference != value["native_reference"]:
        raise ValueError(
            "Final Map and native reference do not match the verified Map history tail"
        )
    return initial


def _final_scope_coverage(value, document_map, baseline):
    # Independent image scopes are not required for every source image: an
    # image may be a bound part of an existing mixed/text/table SourceItem.
    scopes = {f"text:{n.node_id}" for n in document_map.nodes if n.source_content}
    scopes.update(t.scope_id for t in document_map.logical_tables)
    expected = scopes - set(baseline.covered_scope_ids)
    final = value.get("final_scope_coverage")
    if final is None:
        if value.get("map_history"):
            raise ValueError(
                "Map history requires final_scope_coverage for the final Map"
            )
        legacy = value.get("item_compilation_receipt", {}).get(
            "unprocessed_scope_ids", []
        )
        return {"unprocessed_scope_ids": sorted(expected | set(legacy))}
    unprocessed = (
        final.get("unprocessed_scope_ids") if isinstance(final, dict) else None
    )
    if (
        not isinstance(unprocessed, list)
        or any(not isinstance(scope, str) for scope in unprocessed)
        or len(set(unprocessed)) != len(unprocessed)
        or set(unprocessed) != expected
    ):
        raise ValueError(
            "Final scope coverage differs from the current Map and baseline"
        )
    return {"unprocessed_scope_ids": sorted(unprocessed)}


def _source_page_returns(value):
    entries = value.get("host_verification", {}).get("source_page_returns", [])
    page_count = value["capture_receipt"].get("source", {}).get("page_count", 0)
    if not isinstance(entries, list):
        raise ValueError("Source page returns must be an array of actual tool receipts")
    seen = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("evidence_kind") != "TOOL_IMAGE_RETURNED"
            or entry.get("semantic_verification") != "NOT_CLAIMED"
            or entry.get("source_sha256") != value["source_sha256"]
            or entry.get("thread_id") != value["runtime_receipt"]["thread_id"]
            or type(entry.get("page")) is not int
            or not 1 <= entry["page"] <= page_count
            or not all(
                isinstance(entry.get(key), str) and entry[key]
                for key in ("turn_id", "call_id", "source_path", "call_receipt_path")
            )
            or not all(
                re.fullmatch(r"[0-9a-f]{64}", str(entry.get(key, "")))
                for key in ("image_sha256", "call_receipt_sha256")
            )
        ):
            raise ValueError(
                "Source page return is missing its actual source/call binding"
            )
        identity = (entry["turn_id"], entry["call_id"])
        if identity in seen:
            raise ValueError("Source page return repeats the same tool call")
        seen.add(identity)
    return deepcopy(entries)


def _validate_native_source_graph_uncached(
    value: Mapping[str, Any], *, published_snapshot=None
) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported initial native source graph")
    digest = core.canonical_json_sha256(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )
    if digest != value.get("artifact_sha256"):
        raise ValueError("Native source graph envelope hash changed")
    document_map = core.DocumentMapArtifactV1.model_validate(value["document_map"])
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(
        value["baseline"]
    )
    receipt = core.SourceRequirementBaselineValidationReceiptV1.model_validate(
        value["validation_receipt"]
    )
    recompiled = core.compile_document_map_artifact(
        deepcopy(value["native_reference"]), revision=document_map.revision
    )
    if recompiled.model_dump(mode="json") != document_map.model_dump(mode="json"):
        raise ValueError(
            "Native reference no longer compiles to the published exact Document Map"
        )
    initial_map = _validate_map_history(value, document_map)
    if (
        value.get("source_sha256") != document_map.source_sha256
        or baseline.source_sha256 != document_map.source_sha256
        or baseline.document_map_artifact_sha256 != document_map.artifact_sha256
        or baseline.document_map_artifact_id != document_map.artifact_id
        or receipt.artifact_sha256 != baseline.artifact_sha256
        or receipt.artifact_id != baseline.artifact_id
        or receipt.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.document_map_artifact_id != document_map.artifact_id
    ):
        raise ValueError("Initial graph source, Map and baseline lineage differ")
    runtime = value["runtime_receipt"]
    operations = {
        item.get("operation")
        for item in value.get("producer_receipts", [])
        if item.get("success") is True
    }
    codex_runtime = (
        runtime.get("adapter_id") == "reqpilotx-codex-native-loop"
        and runtime.get("terminal_snapshot_verified") is True
        and runtime.get("thread_retained") is True
    )
    provider_runtime = (
        runtime.get("adapter_id") == "reqpilotx-provider-tool-loop"
        and runtime.get("transport_kind") == "PROVIDER_TOOL_LOOP"
        and runtime.get("local_state_persisted") is True
        and runtime.get("thread_retained") is True
        and isinstance(runtime.get("run_state_sha256"), str)
        and len(runtime["run_state_sha256"]) == 64
        and bool(runtime.get("run_state_reference"))
        and (runtime.get("identity") or {}).get("provider") in {"azure", "aliyun"}
    )
    if (
        not (codex_runtime or provider_runtime)
        or runtime.get("status") != "COMPLETED"
        or runtime.get("is_human_accepted") is not False
        or not runtime.get("thread_id")
        or not runtime.get("turn_id")
        or not {"capture", "compile-map", "compile-items", "deliver"}.issubset(
            operations
        )
        or not any(
            item.get("name") == "reqpilot_publish" and item.get("success") is True
            for item in runtime.get("tools", [])
        )
        or value.get("human_acceptance") != "NOT_CLAIMED"
    ):
        raise ValueError(
            "Initial graph requires actual tools and a completed retained run"
        )
    capture = value["capture_receipt"]
    map_receipt = value["map_compilation_receipt"]
    if (
        capture.get("source", {}).get("sha256") != initial_map.source_sha256
        or capture.get("source_ir_id") != map_receipt.get("source_ir_id")
        or map_receipt.get("source_sha256") != initial_map.source_sha256
        or map_receipt.get("document_map_artifact_sha256")
        != initial_map.artifact_sha256
        or (
            map_receipt.get("document_map_artifact_id") is not None
            and map_receipt["document_map_artifact_id"] != initial_map.artifact_id
        )
    ):
        raise ValueError(
            "Initial graph does not bind its actual source capture and Map compilation"
        )
    final_scope_coverage = _final_scope_coverage(value, document_map, baseline)
    source_page_returns = _source_page_returns(value)
    tables = value["table_previews"]
    if set(tables) != {item.scope_id for item in document_map.logical_tables}:
        raise ValueError(
            "Initial graph must retain every complete original logical table"
        )
    for table in document_map.logical_tables:
        core.build_source_requirement_scope_request(
            document_map, tables[table.scope_id]
        )
    assessments = value.get("assessments")
    if assessments is not None:
        from reqpilotx_server.application.agent_workflow.source_item_analysis import (
            validate_source_item_assessments,
        )

        if not value.get("analysis_packet"):
            raise ValueError("Initial assessments need their exact analysis packet")
        validate_source_item_assessments(value["analysis_packet"], assessments)
    snapshot = build_delivery_snapshot(
        document_map,
        baseline,
        tables,
        annotations=value.get("annotations"),
        decomposition=value.get("decomposition"),
        assessments=assessments,
        analysis_policy=value.get("analysis_policy"),
    )
    delivery = value["delivery_receipt"]
    if (
        delivery.get("source_sha256") != document_map.source_sha256
        or delivery.get("baseline_artifact_sha256") != baseline.artifact_sha256
        or delivery.get("source_document_bound_for_visual_export") is not True
    ):
        raise ValueError(
            "Initial graph and exported Matrix do not represent the same state"
        )
    _verify_published_snapshot(value, snapshot, published_snapshot)
    return {
        "document_map": document_map,
        "baseline": baseline,
        "receipt": receipt,
        "table_previews": deepcopy(tables),
        "snapshot": snapshot,
        "initial_document_map": initial_map,
        "final_scope_coverage": final_scope_coverage,
        "source_page_returns": source_page_returns,
    }


def validate_native_source_graph(
    value: Mapping[str, Any], *, published_snapshot=None
) -> dict[str, Any]:
    # Content-addressed artifacts are shared by several UI reads. Never cache
    # current-head selection; verify both immutable digests on every invocation.
    digest = core.canonical_json_sha256(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )
    if digest != value.get("artifact_sha256"):
        raise ValueError("Native source graph envelope hash changed")
    snapshot = value.get("matrix_snapshot")
    if snapshot is None:
        snapshot = (
            published_snapshot
            if published_snapshot is not None
            else getattr(value, "published_snapshot", None)
        )
    snapshot_sha = (
        core.canonical_json_sha256(snapshot) if snapshot is not None else None
    )
    if snapshot_sha is not None and snapshot_sha != value.get("matrix_snapshot_sha256"):
        raise ValueError(
            "Published Matrix snapshot SHA differs from the immutable graph"
        )
    key = (
        digest,
        snapshot_sha,
        id(build_delivery_snapshot),
        id(core.compile_document_map_artifact),
    )
    with _VALIDATED_GRAPHS_LOCK:
        result = _VALIDATED_GRAPHS.get(key)
        if result is None:
            result = _validate_native_source_graph_uncached(
                value, published_snapshot=snapshot
            )
            _VALIDATED_GRAPHS[key] = result
            while len(_VALIDATED_GRAPHS) > 8:
                _VALIDATED_GRAPHS.popitem(last=False)
        else:
            _VALIDATED_GRAPHS.move_to_end(key)
    return deepcopy(result)


def seal_native_source_graph(**values: Any) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA,
        "human_acceptance": "NOT_CLAIMED",
        **deepcopy(values),
    }
    value["artifact_sha256"] = core.canonical_json_sha256(value)
    validate_native_source_graph(value)
    return value


def native_source_graph_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add view metadata without changing the exact Map input projection."""
    validated = validate_native_source_graph(value)
    document_map, baseline = validated["document_map"], validated["baseline"]
    reference = deepcopy(value["native_reference"])
    captured = value["capture_receipt"]
    map_receipt = value["map_compilation_receipt"]
    gaps = [
        *map_receipt.get("uncovered_source_node_ids", []),
        *map_receipt.get("unresolved_source_node_ids", []),
        *validated["final_scope_coverage"]["unprocessed_scope_ids"],
    ]
    complete = not gaps and document_map.evidence_state == "SUFFICIENT"
    reference.update(
        availability="READY",
        artifact_status="WORKING",
        coverage_status="COMPLETE" if complete else "PARTIAL",
        boundary_status="COMPLETE" if complete else "PARTIAL_WITH_EXPLICIT_UNRESOLVED",
        workflow_step="DOCUMENT_STRUCTURE_RECOVERY",
        summary={
            "node_count": len(document_map.nodes),
            "logical_table_count": len(document_map.logical_tables),
            "source_item_count": len(baseline.records),
        },
        reading_projections={},
        page_ownership=[],
        main_document_sections=[],
        working_revision_head=None,
        structure_projection={
            "schema_version": "native-structure-projection-state/v1",
            "state": "ACCEPTED",
            "base_artifact_id": document_map.native_artifact_id,
            "attempt_id": None,
            "attempt_ordinal": None,
            "affected_scope_ids": [],
            "pending_rendered_scope_ids": [],
            "validation_status": "PASS",
            "projection_sha256": core.canonical_json_sha256(reference["document_tree"]),
        },
        physical_table_dispositions=[],
        nested_physical_tables=[],
        nested_visual_tables=[],
        source_anomalies=[],
        unresolved=[
            {
                "code": "portable-capture-gap-" + str(index),
                "pages": [],
                "scope": str(gap),
                "public_summary": "原始捕获或提取范围仍有未解决内容。",
            }
            for index, gap in enumerate(gaps)
        ],
        implementation_findings=[],
        visual_summary={
            "opened_page_count": len(
                {item["page"] for item in validated["source_page_returns"]}
            ),
            "page_count": captured.get("source", {}).get("page_count", 0),
            "receipts": validated["source_page_returns"],
            "evidence_kind": "TOOL_IMAGE_RETURNED",
            "semantic_verification": "NOT_CLAIMED",
            "summary": "原页图片已由工具返回；此记录不证明语义理解或独立质量验收。",
        },
        repeatability={
            "status": "NOT_RUN",
            "stable_semantics": {},
            "refinements_applied": [],
            "unresolved_not_settled_by_replay": [],
            "quality_claim": "Not independently repeated.",
        },
        validation={
            **deepcopy(reference.get("validation", {})),
            "status": "PASS",
            "checks": {"native_projection_preserved": True},
            "quality_claim": "SOURCE_BOUND_PORTABLE_COMPILATION",
        },
        oracle_diagnostic={"authority": "NONE", "status": "NOT_RUN"},
        activity=[],
        feedbacks=[],
        provenance={
            "transport_kind": (
                "PROVIDER_TOOL_LOOP"
                if value["runtime_receipt"].get("adapter_id") == "reqpilotx-provider-tool-loop"
                else "CODEX_APP_SERVER_NATIVE_LOOP"
            ),
            "thread_id": value["runtime_receipt"]["thread_id"],
            "turn_id": value["runtime_receipt"]["turn_id"],
            "native_source_graph_sha256": value["artifact_sha256"],
            "human_acceptance": "NOT_CLAIMED",
        },
    )
    # Default continuation fields are presentation/control metadata. Keep the
    # compiler's evidence_state; never add a fake accepted-decision receipt.
    from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
        _public_continuation,
    )

    continuation = _public_continuation(
        feedbacks=[],
        proposal_decisions=[],
        handoffs=[],
        handoff_events=[],
        handoff_attempts=[],
        proposals=[],
    )
    continuation.update(reference.get("continuation", {}))
    reference["continuation"] = continuation
    reference["feedback_capability"] = {
        "status": "QUEUE_READY",
        "agent_delivery": continuation["status"],
        "message": "反馈保留在同源任务中，可继续由自主 Agent 核对和修订。",
    }
    if (
        core.compile_document_map_artifact(
            reference, revision=document_map.revision
        ).artifact_sha256
        != document_map.artifact_sha256
    ):
        raise ValueError("Application view metadata changed the source graph identity")
    return reference


def native_source_graph_bundle(
    value: Mapping[str, Any], *, document: Mapping[str, Any]
) -> dict[str, Any]:
    validated = validate_native_source_graph(value)
    reference = native_source_graph_reference(value)
    return {
        "portable_source_graph": deepcopy(value),
        "manifest": {
            "authority": "NATIVE_AGENT_SOURCE_GRAPH",
            "source_sha256": value["source_sha256"],
            "native_source_graph_sha256": value["artifact_sha256"],
        },
        "artifact": {
            "artifact_id": reference["artifact_id"],
            "source": {
                "sha256": value["source_sha256"],
                "display_name": document.get("file_name"),
                "page_count": value["capture_receipt"]
                .get("source", {})
                .get("page_count", 0),
            },
            "logical_tables": deepcopy(reference["logical_tables"]),
            "status": "WORKING",
        },
        "document_tree": deepcopy(reference["document_tree"]),
        "document_tree_evidence": {
            "schema_version": "native-structure-node-evidence/v1",
            "source_sha256": value["source_sha256"],
            "tree_sha256": core.canonical_json_sha256(reference["document_tree"]),
            "nodes": [
                {
                    "node_id": node["node_id"],
                    "anchors": node.get("source_anchors", []),
                    "source_content": node.get("source_content", []),
                }
                for node in reference["document_tree"]["nodes"]
            ],
            "table_previews": [
                dict(fragment, logical_table_scope_id=scope)
                for scope, table in validated["table_previews"].items()
                for fragment in table["fragments"]
            ],
        },
        "validation": reference["validation"],
        "repeatability": reference["repeatability"],
        "oracle_diagnostic": reference["oracle_diagnostic"],
        "activity": [],
        "proposal_overrides": None,
    }


def native_source_graph_table_preview(
    value: Mapping[str, Any], scope_id: str
) -> dict[str, Any]:
    validated = validate_native_source_graph(value)
    table = next(
        (
            item
            for item in validated["document_map"].logical_tables
            if item.scope_id == scope_id
        ),
        None,
    )
    if table is None:
        raise ValueError("Original logical table is not in this source graph")
    preview = deepcopy(validated["table_previews"][scope_id])
    preview.update(
        schema_version="native-structure-table-preview/v1",
        artifact_id=validated["document_map"].native_artifact_id,
        title=table.title,
        presentation=core.document_map_table_presentation(table),
        summary={
            "fragment_count": len(preview["fragments"]),
            "row_count": sum(f["row_count"] for f in preview["fragments"]),
            "cell_count": sum(len(f["cells"]) for f in preview["fragments"]),
        },
    )
    # Round-trip the reader projection through the same request compiler used
    # by the producer, preserving real TableIR tokens and global cell identity.
    core.build_source_requirement_scope_request(validated["document_map"], preview)
    return preview


def load_native_source_read_bundle(project_id: str, document_id: str, source_sha256: str, *, container=None):
    """Read the current publication without rebuilding its export graph per click.

    Current-head selection and stored-byte integrity are checked on every read.
    Only a successfully validated immutable publication may populate this cache.
    Mutable raw graphs/snapshots never escape through this compact read model.
    """
    if container is None:
        from reqpilotx_server.application.runtime import RUNTIME_STATE
        container = RUNTIME_STATE.container
    if container is None or container.jobs is None:
        return None
    job = container.jobs.get_latest_succeeded(kind=EXTRACTION_JOB_KIND,
        aggregate_type="sor-document", aggregate_id=document_id,
        payload_filters={"project_id": project_id, "source_sha256": source_sha256},
        require_result_key="native_source_graph_artifact")
    if not job or not job.result or not job.result.get("native_source_graph_artifact"):
        return None
    from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _read_private
    pointer = job.result["native_source_graph_artifact"]
    # This retains the object-store SHA/size/scope checks even on cache hits.
    raw = _read_private(container, pointer, project_id=project_id,
                        document_id=document_id, namespace="working")
    if raw.get("source_sha256") != source_sha256:
        raise ValueError("Initial source graph belongs to another source revision")
    key = (project_id, document_id, source_sha256, pointer["object_sha256"],
           id(build_delivery_snapshot), id(core.compile_document_map_artifact))
    with _SOURCE_READ_BUNDLES_LOCK:
        bundle = _SOURCE_READ_BUNDLES.get(key)
        if bundle is None:
            # Legacy publications need their original retained snapshot proof.
            if "matrix_snapshot" not in raw:
                graph = load_native_source_graph(project_id, document_id, source_sha256, container=container)
                if graph["artifact_sha256"] != raw["artifact_sha256"]:
                    raise ValueError("Native publication changed during read; retry the current version")
            else:
                graph = raw
            validated = validate_native_source_graph(graph)
            bundle = {"root": None,
                "manifest": {"authority": "NATIVE_AGENT_SOURCE_GRAPH", "source_sha256": source_sha256,
                             "native_source_graph_sha256": graph["artifact_sha256"]},
                "document_map": validated["document_map"], "baseline": validated["baseline"],
                "receipt": validated["receipt"], "run_receipts": [graph["runtime_receipt"]],
                "image_annotations": graph.get("annotations"), "analysis_policy": graph.get("analysis_policy")}
            _SOURCE_READ_BUNDLES[key] = bundle
            while len(_SOURCE_READ_BUNDLES) > 8:
                _SOURCE_READ_BUNDLES.popitem(last=False)
        else:
            _SOURCE_READ_BUNDLES.move_to_end(key)
        # Typed Map/baseline/receipt consist of frozen records and tuples. Copy
        # mutable view metadata only, not the full history and Matrix snapshot.
        result = {k: (v if k in {"document_map", "baseline", "receipt"} else deepcopy(v))
                  for k, v in bundle.items()}
    result["native_publication"] = {"job_id": job.job_id, "updated_at": job.updated_at.isoformat(),
        "graph_pointer": deepcopy(pointer), "baseline_revision": result["baseline"].revision}
    return result


def load_native_source_graph(
    project_id: str, document_id: str, source_sha256: str, *, container=None
):
    """Load only this document's succeeded initial extraction publication."""
    if container is None:
        from reqpilotx_server.application.runtime import RUNTIME_STATE

        container = RUNTIME_STATE.container
    if container is None or container.jobs is None:
        return None
    job = container.jobs.get_latest_succeeded(
        kind=EXTRACTION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        payload_filters={
            "project_id": project_id,
            "source_sha256": source_sha256,
        },
        require_result_key="native_source_graph_artifact",
    )
    if not job or not job.result or not job.result.get("native_source_graph_artifact"):
        return None
    from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _read_private

    value = _read_private(
        container,
        job.result["native_source_graph_artifact"],
        project_id=project_id,
        document_id=document_id,
        namespace="working",
    )
    if value.get("source_sha256") != source_sha256:
        raise ValueError("Initial source graph belongs to another source revision")
    value = _LoadedNativeGraph(value)
    value.publication = {
        "job_id": job.job_id,
        "updated_at": job.updated_at.isoformat(),
        "graph_pointer": deepcopy(job.result["native_source_graph_artifact"]),
        "baseline_revision": value["baseline"]["revision"],
    }
    if "matrix_snapshot" not in value:
        # The first native release sealed the exact export hash without embedding
        # its snapshot. Recover that immutable, already-published file from the
        # retained task; the validator checks its SHA and every semantic field.
        from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import _task_root

        task = _task_root(container, project_id, document_id, job.payload["task_id"])
        review = (task / "workspace/review").resolve(strict=True)
        relative = value["host_verification"]["review"]["delivery"]["path"]
        published = (review / relative / "matrix-snapshot.json").resolve(strict=True)
        if not published.is_relative_to(review):
            raise ValueError("Published legacy snapshot escaped its task workspace")
        value.published_snapshot = json.loads(published.read_text())
    validate_native_source_graph(value)
    return value
