from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

if __package__:
    from .private_eval import require_private_eval
else:
    from private_eval import require_private_eval

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.application.extraction.evaluate_llm_requirement_silver import (
    DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH,
    LlmRequirementSilverCheckStatus,
    LlmRequirementSilverRegistry,
    LlmRequirementSilverRegistryError,
    build_llm_requirement_silver_document,
    evaluate_llm_requirement_silver,
    load_llm_requirement_silver,
)
from app.application.extraction.llm_semantics import (
    LlmRequirementExtractionResult,
    extract_requirements_with_provider,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.structure import StructureSourceRefKind
from tests.test_llm_semantics import _FakeProvider
from tests.test_pdf_shadow_silver import _build_matching_pipeline
from tests.test_semantics import _hierarchical_source


DOCUMENT_ID = "DOC-SYNTHETIC-LLM-SILVER"


class LlmRequirementSilverTests(unittest.TestCase):
    def test_builder_creates_a_deterministic_text_free_document(self) -> None:
        source, tree, result = _pipeline()

        first = build_llm_requirement_silver_document(
            source_ir=source,
            structure_tree=tree,
            result=result,
        )
        second = build_llm_requirement_silver_document(
            source_ir=source,
            structure_tree=tree,
            result=result,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(first.items),
            result.assembly.scope_structure_node_ids,
        )
        serialized = json.dumps(first.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("Synthetic measurement tool", serialized)
        self.assertNotIn("maximum reading", serialized)

    def test_builder_rejects_non_successful_provider_runs(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        result = extract_requirements_with_provider(
            source,
            tree,
            _FakeProvider(classification_status=SemanticProviderRunStatus.TIMEOUT),
        )

        with self.assertRaisesRegex(ValueError, "successful provider runs"):
            build_llm_requirement_silver_document(
                source_ir=source,
                structure_tree=tree,
                result=result,
            )

    def test_builder_rejects_cross_operation_provider_identity_drift(self) -> None:
        source, tree, result = _pipeline()
        provider_runs = tuple(
            run.model_copy(update={"provider_version": "2.0.0"})
            if run.operation == "classify_nodes"
            else run
            for run in result.provider_runs
        )
        tampered = replace(
            result,
            provider_runs=provider_runs,
        )

        with self.assertRaisesRegex(ValueError, "share one provider model"):
            build_llm_requirement_silver_document(
                source_ir=source,
                structure_tree=tree,
                result=tampered,
            )

    def test_builder_binds_model_continuation_into_owner_exact_sources(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool requirement continues on this line.",
        )
        tree = build_structure_tree(source)
        result = extract_requirements_with_provider(
            source,
            tree,
            _FakeProvider(mark_content_continuation=True),
        )

        document = build_llm_requirement_silver_document(
            source_ir=source,
            structure_tree=tree,
            result=result,
        )
        continuation = next(
            item
            for item in result.semantics.annotations
            if item.is_continuation_fragment
        )
        continuation_node = next(
            item
            for item in tree.nodes
            if item.structure_node_id == continuation.target_structure_node_id
        )
        owner_target = continuation_node.parent_structure_node_id
        self.assertIsNotNone(owner_target)
        owner = document.items[owner_target]
        continuation_refs = {
            span.source_ref for span in continuation.grounding_spans
        }.intersection(continuation_node.source_refs)

        self.assertTrue(
            continuation_refs.issubset({item.source_ref for item in owner.source_refs})
        )
        self.assertTrue(
            continuation_refs.issubset(
                {item.source_ref for item in owner.selected_evidence_source_refs}
            )
        )
        self.assertTrue(
            {
                span.grounding_span_id
                for span in continuation.grounding_spans
                if span.source_ref in continuation_refs
            }.issubset(owner.source_span_ids)
        )
        self.assertFalse(
            document.items[continuation.target_structure_node_id].include_in_view
        )

    def test_default_registry_strictly_loads(self) -> None:
        registry = load_llm_requirement_silver(require_private_eval("extraction_cases/llm_silver.json"))

        self.assertIn("DOC-QC-T-1067", registry.documents)
        self.assertFalse(registry.provenance.is_human_gold)
        self.assertFalse(registry.provenance.is_serving_activation_gate)

    def test_loader_accepts_only_ai_reviewed_non_serving_silver(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)

        registry = _load_payload(payload)

        self.assertEqual(registry.provenance.label, "AI_REVIEWED_SILVER")
        self.assertFalse(registry.provenance.is_human_gold)
        self.assertFalse(registry.provenance.is_serving_activation_gate)

    def test_loader_rejects_draft_provenance(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        payload["provenance"]["label"] = "DRAFT"

        with self.assertRaisesRegex(
            LlmRequirementSilverRegistryError,
            "Draft LLM Silver",
        ):
            _load_payload(payload)

    def test_loader_rejects_gold_serving_flags_and_free_form_notes(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        variants = []
        gold = copy.deepcopy(payload)
        gold["provenance"]["is_human_gold"] = True
        variants.append(gold)
        serving = copy.deepcopy(payload)
        serving["provenance"]["is_serving_activation_gate"] = True
        variants.append(serving)
        notes = copy.deepcopy(payload)
        notes["documents"][DOCUMENT_ID]["notes"] = "synthetic free-form text"
        variants.append(notes)

        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(LlmRequirementSilverRegistryError):
                    _load_payload(variant)

    def test_loader_rejects_duplicate_stable_target_keys(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        serialized = json.dumps(payload, ensure_ascii=False)
        target_id = result.assembly.scope_structure_node_ids[0]
        item_text = json.dumps(
            payload["documents"][DOCUMENT_ID]["items"][target_id],
            ensure_ascii=False,
        )
        marker = f'"{target_id}": {item_text}'
        duplicated = serialized.replace(marker, f"{marker}, {marker}", 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "llm_silver.json"
            path.write_text(duplicated, encoding="utf-8")
            with self.assertRaises(LlmRequirementSilverRegistryError):
                load_llm_requirement_silver(
                    path,
                    schema_path=DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH,
                )

    def test_matching_in_memory_pipeline_passes_text_free_item_checks(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.PASS)
        self.assertEqual(report.failed_check_count, 0)
        self.assertTrue(
            any(
                check.check_id.endswith(".full_context_refs") for check in report.checks
            )
        )
        serialized = json.dumps(
            {
                "registry": registry.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        self.assertNotIn("Synthetic measurement tool", serialized)
        self.assertNotIn("maximum reading", serialized)

    def test_ocr_refs_are_bound_to_page_bbox_provider_and_image_hash(self) -> None:
        source, tree, _, _ = _build_matching_pipeline()
        result = extract_requirements_with_provider(source, tree, _FakeProvider())
        registry = _load_payload(_registry_payload(source, tree, result))

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=result,
            registry=registry,
        )

        ocr_locators = [
            ref.locator
            for item in registry.documents[DOCUMENT_ID].items.values()
            for ref in (*item.source_refs, *item.full_context_source_refs)
            if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
        ]
        self.assertIs(report.status, LlmRequirementSilverCheckStatus.PASS)
        self.assertTrue(ocr_locators)
        self.assertTrue(all(item.locator_kind == "PDF_OCR" for item in ocr_locators))
        self.assertTrue(
            all(item.bbox and item.page_image_sha256 for item in ocr_locators)
        )
        self.assertEqual(
            {item.provider_id for item in ocr_locators},
            {"aliyun-bailian-qwen-ocr"},
        )

    def test_reviewed_content_digest_detects_same_length_ocr_drift(self) -> None:
        source, tree, _, _ = _build_matching_pipeline()
        result = extract_requirements_with_provider(source, tree, _FakeProvider())
        registry = _load_payload(_registry_payload(source, tree, result))
        snapshot = source.visual_text_snapshots[0]
        observation = snapshot.observations[0]
        replacement = "x" * len(observation.raw_text)
        if replacement == observation.raw_text:
            replacement = "y" * len(observation.raw_text)
        changed_observation = observation.model_copy(update={"raw_text": replacement})
        changed_snapshot = snapshot.model_copy(
            update={
                "observations": (
                    changed_observation,
                    *snapshot.observations[1:],
                )
            }
        )
        changed_source = source.model_copy(
            update={
                "visual_text_snapshots": (
                    changed_snapshot,
                    *source.visual_text_snapshots[1:],
                )
            }
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=changed_source,
            structure_tree=tree,
            result=result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertEqual(
            _check(
                report,
                "binding.reviewed_source_content_sha256",
            ).reason_code,
            "reviewed_source_content_sha256_mismatch",
        )

    def test_provider_status_and_every_version_binding_are_enforced(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        payload["documents"][DOCUMENT_ID]["semantic_provider"][
            "classification_prompt_version"
        ] = "wrong-prompt/v9"
        registry = LlmRequirementSilverRegistry.model_validate(payload)
        failed_run = result.provider_runs[0].model_copy(
            update={"status": SemanticProviderRunStatus.TIMEOUT}
        )
        tampered_result = replace(
            result,
            provider_runs=(failed_run, *result.provider_runs[1:]),
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=tampered_result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertEqual(
            _check(report, "provider.success").reason_code,
            "provider_run_not_successful",
        )
        self.assertEqual(
            _check(report, "provider.classification_binding").reason_code,
            "provider_version_binding_mismatch",
        )

    def test_provider_request_hash_is_bound_to_reviewed_run(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )
        changed_run = result.provider_runs[0].model_copy(
            update={"request_sha256": "f" * 64}
        )
        tampered_result = replace(
            result,
            provider_runs=(changed_run, *result.provider_runs[1:]),
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=tampered_result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertEqual(
            _check(
                report,
                "provider.classification_run_0.request_sha256",
            ).reason_code,
            "provider_request_hash_mismatch",
        )

    def test_provider_run_order_is_bound_to_exact_target_window(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )
        classification_runs = tuple(
            run for run in result.provider_runs if run.operation == "classify_nodes"
        )
        if len(classification_runs) < 2:
            self.skipTest("fixture has only one classification window")
        non_classification_runs = tuple(
            run for run in result.provider_runs if run.operation != "classify_nodes"
        )
        tampered_result = replace(
            result,
            provider_runs=(
                classification_runs[1],
                classification_runs[0],
                *classification_runs[2:],
                *non_classification_runs,
            ),
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=tampered_result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertEqual(
            _check(
                report,
                "provider.classification_run_0.target_structure_node_ids",
            ).reason_code,
            "provider_classification_targets_mismatch",
        )

    def test_component_binding_ignores_run_result_digest(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )
        changed_result = replace(
            result,
            semantics=result.semantics.model_copy(
                update={
                    "classifier_version": (
                        result.semantics.classifier_version.rsplit("result=", 1)[0]
                        + "result="
                        + "f" * 12
                    )
                }
            ),
            assembly=result.assembly.model_copy(
                update={
                    "assembler_version": (
                        result.assembly.assembler_version.rsplit("result=", 1)[0]
                        + "result="
                        + "e" * 12
                    )
                }
            ),
        )

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=changed_result,
            registry=registry,
        )

        self.assertIs(
            _check(report, "binding.classifier_version").status,
            LlmRequirementSilverCheckStatus.PASS,
        )
        self.assertIs(
            _check(report, "binding.assembler_version").status,
            LlmRequirementSilverCheckStatus.PASS,
        )

    def test_component_binding_rejects_unknown_version_suffix(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )
        classification_run = result.provider_runs[0]
        implementation_version = result.semantics.classifier_version.partition(";")[0]
        prompt_tail = classification_run.prompt_version.rsplit("/", 1)[-1]
        invalid_versions = (
            implementation_version + ";unreviewed-metadata=true",
            implementation_version + ";result=" + "f" * 64,
            (
                implementation_version
                + ";model=unexpected-model;prompt="
                + prompt_tail
                + ";result="
                + "f" * 12
            ),
            (
                implementation_version
                + ";model="
                + classification_run.model_id
                + ";prompt=unexpected-prompt;result="
                + "f" * 12
            ),
        )
        for invalid_version in invalid_versions:
            with self.subTest(invalid_version=invalid_version):
                changed_result = replace(
                    result,
                    semantics=result.semantics.model_copy(
                        update={"classifier_version": invalid_version}
                    ),
                )
                report = evaluate_llm_requirement_silver(
                    document_id=DOCUMENT_ID,
                    source_ir=source,
                    structure_tree=tree,
                    result=changed_result,
                    registry=registry,
                )

                self.assertIs(
                    _check(report, "binding.classifier_version").status,
                    LlmRequirementSilverCheckStatus.FAIL,
                )
                self.assertEqual(
                    _check(report, "binding.classifier_version").reason_code,
                    "classifier_version_mismatch",
                )

    def test_selected_evidence_locator_conflict_is_rejected(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        items = payload["documents"][DOCUMENT_ID]["items"]
        target_id = result.assembly.scope_structure_node_ids[0]
        selected_ref = items[target_id]["selected_evidence_source_refs"][0]
        selected_key = (selected_ref["kind"], selected_ref["source_id"])
        self.assertTrue(
            any(
                (bound_ref["kind"], bound_ref["source_id"]) == selected_key
                for item in items.values()
                for group in ("source_refs", "full_context_source_refs")
                for bound_ref in item[group]
            )
        )
        selected_ref["locator"]["page_number"] += 1

        with self.assertRaisesRegex(ValueError, "conflicting locators"):
            LlmRequirementSilverRegistry.model_validate(payload)

    def test_component_binding_rejects_implementation_version_change(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        payload["documents"][DOCUMENT_ID]["semantic_classifier"][
            "component_version"
        ] = "999.0.0"
        registry = LlmRequirementSilverRegistry.model_validate(payload)

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=result,
            registry=registry,
        )

        self.assertIs(
            _check(report, "binding.classifier_version").status,
            LlmRequirementSilverCheckStatus.FAIL,
        )
        self.assertEqual(
            _check(report, "binding.classifier_version").reason_code,
            "classifier_version_mismatch",
        )

    def test_scope_reports_missing_extra_and_duplicate_targets(self) -> None:
        source, tree, result = _pipeline()
        registry = LlmRequirementSilverRegistry.model_validate(
            _registry_payload(source, tree, result)
        )
        targets = result.assembly.scope_structure_node_ids
        tampered_assembly = result.assembly.model_copy(
            update={
                "scope_structure_node_ids": (*targets[1:], targets[-1]),
                "items": (*result.assembly.items[1:], result.assembly.items[-1]),
            }
        )
        tampered_result = replace(result, assembly=tampered_assembly)

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=tampered_result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertTrue(_check(report, "scope.assembly.missing").observed)
        self.assertTrue(_check(report, "scope.assembly.duplicate").observed)
        self.assertEqual(
            _check(report, "pipeline.item_evaluation").reason_code,
            "pipeline_validation_failed",
        )

    def test_item_fields_spans_refs_and_locator_binding_are_exact(self) -> None:
        source, tree, result = _pipeline()
        payload = _registry_payload(source, tree, result)
        target_id = result.assembly.scope_structure_node_ids[-1]
        item = payload["documents"][DOCUMENT_ID]["items"][target_id]
        item["source_order"] += 1
        item["semantic_reason_codes"] = ["tampered_reason"]
        item["is_continuation_fragment"] = not item["is_continuation_fragment"]
        item["source_span_ids"] = ["semantic-span-" + "f" * 32]
        changed_ref = item["source_refs"][0]
        changed_key = (changed_ref["kind"], changed_ref["source_id"])
        for expected_item in payload["documents"][DOCUMENT_ID]["items"].values():
            for ref_group in (
                "selected_evidence_source_refs",
                "source_refs",
                "full_context_source_refs",
            ):
                for bound_ref in expected_item[ref_group]:
                    if (bound_ref["kind"], bound_ref["source_id"]) == changed_key:
                        bound_ref["locator"]["page_number"] += 1
        registry = LlmRequirementSilverRegistry.model_validate(payload)

        report = evaluate_llm_requirement_silver(
            document_id=DOCUMENT_ID,
            source_ir=source,
            structure_tree=tree,
            result=result,
            registry=registry,
        )

        self.assertIs(report.status, LlmRequirementSilverCheckStatus.FAIL)
        self.assertEqual(
            _check(report, f"item.{target_id}.source_order").reason_code,
            "item_source_order_mismatch",
        )
        self.assertEqual(
            _check(report, f"item.{target_id}.span_ids").reason_code,
            "item_span_ids_mismatch",
        )
        self.assertEqual(
            _check(report, f"item.{target_id}.continuation_fragment").reason_code,
            "item_continuation_fragment_mismatch",
        )
        self.assertEqual(
            _check(report, f"item.{target_id}.semantic_reason_codes").reason_code,
            "item_semantic_reason_codes_mismatch",
        )
        locator_checks = [
            check
            for check in report.checks
            if check.category == "locator"
            and check.check_id.startswith(f"locator.{target_id}.")
        ]
        self.assertIn(
            "source_locator_mismatch", {item.reason_code for item in locator_checks}
        )


def _pipeline():  # type: ignore[no-untyped-def]
    source, _ = _hierarchical_source(
        leaf_heading="4.12.1 Synthetic measurement tool",
        leaf_paragraph="The tool shall retain its maximum reading.",
    )
    tree = build_structure_tree(source)
    result = extract_requirements_with_provider(source, tree, _FakeProvider())
    return source, tree, result


def _registry_payload(source, tree, result: LlmRequirementExtractionResult):  # type: ignore[no-untyped-def]
    document = build_llm_requirement_silver_document(
        source_ir=source,
        structure_tree=tree,
        result=result,
    )
    return {
        "schema_version": "llm-requirement-silver/v1",
        "provenance": {
            "label": "AI_REVIEWED_SILVER",
            "reviewed_by": "ai_assistant",
            "reviewed_at_utc": "2026-07-21T00:00:00Z",
            "is_human_gold": False,
            "is_serving_activation_gate": False,
            "limitations": [
                "not_human_gold",
                "not_a_serving_activation_gate",
                "source_hash_and_pipeline_version_bound",
                "text_free_locator_registry",
            ],
        },
        "documents": {DOCUMENT_ID: document.model_dump(mode="json")},
    }


def _load_payload(payload):  # type: ignore[no-untyped-def]
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "llm_silver.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_llm_requirement_silver(
            path,
            schema_path=DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH,
        )


def _check(report, check_id: str):  # type: ignore[no-untyped-def]
    return next(item for item in report.checks if item.check_id == check_id)


if __name__ == "__main__":
    unittest.main()
