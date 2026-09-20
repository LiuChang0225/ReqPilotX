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

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.application.extraction.classify_content import classify_content
from app.application.extraction.evaluate_pdf_shadow_silver import (
    DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
    PdfShadowSilverCheck,
    PdfShadowSilverCheckStatus,
    PdfShadowSilverDiagnosticExpectation,
    PdfShadowSilverFindingExpectation,
    PdfShadowSilverImageCellCellBinding,
    PdfShadowSilverImageCellImageBinding,
    PdfShadowSilverImageCellOwnershipAssignment,
    PdfShadowSilverImageCellOwnershipExpectation,
    PdfShadowSilverImageCellTableBinding,
    PdfShadowSilverPageAsset,
    PdfShadowSilverRegistryError,
    PdfShadowSilverReport,
    PdfShadowSilverStructureScopeExpectation,
    PdfShadowSilverTableExpectation,
    PdfShadowSilverVisualTextAnchor,
    PdfShadowSilverVisualTextAnchorFidelityExpectation,
    PdfShadowSilverVisualTextFidelityExpectation,
    PdfShadowSilverVisualTextRequiredToken,
    PdfShadowSilverVisualTextRegionExpectation,
    PdfShadowSilverVisualTextTokenRegion,
    canonical_visual_text_layout_bundle_sha256,
    canonical_visual_text_raw_text_bundle_sha256,
    canonical_visual_text_token_sha256,
    evaluate_pdf_shadow_silver,
    evaluate_pdf_shadow_silver_subset,
    load_pdf_shadow_silver,
)
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionPolicy,
    RequirementProjectionRole,
)
from app.domain.extraction.semantics import RequirementSubtype
from app.domain.extraction.structure import StructureRelationKind
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceAssetDerivation,
    SourceAssetDerivationKind,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceAssetKind,
    SourceAssetRef,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceNode,
    SourceNodeKind,
    MergedRegion,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


SOURCE_SHA256 = "5db1d344310f2a0a1e7384afb919a5ed4a0889a9af0dbd3bb20977deaac26d6e"
PAGE_ASSET_SHA256 = {
    11: "d999e0a34cf641b42ad84bb96e032eda5a3b04883f3629a1ad0fa9b678aa52ba",
    29: "96efc3c45f396e6ffe6d55f26c2d8686412832e09470e05d2908d9a7506a67ab",
    30: "32206ed78408bafde0ece4eed042c9912834d61e64c2863008b4ba7f01e3b9da",
    31: "92173d061f02a12631cc58479851cef7694d99727100ffae0e1f3efd6282df84",
}
F06_SOURCE_SHA256 = "1f3229f0650e36e9b99d56aa56bb24adbf96935a50328fe82e68e8be3c23d72c"
F06_FIGURE_ASSET_SHA256 = (
    "315c2f7e3e490454a4c9ed5b0bf00337aed4d44f6f66b9680e41b6303f1aa8d9"
)
F06_TILING_PATTERN_TILE_SHA256 = (
    "66a70e6062c90d1afd5a2508b0ad551c97016207582a9f6564e917eebe7cb18f"
)
F06_TILING_PATTERN_TILE_LOCATORS = (
    (13, 15),
    (13, 17),
    (45, 19),
    (45, 21),
    (46, 2),
    (46, 4),
)
F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX = (
    "source_ir.native_block_disposition:"
    "exclude_tiling_pattern_tile_from_content:"
    f"{F06_TILING_PATTERN_TILE_SHA256}"
)
PDS_SOURCE_SHA256 = "bdc1c72d36841093c0a87d50997be2dd7022a807bc8a8bda4358e3d2f4b3c43f"
PDS_ACTIVE_DAMPER_SOURCE_SHA256 = (
    "81a178bb7affc33a74bf0c9ff00f6c605a6fae1530d13c482606a086031fdb97"
)
IMAGE_CELL_ASSET_SHA256 = (
    "5ae0b40b1ebad7b8111acfb84d5b0768ff9a83229970c73c96a63f603719491d"
)
PDS_MASK_COMPOSITES = (
    (
        24,
        "4321afef94e2df378864afe1887690cbdcfb7202a4acd7d665718498cb288eca",
        "f41d0847c79a64f0a2de85eedd73f9de6c3ef1e4f3a4eecd80757ab8ec285cf9",
        "df0e155010806708c89f23012d953e2ab7d934ce0865e9c90b376ba58c404590",
    ),
    (
        27,
        "f105a0490c44b4bceefb05d0124b4280af524185f0156ac75b623517c34506f6",
        "2fcfb7b4ad66e9cefe7ce0de61550cabf45deb6f61b07281be6d34c777a4a033",
        "c9001ec4f406e89461f0b9ee82f2d0128c5f61618ad00a46e76fe8aa24d69683",
    ),
    (
        28,
        "638354ddcf3b334abf3800b7e9bb2b3bc9c8584359523e2714bee8a15c72028b",
        "6624900833eece7b8d7b15c36624e57bdab1e1c45dc8de743ba75d5bb6a12d83",
        "4b5ee1c5f822f56b29741241e917accb5347f0d59470030a35a7af148188488c",
    ),
)


class PdfShadowSilverTests(unittest.TestCase):
    def test_repository_registry_uses_formal_subtype_contract(self) -> None:
        registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))

        expectation = registry.documents["DOC-QC-T-1067"].semantic_expectations[0]
        self.assertIs(
            expectation.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        self.assertEqual(
            set(registry.provenance.limitations),
            {
                "not_human_gold",
                "not_a_serving_activation_gate",
                "source_hash_and_visual_asset_bound",
            },
        )
        self.assertEqual(
            registry.documents["DOC-QC-T-1067"].allowed_visual_provider_ids,
            (
                "aliyun-bailian-qwen-ocr",
                "azure-document-intelligence",
            ),
        )

    def test_matching_pipeline_produces_text_free_pass_checklist(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(report.failed_check_count, 0)
        self.assertEqual(report.blocked_check_count, 0)
        self.assertEqual(report.passed_check_count, 35)
        self.assertEqual(report.schema_version, "pdf-shadow-silver-evaluation/v2")
        self.assertEqual(
            report.source_ir_provenance.source_ir_id,
            source_ir.source_ir_id,
        )
        self.assertEqual(
            report.source_ir_provenance.adapter_id,
            source_ir.adapter_id,
        )
        self.assertEqual(
            report.source_ir_provenance.adapter_version,
            source_ir.adapter_version,
        )
        self.assertFalse(report.is_human_gold)
        self.assertFalse(report.is_serving_activation_gate)
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("Technical requirements", serialized)
        self.assertNotIn("condition alpha", serialized)

    def test_report_adapter_identity_is_required_and_tamper_evident(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()
        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )
        payload = report.model_dump(mode="json")

        missing_adapter_id = json.loads(json.dumps(payload))
        del missing_adapter_id["source_ir_provenance"]["adapter_id"]
        with self.assertRaises(ValueError):
            PdfShadowSilverReport.model_validate(missing_adapter_id)

        tampered_adapter_version = json.loads(json.dumps(payload))
        tampered_adapter_version["source_ir_provenance"]["adapter_version"] = (
            "tampered-adapter-version"
        )
        with self.assertRaisesRegex(
            ValueError,
            "SourceIR provenance does not match",
        ):
            PdfShadowSilverReport.model_validate(tampered_adapter_version)

    def test_subset_report_is_explicit_and_unselected_pages_are_not_checks(
        self,
    ) -> None:
        source_ir, _, _, _ = _build_matching_pipeline()
        selected_snapshot = next(
            snapshot
            for snapshot in source_ir.visual_text_snapshots
            if snapshot.page_number == 11
        )
        subset_source_ir = source_ir.model_copy(
            update={"visual_text_snapshots": (selected_snapshot,)}
        )

        report = evaluate_pdf_shadow_silver_subset(
            document_id="DOC-QC-T-1067",
            source_ir=subset_source_ir,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
            selected_page_numbers=(11,),
            provider_id=selected_snapshot.provider_id,
            provider_version=selected_snapshot.provider_version,
            expected_evaluated_content_types=(
                selected_snapshot.evaluated_content_types
            ),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(report.artifact_scope, "PAGE_SUBSET")
        self.assertEqual(report.quality_claim, "CAPTURE_BINDING_ONLY")
        self.assertEqual(report.unselected_page_disposition, "NOT_EVALUATED")
        self.assertEqual(
            report.provider_identity_scope,
            "FULL_AUTHORIZED_ROUTE",
        )
        self.assertFalse(report.is_full_document_checklist)
        self.assertFalse(report.is_content_acceptance)
        self.assertEqual(report.blocked_check_count, 0)
        self.assertEqual(
            report.source_ir_provenance.source_ir_id,
            subset_source_ir.source_ir_id,
        )
        self.assertEqual(
            report.source_ir_provenance.adapter_version,
            subset_source_ir.adapter_version,
        )
        self.assertEqual(report.selected_page_numbers, (11,))
        self.assertEqual(
            tuple(item.page_number for item in report.authorized_page_bindings),
            report.authorized_page_numbers,
        )
        self.assertEqual(
            report.unevaluated_authorized_page_numbers,
            (29, 30, 31),
        )
        self.assertEqual(
            tuple(item.page_number for item in report.page_evidence),
            (11,),
        )
        unselected_page_suffixes = (":29", ":30", ":31")
        self.assertFalse(
            any(
                check.check_id.endswith(unselected_page_suffixes)
                for check in report.checks
            )
        )
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("Technical requirements", serialized)
        self.assertNotIn("condition alpha", serialized)

    def test_subset_evaluator_rejects_page_outside_full_silver_route(self) -> None:
        source_ir, _, _, _ = _build_matching_pipeline()
        selected_snapshot = source_ir.visual_text_snapshots[0]

        with self.assertRaisesRegex(ValueError, "subset_page_not_authorized"):
            evaluate_pdf_shadow_silver_subset(
                document_id="DOC-QC-T-1067",
                source_ir=source_ir.model_copy(
                    update={"visual_text_snapshots": (selected_snapshot,)}
                ),
                registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
                selected_page_numbers=(999,),
                provider_id=selected_snapshot.provider_id,
                provider_version=selected_snapshot.provider_version,
                expected_evaluated_content_types=(
                    selected_snapshot.evaluated_content_types
                ),
            )

    def test_azure_visual_provider_is_allowed(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            visual_provider_id="azure-document-intelligence",
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(provider_check.reason_code, "matched")
        self.assertEqual(
            provider_check.observed,
            ("azure-document-intelligence",),
        )

    def test_forbidden_visual_provider_blocks_content_expectations(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            visual_provider_id="macos-vision-text",
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            provider_check.reason_code,
            "visual_provider_not_allowed",
        )
        self.assertEqual(provider_check.observed, ("macos-vision-text",))
        self.assertTrue(
            all(
                check.reason_code == "silver_binding_failed"
                and check.status is PdfShadowSilverCheckStatus.BLOCKED
                for check in report.checks
                if check.category != "binding"
            )
        )

    def test_missing_registered_page_snapshot_fails_provider_policy(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            visual_pages={11, 29, 30},
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            provider_check.reason_code,
            "visual_page_coverage_missing",
        )

    def test_missing_every_registered_snapshot_retains_explicit_reason(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            visual_pages=set(),
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertEqual(provider_check.reason_code, "visual_snapshot_missing")

    def test_empty_registered_page_scope_accepts_native_only_binding(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            visual_pages=set(),
        )
        registry = _registry_with_page_assets(())

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertEqual(provider_check.reason_code, "matched")
        self.assertEqual(provider_check.observed, ())
        self.assertFalse(
            any(
                check.check_id.startswith("binding.visual_page:")
                for check in report.checks
            )
        )

    def test_unregistered_visual_page_fails_exact_scope(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()
        registry = _registry_with_page_assets(
            tuple(
                asset
                for asset in load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
                .documents["DOC-QC-T-1067"]
                .page_assets
                if asset.page_number != 31
            )
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        provider_check = _check_by_id(
            report,
            "binding.visual_provider_policy",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(provider_check.reason_code, "visual_page_out_of_scope")

    def test_duplicate_visual_page_fails_exact_multiplicity(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            duplicate_visual_page=11,
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        page_check = _check_by_id(report, "binding.visual_page:11")
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(page_check.observed, 2)
        self.assertEqual(page_check.reason_code, "visual_page_ambiguous")

    def test_page_asset_mismatch_blocks_every_content_expectation(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            page_asset_overrides={31: "f" * 64},
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        page_check = _check_by_id(report, "binding.page_asset:31")
        self.assertEqual(page_check.reason_code, "page_asset_hash_mismatch")
        content_checks = [
            check for check in report.checks if check.category != "binding"
        ]
        self.assertTrue(content_checks)
        self.assertEqual(
            {check.reason_code for check in content_checks},
            {"silver_binding_failed"},
        )
        self.assertEqual(
            {check.status for check in content_checks},
            {PdfShadowSilverCheckStatus.BLOCKED},
        )
        self.assertEqual(report.blocked_check_count, len(content_checks))
        self.assertEqual(
            report.failed_check_count,
            sum(
                check.status is PdfShadowSilverCheckStatus.FAIL
                for check in report.checks
            ),
        )
        self.assertEqual(
            report.passed_check_count
            + report.failed_check_count
            + report.blocked_check_count,
            len(report.checks),
        )

    def test_duplicate_section_label_fails_without_selecting_a_candidate(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline(
            duplicate_equipment_section=True,
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        semantic_check = _check_by_id(report, "semantic:4.12.1.class")
        self.assertEqual(semantic_check.reason_code, "section_ambiguous")
        self.assertEqual(semantic_check.observed, 2)
        projection_check = _check_by_id(
            report,
            "projection:4.12.1.primary_count",
        )
        self.assertEqual(projection_check.reason_code, "section_ambiguous")

    def test_tampered_semantic_grounding_fails_closed(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()
        tampered_semantics = semantics.model_copy(
            update={"source_ir_id": "sir-00000000000000000000000000000000"}
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=tampered_semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        grounding = _check_by_id(report, "binding.semantic_grounding")
        self.assertEqual(grounding.reason_code, "semantic_grounding_invalid")
        self.assertTrue(
            all(
                check.reason_code == "silver_binding_failed"
                and check.status is PdfShadowSilverCheckStatus.BLOCKED
                for check in report.checks
                if check.category != "binding"
            )
        )

    def test_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(
                '{"schema_version":"pdf-shadow-silver/v1",'
                '"schema_version":"pdf-shadow-silver/v1"}',
                encoding="utf-8",
            )
            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_tampered_image_cell_relation_kind(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        assignment = _image_cell_assignment().model_dump(mode="json")
        assignment["relation_kind"] = "contains"
        payload["documents"]["DOC-F06-ACTIVE-SUSPENSION"][
            "image_cell_ownership_expectations"
        ] = [
            {
                "page_number": 4,
                "policy_id": "pdf-image-cell-geometry/v1",
                "assignments": [assignment],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_legacy_short_requirement_subtype(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["semantic_expectations"][0][
            "requirement_subtype"
        ] = "TEST_EQUIPMENT"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_forbidden_visual_provider_policy(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["allowed_visual_provider_ids"] = [
            "macos-vision-text"
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_invalid_document_projection_policy(self) -> None:
        base = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        for projection_policy in ("FORMAL_CONFIRMED", "AUTO"):
            with self.subTest(projection_policy=projection_policy):
                payload = json.loads(json.dumps(base))
                payload["documents"]["DOC-QC-T-1067"]["projection_policy"] = (
                    projection_policy
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "silver.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaises(PdfShadowSilverRegistryError):
                        load_pdf_shadow_silver(
                            path,
                            schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                        )

    def test_loader_requires_visual_text_fidelity_contract_array(self) -> None:
        base = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        for missing_field in ("projection_policy", "visual_text_fidelity_expectations"):
            with self.subTest(missing_field=missing_field):
                payload = json.loads(json.dumps(base))
                payload["documents"]["DOC-QC-T-1067"].pop(missing_field)
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "silver.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaises(PdfShadowSilverRegistryError):
                        load_pdf_shadow_silver(
                            path,
                            schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                        )

    def test_loader_rejects_forbidden_visual_text_fidelity_provider(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectation = _visual_text_fidelity_payload()
        expectation["provider_id"] = "macos-vision-text"
        payload["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            expectation
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_duplicate_visual_text_fidelity_profile(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        first = _visual_text_fidelity_payload()
        duplicate_key = {**first, "character_count": first["character_count"] + 1}
        payload["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            first,
            duplicate_key,
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_visual_text_fidelity_outside_page_scope(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectation = _visual_text_fidelity_payload()
        expectation["page_number"] = 999
        payload["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            expectation
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_visual_text_fidelity_provider_not_allowed_by_document(
        self,
    ) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["allowed_visual_provider_ids"] = [
            "azure-document-intelligence"
        ]
        payload["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            _visual_text_fidelity_payload()
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_counts_visual_text_fidelity_case_for_document_coverage(
        self,
    ) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectation = _visual_text_fidelity_payload()
        expectation["case_id"] = "TAB-999"
        payload["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            expectation
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_multiple_image_mask_groups_for_one_page(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectations = payload["documents"]["DOC-PDS-OIL-PUMP"][
            "image_mask_composite_expectations"
        ]
        expectations.append(json.loads(json.dumps(expectations[0])))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_overlapping_native_disposition_locators(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectations = payload["documents"]["DOC-F06-ACTIVE-SUSPENSION"][
            "native_block_disposition_expectations"
        ]
        duplicate_locator_group = json.loads(json.dumps(expectations[0]))
        duplicate_locator_group["content_sha256"] = "c" * 64
        expectations.append(duplicate_locator_group)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_accepts_empty_remote_ocr_scope(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["page_assets"] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            registry = load_pdf_shadow_silver(
                path,
                schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
            )

        self.assertEqual(
            registry.documents["DOC-QC-T-1067"].page_assets,
            (),
        )

    def test_page_asset_rotation_defaults_to_zero(self) -> None:
        page_asset = PdfShadowSilverPageAsset(
            page_number=1,
            page_image_sha256="a" * 64,
        )

        self.assertEqual(page_asset.input_rotation_cw_degrees, 0)

    def test_page_asset_rotation_accepts_only_quarter_turn_integers(self) -> None:
        for invalid_rotation in (45, True):
            with self.subTest(input_rotation_cw_degrees=invalid_rotation):
                with self.assertRaises(ValueError):
                    PdfShadowSilverPageAsset(
                        page_number=1,
                        input_rotation_cw_degrees=invalid_rotation,
                        page_image_sha256="a" * 64,
                    )

        rotated = PdfShadowSilverPageAsset(
            page_number=1,
            input_rotation_cw_degrees=90,
            page_image_sha256="a" * 64,
        )
        self.assertEqual(rotated.input_rotation_cw_degrees, 90)

    def test_loader_accepts_optional_page_asset_rotation(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["page_assets"][0][
            "input_rotation_cw_degrees"
        ] = 270
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            registry = load_pdf_shadow_silver(
                path,
                schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
            )

        self.assertEqual(
            registry.documents["DOC-QC-T-1067"]
            .page_assets[0]
            .input_rotation_cw_degrees,
            270,
        )

    def test_rendered_page_hash_binding_preserves_qc_t_33_checks(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()
        registry = _registry_with_page_assets(
            tuple(
                PdfShadowSilverPageAsset(
                    page_number=page_number,
                    page_image_sha256=page_hash,
                )
                for page_number, page_hash in sorted(PAGE_ASSET_SHA256.items())
            )
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(report.passed_check_count, 35)

    def test_rendered_page_hash_mismatch_fails_binding(self) -> None:
        source_ir, tree, semantics, view = _build_matching_pipeline()
        registry = _registry_with_page_assets(
            tuple(
                PdfShadowSilverPageAsset(
                    page_number=page_number,
                    page_image_sha256=("f" * 64 if page_number == 31 else page_hash),
                )
                for page_number, page_hash in sorted(PAGE_ASSET_SHA256.items())
            )
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        page_check = _check_by_id(report, "binding.page_asset:31")
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(page_check.reason_code, "page_image_hash_mismatch")

    def test_visual_text_fidelity_profile_passes_without_report_text(self) -> None:
        source_ir, tree, semantics, view, snapshot = (
            _build_visual_text_fidelity_pipeline()
        )
        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_with_visual_text_fidelity(snapshot),
        )

        prefix = "source_ir.visual_text:11:aliyun-bailian-qwen-ocr"
        for suffix in (
            "provider_profile",
            "observation_count",
            "character_count",
            "raw_text_bundle_sha256",
            "layout_bundle_sha256",
            "projected_requirement_count",
        ):
            self.assertIs(
                _check_by_id(report, f"{prefix}.{suffix}").status,
                PdfShadowSilverCheckStatus.PASS,
            )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("Technical requirements", serialized)
        self.assertNotIn("General requirements", serialized)

    def test_ncs_shape_separates_four_regressions_from_seventeen_blocked_checks(
        self,
    ) -> None:
        source_ir, _, _, _, snapshot = _build_visual_text_fidelity_pipeline()
        diagnostic_codes = (
            "pdf_full_page_image_without_visible_text",
            "pdf_mrc_scan_page_composited",
            "pdf_mrc_source_layer_folded",
            "pdf_visual_text_observations_captured",
        )
        diagnostics = tuple(
            SourceDiagnostic(
                code=code,
                message="Synthetic NCS evaluator coverage diagnostic.",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    block_index=code_index,
                ),
            )
            for page_number in range(1, 5)
            for code_index, code in enumerate(diagnostic_codes)
        )
        source_ir = source_ir.model_copy(update={"diagnostics": diagnostics})
        tree = build_structure_tree(source_ir)
        semantics = classify_content(source_ir, tree)
        view = project_requirements(source_ir, tree, semantics)
        registry = _registry_with_ncs_diagnostic_shape(
            snapshot,
            diagnostic_codes=diagnostic_codes,
        )

        passing_report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )
        actual_expectation_ids = {
            f"source_ir.diagnostic:{code}:{page_number}.count"
            for page_number in range(1, 5)
            for code in diagnostic_codes
        } | {
            (
                "source_ir.native_block_disposition_coverage:"
                "pdf_tiling_pattern_tile_excluded.exact_bindings"
            )
        }
        actual_expectation_checks = tuple(
            check
            for check in passing_report.checks
            if check.check_id in actual_expectation_ids
        )
        self.assertIs(
            passing_report.status,
            PdfShadowSilverCheckStatus.PASS,
        )
        self.assertEqual(len(actual_expectation_checks), 17)
        self.assertTrue(
            all(
                check.status is PdfShadowSilverCheckStatus.PASS
                and check.observed is not None
                for check in actual_expectation_checks
            )
        )

        changed_snapshot = snapshot.model_copy(
            update={"observations": snapshot.observations[:1]}
        )
        changed_source_ir, changed_tree, changed_semantics, changed_view = (
            _rebuild_with_visual_snapshot(source_ir, changed_snapshot)
        )
        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=changed_source_ir,
            structure_tree=changed_tree,
            semantics=changed_semantics,
            requirement_view=changed_view,
            registry=registry,
        )

        prefix = "source_ir.visual_text:11:aliyun-bailian-qwen-ocr"
        expected_failed_ids = {
            f"{prefix}.observation_count",
            f"{prefix}.character_count",
            f"{prefix}.raw_text_bundle_sha256",
            f"{prefix}.layout_bundle_sha256",
        }
        failed_checks = {
            check.check_id
            for check in report.checks
            if check.status is PdfShadowSilverCheckStatus.FAIL
        }
        blocked_checks = tuple(
            check
            for check in report.checks
            if check.status is PdfShadowSilverCheckStatus.BLOCKED
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(failed_checks, expected_failed_ids)
        self.assertEqual(report.failed_check_count, 4)
        self.assertEqual(report.blocked_check_count, 17)
        serialized_rollup = report.model_dump(mode="json")
        self.assertEqual(serialized_rollup["failed_check_count"], 4)
        self.assertEqual(serialized_rollup["blocked_check_count"], 17)
        self.assertEqual(
            {check.check_id for check in blocked_checks},
            actual_expectation_ids,
        )
        self.assertTrue(
            all(
                check.reason_code == "silver_binding_failed" and check.observed is None
                for check in blocked_checks
            )
        )
        self.assertEqual(
            report.passed_check_count
            + report.failed_check_count
            + report.blocked_check_count,
            len(report.checks),
        )

    def test_non_requirement_artifact_gate_is_zero_and_detects_projection_mutations(
        self,
    ) -> None:
        source_ir, _, _, _ = _build_matching_pipeline(visual_pages={11})
        snapshot = _visual_snapshot(
            page_number=11,
            page_image_sha256=PAGE_ASSET_SHA256[11],
            texts=("The pump shall withstand 10 bar.",),
            provider_id="aliyun-bailian-qwen-ocr",
        )
        source_ir, tree, semantics, shadow_view = _rebuild_with_visual_snapshot(
            source_ir,
            snapshot,
        )
        non_requirement_view = project_requirements(
            source_ir,
            tree,
            semantics,
            projection_policy=RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
        )
        registry = _registry_with_non_requirement_visual_text_fidelity(snapshot)

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=non_requirement_view,
            registry=registry,
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertIs(
            non_requirement_view.projection_policy,
            RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
        )
        self.assertEqual(non_requirement_view.nodes, ())
        self.assertEqual(
            sum(
                node.decision_mode
                in {RequirementDecisionMode.DIRECT, RequirementDecisionMode.ROLLUP}
                for node in non_requirement_view.nodes
            ),
            0,
        )
        self.assertIsNone(non_requirement_view.requirement_set_id)
        expected_checks = {
            "binding.projection_policy": (
                RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT.value,
                RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT.value,
            ),
            "projection.non_requirement_artifact.node_count": (0, 0),
            "projection.non_requirement_artifact.decision_count": (0, 0),
            "projection.non_requirement_artifact.requirement_set_bound": (
                False,
                False,
            ),
            (
                "source_ir.visual_text:11:aliyun-bailian-qwen-ocr."
                "projected_requirement_count"
            ): (0, 0),
        }
        for check_id, (expected, observed) in expected_checks.items():
            with self.subTest(check_id=check_id):
                check = _check_by_id(report, check_id)
                self.assertIs(check.status, PdfShadowSilverCheckStatus.PASS)
                self.assertEqual(check.expected, expected)
                self.assertEqual(check.observed, observed)

        shadow_report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=shadow_view,
            registry=registry,
        )
        self.assertIs(shadow_report.status, PdfShadowSilverCheckStatus.FAIL)
        for check_id in (
            "binding.projection_policy",
            "projection.non_requirement_artifact.node_count",
            "projection.non_requirement_artifact.decision_count",
            (
                "source_ir.visual_text:11:aliyun-bailian-qwen-ocr."
                "projected_requirement_count"
            ),
        ):
            with self.subTest(mutation="shadow_projection", check_id=check_id):
                self.assertIs(
                    _check_by_id(shadow_report, check_id).status,
                    PdfShadowSilverCheckStatus.FAIL,
                )

        requirement_set_report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=non_requirement_view.model_copy(
                update={"requirement_set_id": "requirement-set-mutated"}
            ),
            registry=registry,
        )
        self.assertIs(requirement_set_report.status, PdfShadowSilverCheckStatus.FAIL)
        requirement_set_check = _check_by_id(
            requirement_set_report,
            "projection.non_requirement_artifact.requirement_set_bound",
        )
        self.assertIs(
            requirement_set_check.status,
            PdfShadowSilverCheckStatus.FAIL,
        )
        self.assertEqual(
            requirement_set_check.reason_code,
            "non_requirement_artifact_requirement_set_forbidden",
        )

    def test_visual_text_fidelity_detects_deleted_and_duplicated_rows(self) -> None:
        source_ir, _, _, _, snapshot = _build_visual_text_fidelity_pipeline()
        registry = _registry_with_visual_text_fidelity(snapshot)
        duplicate_snapshot = _visual_snapshot(
            page_number=11,
            page_image_sha256=PAGE_ASSET_SHA256[11],
            texts=(
                "4 Technical requirements",
                "4.1 General requirements",
                "4.1 General requirements",
            ),
            provider_id="aliyun-bailian-qwen-ocr",
        )
        variants = {
            "deleted": snapshot.model_copy(update={"observations": ()}),
            "duplicated": duplicate_snapshot,
        }

        for variant, changed_snapshot in variants.items():
            with self.subTest(variant=variant):
                changed = _rebuild_with_visual_snapshot(source_ir, changed_snapshot)
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QC-T-1067",
                    source_ir=changed[0],
                    structure_tree=changed[1],
                    semantics=changed[2],
                    requirement_view=changed[3],
                    registry=registry,
                )

                check = _check_by_id(
                    report,
                    "source_ir.visual_text:11:aliyun-bailian-qwen-ocr."
                    "observation_count",
                )
                self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
                self.assertEqual(
                    check.reason_code,
                    "visual_text_observation_count_mismatch",
                )

    def test_visual_text_fidelity_detects_text_change_with_stable_id(self) -> None:
        source_ir, _, _, _, snapshot = _build_visual_text_fidelity_pipeline()
        original = snapshot.observations[0]
        replacement_text = original.raw_text[:-1] + (
            "x" if original.raw_text[-1] != "x" else "y"
        )
        changed_observation = original.model_copy(update={"raw_text": replacement_text})
        self.assertEqual(changed_observation.observation_id, original.observation_id)
        changed_snapshot = snapshot.model_copy(
            update={
                "observations": (
                    changed_observation,
                    *snapshot.observations[1:],
                )
            }
        )
        changed = _rebuild_with_visual_snapshot(source_ir, changed_snapshot)

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=changed[0],
            structure_tree=changed[1],
            semantics=changed[2],
            requirement_view=changed[3],
            registry=_registry_with_visual_text_fidelity(snapshot),
        )

        check = _check_by_id(
            report,
            "source_ir.visual_text:11:aliyun-bailian-qwen-ocr.raw_text_bundle_sha256",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(check.reason_code, "visual_text_raw_text_bundle_mismatch")
        self.assertNotIn(
            replacement_text,
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False),
        )

    def test_visual_text_fidelity_detects_layout_and_order_changes(self) -> None:
        source_ir, _, _, _, snapshot = _build_visual_text_fidelity_pipeline()
        first = snapshot.observations[0]
        shifted_bbox = (
            first.bbox[0] + 1.0,
            first.bbox[1],
            first.bbox[2] + 1.0,
            first.bbox[3],
        )
        shifted = _visual_observation_with_bbox(first, shifted_bbox)
        layout_snapshot = snapshot.model_copy(
            update={"observations": (shifted, *snapshot.observations[1:])}
        )
        ordered_texts = tuple(item.raw_text for item in snapshot.observations)
        order_snapshot = _visual_snapshot(
            page_number=11,
            page_image_sha256=PAGE_ASSET_SHA256[11],
            texts=tuple(reversed(ordered_texts)),
            provider_id="aliyun-bailian-qwen-ocr",
        )
        variants = (
            (
                layout_snapshot,
                "layout_bundle_sha256",
                "visual_text_layout_bundle_mismatch",
            ),
            (
                order_snapshot,
                "raw_text_bundle_sha256",
                "visual_text_raw_text_bundle_mismatch",
            ),
        )

        for changed_snapshot, suffix, reason_code in variants:
            with self.subTest(suffix=suffix):
                changed = _rebuild_with_visual_snapshot(source_ir, changed_snapshot)
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QC-T-1067",
                    source_ir=changed[0],
                    structure_tree=changed[1],
                    semantics=changed[2],
                    requirement_view=changed[3],
                    registry=_registry_with_visual_text_fidelity(snapshot),
                )

                check = _check_by_id(
                    report,
                    f"source_ir.visual_text:11:aliyun-bailian-qwen-ocr.{suffix}",
                )
                self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
                self.assertEqual(check.reason_code, reason_code)

    def test_visual_text_fidelity_rejects_provider_version_profile_mismatch(
        self,
    ) -> None:
        source_ir, tree, semantics, view, snapshot = (
            _build_visual_text_fidelity_pipeline()
        )
        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_with_visual_text_fidelity(
                snapshot,
                provider_version_sha256="f" * 64,
            ),
        )

        profile = _check_by_id(
            report,
            "source_ir.visual_text:11:aliyun-bailian-qwen-ocr.provider_profile",
        )
        count = _check_by_id(
            report,
            "source_ir.visual_text:11:aliyun-bailian-qwen-ocr.observation_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            profile.reason_code,
            "visual_text_provider_profile_mismatch",
        )
        self.assertEqual(
            count.reason_code,
            "visual_text_provider_profile_mismatch",
        )

    def test_visual_text_reviewed_region_is_exact_and_text_free(self) -> None:
        source_ir, tree, semantics, view, snapshot = (
            _build_visual_text_fidelity_pipeline()
        )
        first = snapshot.observations[0]
        region = PdfShadowSilverVisualTextRegionExpectation(
            region_id="handwriting",
            bbox_q100=(3_900, 9_900, 56_100, 12_100),
            observation_count=1,
            character_count=len(first.raw_text),
            raw_text_bundle_sha256=(
                canonical_visual_text_raw_text_bundle_sha256((first,))
            ),
            layout_bundle_sha256=(canonical_visual_text_layout_bundle_sha256((first,))),
        )
        registry = _registry_with_visual_text_fidelity(
            snapshot,
            reviewed_regions=(region,),
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        prefix = "source_ir.visual_text:11:aliyun-bailian-qwen-ocr.region:handwriting"
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertIs(
            _check_by_id(report, f"{prefix}.raw_text_bundle_sha256").status,
            PdfShadowSilverCheckStatus.PASS,
        )
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn(first.raw_text, serialized)

        moved_region = region.model_copy(
            update={"bbox_q100": (3_900, 12_900, 56_100, 15_100)}
        )
        failed = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_with_visual_text_fidelity(
                snapshot,
                reviewed_regions=(moved_region,),
            ),
        )
        self.assertIs(failed.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            _check_by_id(
                failed,
                f"{prefix}.raw_text_bundle_sha256",
            ).reason_code,
            "visual_text_region_raw_text_bundle_mismatch",
        )

    def test_visual_text_anchor_fidelity_uses_geometric_order_and_is_text_free(
        self,
    ) -> None:
        source_ir, tree, semantics, view, snapshot = _build_anchor_fidelity_pipeline()
        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_with_visual_text_anchor_fidelity(snapshot),
        )

        prefix = "source_ir.visual_text:11:aliyun-bailian-qwen-ocr"
        for suffix in (
            "provider_profile",
            "observation_count_min",
            "observation_count_max",
            "character_count_min",
            "character_count_max",
            "ordered_anchor_sequence",
            "anchor:0.occurrences",
            "anchor:0.axis_center",
            "region:comments.token:0.occurrences",
            "projected_requirement_count",
        ):
            self.assertIs(
                _check_by_id(report, f"{prefix}.{suffix}").status,
                PdfShadowSilverCheckStatus.PASS,
            )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        for raw_text in ("Gamma anchor", "Alpha approved", "Beta comments"):
            self.assertNotIn(raw_text, serialized)

    def test_visual_text_anchor_fidelity_detects_content_region_and_geometry_drift(
        self,
    ) -> None:
        source_ir, _, _, _, snapshot = _build_anchor_fidelity_pipeline()
        registry = _registry_with_visual_text_anchor_fidelity(snapshot)
        variants = {
            "missing": (
                _anchor_snapshot(
                    (
                        ("Gamma anchor", (290.0, 100.0, 310.0, 120.0)),
                        ("Xxxxx approved", (90.0, 100.0, 110.0, 120.0)),
                        ("Beta comments", (190.0, 100.0, 210.0, 120.0)),
                    )
                ),
                "anchor:0.occurrences",
                "visual_text_anchor_occurrence_mismatch",
            ),
            "duplicated": (
                _anchor_snapshot(
                    (
                        ("Beta anchor", (290.0, 100.0, 310.0, 120.0)),
                        ("Alpha approved", (90.0, 100.0, 110.0, 120.0)),
                        ("Beta comments", (190.0, 100.0, 210.0, 120.0)),
                    )
                ),
                "anchor:1.occurrences",
                "visual_text_anchor_occurrence_mismatch",
            ),
            "geometric_order": (
                _anchor_snapshot(
                    (
                        ("Gamma anchor", (290.0, 100.0, 310.0, 120.0)),
                        ("Alpha approved", (190.0, 100.0, 210.0, 120.0)),
                        ("Beta comments", (90.0, 100.0, 110.0, 120.0)),
                    )
                ),
                "ordered_anchor_sequence",
                "visual_text_anchor_sequence_mismatch",
            ),
            "axis_position": (
                _anchor_snapshot(
                    (
                        ("Gamma anchor", (290.0, 100.0, 310.0, 120.0)),
                        ("Alpha approved", (90.0, 100.0, 110.0, 120.0)),
                        ("Beta comments", (192.1, 100.0, 212.1, 120.0)),
                    )
                ),
                "anchor:1.axis_center",
                "visual_text_anchor_axis_center_mismatch",
            ),
            "region_token": (
                _anchor_snapshot(
                    (
                        ("Gamma anchor", (290.0, 100.0, 310.0, 120.0)),
                        ("Alpha approved", (90.0, 100.0, 110.0, 120.0)),
                        ("Beta nonsense", (190.0, 100.0, 210.0, 120.0)),
                    )
                ),
                "region:comments.token:1.occurrences",
                "visual_text_region_token_occurrence_mismatch",
            ),
        }

        prefix = "source_ir.visual_text:11:aliyun-bailian-qwen-ocr"
        for variant, (changed_snapshot, suffix, reason) in variants.items():
            with self.subTest(variant=variant):
                changed = _rebuild_anchor_fidelity_pipeline(
                    source_ir,
                    changed_snapshot,
                )
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QC-T-1067",
                    source_ir=changed[0],
                    structure_tree=changed[1],
                    semantics=changed[2],
                    requirement_view=changed[3],
                    registry=registry,
                )
                check = _check_by_id(report, f"{prefix}.{suffix}")
                self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
                self.assertEqual(check.reason_code, reason)

    def test_visual_text_anchor_fidelity_optional_raw_bundle_is_strict(self) -> None:
        source_ir, _, _, _, snapshot = _build_anchor_fidelity_pipeline()
        changed_snapshot = _anchor_snapshot(
            (
                ("Gamma marker", (290.0, 100.0, 310.0, 120.0)),
                ("Alpha approved", (90.0, 100.0, 110.0, 120.0)),
                ("Beta comments", (190.0, 100.0, 210.0, 120.0)),
            )
        )
        changed = _rebuild_anchor_fidelity_pipeline(source_ir, changed_snapshot)
        without_raw = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=changed[0],
            structure_tree=changed[1],
            semantics=changed[2],
            requirement_view=changed[3],
            registry=_registry_with_visual_text_anchor_fidelity(snapshot),
        )
        with_raw = evaluate_pdf_shadow_silver(
            document_id="DOC-QC-T-1067",
            source_ir=changed[0],
            structure_tree=changed[1],
            semantics=changed[2],
            requirement_view=changed[3],
            registry=_registry_with_visual_text_anchor_fidelity(
                snapshot,
                ordered_raw_text_bundle_sha256=(
                    canonical_visual_text_raw_text_bundle_sha256(snapshot.observations)
                ),
            ),
        )

        self.assertIs(without_raw.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            _check_by_id(
                with_raw,
                "source_ir.visual_text:11:aliyun-bailian-qwen-ocr."
                "ordered_raw_text_bundle_sha256",
            ).reason_code,
            "visual_text_raw_text_bundle_mismatch",
        )

    def test_loader_accepts_anchor_fidelity_and_requires_exact_page_scope(self) -> None:
        base = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        document = base["documents"]["DOC-QC-T-1067"]
        document["page_assets"] = [document["page_assets"][0]]
        document["visual_text_fidelity_expectations"] = [
            _visual_text_anchor_fidelity_payload()
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            loaded = load_pdf_shadow_silver(
                path,
                schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
            )
        self.assertIsInstance(
            loaded.documents["DOC-QC-T-1067"].visual_text_fidelity_expectations[0],
            PdfShadowSilverVisualTextAnchorFidelityExpectation,
        )

        invalid = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        invalid["documents"]["DOC-QC-T-1067"]["visual_text_fidelity_expectations"] = [
            _visual_text_anchor_fidelity_payload()
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_requires_exactly_one_page_hash_binding(self) -> None:
        base = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        for update in (
            {"source_asset_sha256": "a" * 64, "page_image_sha256": "b" * 64},
            {},
        ):
            with self.subTest(update=update):
                payload = json.loads(json.dumps(base))
                page_asset = payload["documents"]["DOC-QC-T-1067"]["page_assets"][0]
                page_asset.pop("source_asset_sha256", None)
                page_asset.update(update)
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "silver.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(PdfShadowSilverRegistryError):
                        load_pdf_shadow_silver(
                            path,
                            schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                        )

    def test_numbered_child_ordinals_can_preserve_duplicates(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QC-T-1067"]["numbered_children"][0]["ordinals"] = [
            1,
            2,
            3,
            4,
            5,
            5,
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            registry = load_pdf_shadow_silver(
                path,
                schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
            )

        self.assertEqual(
            registry.documents["DOC-QC-T-1067"].numbered_children[0].ordinals,
            (1, 2, 3, 4, 5, 5),
        )

    def test_semantic_expectations_support_non_requirement_classes(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        expectation = payload["documents"]["DOC-QC-T-1067"]["semantic_expectations"][0]
        expectation["semantic_class"] = "SCHEDULE"
        expectation["requirement_subtype"] = None
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            registry = load_pdf_shadow_silver(
                path,
                schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
            )

        parsed = registry.documents["DOC-QC-T-1067"].semantic_expectations[0]
        self.assertEqual(parsed.semantic_class.value, "SCHEDULE")
        self.assertIsNone(parsed.requirement_subtype)

    def test_loader_rejects_invalid_semantic_subtype_ownership(self) -> None:
        base = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        for semantic_class, subtype in (
            ("SCHEDULE", "PRODUCT_REQUIREMENT"),
            ("REQUIREMENT", None),
        ):
            with self.subTest(semantic_class=semantic_class):
                payload = json.loads(json.dumps(base))
                expectation = payload["documents"]["DOC-QC-T-1067"][
                    "semantic_expectations"
                ][0]
                expectation["semantic_class"] = semantic_class
                expectation["requirement_subtype"] = subtype
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "silver.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(PdfShadowSilverRegistryError):
                        load_pdf_shadow_silver(
                            path,
                            schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                        )

    def test_loader_requires_every_registered_case_to_have_expectations(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QSK-J02-311"]["case_ids"].append("TAB-999")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_loader_rejects_expectation_for_unregistered_case(self) -> None:
        payload = json.loads(require_private_eval("extraction_cases/silver.json").read_text(encoding="utf-8"))
        payload["documents"]["DOC-QSK-J02-311"]["outline_chains"][0]["case_id"] = (
            "TAB-999"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PdfShadowSilverRegistryError):
                load_pdf_shadow_silver(
                    path,
                    schema_path=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
                )

    def test_qsk_native_only_table_scope_passes_without_row_projection(self) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QSK-J02-311",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_without_qsk_mask_expectations(),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(report.failed_check_count, 0)
        self.assertEqual(report.passed_check_count, 12)
        self.assertEqual(
            _check_by_id(
                report,
                "projection.scope:8.3.1:content:table.count",
            ).observed,
            1,
        )
        self.assertEqual(
            _check_by_id(
                report,
                "projection.scope:8.3.1:any:table_row.count",
            ).observed,
            0,
        )

    def test_qsk_registry_binds_two_exact_image_mask_pages(self) -> None:
        document = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")).documents["DOC-QSK-J02-311"]
        expectations = document.image_mask_composite_expectations

        self.assertEqual(
            tuple((item.page_number, item.block_indices) for item in expectations),
            (
                (32, (5, 7, 11, 15, 19, 23)),
                (33, (2, 4, 8, 12, 16, 20)),
            ),
        )
        self.assertTrue(
            all(
                item.expected_source_status == "COMPLETE"
                and item.projected_requirement_count == 0
                and len(item.composite_content_sha256) == 6
                and len(item.base_content_sha256) == 6
                and len(item.mask_content_sha256) == 6
                for item in expectations
            )
        )
        canonical = json.dumps(
            [item.model_dump(mode="json") for item in expectations],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "84560e2f96bbb95af4a452e8f21cf699a30c2a79ab2f16238cc3c73d8e06ffb9",
        )

    def test_finding_expectation_counts_machine_codes_in_section_scope(self) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()
        registry = _registry_without_qsk_mask_expectations()
        document = registry.documents["DOC-QSK-J02-311"].model_copy(
            update={
                "finding_expectations": (
                    PdfShadowSilverFindingExpectation(
                        case_id="STR-002",
                        section_label="8.3.1",
                        code="section_ancestor_missing",
                        count=0,
                    ),
                )
            }
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-QSK-J02-311": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QSK-J02-311",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        finding_check = _check_by_id(
            report,
            "finding:section_ancestor_missing:8.3.1.count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(finding_check.observed, 0)

    def test_diagnostic_expectation_reports_match_and_count_mismatch(self) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()
        diagnostic_message = "Synthetic page warning must stay out of reports."
        source_ir = source_ir.model_copy(
            update={
                "diagnostics": (
                    SourceDiagnostic(
                        code="synthetic_page_warning",
                        message=diagnostic_message,
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=66,
                            block_index=99,
                        ),
                    ),
                )
            }
        )

        for expected_count, expected_status, expected_reason in (
            (1, PdfShadowSilverCheckStatus.PASS, "matched"),
            (2, PdfShadowSilverCheckStatus.FAIL, "diagnostic_count_mismatch"),
        ):
            with self.subTest(expected_count=expected_count):
                registry = _registry_with_qsk_expectation(
                    "diagnostic_expectations",
                    PdfShadowSilverDiagnosticExpectation(
                        case_id="STR-002",
                        code="synthetic_page_warning",
                        page_number=66,
                        count=expected_count,
                    ),
                )
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QSK-J02-311",
                    source_ir=source_ir,
                    structure_tree=tree,
                    semantics=semantics,
                    requirement_view=view,
                    registry=registry,
                )

                check = _check_by_id(
                    report,
                    "source_ir.diagnostic:synthetic_page_warning:66.count",
                )
                self.assertIs(report.status, expected_status)
                self.assertIs(check.status, expected_status)
                self.assertEqual(check.observed, 1)
                self.assertEqual(check.reason_code, expected_reason)
                self.assertNotIn(
                    diagnostic_message,
                    json.dumps(report.model_dump(mode="json")),
                )

    def test_structure_scope_expectation_reports_match_and_count_mismatch(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()

        for expected_count, expected_status, expected_reason in (
            (2, PdfShadowSilverCheckStatus.PASS, "matched"),
            (1, PdfShadowSilverCheckStatus.FAIL, "structure_scope_count_mismatch"),
        ):
            with self.subTest(expected_count=expected_count):
                registry = _registry_with_qsk_expectation(
                    "structure_scope_expectations",
                    PdfShadowSilverStructureScopeExpectation(
                        case_id="STR-002",
                        section_label="8.3.1",
                        target_kind="table",
                        direct_only=True,
                        count=expected_count,
                    ),
                )
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QSK-J02-311",
                    source_ir=source_ir,
                    structure_tree=tree,
                    semantics=semantics,
                    requirement_view=view,
                    registry=registry,
                )

                check = _check_by_id(
                    report,
                    "structure.scope:8.3.1:direct:table.count",
                )
                self.assertIs(report.status, expected_status)
                self.assertIs(check.status, expected_status)
                self.assertEqual(check.observed, 2)
                self.assertEqual(check.reason_code, expected_reason)

    def test_table_expectation_reports_match_and_count_mismatch(self) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()

        for expected_count, expected_status, expected_reason in (
            (1, PdfShadowSilverCheckStatus.PASS, "matched"),
            (2, PdfShadowSilverCheckStatus.FAIL, "table_scope_count_mismatch"),
        ):
            with self.subTest(expected_count=expected_count):
                registry = _registry_with_qsk_expectation(
                    "table_expectations",
                    PdfShadowSilverTableExpectation(
                        case_id="STR-002",
                        section_label="8.3.1",
                        page_number=66,
                        row_count=2,
                        column_count=2,
                        count=expected_count,
                    ),
                )
                report = evaluate_pdf_shadow_silver(
                    document_id="DOC-QSK-J02-311",
                    source_ir=source_ir,
                    structure_tree=tree,
                    semantics=semantics,
                    requirement_view=view,
                    registry=registry,
                )

                check = _check_by_id(
                    report,
                    "structure.table:8.3.1:66:2x2.count",
                )
                self.assertIs(report.status, expected_status)
                self.assertIs(check.status, expected_status)
                self.assertEqual(check.observed, 1)
                self.assertEqual(check.reason_code, expected_reason)

    def test_f06_native_fixture_passes_table_note_and_figure_expectations(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            (report.passed_check_count, report.failed_check_count), (42, 0)
        )
        disposition_prefix = F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX
        self.assertEqual(
            _check_by_id(report, f"{disposition_prefix}.source_status").observed,
            "COMPLETE",
        )
        self.assertEqual(
            _check_by_id(
                report,
                f"{disposition_prefix}.diagnostic_locator_keys",
            ).observed,
            tuple(
                f"{page}:{block}" for page, block in F06_TILING_PATTERN_TILE_LOCATORS
            ),
        )
        self.assertEqual(
            _check_by_id(report, f"{disposition_prefix}.content_sha256").observed,
            F06_TILING_PATTERN_TILE_SHA256,
        )
        self.assertEqual(
            _check_by_id(
                report,
                "structure.table_note:9.16.2:28:4x7.applies_to_rows",
            ).observed,
            (1, 2),
        )
        merged_support = next(
            node for node in tree.nodes if node.boundary_basis.value == "merged_range"
        )
        self.assertNotIn(
            merged_support.structure_node_id,
            {node.target_structure_node_id for node in view.nodes},
        )
        primary = next(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.PRIMARY
            and set(merged_support.source_refs).issubset(
                set(node.full_context_source_refs)
            )
        )
        self.assertIsNotNone(primary)
        self.assertEqual(
            _check_by_id(
                report,
                "figure:9.16.3:28:image.reference_relation_count",
            ).observed,
            2,
        )
        serialized = json.dumps(report.model_dump(mode="json"))
        self.assertNotIn("Synthetic tolerance table", serialized)
        self.assertNotIn("Synthetic calculation diagram", serialized)
        self.assertNotIn("Synthetic tiling-pattern tile disposition", serialized)

    def test_image_cell_ownership_passes_exact_text_free_page_contract(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_image_cell_pipeline()
        assignment = _image_cell_assignment()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_for_image_cell_assignment(assignment),
        )

        prefix = "image_cell_ownership:4:0"
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            _check_by_id(
                report,
                "image_cell_ownership:4.exact_assignments",
            ).expected,
            _check_by_id(
                report,
                "image_cell_ownership:4.exact_assignments",
            ).observed,
        )
        for suffix in (
            "placement_count",
            "unique_table_count",
            "unique_cell_count",
            "independent_structure_count",
            "relation_count",
            "projected_requirement_count",
        ):
            with self.subTest(suffix=suffix):
                self.assertIs(
                    _check_by_id(report, f"{prefix}.{suffix}").status,
                    PdfShadowSilverCheckStatus.PASS,
                )
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("cell secret", serialized)
        self.assertNotIn("synthetic/image-cell.png", serialized)

    def test_multi_cell_ownership_accepts_one_exact_grounded_relation(self) -> None:
        source_ir, tree, semantics, view = _build_image_cell_pipeline(multi_cell=True)
        assignment = _image_multi_cell_assignment()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_for_image_cell_assignment(assignment),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            _check_by_id(
                report,
                "image_cell_ownership:4:0.unique_cell_count",
            ).observed,
            4,
        )
        self.assertEqual(
            _check_by_id(
                report,
                "image_cell_ownership:4:0.relation_count",
            ).observed,
            1,
        )
        self.assertEqual(
            sum(
                relation.kind is StructureRelationKind.PLACED_OVER_CELLS
                for relation in tree.relations
            ),
            1,
        )

    def test_tampered_image_cell_binding_fails_exact_relation_grounding(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_image_cell_pipeline()
        assignment = _image_cell_assignment()
        tampered_cell = assignment.cell.model_copy(
            update={"bbox_q100": (25_100, 10_000, 45_000, 20_000)}
        )
        tampered_assignment = assignment.model_copy(update={"cell": tampered_cell})

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_for_image_cell_assignment(tampered_assignment),
        )

        coverage = _check_by_id(
            report,
            "image_cell_ownership:4.exact_assignments",
        )
        relation = _check_by_id(
            report,
            "image_cell_ownership:4:0.relation_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            coverage.reason_code,
            "image_cell_reviewed_page_coverage_mismatch",
        )
        self.assertEqual(
            relation.reason_code,
            "image_cell_relation_grounding_mismatch",
        )
        self.assertEqual(relation.observed, 0)

    def test_ambiguous_image_table_geometry_fails_closed(self) -> None:
        source_ir, tree, semantics, view = _build_image_cell_pipeline(
            ambiguous_table=True
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_for_image_cell_assignment(_image_cell_assignment()),
        )

        table_check = _check_by_id(
            report,
            "image_cell_ownership:4:0.unique_table_count",
        )
        relation_check = _check_by_id(
            report,
            "image_cell_ownership:4:0.relation_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            table_check.reason_code,
            "image_cell_table_ownership_not_unique",
        )
        self.assertEqual(table_check.observed, 0)
        self.assertEqual(relation_check.observed, 0)

    def test_empty_native_disposition_registry_rejects_unregistered_fact(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_qsk_native_pipeline()
        source_ir = source_ir.model_copy(
            update={
                "diagnostics": (
                    SourceDiagnostic(
                        code="pdf_tiling_pattern_tile_excluded",
                        severity=SourceDiagnosticSeverity.INFO,
                        message="Synthetic unexpected disposition.",
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=66,
                            block_index=99,
                        ),
                        evidence_sha256="d" * 64,
                    ),
                )
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-QSK-J02-311",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        check = _check_by_id(
            report,
            "source_ir.native_block_disposition_coverage:"
            "pdf_tiling_pattern_tile_excluded.exact_bindings",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "native_block_disposition_coverage_mismatch",
        )
        self.assertEqual(len(check.observed), 1)

    def test_f06_native_disposition_requires_complete_source_status(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        source_ir = source_ir.model_copy(update={"status": SourceIRStatus.PARTIAL})

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        check = _check_by_id(
            report,
            f"{F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX}.source_status",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "native_block_disposition_source_status_mismatch",
        )
        self.assertEqual(check.observed, "PARTIAL")

    def test_f06_native_disposition_rejects_missing_bbox_diagnostic(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        source_ir = source_ir.model_copy(
            update={
                "diagnostics": (
                    *source_ir.diagnostics,
                    SourceDiagnostic(
                        code="pdf_block_bbox_missing",
                        message="Synthetic forbidden missing bbox diagnostic.",
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=13,
                            block_index=15,
                        ),
                    ),
                )
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        check = _check_by_id(
            report,
            f"{F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX}.forbidden_diagnostic_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "native_block_disposition_forbidden_diagnostic_present",
        )
        self.assertEqual(check.observed, 1)

    def test_f06_native_disposition_requires_exact_locator_set(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        source_ir = source_ir.model_copy(
            update={"diagnostics": source_ir.diagnostics[:-1]}
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        locator_check = _check_by_id(
            report,
            f"{F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX}.diagnostic_locator_keys",
        )
        count_check = _check_by_id(
            report,
            f"{F06_TILING_PATTERN_DISPOSITION_CHECK_PREFIX}.diagnostic_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            locator_check.reason_code,
            "native_block_disposition_locator_mismatch",
        )
        self.assertEqual(count_check.observed, 5)

    def test_f06_native_disposition_binds_exact_content_sha256(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
        document = registry.documents["DOC-F06-ACTIVE-SUSPENSION"]
        disposition = document.native_block_disposition_expectations[0].model_copy(
            update={"content_sha256": "0" * 64}
        )
        document = document.model_copy(
            update={"native_block_disposition_expectations": (disposition,)}
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-F06-ACTIVE-SUSPENSION": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(
            report,
            "source_ir.native_block_disposition:"
            "exclude_tiling_pattern_tile_from_content:"
            f"{'0' * 64}.content_sha256",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "native_block_disposition_content_sha256_mismatch",
        )
        self.assertEqual(
            check.observed,
            (F06_TILING_PATTERN_TILE_SHA256,) * 6,
        )

    def test_f06_native_disposition_rejects_unregistered_extra_binding(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        source_ir = source_ir.model_copy(
            update={
                "diagnostics": (
                    *source_ir.diagnostics,
                    SourceDiagnostic(
                        code="pdf_tiling_pattern_tile_excluded",
                        severity=SourceDiagnosticSeverity.INFO,
                        message="Synthetic unregistered disposition binding.",
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=47,
                            block_index=1,
                        ),
                        evidence_sha256="c" * 64,
                    ),
                )
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        check = _check_by_id(
            report,
            "source_ir.native_block_disposition_coverage:"
            "pdf_tiling_pattern_tile_excluded.exact_bindings",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "native_block_disposition_coverage_mismatch",
        )
        self.assertEqual(len(check.observed), 7)

    def test_f06_table_note_scope_mismatch_is_a_machine_failure(self) -> None:
        source_ir, tree, semantics, view = _build_f06_native_pipeline()
        registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
        document = registry.documents["DOC-F06-ACTIVE-SUSPENSION"]
        note = document.table_note_expectations[0].model_copy(
            update={"applies_to_row_indices": (1,)}
        )
        document = document.model_copy(update={"table_note_expectations": (note,)})
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-F06-ACTIVE-SUSPENSION": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-F06-ACTIVE-SUSPENSION",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(
            report,
            "structure.table_note:9.16.2:28:4x7.applies_to_rows",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(check.reason_code, "table_note_scope_mismatch")
        self.assertEqual(check.observed, (1, 2))

    def test_pds_native_fixture_passes_exact_option_state_projection(self) -> None:
        source_ir, tree, semantics, view = _build_pds_native_pipeline()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_without_pds_image_cell_ownership_expectations(),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            (report.passed_check_count, report.failed_check_count),
            (52, 0),
        )
        self.assertEqual(
            _check_by_id(
                report,
                "revision_history:1:0:15x10.semantic_classes",
            ).observed,
            ("7:VERSION_METADATA", "8:VERSION_METADATA"),
        )
        self.assertEqual(
            _check_by_id(
                report,
                "option_state:4.1:9.source_states",
            ).observed,
            (
                "checked",
                "unchecked",
                "unchecked",
                "checked",
                "unchecked",
                "unchecked",
                "unchecked",
                "checked",
                "unchecked",
                "unchecked",
                "unchecked",
            ),
        )
        self.assertEqual(
            _check_by_id(
                report,
                "option_state:4.1:9.projected_checked_count",
            ).observed,
            3,
        )
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("区域甲", serialized)
        self.assertNotIn("风险项甲", serialized)

    def test_pds_image_mask_composite_hash_mismatch_is_machine_failure(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_pds_native_pipeline()
        registry = _registry_without_pds_image_cell_ownership_expectations()
        document = registry.documents["DOC-PDS-OIL-PUMP"]
        expectation = document.image_mask_composite_expectations[0].model_copy(
            update={
                "composite_content_sha256": (
                    "0" * 64,
                    *document.image_mask_composite_expectations[
                        0
                    ].composite_content_sha256[1:],
                )
            }
        )
        document = document.model_copy(
            update={"image_mask_composite_expectations": (expectation,)}
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-PDS-OIL-PUMP": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(
            report,
            "source_ir.image_mask_composite:4.composite_content_sha256",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(check.reason_code, "image_mask_composite_hash_mismatch")

    def test_pds_repeated_composite_may_share_one_content_asset(self) -> None:
        source_ir, _, _, _ = _build_pds_native_pipeline()
        image_nodes = tuple(
            node for node in source_ir.nodes if node.asset_derivation is not None
        )
        first_asset_ref = image_nodes[0].asset_ref
        second_asset_ref = image_nodes[1].asset_ref
        shared_nodes = tuple(
            node.model_copy(update={"asset_ref": first_asset_ref})
            if node.source_node_id == image_nodes[1].source_node_id
            else node
            for node in source_ir.nodes
        )
        shared_source_ir = source_ir.model_copy(
            update={
                "nodes": shared_nodes,
                "assets": tuple(
                    asset
                    for asset in source_ir.assets
                    if asset.asset_id != second_asset_ref
                ),
            }
        )
        tree = build_structure_tree(shared_source_ir)
        semantics = classify_content(shared_source_ir, tree)
        view = project_requirements(shared_source_ir, tree, semantics)

        registry = _registry_without_pds_image_cell_ownership_expectations()
        document = registry.documents["DOC-PDS-OIL-PUMP"]
        original = document.image_mask_composite_expectations[0]
        repeated = original.model_copy(
            update={
                "composite_content_sha256": (
                    original.composite_content_sha256[0],
                    original.composite_content_sha256[0],
                    original.composite_content_sha256[2],
                )
            }
        )
        document = document.model_copy(
            update={"image_mask_composite_expectations": (repeated,)}
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-PDS-OIL-PUMP": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=shared_source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(
            report,
            "source_ir.image_mask_composite:4.unique_asset_count",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(check.expected, 2)
        self.assertEqual(check.observed, 2)

    def test_pds_image_mask_diagnostics_must_ground_each_placement(self) -> None:
        source_ir, tree, semantics, view = _build_pds_native_pipeline()
        first_image_id = next(
            node.source_node_id
            for node in source_ir.nodes
            if node.asset_derivation is not None
        )
        tampered_source_ir = source_ir.model_copy(
            update={
                "diagnostics": tuple(
                    diagnostic.model_copy(update={"source_node_id": first_image_id})
                    if diagnostic.code == "pdf_image_mask_materialized"
                    else diagnostic
                    for diagnostic in source_ir.diagnostics
                )
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=tampered_source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=_registry_without_pds_image_cell_ownership_expectations(),
        )

        check = _check_by_id(
            report,
            "source_ir.image_mask_composite:4.materialized_diagnostic_grounding",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "image_mask_materialized_diagnostic_grounding_mismatch",
        )

    def test_pds_option_state_sequence_mismatch_is_a_machine_failure(self) -> None:
        source_ir, tree, semantics, view = _build_pds_native_pipeline()
        registry = _registry_without_pds_image_cell_ownership_expectations()
        document = registry.documents["DOC-PDS-OIL-PUMP"]
        expectation = document.option_state_expectations[1].model_copy(
            update={
                "states": (
                    *document.option_state_expectations[1].states[:-1],
                    document.option_state_expectations[1].states[0],
                )
            }
        )
        document = document.model_copy(
            update={
                "option_state_expectations": (
                    document.option_state_expectations[0],
                    expectation,
                )
            }
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-PDS-OIL-PUMP": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(report, "option_state:4.1:9.source_states")
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(check.reason_code, "option_state_source_states_mismatch")

    def test_pds_active_damper_revision_history_passes_text_free_checklist(
        self,
    ) -> None:
        source_ir, tree, semantics, view = _build_pds_active_damper_pipeline()

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-ACTIVE-DAMPER",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json")),
        )

        self.assertIs(report.status, PdfShadowSilverCheckStatus.PASS)
        self.assertEqual(
            (report.passed_check_count, report.failed_check_count),
            (12, 0),
        )
        self.assertEqual(
            _check_by_id(
                report,
                "revision_history:1:0:48x23.semantic_classes",
            ).observed,
            ("7:VERSION_METADATA",),
        )
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("修订记录", serialized)
        self.assertNotIn("V1.0", serialized)

    def test_revision_history_semantic_mismatch_is_a_machine_failure(self) -> None:
        source_ir, tree, semantics, view = _build_pds_native_pipeline()
        registry = _registry_without_pds_image_cell_ownership_expectations()
        document = registry.documents["DOC-PDS-OIL-PUMP"]
        expectation = document.revision_history_expectations[0].model_copy(
            update={"revision_row_indices": (7, 9)}
        )
        document = document.model_copy(
            update={"revision_history_expectations": (expectation,)}
        )
        registry = registry.model_copy(
            update={
                "documents": {
                    **registry.documents,
                    "DOC-PDS-OIL-PUMP": document,
                }
            }
        )

        report = evaluate_pdf_shadow_silver(
            document_id="DOC-PDS-OIL-PUMP",
            source_ir=source_ir,
            structure_tree=tree,
            semantics=semantics,
            requirement_view=view,
            registry=registry,
        )

        check = _check_by_id(
            report,
            "revision_history:1:0:15x10.semantic_classes",
        )
        self.assertIs(report.status, PdfShadowSilverCheckStatus.FAIL)
        self.assertEqual(
            check.reason_code,
            "revision_history_semantic_classes_mismatch",
        )
        self.assertEqual(check.observed[1], "9:UNRESOLVED")

    def test_report_model_supports_source_and_finding_without_body_text(self) -> None:
        for category in ("source_ir", "finding"):
            with self.subTest(category=category):
                check = PdfShadowSilverCheck(
                    check_id=f"{category}.foundation",
                    category=category,
                    status=PdfShadowSilverCheckStatus.PASS,
                    reason_code="matched",
                    expected="structure_number_gap",
                    observed="structure_number_gap",
                )
                self.assertEqual(check.category, category)

        with self.assertRaisesRegex(ValueError, "text-free"):
            PdfShadowSilverCheck(
                check_id="source_ir.unsafe",
                category="source_ir",
                status=PdfShadowSilverCheckStatus.FAIL,
                reason_code="unsafe_value",
                expected="customer sentence must never enter the report",
                observed=None,
            )


def _check_by_id(report: object, check_id: str) -> object:
    return next(check for check in report.checks if check.check_id == check_id)


def _visual_text_anchor_fidelity_payload() -> dict[str, object]:
    return {
        "case_id": "STR-003",
        "page_number": 11,
        "policy_id": "visual-text-anchor-fidelity/v2",
        "provider_id": "aliyun-bailian-qwen-ocr",
        "provider_version_sha256": hashlib.sha256(b"1.0.0").hexdigest(),
        "coverage_claim": "reviewed_anchor_sequence",
        "observation_count_min": 1,
        "observation_count_max": 4,
        "character_count_min": 1,
        "character_count_max": 200,
        "anchor_order_axis": "x",
        "ordered_anchors": [
            {
                "token_sha256": canonical_visual_text_token_sha256("alpha"),
                "axis_center_q100": 10_000,
                "max_axis_delta_q100": 100,
            }
        ],
        "reviewed_token_regions": [],
        "projected_requirement_count": 0,
    }


def _visual_text_fidelity_payload() -> dict[str, object]:
    return {
        "case_id": "STR-003",
        "page_number": 11,
        "policy_id": "visual-text-page-fidelity/v1",
        "provider_id": "aliyun-bailian-qwen-ocr",
        "provider_version_sha256": hashlib.sha256(b"1.0.0").hexdigest(),
        "observation_count": 1,
        "character_count": 1,
        "raw_text_bundle_sha256": "a" * 64,
        "layout_bundle_sha256": "b" * 64,
        "reviewed_regions": [],
        "projected_requirement_count": 0,
    }


def _build_visual_text_fidelity_pipeline() -> tuple[
    object, object, object, object, object
]:
    source_ir, _, _, _ = _build_matching_pipeline(visual_pages={11})
    snapshot = _visual_snapshot(
        page_number=11,
        page_image_sha256=PAGE_ASSET_SHA256[11],
        texts=(
            "4 Technical requirements",
            "4.1 General requirements",
        ),
        provider_id="aliyun-bailian-qwen-ocr",
    )
    rebuilt = _rebuild_with_visual_snapshot(source_ir, snapshot)
    return (*rebuilt, snapshot)


def _rebuild_with_visual_snapshot(
    source_ir: SourceIRSnapshot,
    snapshot: VisualTextSnapshot,
) -> tuple[object, object, object, object]:
    snapshots = tuple(
        snapshot if item.page_number == snapshot.page_number else item
        for item in source_ir.visual_text_snapshots
    )
    updated_source_ir = source_ir.model_copy(
        update={"visual_text_snapshots": snapshots}
    )
    tree = build_structure_tree(updated_source_ir)
    semantics = classify_content(updated_source_ir, tree)
    view = project_requirements(updated_source_ir, tree, semantics)
    return updated_source_ir, tree, semantics, view


def _registry_with_visual_text_fidelity(
    snapshot: VisualTextSnapshot,
    *,
    provider_version_sha256: str | None = None,
    reviewed_regions: tuple[
        PdfShadowSilverVisualTextRegionExpectation,
        ...,
    ] = (),
) -> object:
    expectation = PdfShadowSilverVisualTextFidelityExpectation(
        case_id="STR-003",
        page_number=snapshot.page_number,
        policy_id="visual-text-page-fidelity/v1",
        provider_id=snapshot.provider_id,
        provider_version_sha256=(
            provider_version_sha256
            or hashlib.sha256(snapshot.provider_version.encode("utf-8")).hexdigest()
        ),
        observation_count=len(snapshot.observations),
        character_count=sum(len(item.raw_text) for item in snapshot.observations),
        raw_text_bundle_sha256=canonical_visual_text_raw_text_bundle_sha256(
            snapshot.observations
        ),
        layout_bundle_sha256=canonical_visual_text_layout_bundle_sha256(
            snapshot.observations
        ),
        reviewed_regions=reviewed_regions,
        projected_requirement_count=0,
    )
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    document = registry.documents["DOC-QC-T-1067"].model_copy(
        update={
            "case_ids": ("STR-003",),
            "page_assets": tuple(
                item
                for item in registry.documents["DOC-QC-T-1067"].page_assets
                if item.page_number == snapshot.page_number
            ),
            "visual_text_fidelity_expectations": (expectation,),
            "outline_chains": (),
            "continuations": (),
            "numbered_children": (),
            "semantic_expectations": (),
            "projection_expectations": (),
            "finding_expectations": (),
            "projection_scope_expectations": (),
            "diagnostic_expectations": (),
            "native_block_disposition_expectations": (),
            "structure_scope_expectations": (),
            "table_expectations": (),
            "table_note_expectations": (),
            "revision_history_expectations": (),
            "figure_expectations": (),
            "image_mask_composite_expectations": (),
            "image_cell_ownership_expectations": (),
            "option_state_expectations": (),
        }
    )
    document = type(document).model_validate(document.model_dump(mode="python"))
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QC-T-1067": document,
            }
        }
    )


def _registry_with_ncs_diagnostic_shape(
    snapshot: VisualTextSnapshot,
    *,
    diagnostic_codes: tuple[str, ...],
) -> object:
    registry = _registry_with_visual_text_fidelity(snapshot)
    document = registry.documents["DOC-QC-T-1067"].model_copy(
        update={
            "case_ids": ("STR-003", "ROLE-003"),
            "diagnostic_expectations": tuple(
                PdfShadowSilverDiagnosticExpectation(
                    case_id="ROLE-003",
                    code=code,
                    page_number=page_number,
                    count=1,
                )
                for page_number in range(1, 5)
                for code in diagnostic_codes
            ),
        }
    )
    document = type(document).model_validate(document.model_dump(mode="python"))
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QC-T-1067": document,
            }
        }
    )


def _registry_with_non_requirement_visual_text_fidelity(
    snapshot: VisualTextSnapshot,
) -> object:
    registry = _registry_with_visual_text_fidelity(snapshot)
    document = registry.documents["DOC-QC-T-1067"]
    document = document.model_copy(
        update={
            "case_ids": ("STR-003",),
            "projection_policy": (RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT),
            "outline_chains": (),
            "continuations": (),
            "numbered_children": (),
            "semantic_expectations": (),
            "projection_expectations": (),
            "finding_expectations": (),
            "projection_scope_expectations": (),
            "diagnostic_expectations": (),
            "native_block_disposition_expectations": (),
            "structure_scope_expectations": (),
            "table_expectations": (),
            "table_note_expectations": (),
            "revision_history_expectations": (),
            "figure_expectations": (),
            "image_mask_composite_expectations": (),
            "image_cell_ownership_expectations": (),
            "option_state_expectations": (),
        }
    )
    document = type(document).model_validate(document.model_dump(mode="python"))
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QC-T-1067": document,
            }
        }
    )


def _anchor_snapshot(
    records: tuple[
        tuple[str, tuple[float, float, float, float]],
        ...,
    ] = (
        ("Gamma anchor", (290.0, 100.0, 310.0, 120.0)),
        ("Alpha approved", (90.0, 100.0, 110.0, 120.0)),
        ("Beta comments", (190.0, 100.0, 210.0, 120.0)),
    ),
) -> VisualTextSnapshot:
    snapshot = _visual_snapshot(
        page_number=11,
        page_image_sha256=PAGE_ASSET_SHA256[11],
        texts=tuple(item[0] for item in records),
        provider_id="aliyun-bailian-qwen-ocr",
    )
    return snapshot.model_copy(
        update={
            "observations": tuple(
                _visual_observation_with_bbox(observation, records[index][1])
                for index, observation in enumerate(snapshot.observations)
            )
        }
    )


def _build_anchor_fidelity_pipeline() -> tuple[object, object, object, object, object]:
    source_ir, _, _, _ = _build_matching_pipeline(visual_pages={11})
    snapshot = _anchor_snapshot()
    rebuilt = _rebuild_anchor_fidelity_pipeline(source_ir, snapshot)
    return (*rebuilt, snapshot)


def _rebuild_anchor_fidelity_pipeline(
    source_ir: SourceIRSnapshot,
    snapshot: VisualTextSnapshot,
) -> tuple[object, object, object, object]:
    updated_source_ir, tree, semantics, _ = _rebuild_with_visual_snapshot(
        source_ir,
        snapshot,
    )
    view = project_requirements(
        updated_source_ir,
        tree,
        semantics,
        projection_policy=RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
    )
    return updated_source_ir, tree, semantics, view


def _registry_with_visual_text_anchor_fidelity(
    snapshot: VisualTextSnapshot,
    *,
    ordered_raw_text_bundle_sha256: str | None = None,
) -> object:
    expectation = PdfShadowSilverVisualTextAnchorFidelityExpectation(
        case_id="ROLE-003",
        page_number=11,
        policy_id="visual-text-anchor-fidelity/v2",
        provider_id=snapshot.provider_id,
        provider_version_sha256=hashlib.sha256(
            snapshot.provider_version.encode("utf-8")
        ).hexdigest(),
        coverage_claim="reviewed_anchor_sequence",
        observation_count_min=2,
        observation_count_max=4,
        character_count_min=20,
        character_count_max=80,
        ordered_raw_text_bundle_sha256=ordered_raw_text_bundle_sha256,
        anchor_order_axis="x",
        ordered_anchors=(
            PdfShadowSilverVisualTextAnchor(
                token_sha256=canonical_visual_text_token_sha256("alpha"),
                axis_center_q100=10_000,
                max_axis_delta_q100=100,
            ),
            PdfShadowSilverVisualTextAnchor(
                token_sha256=canonical_visual_text_token_sha256("beta"),
                axis_center_q100=20_000,
                max_axis_delta_q100=100,
            ),
            PdfShadowSilverVisualTextAnchor(
                token_sha256=canonical_visual_text_token_sha256("gamma"),
                axis_center_q100=30_000,
                max_axis_delta_q100=100,
            ),
        ),
        reviewed_token_regions=(
            PdfShadowSilverVisualTextTokenRegion(
                region_id="comments",
                bbox_q100=(5_000, 9_000, 25_000, 13_000),
                membership="centroid",
                required_tokens=(
                    PdfShadowSilverVisualTextRequiredToken(
                        token_sha256=canonical_visual_text_token_sha256("approved"),
                        exact_occurrences=1,
                    ),
                    PdfShadowSilverVisualTextRequiredToken(
                        token_sha256=canonical_visual_text_token_sha256("comments"),
                        exact_occurrences=1,
                    ),
                ),
            ),
        ),
        projected_requirement_count=0,
    )
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    original = registry.documents["DOC-QC-T-1067"]
    document = original.model_copy(
        update={
            "case_ids": ("ROLE-003",),
            "projection_policy": RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
            "page_assets": tuple(
                item for item in original.page_assets if item.page_number == 11
            ),
            "visual_text_fidelity_expectations": (expectation,),
            "outline_chains": (),
            "continuations": (),
            "numbered_children": (),
            "semantic_expectations": (),
            "projection_expectations": (),
            "finding_expectations": (),
            "projection_scope_expectations": (),
            "diagnostic_expectations": (),
            "native_block_disposition_expectations": (),
            "structure_scope_expectations": (),
            "table_expectations": (),
            "table_note_expectations": (),
            "revision_history_expectations": (),
            "figure_expectations": (),
            "image_mask_composite_expectations": (),
            "image_cell_ownership_expectations": (),
            "option_state_expectations": (),
        }
    )
    document = type(document).model_validate(document.model_dump(mode="python"))
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QC-T-1067": document,
            }
        }
    )


def _visual_observation_with_bbox(
    observation: VisualTextObservation,
    bbox: tuple[float, float, float, float],
) -> VisualTextObservation:
    return VisualTextObservation(
        observation_id=stable_visual_text_observation_id(
            source_sha256=observation.source_sha256,
            page_image_sha256=observation.page_image_sha256,
            provider_id=observation.provider_id,
            provider_version=observation.provider_version,
            page_number=observation.page_number,
            observation_index=observation.observation_index,
            bbox=bbox,
        ),
        observation_index=observation.observation_index,
        source_sha256=observation.source_sha256,
        page_number=observation.page_number,
        bbox=bbox,
        raw_text=observation.raw_text,
        confidence=observation.confidence,
        provider_id=observation.provider_id,
        provider_version=observation.provider_version,
        page_image_sha256=observation.page_image_sha256,
    )


def _registry_with_page_assets(page_assets: tuple[object, ...]) -> object:
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    document = registry.documents["DOC-QC-T-1067"].model_copy(
        update={"page_assets": page_assets}
    )
    return registry.model_copy(update={"documents": {"DOC-QC-T-1067": document}})


def _registry_with_qsk_expectation(
    field_name: str,
    expectation: object,
) -> object:
    registry = _registry_without_qsk_mask_expectations()
    document = registry.documents["DOC-QSK-J02-311"].model_copy(
        update={field_name: (expectation,)}
    )
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QSK-J02-311": document,
            }
        }
    )


def _registry_without_qsk_mask_expectations() -> object:
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    document = registry.documents["DOC-QSK-J02-311"].model_copy(
        update={"image_mask_composite_expectations": ()}
    )
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-QSK-J02-311": document,
            }
        }
    )


def _image_cell_assignment() -> PdfShadowSilverImageCellOwnershipAssignment:
    return PdfShadowSilverImageCellOwnershipAssignment(
        image=PdfShadowSilverImageCellImageBinding(
            page_number=4,
            block_index=7,
            kind=SourceNodeKind.IMAGE,
            content_sha256=IMAGE_CELL_ASSET_SHA256,
            bbox_q100=(27_000, 12_000, 42_000, 18_000),
        ),
        table=PdfShadowSilverImageCellTableBinding(
            index=0,
            bbox_q100=(5_000, 10_000, 45_000, 30_000),
            shape=(2, 2),
        ),
        cell=PdfShadowSilverImageCellCellBinding(
            row=0,
            column=1,
            span=(1, 1),
            bbox_q100=(25_000, 10_000, 45_000, 20_000),
        ),
    )


def _image_multi_cell_assignment() -> PdfShadowSilverImageCellOwnershipAssignment:
    return PdfShadowSilverImageCellOwnershipAssignment(
        image=PdfShadowSilverImageCellImageBinding(
            page_number=4,
            block_index=7,
            kind=SourceNodeKind.IMAGE,
            content_sha256=IMAGE_CELL_ASSET_SHA256,
            bbox_q100=(5_000, 10_000, 45_000, 30_000),
        ),
        table=PdfShadowSilverImageCellTableBinding(
            index=0,
            bbox_q100=(5_000, 10_000, 45_000, 30_000),
            shape=(2, 2),
        ),
        cells=tuple(
            PdfShadowSilverImageCellCellBinding(
                row=row,
                column=column,
                span=(1, 1),
                bbox_q100=(
                    5_000 + column * 20_000,
                    10_000 + row * 10_000,
                    5_000 + (column + 1) * 20_000,
                    10_000 + (row + 1) * 10_000,
                ),
            )
            for row in range(2)
            for column in range(2)
        ),
        relation_kind="placed_over_cells",
    )


def _registry_for_image_cell_assignment(
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
) -> object:
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    document = registry.documents["DOC-F06-ACTIVE-SUSPENSION"].model_copy(
        update={
            "case_ids": (),
            "page_assets": (),
            "outline_chains": (),
            "continuations": (),
            "numbered_children": (),
            "semantic_expectations": (),
            "projection_expectations": (),
            "finding_expectations": (),
            "projection_scope_expectations": (),
            "diagnostic_expectations": (),
            "native_block_disposition_expectations": (),
            "structure_scope_expectations": (),
            "table_expectations": (),
            "table_note_expectations": (),
            "revision_history_expectations": (),
            "figure_expectations": (),
            "image_mask_composite_expectations": (),
            "image_cell_ownership_expectations": (
                PdfShadowSilverImageCellOwnershipExpectation(
                    page_number=4,
                    policy_id="pdf-image-cell-geometry/v1",
                    assignments=(assignment,),
                ),
            ),
            "option_state_expectations": (),
        }
    )
    return registry.model_copy(
        update={
            "documents": {
                "DOC-F06-ACTIVE-SUSPENSION": document,
            }
        }
    )


def _registry_without_pds_image_cell_ownership_expectations() -> object:
    registry = load_pdf_shadow_silver(require_private_eval("extraction_cases/silver.json"))
    document = registry.documents["DOC-PDS-OIL-PUMP"].model_copy(
        update={"image_cell_ownership_expectations": ()}
    )
    return registry.model_copy(
        update={
            "documents": {
                **registry.documents,
                "DOC-PDS-OIL-PUMP": document,
            }
        }
    )


def _build_matching_pipeline(
    *,
    page_asset_overrides: dict[int, str] | None = None,
    duplicate_equipment_section: bool = False,
    visual_provider_id: str = "aliyun-bailian-qwen-ocr",
    visual_pages: set[int] | None = None,
    duplicate_visual_page: int | None = None,
) -> tuple[object, object, object, object]:
    page_assets = dict(PAGE_ASSET_SHA256)
    page_assets.update(page_asset_overrides or {})
    page_texts = {
        11: ("4 Technical requirements",),
        29: (
            "4.12 Connector tests",
            "4.12.1 Peak-reading force test equipment",
            "4.12.2 Test method",
            "4.12.2.1 Connection force",
            "Test sequence:",
            "1) condition alpha",
            "2) condition beta",
            "3) condition gamma",
            "4) condition delta",
        ),
        30: (
            "5) condition epsilon",
            "6) condition zeta",
        ),
        31: (
            "4.13.3 Equipment must satisfy the following requirements",
            "1) condition one",
            "2) condition two",
            "3) condition three",
            "4) condition four",
            "5) condition five",
            *(
                ("4.12.1 Duplicate force test equipment",)
                if duplicate_equipment_section
                else ()
            ),
        ),
    }
    visual_snapshots = list(
        _visual_snapshot(
            page_number=page_number,
            page_image_sha256=page_assets[page_number],
            texts=texts,
            provider_id=visual_provider_id,
        )
        for page_number, texts in page_texts.items()
        if visual_pages is None or page_number in visual_pages
    )
    if duplicate_visual_page is not None:
        visual_snapshots.append(
            _visual_snapshot(
                page_number=duplicate_visual_page,
                page_image_sha256=page_assets[duplicate_visual_page],
                texts=page_texts[duplicate_visual_page],
                provider_id="azure-document-intelligence",
            )
        )
    visual_snapshots.sort(
        key=lambda item: (
            item.page_number,
            item.provider_id,
            item.provider_version,
            item.visual_text_snapshot_id,
        )
    )
    adapter_id = "synthetic-pdf-visual"
    adapter_version = "1.0.0"
    source_nodes = []
    source_assets = []
    for source_order, page_number in enumerate(sorted(page_assets)):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=source_order,
            bbox=(0.0, 0.0, 600.0, 800.0),
        )
        asset_id = f"asset-synthetic-page-{page_number}"
        source_assets.append(
            SourceAssetRef(
                asset_id=asset_id,
                kind=SourceAssetKind.IMAGE,
                content_sha256=page_assets[page_number],
                media_type="image/png",
                storage_key=f"synthetic/page-{page_number}.png",
                native_locator=locator,
            )
        )
        source_nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA256,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.IMAGE,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.IMAGE,
                source_order=source_order,
                native_locator=locator,
                asset_ref=asset_id,
            )
        )
    document_id = stable_document_id(SOURCE_SHA256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.PARTIAL,
        nodes=tuple(source_nodes),
        assets=tuple(source_assets),
        visual_text_snapshots=tuple(visual_snapshots),
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _build_qsk_native_pipeline() -> tuple[object, object, object, object]:
    source_sha256 = "18a2e074b836c9c0c44d5077341ee5e20941fed1702f94aae2931d0e4071ec09"
    adapter_id = "synthetic-native-pdf"
    adapter_version = "1.0.0"
    source_nodes: list[SourceNode] = []
    for page_number, block_index, raw_text in (
        (65, 0, "8 Technical requirements"),
        (65, 1, "8.3 Interface requirements"),
        (66, 0, "8.3.1 Interface requirements"),
    ):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=block_index,
            bbox=(70.0, 100.0 + block_index * 30.0, 520.0, 120.0 + block_index * 30.0),
        )
        source_nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=source_sha256,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(source_nodes),
                raw_text=raw_text,
                native_locator=locator,
            )
        )

    def table_node(
        *,
        page_number: int,
        bbox: tuple[float, float, float, float],
        data: tuple[str, str],
    ) -> SourceNode:
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
            bbox=bbox,
        )
        table_id = stable_table_id(
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=locator,
        )
        values = (("Field", "Requirement"), data)
        row_height = (bbox[3] - bbox[1]) / 2.0
        column_width = (bbox[2] - bbox[0]) / 2.0
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=values[row][column],
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=0,
                    row_index=row,
                    column_index=column,
                    bbox=(
                        bbox[0] + column * column_width,
                        bbox[1] + row * row_height,
                        bbox[0] + (column + 1) * column_width,
                        bbox[1] + (row + 1) * row_height,
                    ),
                ),
            )
            for row in range(2)
            for column in range(2)
        )
        return SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source_sha256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.TABLE,
                native_locator=locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=len(source_nodes),
            raw_text="",
            native_locator=locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=2,
                column_count=2,
                cells=cells,
            ),
        )

    source_nodes.append(
        table_node(
            page_number=66,
            bbox=(50.0, 600.0, 550.0, 780.0),
            data=("First item", "Value one"),
        )
    )
    source_nodes.append(
        table_node(
            page_number=67,
            bbox=(50.0, 90.0, 550.0, 350.0),
            data=("Second item", "Value two"),
        )
    )
    document_id = stable_document_id(source_sha256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(source_nodes),
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _build_image_cell_pipeline(
    *,
    ambiguous_table: bool = False,
    multi_cell: bool = False,
) -> tuple[object, object, object, object]:
    adapter_id = "synthetic-native-pdf"
    adapter_version = "1.0.0"
    table_specs = [
        (0, (50.0, 100.0, 450.0, 300.0)),
    ]
    if ambiguous_table:
        table_specs.append((1, (50.0, 100.0, 450.0, 300.0)))

    source_nodes: list[SourceNode] = []
    for table_index, table_bbox in table_specs:
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=4,
            table_index=table_index,
            bbox=table_bbox,
        )
        table_id = stable_table_id(
            source_sha256=F06_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=table_locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=("cell secret" if (row, column) == (0, 0) else ""),
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=4,
                    table_index=table_index,
                    row_index=row,
                    column_index=column,
                    bbox=(
                        50.0 + column * 200.0,
                        100.0 + row * 100.0,
                        50.0 + (column + 1) * 200.0,
                        100.0 + (row + 1) * 100.0,
                    ),
                ),
            )
            for row in range(2)
            for column in range(2)
        )
        source_nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=F06_SOURCE_SHA256,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.TABLE,
                    native_locator=table_locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=len(source_nodes),
                native_locator=table_locator,
                table_payload=TableIR(
                    table_id=table_id,
                    row_count=2,
                    column_count=2,
                    cells=cells,
                ),
            )
        )

    image_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=4,
        block_index=7,
        bbox=(
            (50.0, 100.0, 450.0, 300.0) if multi_cell else (270.0, 120.0, 420.0, 180.0)
        ),
    )
    image_asset = SourceAssetRef(
        asset_id="asset-image-cell",
        kind=SourceAssetKind.IMAGE,
        content_sha256=IMAGE_CELL_ASSET_SHA256,
        media_type="image/png",
        storage_key="synthetic/image-cell.png",
        native_locator=image_locator,
    )
    source_nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=F06_SOURCE_SHA256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=len(source_nodes),
            native_locator=image_locator,
            asset_ref=image_asset.asset_id,
        )
    )
    document_id = stable_document_id(F06_SOURCE_SHA256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=F06_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=F06_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(source_nodes),
        assets=(image_asset,),
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _build_f06_native_pipeline() -> tuple[object, object, object, object]:
    adapter_id = "synthetic-native-pdf"
    adapter_version = "1.0.0"
    source_nodes: list[SourceNode] = []

    def paragraph(raw_text: str, *, y0: float) -> SourceNode:
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=28,
            block_index=len(source_nodes),
            bbox=(70.0, y0, 530.0, y0 + 16.0),
        )
        node = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=F06_SOURCE_SHA256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.PARAGRAPH,
                native_locator=locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=len(source_nodes),
            raw_text=raw_text,
            native_locator=locator,
        )
        source_nodes.append(node)
        return node

    paragraph("9 Synthetic requirements", y0=40.0)
    paragraph("9.16 Synthetic performance group", y0=65.0)
    paragraph("9.16.2 Damping performance requirements", y0=90.0)
    paragraph("Table 5 Synthetic tolerance table", y0=115.0)
    paragraph("Synthetic descriptive context alpha.", y0=132.0)
    paragraph("Synthetic descriptive context beta.", y0=149.0)

    table_bbox = (70.0, 175.0, 530.0, 295.0)
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=28,
        table_index=0,
        bbox=table_bbox,
    )
    table_id = stable_table_id(
        source_sha256=F06_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        native_locator=table_locator,
    )
    values = (
        ("Metric", "A", "B", "C", "D", "E", "F"),
        ("Start", "±10%", "±10%", "±8%", "±8%", "±8%", "±5%"),
        ("Stop", "±10%", "±10%", "±8%", "±8%", "±8%", "±5%"),
        ("Note: shared tolerance applies.", "", "", "", "", "", ""),
    )
    row_height = (table_bbox[3] - table_bbox[1]) / 4.0
    column_width = (table_bbox[2] - table_bbox[0]) / 7.0
    cells: list[TableCellIR] = []
    for row in range(4):
        for column in range(7):
            if row == 3 and column > 0:
                continue
            bbox = (
                (
                    table_bbox[0],
                    table_bbox[1] + row * row_height,
                    table_bbox[2],
                    table_bbox[1] + (row + 1) * row_height,
                )
                if row == 3 and column == 0
                else None
                if row == 3
                else (
                    table_bbox[0] + column * column_width,
                    table_bbox[1] + row * row_height,
                    table_bbox[0] + (column + 1) * column_width,
                    table_bbox[1] + (row + 1) * row_height,
                )
            )
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    column_span=7 if row == 3 else 1,
                    raw_text=values[row][column],
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=28,
                        table_index=0,
                        row_index=row,
                        column_index=column,
                        bbox=bbox,
                    ),
                )
            )
    source_nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=F06_SOURCE_SHA256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.TABLE,
                native_locator=table_locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=len(source_nodes),
            raw_text="",
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=4,
                column_count=7,
                cells=tuple(cells),
                merged_regions=(
                    MergedRegion(
                        row=3,
                        column=0,
                        row_span=1,
                        column_span=7,
                    ),
                ),
            ),
        )
    )
    paragraph("Surface shall remain complete and smooth.", y0=310.0)

    paragraph("9.16.3 Energy loss requirement", y0=340.0)
    paragraph("Energy loss shall remain below the acceptance threshold.", y0=365.0)
    paragraph("See Figure 2 for the calculation boundary.", y0=390.0)
    paragraph("Synthetic calculation context alpha.", y0=415.0)
    paragraph("Synthetic calculation context beta.", y0=440.0)
    image_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=28,
        block_index=len(source_nodes),
        bbox=(180.0, 470.0, 420.0, 600.0),
    )
    asset = SourceAssetRef(
        asset_id="asset-synthetic-f06-figure",
        kind=SourceAssetKind.IMAGE,
        content_sha256=F06_FIGURE_ASSET_SHA256,
        media_type="image/jpeg",
        storage_key="synthetic/f06-figure.jpg",
        native_locator=image_locator,
    )
    source_nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=F06_SOURCE_SHA256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=len(source_nodes),
            native_locator=image_locator,
            asset_ref=asset.asset_id,
        )
    )
    paragraph("Figure 2 Synthetic calculation diagram", y0=615.0)
    paragraph("9.16.4 Another requirement", y0=645.0)
    paragraph("Table 5 Synthetic second table", y0=670.0)

    source_diagnostics = tuple(
        SourceDiagnostic(
            code="pdf_tiling_pattern_tile_excluded",
            severity=SourceDiagnosticSeverity.INFO,
            message="Synthetic tiling-pattern tile disposition.",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                block_index=block_index,
            ),
            evidence_sha256=F06_TILING_PATTERN_TILE_SHA256,
        )
        for page_number, block_index in F06_TILING_PATTERN_TILE_LOCATORS
    )
    document_id = stable_document_id(F06_SOURCE_SHA256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=F06_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=F06_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(source_nodes),
        assets=(asset,),
        diagnostics=source_diagnostics,
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _revision_history_table_node(
    *,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    row_count: int,
    column_count: int,
    revision_rows: dict[int, str],
) -> SourceNode:
    table_bbox = (35.0, 35.0, 560.0, 805.0)
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=table_bbox,
    )
    table_id = stable_table_id(
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        native_locator=table_locator,
    )
    row_text = {
        5: "修订记录",
        6: "版本号 修改说明 日期",
        **revision_rows,
    }
    row_height = (table_bbox[3] - table_bbox[1]) / row_count
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row_index, 0),
            row=row_index,
            column=0,
            raw_text=row_text.get(row_index, ""),
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row_index,
                column_index=0,
                bbox=(
                    table_bbox[0],
                    table_bbox[1] + row_index * row_height,
                    table_bbox[2],
                    table_bbox[1] + (row_index + 1) * row_height,
                ),
            ),
        )
        for row_index in range(row_count)
    )
    return SourceNode(
        source_node_id=stable_node_id(
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=row_count,
            column_count=column_count,
            cells=cells,
        ),
    )


def _build_pds_native_pipeline() -> tuple[object, object, object, object]:
    adapter_id = "synthetic-native-pdf"
    adapter_version = "1.0.0"
    specs = (
        ("3.3 项目风险", 90.0),
        ("请选择风险属性", 115.0),
        ("[ √ ] 风险项甲", 140.0),
        ("[   ] 风险项乙", 165.0),
        ("[   ] 风险项丙", 190.0),
        ("[   ] 风险项丁", 215.0),
        ("[ √ ] 风险项戊", 240.0),
        ("[   ] 风险项己", 265.0),
        ("[   ] 风险项庚", 290.0),
        ("4.1 法规与认证", 330.0),
        ("请选择适用认证区域", 355.0),
        ("[ √ ] 区域甲", 380.0),
        ("[   ] 区域乙", 405.0),
        ("[   ] 区域丙", 430.0),
        ("[ √ ] 区域丁", 455.0),
        ("[   ] 区域戊", 480.0),
        ("[   ] 区域己", 505.0),
        ("[   ] 区域庚", 530.0),
        ("[ √ ] 区域辛", 555.0),
        ("[   ] 区域壬", 580.0),
        ("[   ] 区域癸", 605.0),
        ("[   ] 区域子", 630.0),
    )
    source_nodes: list[SourceNode] = [
        _revision_history_table_node(
            source_sha256=PDS_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            row_count=15,
            column_count=10,
            revision_rows={7: "V1.0 初始发布", 8: "V1.1 内容更新"},
        )
    ]
    source_assets: list[SourceAssetRef] = []
    source_diagnostics: list[SourceDiagnostic] = []
    for index, (block_index, content_hash, base_hash, mask_hash) in enumerate(
        PDS_MASK_COMPOSITES
    ):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=4,
            block_index=block_index,
            bbox=(200.0, 100.0 + index * 60.0, 300.0, 140.0 + index * 60.0),
        )
        asset_id = f"asset-pds-mask-{block_index}"
        source_assets.append(
            SourceAssetRef(
                asset_id=asset_id,
                kind=SourceAssetKind.IMAGE,
                content_sha256=content_hash,
                media_type="image/png",
                storage_key=f"synthetic/pds-mask-{block_index}.png",
            )
        )
        source_node_id = stable_node_id(
            source_sha256=PDS_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            kind=SourceNodeKind.IMAGE,
            native_locator=locator,
        )
        diagnostic = SourceDiagnostic(
            code="pdf_image_mask_materialized",
            severity=SourceDiagnosticSeverity.INFO,
            message="Synthetic mask composite materialized.",
            source_node_id=source_node_id,
            native_locator=locator,
        )
        source_diagnostics.append(diagnostic)
        source_nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=SourceNodeKind.IMAGE,
                source_order=len(source_nodes),
                native_locator=locator,
                visual_locator=locator,
                asset_ref=asset_id,
                asset_derivation=SourceAssetDerivation(
                    kind=SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
                    base_content_sha256=base_hash,
                    mask_content_sha256=mask_hash,
                ),
                extraction_diagnostics=(diagnostic,),
            )
        )
    for block_index, (raw_text, y0) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=9,
            block_index=block_index,
            bbox=(70.0, y0, 530.0, y0 + 16.0),
        )
        source_nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=PDS_SOURCE_SHA256,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(source_nodes),
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(PDS_SOURCE_SHA256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=PDS_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=PDS_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(source_nodes),
        assets=tuple(source_assets),
        diagnostics=tuple(source_diagnostics),
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _build_pds_active_damper_pipeline() -> tuple[object, object, object, object]:
    adapter_id = "synthetic-native-pdf"
    adapter_version = "1.0.0"
    source_node = _revision_history_table_node(
        source_sha256=PDS_ACTIVE_DAMPER_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        row_count=48,
        column_count=23,
        revision_rows={7: "V1.0 初始发布"},
    )
    document_id = stable_document_id(PDS_ACTIVE_DAMPER_SOURCE_SHA256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=PDS_ACTIVE_DAMPER_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=PDS_ACTIVE_DAMPER_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=(source_node,),
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _visual_snapshot(
    *,
    page_number: int,
    page_image_sha256: str,
    texts: tuple[str, ...],
    provider_id: str,
) -> VisualTextSnapshot:
    provider_version = "1.0.0"
    observations = []
    for index, raw_text in enumerate(texts):
        bbox = (40.0, 100.0 + index * 30.0, 560.0, 120.0 + index * 30.0)
        observations.append(
            VisualTextObservation(
                observation_id=stable_visual_text_observation_id(
                    source_sha256=SOURCE_SHA256,
                    page_image_sha256=page_image_sha256,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_number=page_number,
                    observation_index=index,
                    bbox=bbox,
                ),
                observation_index=index,
                source_sha256=SOURCE_SHA256,
                page_number=page_number,
                bbox=bbox,
                raw_text=raw_text,
                confidence=0.99,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_image_sha256,
            )
        )
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=SOURCE_SHA256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1200,
            page_image_height_px=1600,
        ),
        source_sha256=SOURCE_SHA256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1200,
        page_image_height_px=1600,
        observations=tuple(observations),
    )


if __name__ == "__main__":
    unittest.main()
