from __future__ import annotations

# ruff: noqa: E402 -- isolate writable application paths before its imports.
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.native_source_graph import (
    seal_native_source_graph,
    validate_native_source_graph,
    native_source_graph_reference,
    native_source_graph_bundle,
    native_source_graph_table_preview,
)
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
from app.application.agent_workflow.document_map_revision import (
    compile_document_map_revision,
    rebind_baseline_for_document_map_revision,
)
from .test_source_requirement_baseline import (
    _text_candidate,
    _text_item,
    _fragment,
    _candidate,
    _record,
)


def graph_fixture():
    text = "The controller shall provide a diagnostic interface."
    source_sha = "a" * 64
    reference = {
        "schema_version": "native-structure-reference-public/v1",
        "artifact_id": "portable-map-synthetic",
        "source_sha256": source_sha,
        "document_tree": {
            "root_ids": ["doc"],
            "nodes": [
                {
                    "node_id": "doc",
                    "parent_id": None,
                    "kind": "DOCUMENT",
                    "label": "",
                    "title": "synthetic.pdf",
                    "title_authority": "AGENT_DISPLAY_LABEL",
                    "pages": [1],
                    "order": 0,
                    "source_anchors": [
                        {
                            "anchor_id": "body",
                            "source_handle": "body",
                            "page": 1,
                            "bbox": [0, 100, 100, 120],
                            "source_bounds": [0, 0, 100, 500],
                            "anchor_kind": "SOURCE",
                            "precision": "SOURCE_IR_BLOCK",
                        }
                    ],
                    "source_content": [
                        {
                            "content_id": "body",
                            "page": 1,
                            "bbox": [0, 100, 100, 120],
                            "source_bounds": [0, 0, 100, 500],
                            "source_handles": ["body"],
                            "text": text,
                            "text_sha256": core.canonical_text_sha256(text),
                            "precision": "SOURCE_IR_BLOCK",
                            "is_title": False,
                        }
                    ],
                    "content_blocks": [
                        {"kind": "LOGICAL_TABLE", "logical_table_scope_id": "limits"}
                    ],
                }
            ],
        },
        "logical_tables": [
            {
                "scope_id": "limits",
                "title": "Limits",
                "owner_unit_id": "doc",
                "role": "SOURCE_TABLE",
                "pages": [1],
                "fragments": [
                    {"table_token": "table-sourceir-a9", "page": 1, "row_range": [0, 2]}
                ],
            }
        ],
        "structure_proposals": [],
        "continuation": {"evidence_state": "SUFFICIENT"},
    }
    document_map = core.compile_document_map_artifact(reference)
    preview = {
        "source_sha256": source_sha,
        "logical_table_scope_id": "limits",
        "fragments": [
            _fragment(
                "original-table-fragment",
                "table-sourceir-a9",
                page=1,
                rows=(("Parameter", "Limit"), ("Voltage", "12 V")),
            )
        ],
    }
    tr = core.build_source_item_text_scope_request(document_map, node_id="doc")
    text_candidate = _text_candidate(
        tr,
        items=(
            _text_item(
                tr,
                key="interface",
                label="Diagnostic interface",
                start=0,
                end=len(text),
            ),
        ),
    )
    table_request = core.build_source_requirement_scope_request(document_map, preview)
    table_candidate = _candidate(
        table_request,
        records=(
            _record(
                "voltage",
                "Voltage",
                core.SourceRecordForm.TABLE_RECORD,
                (
                    core.SourceRequirementRowGroupV1(
                        fragment_id="original-table-fragment", row_start=1, row_end=2
                    ),
                ),
            ),
        ),
        context=(
            core.SourceRequirementContextRowGroupV1(
                role=core.ContextRowRole.HEADER,
                row_group=core.SourceRequirementRowGroupV1(
                    fragment_id="original-table-fragment", row_start=0, row_end=1
                ),
                reason_codes=("shared_header",),
            ),
        ),
    )
    baseline, receipt = core.compile_source_requirement_baseline(
        document_map=document_map,
        text_requests=(tr,),
        text_candidates=(text_candidate,),
        requests=(table_request,),
        candidates=(table_candidate,),
    )
    tables = {"limits": preview}
    snapshot = build_delivery_snapshot(document_map, baseline, tables)
    runtime = {
        "adapter_id": "reqpilotx-codex-native-loop",
        "status": "COMPLETED",
        "terminal_snapshot_verified": True,
        "thread_retained": True,
        "thread_id": "thread-synthetic",
        "turn_id": "turn-synthetic",
        "is_human_accepted": False,
        "tools": [{"name": "reqpilot_publish", "success": True}],
    }
    return seal_native_source_graph(
        source_sha256=source_sha,
        document_map=document_map.model_dump(mode="json"),
        baseline=baseline.model_dump(mode="json"),
        validation_receipt=receipt.model_dump(mode="json"),
        native_reference=reference,
        table_previews=tables,
        runtime_receipt=runtime,
        producer_receipts=[
            {"operation": operation, "success": True}
            for operation in ("capture", "compile-map", "compile-items", "deliver")
        ],
        capture_receipt={
            "source": {"sha256": source_sha, "page_count": 1},
            "source_ir_id": "captured-ir",
        },
        map_compilation_receipt={
            "source_sha256": source_sha,
            "source_ir_id": "captured-ir",
            "document_map_artifact_sha256": document_map.artifact_sha256,
        },
        item_compilation_receipt={"unprocessed_scope_ids": []},
        matrix_snapshot_sha256=core.canonical_json_sha256(snapshot),
        delivery_receipt={
            "source_sha256": source_sha,
            "baseline_artifact_sha256": baseline.artifact_sha256,
            "source_document_bound_for_visual_export": True,
        },
    )


def advance_map(graph, name):
    result = deepcopy(graph)
    result.pop("artifact_sha256")
    result.setdefault("initial_document_map", deepcopy(graph["document_map"]))
    result.setdefault("initial_native_reference", deepcopy(graph["native_reference"]))
    candidate_reference = deepcopy(graph["native_reference"])
    candidate_reference["document_tree"]["root_ids"] = [name]
    candidate_reference["document_tree"]["nodes"][0]["node_id"] = name
    candidate_reference["logical_tables"][0]["owner_unit_id"] = name
    candidate = core.compile_document_map_artifact(candidate_reference)
    revision = compile_document_map_revision(graph["document_map"], candidate)
    rebound = rebind_baseline_for_document_map_revision(
        graph["document_map"],
        revision["document_map"],
        graph["baseline"],
        graph["validation_receipt"],
        graph["table_previews"],
    )
    result["map_history"] = [
        *result.get("map_history", []),
        {"candidate_map": candidate.model_dump(mode="json"), **revision},
    ]
    result.update(
        document_map=revision["document_map"],
        native_reference=revision["native_reference"],
        baseline=rebound["baseline"],
        validation_receipt=rebound["validation_receipt"],
        table_previews=rebound["table_previews"],
        final_scope_coverage={"unprocessed_scope_ids": []},
    )
    result["delivery_receipt"]["baseline_artifact_sha256"] = rebound["baseline"][
        "artifact_sha256"
    ]
    result["matrix_snapshot_sha256"] = core.canonical_json_sha256(
        build_delivery_snapshot(
            result["document_map"], result["baseline"], result["table_previews"]
        )
    )
    return seal_native_source_graph(**result)


class NativeSourceGraphTests(unittest.TestCase):
    def test_immutable_validation_cache_rechecks_hashes_and_does_not_expose_mutable_results(
        self,
    ):
        from app.application.agent_workflow import native_source_graph as module

        graph = graph_fixture()
        module._VALIDATED_GRAPHS.clear()
        with patch.object(
            module,
            "_validate_native_source_graph_uncached",
            wraps=module._validate_native_source_graph_uncached,
        ) as validate:
            first = validate_native_source_graph(graph)
            title = first["snapshot"]["nodes"][0]["display_title"]
            first["snapshot"]["nodes"][0]["display_title"] = "caller mutation"
            second = validate_native_source_graph(graph)
            self.assertEqual(second["snapshot"]["nodes"][0]["display_title"], title)
            self.assertEqual(validate.call_count, 1)
            changed = deepcopy(graph)
            changed["baseline"]["records"][0]["label"] = "unsealed mutation"
            with self.assertRaisesRegex(ValueError, "envelope hash"):
                validate_native_source_graph(changed)

    def test_reference_bridge_preserves_the_published_map_revision(self):
        from app.application.agent_workflow import (
            source_requirement_reference_bridge as bridge,
        )

        graph = advance_map(graph_fixture(), "corrected-doc")
        public = native_source_graph_reference(graph)
        with (
            patch(
                "app.application.agent_workflow.native_structure_bridge.get_native_structure_reference",
                return_value=public,
            ),
            patch(
                "app.application.agent_workflow.native_source_graph.load_native_source_graph",
                return_value=graph,
            ),
        ):
            bundle = bridge._load_base_reference(
                project_id="project",
                document_id="document",
                source_sha256=graph["source_sha256"],
            )
        self.assertEqual(bundle["document_map"].revision, 2)
        self.assertEqual(
            bundle["document_map"].artifact_sha256,
            graph["document_map"]["artifact_sha256"],
        )
        self.assertEqual(
            bundle["baseline"].artifact_sha256, graph["baseline"]["artifact_sha256"]
        )

    def test_graph_and_product_baselines_share_revision_and_publication_order(self):
        from app.application.agent_workflow import source_requirement_jobs as jobs

        graph = advance_map(graph_fixture(), "latest-doc")
        verified = validate_native_source_graph(graph)
        when = datetime.now(UTC)
        base = {
            "document_map": verified["document_map"],
            "baseline": verified["baseline"],
            "receipt": verified["receipt"],
            "manifest": {},
            "native_publication": {"updated_at": when.isoformat()},
        }
        candidate = SimpleNamespace(
            kind="agent.source-item.direct-revision",
            updated_at=when + timedelta(seconds=1),
            payload={},
            result={"baseline_revision_artifact": {"baseline_revision": 1}},
        )
        container = SimpleNamespace(
            jobs=SimpleNamespace(
                get_latest_succeeded=lambda **kwargs: candidate
                if kwargs["kind"] == candidate.kind
                else None
            ),
            object_store=None,
        )
        with patch.object(jobs, "_load_product_baseline_artifact") as read:
            actual = jobs.resolve_effective_source_requirement_bundle(
                project_id="project",
                document_id="document",
                base_bundle=base,
                container=container,
            )
            self.assertFalse(read.called)
            self.assertEqual(actual["baseline"], base["baseline"])
        candidate.result["baseline_revision_artifact"]["baseline_revision"] = 3
        product = SimpleNamespace(
            source_sha256=graph["source_sha256"],
            document_map_artifact_id=verified["document_map"].artifact_id,
            document_map_artifact_sha256=verified["document_map"].artifact_sha256,
            baseline=verified["baseline"],
            validation_receipt=verified["receipt"],
            product_run_receipts=(),
        )
        with (
            patch.object(
                jobs, "_load_product_baseline_artifact", return_value=product
            ) as read,
            patch(
                "app.application.agent_workflow.source_item_loop_jobs.load_native_loop_artifact",
                return_value=(None, None),
            ),
        ):
            actual = jobs.resolve_effective_source_requirement_bundle(
                project_id="project",
                document_id="document",
                base_bundle=base,
                container=container,
            )
            self.assertTrue(read.called)
            self.assertEqual(
                actual["product_baseline_pointer"],
                candidate.result["baseline_revision_artifact"],
            )

    def test_visual_summary_projects_only_bound_tool_returns_without_quality_claim(
        self,
    ):
        value = graph_fixture()
        value.pop("artifact_sha256")
        entry = {
            "evidence_kind": "TOOL_IMAGE_RETURNED",
            "semantic_verification": "NOT_CLAIMED",
            "source_sha256": value["source_sha256"],
            "source_path": "source.pdf",
            "page": 1,
            "thread_id": "thread-synthetic",
            "turn_id": "turn-original",
            "call_id": "call-image",
            "image_sha256": "b" * 64,
            "call_receipt_path": "runtime/calls/image.json",
            "call_receipt_sha256": "c" * 64,
        }
        value["host_verification"] = {"source_page_returns": [entry]}
        graph = seal_native_source_graph(**value)
        summary = native_source_graph_reference(graph)["visual_summary"]
        self.assertEqual(summary["opened_page_count"], 1)
        self.assertEqual(summary["semantic_verification"], "NOT_CLAIMED")
        self.assertEqual(summary["receipts"][0]["call_id"], "call-image")
        value["host_verification"]["source_page_returns"] = [
            entry,
            {**entry, "call_id": "call-repeated-page"},
        ]
        self.assertEqual(
            native_source_graph_reference(seal_native_source_graph(**value))[
                "visual_summary"
            ]["opened_page_count"],
            1,
        )
        for change in (
            {"page": 2},
            {"source_sha256": "d" * 64},
            {"semantic_verification": "PASSED"},
        ):
            value["host_verification"]["source_page_returns"] = [{**entry, **change}]
            with self.subTest(change=change), self.assertRaises(ValueError):
                seal_native_source_graph(**value)

    def test_initial_capture_binding_and_two_step_map_history_adopt_the_exact_final_map(
        self,
    ):
        initial = graph_fixture()
        graph = advance_map(
            advance_map(initial, "first-correction"), "final-correction"
        )
        validated = validate_native_source_graph(graph)
        self.assertEqual(
            validated["initial_document_map"].artifact_sha256,
            initial["document_map"]["artifact_sha256"],
        )
        self.assertEqual(
            validated["document_map"].artifact_sha256,
            graph["document_map"]["artifact_sha256"],
        )
        self.assertEqual(
            graph["map_compilation_receipt"], initial["map_compilation_receipt"]
        )
        reference = native_source_graph_reference(graph)
        self.assertEqual(
            core.compile_document_map_artifact(
                reference, revision=graph["document_map"]["revision"]
            ).artifact_sha256,
            graph["document_map"]["artifact_sha256"],
        )
        self.assertIn("receipt_sha256", reference["validation"])

    def test_map_history_cannot_be_resigned_with_false_outputs_or_initial_capture_binding(
        self,
    ):
        graph = advance_map(graph_fixture(), "corrected-doc")
        mutations = [
            lambda value: value["map_history"][0]["receipt"].update(
                source_conserved=False
            ),
            lambda value: value["map_history"][0]["native_reference"].update(
                artifact_id="other-artifact"
            ),
            lambda value: value["map_compilation_receipt"].update(
                document_map_artifact_sha256=value["document_map"]["artifact_sha256"]
            ),
            lambda value: value["map_history"].clear(),
        ]
        for mutate in mutations:
            bad = deepcopy(graph)
            bad.pop("artifact_sha256")
            mutate(bad)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                seal_native_source_graph(**bad)

    def test_final_scope_coverage_replaces_old_initial_scope_status_and_is_cross_checked(
        self,
    ):
        initial = graph_fixture()
        initial["item_compilation_receipt"]["unprocessed_scope_ids"] = ["text:doc"]
        initial.pop("artifact_sha256")
        graph = advance_map(seal_native_source_graph(**initial), "corrected-doc")
        self.assertEqual(
            native_source_graph_reference(graph)["coverage_status"], "COMPLETE"
        )
        for replacement in (
            None,
            {"unprocessed_scope_ids": ["text:corrected-doc"]},
            {"unprocessed_scope_ids": ["", ""]},
        ):
            bad = deepcopy(graph)
            bad.pop("artifact_sha256")
            if replacement is None:
                bad.pop("final_scope_coverage")
            else:
                bad["final_scope_coverage"] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                seal_native_source_graph(**bad)

    def test_declared_complete_final_scope_cannot_hide_unprocessed_current_map_content(
        self,
    ):
        graph = graph_fixture()
        graph.pop("artifact_sha256")
        document_map = core.DocumentMapArtifactV1.model_validate(graph["document_map"])
        request = core.build_source_requirement_scope_request(
            document_map, graph["table_previews"]["limits"]
        )
        candidate = _candidate(
            request,
            records=(
                _record(
                    "voltage",
                    "Voltage",
                    core.SourceRecordForm.TABLE_RECORD,
                    (
                        core.SourceRequirementRowGroupV1(
                            fragment_id="original-table-fragment",
                            row_start=1,
                            row_end=2,
                        ),
                    ),
                ),
            ),
            context=(
                core.SourceRequirementContextRowGroupV1(
                    role=core.ContextRowRole.HEADER,
                    row_group=core.SourceRequirementRowGroupV1(
                        fragment_id="original-table-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_header",),
                ),
            ),
        )
        baseline, validation = core.compile_source_requirement_baseline(
            document_map=document_map, requests=(request,), candidates=(candidate,)
        )
        graph.update(
            baseline=baseline.model_dump(mode="json"),
            validation_receipt=validation.model_dump(mode="json"),
        )
        graph["delivery_receipt"]["baseline_artifact_sha256"] = baseline.artifact_sha256
        graph["matrix_snapshot_sha256"] = core.canonical_json_sha256(
            build_delivery_snapshot(document_map, baseline, graph["table_previews"])
        )
        graph["final_scope_coverage"] = {"unprocessed_scope_ids": []}
        with self.assertRaisesRegex(ValueError, "Final scope coverage"):
            seal_native_source_graph(**graph)
        graph["final_scope_coverage"]["unprocessed_scope_ids"] = ["text:doc"]
        self.assertEqual(
            native_source_graph_reference(seal_native_source_graph(**graph))[
                "coverage_status"
            ],
            "PARTIAL",
        )

    def test_application_projection_preserves_exact_native_map_and_sourceir_handles(
        self,
    ):
        graph = graph_fixture()
        reference = native_source_graph_reference(graph)
        self.assertEqual(
            reference["document_tree"], graph["native_reference"]["document_tree"]
        )
        self.assertEqual(
            core.compile_document_map_artifact(reference).artifact_sha256,
            graph["document_map"]["artifact_sha256"],
        )
        self.assertEqual(
            reference["document_tree"]["nodes"][0]["source_anchors"][0]["precision"],
            "SOURCE_IR_BLOCK",
        )
        bundle = native_source_graph_bundle(
            graph, document={"file_name": "synthetic.pdf"}
        )
        self.assertEqual(bundle["artifact"]["artifact_id"], "portable-map-synthetic")
        self.assertEqual(
            bundle["portable_source_graph"]["artifact_sha256"], graph["artifact_sha256"]
        )

    def test_original_table_tokens_and_cells_survive_application_preview(self):
        graph = graph_fixture()
        preview = native_source_graph_table_preview(graph, "limits")
        self.assertEqual(
            preview["fragments"], graph["table_previews"]["limits"]["fragments"]
        )
        self.assertEqual(preview["fragments"][0]["table_token"], "table-sourceir-a9")
        self.assertEqual(preview["fragments"][0]["cells"][-1]["text"], "12 V")

    def test_runtime_success_or_self_signed_projection_is_not_enough(self):
        graph = graph_fixture()
        for mutate in (
            lambda value: value["native_reference"]["document_tree"]["nodes"][0].update(
                title="Invented chapter"
            ),
            lambda value: value["producer_receipts"].clear(),
            lambda value: value["runtime_receipt"].update(status="UNKNOWN"),
        ):
            changed = deepcopy(graph)
            mutate(changed)
            changed["artifact_sha256"] = core.canonical_json_sha256(
                {
                    key: value
                    for key, value in changed.items()
                    if key != "artifact_sha256"
                }
            )
            with self.assertRaises(ValueError):
                validate_native_source_graph(changed)
