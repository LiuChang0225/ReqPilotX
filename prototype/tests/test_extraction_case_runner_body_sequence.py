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
import hashlib
import json
import re
import unittest
from types import SimpleNamespace

from app.adapters.documents.pdf_visual_text_runtime import visual_text_provider_id
from app.domain.extraction.source_ir import SourceNodeKind
from scripts import extraction_case_runner as runner
from scripts.pdf_body_review_artifacts import (
    canonical_pdf_image_occurrence_sha256,
    canonical_pdf_table_occurrence_sha256,
    summarize_pdf_content_image_occurrences,
    summarize_pdf_content_table_occurrences,
)
from scripts.pdf_body_review_runtime import VerifiedPdfBodyReviewProfile


SOURCE_SHA256 = "a" * 64
SECOND_SOURCE_SHA256 = "d" * 64
PAGE_IMAGE_SHA256 = "b" * 64
DOCUMENT_ID = "DOC-SYNTHETIC-PDF"
VISUAL_TEXT_PROVIDER_VERSION = "test-v1"
VISUAL_TABLE_PROVIDER_VERSION = "table-test-v1"
TABLE_BBOX = (10.0, 100.0, 400.0, 260.0)
BASE_LINES = (
    "Alpha requirement",
    "Beta long requirement",
    "Header",
    "Left",
    "Right",
)
TABLE_TOPOLOGY = {
    "row_count": 2,
    "column_count": 2,
    "cells": [
        {
            "row": 0,
            "column": 0,
            "row_span": 1,
            "column_span": 2,
            "text_lines": ("Header",),
        },
        {
            "row": 1,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "text_lines": ("Left",),
        },
        {
            "row": 1,
            "column": 1,
            "row_span": 1,
            "column_span": 1,
            "text_lines": ("Right",),
        },
    ],
}


class PdfReviewedBodySequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _body_snapshot()
        self.case = _case(DOCUMENT_ID)
        self.document_results = _document_results({DOCUMENT_ID: SOURCE_SHA256})
        self.remote_profile = _ready_profile("REMOTE_VISUAL")
        self.native_profile = _ready_profile("NATIVE")

    def evaluate(
        self,
        *,
        snapshot=None,
        profile=None,
        provider="bailian",
        verified_profiles=None,
        visual_runtime_identities=None,
    ):
        resolved_snapshot = self.snapshot if snapshot is None else snapshot
        selected_profile = self.remote_profile if profile is None else profile
        if verified_profiles is None:
            verified_profiles = (
                {}
                if selected_profile.get("status") == "PENDING"
                else {
                    DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                        ready_profile=selected_profile,
                        reviewed_profile=_reviewed_profile(),
                    )
                }
            )
        if (
            visual_runtime_identities is None
            and selected_profile.get("review_mode") == "REMOTE_VISUAL"
        ):
            visual_runtime_identities = {
                DOCUMENT_ID: _visual_runtime_identity()
            }
        return runner._evaluate_pdf_reviewed_body_sequence(
            case=self.case,
            document_results=self.document_results,
            source_ir_snapshots={(SOURCE_SHA256, "pdf"): resolved_snapshot},
            reviewed_body_profiles_by_document={
                DOCUMENT_ID: selected_profile
            },
            verified_review_profiles_by_document=verified_profiles,
            pdf_visual_provider=provider,
            visual_runtime_identities_by_document=visual_runtime_identities,
        )

    def test_ready_profile_has_no_unverified_evaluator_bypass(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "pdf_body_review_verified_profile_missing_or_changed",
        ):
            runner._evaluate_pdf_reviewed_body_sequence(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots={(SOURCE_SHA256, "pdf"): self.snapshot},
                reviewed_body_profiles_by_document={
                    DOCUMENT_ID: self.remote_profile
                },
                verified_review_profiles_by_document={},
                pdf_visual_provider="bailian",
                visual_runtime_identities_by_document={
                    DOCUMENT_ID: _visual_runtime_identity()
                },
            )

    def test_remote_visual_profile_consumes_observations_without_digest_leak(
        self,
    ) -> None:
        outcome = self.evaluate()

        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["evidence"]["observed_body_line_count"], 5)
        self.assertEqual(outcome["evidence"]["observed_table_cell_count"], 3)
        self.assertIsNone(re.search(r"[a-f0-9]{64}", json.dumps(outcome)))

    def test_verified_remote_profile_replays_only_page_text_provider_version(
        self,
    ) -> None:
        reviewed_profile = _reviewed_profile()
        verified = {
            DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                ready_profile=self.remote_profile,
                reviewed_profile=reviewed_profile,
            )
        }
        runtime_identity = _visual_runtime_identity()

        self.assertEqual(
            self.evaluate(
                verified_profiles=verified,
                visual_runtime_identities={DOCUMENT_ID: runtime_identity},
            )["status"],
            "PASS",
        )

        changed = copy.deepcopy(reviewed_profile)
        changed["visual_text_provider_version_sha256"] = "f" * 64
        outcome = self.evaluate(
            verified_profiles={
                DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                    ready_profile=self.remote_profile,
                    reviewed_profile=changed,
                )
            },
            visual_runtime_identities={DOCUMENT_ID: runtime_identity},
        )
        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_body_remote_visual_provider_profile_mismatch",
        )
        self.assertEqual(
            outcome["evidence"]["provider_profile_mismatch_pages"],
            [{"document_id": DOCUMENT_ID, "page_number": 2}],
        )

        changed = copy.deepcopy(reviewed_profile)
        changed["visual_table_provider_version_sha256"] = "e" * 64
        outcome = self.evaluate(
            verified_profiles={
                DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                    ready_profile=self.remote_profile,
                    reviewed_profile=changed,
                )
            },
            visual_runtime_identities={DOCUMENT_ID: runtime_identity},
        )
        self.assertEqual(outcome["status"], "PASS")

    def test_remote_table_baseline_measures_native_parser_not_visual_sidecar(
        self,
    ) -> None:
        reviewed_profile = _reviewed_profile()
        verified = {
            DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                ready_profile=self.remote_profile,
                reviewed_profile=reviewed_profile,
            )
        }
        runtime_identities = {DOCUMENT_ID: _visual_runtime_identity()}

        missing = copy.deepcopy(self.snapshot)
        missing.visual_table_snapshots = ()
        outcome = self.evaluate(
            snapshot=missing,
            verified_profiles=verified,
            visual_runtime_identities=runtime_identities,
        )
        self.assertEqual(outcome["status"], "PASS")

        changed = copy.deepcopy(self.snapshot)
        changed.visual_table_snapshots[0].cells[1].raw_text = "Changed visually"
        outcome = self.evaluate(
            snapshot=changed,
            verified_profiles=verified,
            visual_runtime_identities=runtime_identities,
        )
        self.assertEqual(outcome["status"], "PASS")

        changed = copy.deepcopy(self.snapshot)
        changed.nodes[2].table_payload.cells[1].raw_text = "Changed natively"
        outcome = self.evaluate(
            snapshot=changed,
            verified_profiles=verified,
            visual_runtime_identities=runtime_identities,
        )
        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_reviewed_body_sequence_mismatch",
        )

    def test_table_review_provenance_does_not_require_runtime_table_provider(
        self,
    ) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.nodes = tuple(
            node for node in snapshot.nodes if node.kind is not SourceNodeKind.TABLE
        )
        snapshot.visual_table_snapshots = ()
        profile = _ready_profile_without_table()
        verified = {
            DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                ready_profile=profile,
                reviewed_profile=_reviewed_profile(),
            )
        }

        passed = self.evaluate(
            snapshot=snapshot,
            profile=profile,
            verified_profiles=verified,
            visual_runtime_identities={DOCUMENT_ID: _visual_runtime_identity()},
        )
        self.assertEqual(passed["status"], "PASS")

        missing_table_identity = _visual_runtime_identity()
        missing_table_identity.pop("visual_table_provider_version_sha256")
        failed = self.evaluate(
            snapshot=snapshot,
            profile=profile,
            verified_profiles=verified,
            visual_runtime_identities={DOCUMENT_ID: missing_table_identity},
        )
        self.assertEqual(failed["status"], "PASS")

    def test_native_profile_needs_no_remote_provider_or_visual_snapshot(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.visual_text_snapshots = ()

        outcome = self.evaluate(
            snapshot=snapshot,
            profile=self.native_profile,
            provider="off",
        )

        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["evidence"]["ready_native_profile_count"], 1)
        self.assertEqual(
            outcome["evidence"]["ready_remote_visual_profile_count"],
            0,
        )

    def test_remote_observation_omission_duplication_truncation_and_order(self) -> None:
        variants = {
            "missing": BASE_LINES[1:],
            "duplicated": (BASE_LINES[0], *BASE_LINES),
            "truncated": (BASE_LINES[0], "Beta", *BASE_LINES[2:]),
            "reordered": (BASE_LINES[1], BASE_LINES[0], *BASE_LINES[2:]),
        }
        for label, lines in variants.items():
            with self.subTest(label=label):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot.visual_text_snapshots = (_visual_snapshot(lines),)
                outcome = self.evaluate(snapshot=snapshot)
                self.assertEqual(outcome["status"], "FAIL")
                self.assertEqual(
                    outcome["reason_code"],
                    "pdf_reviewed_body_sequence_mismatch",
                )
                self.assertEqual(
                    outcome["evidence"]["mismatched_pages"],
                    [{"document_id": DOCUMENT_ID, "page_number": 2}],
                )
                if label == "reordered":
                    self.assertEqual(
                        outcome["evidence"]["body_content_mismatch_count"],
                        0,
                    )
                    self.assertEqual(
                        outcome["evidence"]["body_order_mismatch_count"],
                        1,
                    )
                else:
                    self.assertEqual(
                        outcome["evidence"]["body_content_mismatch_count"],
                        1,
                    )

    def test_remote_lines_use_nfkc_and_whitespace_normalization(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.visual_text_snapshots = (
            _visual_snapshot(
                (
                    "Ａｌｐｈａ   requirement",
                    "Beta\tlong  requirement",
                    *BASE_LINES[2:],
                )
            ),
        )

        outcome = self.evaluate(snapshot=snapshot)

        self.assertEqual(outcome["status"], "PASS")

    def test_native_line_change_fails_even_with_provider_off(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.nodes[1].raw_text = "Beta"
        snapshot.visual_text_snapshots = ()

        outcome = self.evaluate(
            snapshot=snapshot,
            profile=self.native_profile,
            provider="off",
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["evidence"]["body_content_mismatch_count"], 1)

    def test_native_table_row_cell_and_visible_span_changes_fail(self) -> None:
        added_row = copy.deepcopy(self.snapshot)
        added_row.nodes[2].table_payload.row_count = 3

        changed_cell_span = copy.deepcopy(self.snapshot)
        changed_cell_span.nodes[2].table_payload.cells[1].column_span = 2

        changed_cell_text = copy.deepcopy(self.snapshot)
        changed_cell_text.nodes[2].table_payload.cells[1].raw_text = "Changed"

        for label, snapshot in (
            ("row", added_row),
            ("cell-span", changed_cell_span),
            ("cell-text", changed_cell_text),
        ):
            with self.subTest(label=label):
                snapshot.visual_text_snapshots = ()
                outcome = self.evaluate(
                    snapshot=snapshot,
                    profile=self.native_profile,
                    provider="off",
                )
                self.assertEqual(outcome["status"], "FAIL")
                self.assertEqual(
                    outcome["evidence"][
                        "body_sequence_mismatch_count"
                        if label == "cell-text"
                        else "table_inventory_mismatch_page_count"
                    ],
                    1,
                )

    def test_native_table_internal_merge_catalog_is_not_a4_content(self) -> None:
        changed = copy.deepcopy(self.snapshot)
        changed.nodes[2].table_payload.merged_regions = ()
        changed.visual_text_snapshots = ()

        outcome = self.evaluate(
            snapshot=changed,
            profile=self.native_profile,
            provider="off",
        )

        self.assertEqual(outcome["status"], "PASS")

    def test_native_image_omission_and_source_order_change_fail(self) -> None:
        image_occurrences = (
            _image_occurrence("1" * 64),
            _image_occurrence("2" * 64),
        )
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.nodes = (
            *snapshot.nodes,
            _image_node("image-one", "asset-one", block_index=5),
            _image_node("image-two", "asset-two", block_index=6),
        )
        snapshot.assets = (
            _asset("asset-one", "1" * 64),
            _asset("asset-two", "2" * 64),
        )
        snapshot.visual_text_snapshots = ()
        profile = _ready_profile(
            "NATIVE",
            image_occurrences=image_occurrences,
        )
        reviewed_occurrences = [
            _reviewed_image_occurrence(
                content_sha256="1" * 64,
                block_index=5,
                disposition="CONTENT",
            ),
            _reviewed_image_occurrence(
                content_sha256="2" * 64,
                block_index=6,
                disposition="CONTENT",
            ),
        ]
        verified = {
            DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                ready_profile=profile,
                reviewed_profile={
                    "pages": [
                        {
                            "page_number": 2,
                            "image_occurrences": reviewed_occurrences,
                            **summarize_pdf_content_image_occurrences(
                                reviewed_occurrences
                            ),
                        }
                    ]
                },
            )
        }

        self.assertEqual(
            self.evaluate(
                snapshot=snapshot,
                profile=profile,
                provider="off",
                verified_profiles=verified,
            )["status"],
            "PASS",
        )

        missing = copy.deepcopy(snapshot)
        missing.nodes = tuple(
            node for node in missing.nodes if node.asset_ref != "asset-one"
        )
        self.assertEqual(
            self.evaluate(
                snapshot=missing,
                profile=profile,
                provider="off",
                verified_profiles=verified,
            )["status"],
            "FAIL",
        )

        reordered = copy.deepcopy(snapshot)
        reordered.nodes[-2].source_order = 6
        reordered.nodes[-1].source_order = 5
        outcome = self.evaluate(
            snapshot=reordered,
            profile=profile,
            provider="off",
            verified_profiles=verified,
        )
        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_body_review_image_inventory_mismatch",
        )

    def test_verified_image_inventory_excludes_reviewed_non_content_only(self) -> None:
        reviewed_occurrences = [
            _reviewed_image_occurrence(
                content_sha256="1" * 64,
                block_index=5,
                disposition="NON_CONTENT",
            ),
            _reviewed_image_occurrence(
                content_sha256="2" * 64,
                block_index=6,
                disposition="CONTENT",
            ),
        ]
        image_summary = summarize_pdf_content_image_occurrences(
            reviewed_occurrences
        )
        profile = _ready_profile(
            "NATIVE",
            image_occurrences=(_image_occurrence("2" * 64),),
        )
        reviewed_profile = {
            "pages": [
                {
                    "page_number": 2,
                    "image_occurrences": reviewed_occurrences,
                    **image_summary,
                }
            ]
        }
        verified = {
            DOCUMENT_ID: VerifiedPdfBodyReviewProfile(
                ready_profile=profile,
                reviewed_profile=reviewed_profile,
            )
        }
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.nodes = (
            *snapshot.nodes,
            _image_node("image-one", "asset-one", block_index=5),
            _image_node("image-two", "asset-two", block_index=6),
        )
        snapshot.assets = (
            _asset("asset-one", "1" * 64),
            _asset("asset-two", "2" * 64),
        )
        snapshot.visual_text_snapshots = ()

        outcome = self.evaluate(
            snapshot=snapshot,
            profile=profile,
            provider="off",
            verified_profiles=verified,
        )
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["evidence"]["observed_image_count"], 1)

        missing_non_content = copy.deepcopy(snapshot)
        missing_non_content.nodes = tuple(
            node
            for node in missing_non_content.nodes
            if node.asset_ref != "asset-one"
        )
        mismatch = self.evaluate(
            snapshot=missing_non_content,
            profile=profile,
            provider="off",
            verified_profiles=verified,
        )
        self.assertEqual(mismatch["status"], "FAIL")
        self.assertEqual(
            mismatch["reason_code"],
            "pdf_body_review_image_inventory_mismatch",
        )
        self.assertEqual(mismatch["evidence"]["content_mismatch_pages"], [])

    def test_table_channel_reordering_changes_only_sequence_digest(self) -> None:
        first = copy.deepcopy(self.snapshot)
        second_table = copy.deepcopy(first.nodes[2])
        second_table.source_node_id = "node-table-second"
        second_table.native_locator = _locator(
            page_number=2,
            table_index=1,
            bbox=(10.0, 280.0, 400.0, 440.0),
        )
        second_table.table_payload.cells[0].raw_text = "Second header"
        first.nodes = (*first.nodes[:3], second_table, *first.nodes[3:])

        reordered = copy.deepcopy(first)
        reordered.nodes = (
            *reordered.nodes[:2],
            reordered.nodes[3],
            reordered.nodes[2],
            *reordered.nodes[4:],
        )

        first_measurement = runner._measure_pdf_body_page(
            first,
            page_number=2,
            review_mode="NATIVE",
        )
        reordered_measurement = runner._measure_pdf_body_page(
            reordered,
            page_number=2,
            review_mode="NATIVE",
        )

        self.assertEqual(
            first_measurement["content_multiset_sha256"],
            reordered_measurement["content_multiset_sha256"],
        )
        self.assertNotEqual(
            first_measurement["sequence_sha256"],
            reordered_measurement["sequence_sha256"],
        )

    def test_cross_channel_interleaving_is_outside_a4_v1_contract(self) -> None:
        image = _image_node("image-one", "asset-one", block_index=5)
        before = copy.deepcopy(self.snapshot)
        before.nodes = (before.nodes[0], image, *before.nodes[1:])
        before.assets = (_asset("asset-one", "1" * 64),)

        after = copy.deepcopy(before)
        after.nodes = (*after.nodes[:1], *after.nodes[2:], after.nodes[1])

        before_measurement = runner._measure_pdf_body_page(
            before,
            page_number=2,
            review_mode="NATIVE",
        )
        after_measurement = runner._measure_pdf_body_page(
            after,
            page_number=2,
            review_mode="NATIVE",
        )

        self.assertEqual(before_measurement, after_measurement)

    def test_review_table_override_is_strict_and_independent_of_native_candidates(
        self,
    ) -> None:
        changed_native = copy.deepcopy(self.snapshot)
        changed_native.nodes[2].table_payload.cells[1].raw_text = "native mismatch"

        measurement = runner._measure_pdf_body_page(
            changed_native,
            page_number=2,
            review_mode="REMOTE_VISUAL",
            visual_snapshot=changed_native.visual_text_snapshots[0],
            table_topologies_override=[TABLE_TOPOLOGY],
        )

        self.assertEqual(measurement["table_count"], 1)
        self.assertEqual(measurement["table_cell_count"], 3)
        self.assertEqual(measurement["merged_region_count"], 1)
        self.assertEqual(
            measurement["content_multiset_sha256"],
            self.remote_profile["pages"][0]["content_multiset_sha256"],
        )

        incomplete = copy.deepcopy(TABLE_TOPOLOGY)
        incomplete["cells"] = incomplete["cells"][:-1]
        with self.assertRaisesRegex(ValueError, "full rectangle"):
            runner._measure_pdf_body_page(
                changed_native,
                page_number=2,
                review_mode="REMOTE_VISUAL",
                visual_snapshot=changed_native.visual_text_snapshots[0],
                table_topologies_override=[incomplete],
            )

    def test_native_reviewed_table_inventory_filters_non_content_and_detects_drift(
        self,
    ) -> None:
        reviewed = _reviewed_profile()["pages"][0]["table_occurrences"]
        reviewed[0]["disposition"] = "NON_CONTENT"

        measurement = runner._measure_pdf_body_page(
            self.snapshot,
            page_number=2,
            review_mode="NATIVE",
            reviewed_table_occurrences=reviewed,
        )

        self.assertTrue(measurement["table_inventory_matches"])
        self.assertEqual(measurement["table_count"], 0)

        missing = copy.deepcopy(self.snapshot)
        missing.nodes = tuple(
            node for node in missing.nodes if node.source_node_id != "node-table"
        )
        mismatch = runner._measure_pdf_body_page(
            missing,
            page_number=2,
            review_mode="NATIVE",
            reviewed_table_occurrences=reviewed,
        )
        self.assertFalse(mismatch["table_inventory_matches"])
        self.assertEqual(mismatch["table_count"], 0)

    def test_mixed_mode_prioritizes_ready_mismatch_over_pending_or_remote_gap(
        self,
    ) -> None:
        native_id = "DOC-NATIVE"
        remote_id = "DOC-REMOTE"
        case = _case(native_id, remote_id)
        native_snapshot = copy.deepcopy(self.snapshot)
        native_snapshot.nodes[0].raw_text = "wrong"
        native_snapshot.visual_text_snapshots = ()
        profiles = {
            native_id: _ready_profile("NATIVE"),
            remote_id: _pending_profile(
                review_mode="REMOTE_VISUAL",
                source_sha256=SECOND_SOURCE_SHA256,
            ),
        }
        outcome = runner._evaluate_pdf_reviewed_body_sequence(
            case=case,
            document_results=_document_results(
                {
                    native_id: SOURCE_SHA256,
                    remote_id: SECOND_SOURCE_SHA256,
                }
            ),
            source_ir_snapshots={(SOURCE_SHA256, "pdf"): native_snapshot},
            reviewed_body_profiles_by_document=profiles,
            verified_review_profiles_by_document={
                native_id: VerifiedPdfBodyReviewProfile(
                    ready_profile=profiles[native_id],
                    reviewed_profile=_reviewed_profile(),
                )
            },
            pdf_visual_provider="off",
        )
        self.assertEqual(outcome["status"], "FAIL")

        native_snapshot = copy.deepcopy(self.snapshot)
        native_snapshot.visual_text_snapshots = ()
        outcome = runner._evaluate_pdf_reviewed_body_sequence(
            case=case,
            document_results=_document_results(
                {
                    native_id: SOURCE_SHA256,
                    remote_id: SECOND_SOURCE_SHA256,
                }
            ),
            source_ir_snapshots={(SOURCE_SHA256, "pdf"): native_snapshot},
            reviewed_body_profiles_by_document=profiles,
            verified_review_profiles_by_document={
                native_id: VerifiedPdfBodyReviewProfile(
                    ready_profile=profiles[native_id],
                    reviewed_profile=_reviewed_profile(),
                )
            },
            pdf_visual_provider="off",
        )
        self.assertEqual(outcome["status"], "GAP")
        self.assertEqual(outcome["reason_code"], "pdf_body_review_pending")

    def test_native_ready_is_evaluated_before_remote_ready_provider_gap(self) -> None:
        native_id = "DOC-NATIVE"
        remote_id = "DOC-REMOTE"
        remote_snapshot = _body_snapshot(source_sha256=SECOND_SOURCE_SHA256)
        profiles = {
            native_id: _ready_profile("NATIVE"),
            remote_id: _ready_profile(
                "REMOTE_VISUAL",
                source_sha256=SECOND_SOURCE_SHA256,
            ),
        }
        outcome = runner._evaluate_pdf_reviewed_body_sequence(
            case=_case(native_id, remote_id),
            document_results=_document_results(
                {
                    native_id: SOURCE_SHA256,
                    remote_id: SECOND_SOURCE_SHA256,
                }
            ),
            source_ir_snapshots={
                (SOURCE_SHA256, "pdf"): self.snapshot,
                (SECOND_SOURCE_SHA256, "pdf"): remote_snapshot,
            },
            reviewed_body_profiles_by_document=profiles,
            verified_review_profiles_by_document={
                document_id: VerifiedPdfBodyReviewProfile(
                    ready_profile=profile,
                    reviewed_profile=_reviewed_profile(),
                )
                for document_id, profile in profiles.items()
            },
            pdf_visual_provider="off",
        )

        self.assertEqual(outcome["status"], "GAP")
        self.assertEqual(outcome["reason_code"], "pdf_body_remote_visual_not_run")
        self.assertEqual(outcome["evidence"]["observed_page_count"], 1)
        self.assertEqual(
            outcome["evidence"]["remote_visual_not_run_pages"],
            [{"document_id": remote_id, "page_number": 2}],
        )

    def test_pending_profile_needs_no_snapshot_or_remote_provider(self) -> None:
        pending = _pending_profile(review_mode="REMOTE_VISUAL")

        outcome = runner._evaluate_pdf_reviewed_body_sequence(
            case=self.case,
            document_results={},
            source_ir_snapshots={},
            reviewed_body_profiles_by_document={DOCUMENT_ID: pending},
            verified_review_profiles_by_document={},
            pdf_visual_provider="off",
        )

        self.assertEqual(outcome["status"], "GAP")
        self.assertEqual(outcome["reason_code"], "pdf_body_review_pending")
        self.assertEqual(
            outcome["evidence"]["pending_pages"],
            [{"document_id": DOCUMENT_ID, "page_number": 2}],
        )

    def test_manifest_pending_profiles_are_strict_source_bound_and_mixed(self) -> None:
        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        registry = json.loads(
            require_private_eval("extraction_cases/oracles.json").read_text()
        )
        assertion = manifest["cases"]["FMT-005"]["assertions"][3]
        self.assertEqual(
            assertion["target"],
            "document_body.reviewed_content_channels",
        )
        self.assertEqual(
            assertion["expected"],
            "human-reviewed page-bound text, image and table channel profiles",
        )
        profiles = assertion["evaluation"]["reviewed_body_profiles_by_document"]
        runner.validate_manifest(manifest, oracle_registry=registry)
        self.assertEqual(
            profiles["DOC-F06-ACTIVE-SUSPENSION"]["review_mode"],
            "NATIVE",
        )
        self.assertEqual(profiles["DOC-PEF-SOR"]["review_mode"], "REMOTE_VISUAL")
        profiles["DOC-F06-ACTIVE-SUSPENSION"]["approved_visual_provider"] = "local-ocr"
        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)

        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        profile = manifest["cases"]["FMT-005"]["assertions"][3]["evaluation"][
            "reviewed_body_profiles_by_document"
        ]["DOC-F06-ACTIVE-SUSPENSION"]
        profile["source_sha256"] = "f" * 64
        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)


class PdfTocCandidateFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_independent_of_adapter_derived_node_ids(self) -> None:
        first = SimpleNamespace(
            nodes=(
                _text_node(
                    source_node_id=f"node-{'1' * 32}",
                    block_index=0,
                    raw_text="1. Project scope",
                    kind=SourceNodeKind.PARAGRAPH,
                    page_number=1,
                ),
            )
        )
        second = copy.deepcopy(first)
        second.nodes[0].source_node_id = f"node-{'2' * 32}"

        first_refs = runner._pdf_toc_candidate_source_refs(first, expected_pages={1})
        second_refs = runner._pdf_toc_candidate_source_refs(second, expected_pages={1})

        self.assertEqual(
            runner._pdf_toc_candidate_fingerprint(first, first_refs),
            runner._pdf_toc_candidate_fingerprint(second, second_refs),
        )
        second.nodes[0].raw_text = "1. Project"
        self.assertNotEqual(
            runner._pdf_toc_candidate_fingerprint(first, first_refs),
            runner._pdf_toc_candidate_fingerprint(second, second_refs),
        )


def _case(*document_ids: str) -> dict:
    return {
        "document_ids": list(document_ids),
        "locators": [
            {
                "document_id": document_id,
                "kind": "page_region",
                "pointer": "toc=1-1;body=2-2",
            }
            for document_id in document_ids
        ],
    }


def _document_results(source_hashes: dict[str, str]) -> dict[str, dict[str, str]]:
    return {
        document_id: {
            "declared_format": "pdf",
            "detected_format": "pdf",
            "source_ir_status": "COMPLETE",
            "source_sha256": source_sha256,
        }
        for document_id, source_sha256 in source_hashes.items()
    }


def _body_snapshot(*, source_sha256: str = SOURCE_SHA256):
    table = SimpleNamespace(
        table_id="table-synthetic",
        row_count=2,
        column_count=2,
        cells=[
            _cell(row=0, column=0, raw_text="Header", column_span=2),
            _cell(row=1, column=0, raw_text="Left"),
            _cell(row=1, column=1, raw_text="Right"),
        ],
        header_regions=(),
        merged_regions=(SimpleNamespace(row=0, column=0, row_span=1, column_span=2),),
        notes=(),
    )
    nodes = (
        _text_node(
            source_node_id="node-alpha",
            block_index=0,
            raw_text="Alpha requirement",
            kind=SourceNodeKind.PARAGRAPH,
        ),
        _text_node(
            source_node_id="node-beta",
            block_index=1,
            raw_text="Beta long requirement",
            kind=SourceNodeKind.PARAGRAPH,
        ),
        SimpleNamespace(
            source_node_id="node-table",
            source_order=2,
            kind=SourceNodeKind.TABLE,
            raw_text="Header Left Right",
            native_locator=_locator(
                page_number=2,
                table_index=0,
                bbox=TABLE_BBOX,
            ),
            visual_locator=None,
            table_payload=table,
            asset_ref=None,
        ),
        _text_node(
            source_node_id="node-table-header-raw",
            block_index=2,
            raw_text="Header",
            kind=SourceNodeKind.PARAGRAPH,
        ),
        _text_node(
            source_node_id="node-table-left-raw",
            block_index=3,
            raw_text="Left",
            kind=SourceNodeKind.PARAGRAPH,
        ),
        _text_node(
            source_node_id="node-table-right-raw",
            block_index=4,
            raw_text="Right",
            kind=SourceNodeKind.PARAGRAPH,
        ),
    )
    return SimpleNamespace(
        source_sha256=source_sha256,
        nodes=nodes,
        assets=(),
        visual_text_snapshots=(_visual_snapshot(BASE_LINES),),
        visual_table_snapshots=(
            _visual_table_snapshot(source_sha256=source_sha256),
        ),
    )


def _visual_snapshot(lines):
    provider_id = visual_text_provider_id("bailian")
    observations = tuple(
        SimpleNamespace(
            observation_id=f"observation-{index}",
            observation_index=index,
            page_number=2,
            raw_text=line,
            bbox=(20.0, 100.0 + index * 20.0, 300.0, 115.0 + index * 20.0),
            provider_id=provider_id,
            provider_version=VISUAL_TEXT_PROVIDER_VERSION,
        )
        for index, line in enumerate(lines)
    )
    return SimpleNamespace(
        page_number=2,
        page_bbox=(0.0, 0.0, 612.0, 792.0),
        page_image_sha256=PAGE_IMAGE_SHA256,
        provider_id=provider_id,
        provider_version=VISUAL_TEXT_PROVIDER_VERSION,
        observations=observations,
    )


def _visual_table_snapshot(*, source_sha256: str = SOURCE_SHA256):
    return SimpleNamespace(
        visual_table_snapshot_id="visual-table-synthetic",
        source_sha256=source_sha256,
        provider_id=visual_text_provider_id("bailian"),
        provider_version=VISUAL_TABLE_PROVIDER_VERSION,
        page_number=2,
        page_bbox=(0.0, 0.0, 612.0, 792.0),
        table_source_node_id="node-table",
        table_id="table-synthetic",
        table_index=0,
        table_bbox=TABLE_BBOX,
        crop_image_sha256="9" * 64,
        row_count=2,
        column_count=2,
        cells=[
            SimpleNamespace(
                visual_table_cell_id="visual-cell-0-0",
                row=0,
                column=0,
                row_span=1,
                column_span=2,
                raw_text="Header",
            ),
            SimpleNamespace(
                visual_table_cell_id="visual-cell-1-0",
                row=1,
                column=0,
                row_span=1,
                column_span=1,
                raw_text="Left",
            ),
            SimpleNamespace(
                visual_table_cell_id="visual-cell-1-1",
                row=1,
                column=1,
                row_span=1,
                column_span=1,
                raw_text="Right",
            ),
        ],
    )


def _reviewed_profile() -> dict:
    image_summary = summarize_pdf_content_image_occurrences([])
    table_occurrences = [
        {
            "occurrence_sha256": canonical_pdf_table_occurrence_sha256(
                identity
            ),
            **identity,
            "disposition": "CONTENT",
        }
        for identity, _ in runner._pdf_body_review_native_table_records(
            _body_snapshot(),
            page_number=2,
        )
    ]
    table_summary = summarize_pdf_content_table_occurrences(table_occurrences)
    return {
        "visual_provider_id": visual_text_provider_id("bailian"),
        "visual_text_provider_version_sha256": hashlib.sha256(
            VISUAL_TEXT_PROVIDER_VERSION.encode("utf-8")
        ).hexdigest(),
        "visual_table_provider_version_sha256": hashlib.sha256(
            VISUAL_TABLE_PROVIDER_VERSION.encode("utf-8")
        ).hexdigest(),
        "pages": [
            {
                "page_number": 2,
                "image_occurrences": [],
                **image_summary,
                "table_occurrences": table_occurrences,
                **table_summary,
            }
        ],
    }


def _visual_runtime_identity() -> dict[str, str]:
    provider_id = visual_text_provider_id("bailian")
    return {
        "visual_text_provider_id": provider_id,
        "visual_text_provider_version_sha256": hashlib.sha256(
            VISUAL_TEXT_PROVIDER_VERSION.encode("utf-8")
        ).hexdigest(),
        "visual_table_provider_id": provider_id,
        "visual_table_provider_version_sha256": hashlib.sha256(
            VISUAL_TABLE_PROVIDER_VERSION.encode("utf-8")
        ).hexdigest(),
    }


def _text_node(
    *,
    source_node_id: str,
    block_index: int,
    raw_text: str,
    kind: SourceNodeKind,
    page_number: int = 2,
):
    return SimpleNamespace(
        source_node_id=source_node_id,
        kind=kind,
        raw_text=raw_text,
        native_locator=_locator(
            page_number=page_number,
            block_index=block_index,
            bbox=(10.0, 20.0 + block_index * 20.0, 200.0, 35.0 + block_index * 20.0),
        ),
        visual_locator=None,
        table_payload=None,
        asset_ref=None,
    )


def _image_node(
    source_node_id: str,
    asset_ref: str,
    *,
    block_index: int,
):
    return SimpleNamespace(
        source_node_id=source_node_id,
        source_order=block_index,
        kind=SourceNodeKind.IMAGE,
        raw_text="",
        native_locator=_locator(
            page_number=2,
            block_index=block_index,
            bbox=(10.0, 20.0, 200.0, 120.0),
        ),
        visual_locator=None,
        table_payload=None,
        asset_ref=asset_ref,
        asset_derivation=None,
    )


def _asset(asset_id: str, content_sha256: str):
    return SimpleNamespace(
        asset_id=asset_id,
        kind=SimpleNamespace(value="image"),
        media_type="image/png",
        content_sha256=content_sha256,
    )


def _image_occurrence(content_sha256: str) -> dict[str, str]:
    return {
        "node_kind": "image",
        "asset_kind": "image",
        "media_type": "image/png",
        "content_sha256": content_sha256,
    }


def _reviewed_image_occurrence(
    *,
    content_sha256: str,
    block_index: int,
    disposition: str,
) -> dict:
    identity = {
        "source_order": block_index,
        "node_kind": "image",
        "asset_kind": "image",
        "media_type": "image/png",
        "content_sha256": content_sha256,
        "native_locator": {
            "format": "pdf",
            "page_number": 2,
            "block_index": block_index,
            "bbox": [10.0, 20.0, 200.0, 120.0],
        },
        "derivation": None,
    }
    return {
        "occurrence_sha256": canonical_pdf_image_occurrence_sha256(identity),
        **identity,
        "disposition": disposition,
    }


def _cell(*, row: int, column: int, raw_text: str, column_span: int = 1):
    return SimpleNamespace(
        cell_id=f"cell-{row}-{column}",
        row=row,
        column=column,
        row_span=1,
        column_span=column_span,
        raw_text=raw_text,
        formula=None,
        display_value=None,
        native_locator=_locator(
            page_number=2,
            table_index=0,
            row_index=row,
            column_index=column,
        ),
    )


def _locator(
    *,
    page_number: int,
    block_index: int | None = None,
    table_index: int | None = None,
    row_index: int | None = None,
    column_index: int | None = None,
    bbox=None,
):
    return SimpleNamespace(
        format=SimpleNamespace(value="pdf"),
        whole_document=None,
        page_number=page_number,
        block_index=block_index,
        bbox=bbox,
        body_element_index=None,
        paragraph_index=None,
        table_index=table_index,
        row_index=row_index,
        column_index=column_index,
        object_index=None,
        workbook=None,
        sheet=None,
        cell_range=None,
    )


def _pending_profile(
    *,
    review_mode: str,
    source_sha256: str = SOURCE_SHA256,
) -> dict:
    return {
        "status": "PENDING",
        "source_sha256": source_sha256,
        "body_page_start": 2,
        "body_page_end": 2,
        "review_mode": review_mode,
        "review_approved": False,
    }


def _ready_profile(
    review_mode: str,
    *,
    source_sha256: str = SOURCE_SHA256,
    image_occurrences: tuple[dict[str, str], ...] = (),
) -> dict:
    content_multiset_sha256, sequence_sha256 = _review_digests(
        BASE_LINES,
        [TABLE_TOPOLOGY],
        image_occurrences,
    )
    page = {
        "page_number": 2,
        "expected_body_line_count": len(BASE_LINES),
        "expected_image_count": len(image_occurrences),
        "expected_table_count": 1,
        "expected_table_row_count": 2,
        "expected_table_cell_count": 3,
        "expected_merged_region_count": 1,
        "content_multiset_sha256": content_multiset_sha256,
        "sequence_sha256": sequence_sha256,
    }
    profile = {
        "status": "READY",
        "source_sha256": source_sha256,
        "body_page_start": 2,
        "body_page_end": 2,
        "review_mode": review_mode,
        "review_approved": True,
        "review_artifact_sha256": "c" * 64,
        "pages": [page],
    }
    if review_mode == "REMOTE_VISUAL":
        profile["approved_visual_provider"] = "bailian"
        page["page_image_sha256"] = PAGE_IMAGE_SHA256
    return profile


def _ready_profile_without_table() -> dict:
    content_multiset_sha256, sequence_sha256 = _review_digests(
        BASE_LINES,
        [],
    )
    return {
        "status": "READY",
        "source_sha256": SOURCE_SHA256,
        "body_page_start": 2,
        "body_page_end": 2,
        "review_mode": "REMOTE_VISUAL",
        "review_approved": True,
        "review_artifact_sha256": "c" * 64,
        "approved_visual_provider": "bailian",
        "pages": [
            {
                "page_number": 2,
                "page_image_sha256": PAGE_IMAGE_SHA256,
                "expected_body_line_count": len(BASE_LINES),
                "expected_image_count": 0,
                "expected_table_count": 0,
                "expected_table_row_count": 0,
                "expected_table_cell_count": 0,
                "expected_merged_region_count": 0,
                "content_multiset_sha256": content_multiset_sha256,
                "sequence_sha256": sequence_sha256,
            }
        ],
    }


def _review_digests(
    lines,
    table_topologies,
    image_occurrences=(),
) -> tuple[str, str]:
    content_items = [{"kind": "physical_line", "value": line} for line in lines]
    content_items.extend(
        {"kind": "table_topology", "value": topology} for topology in table_topologies
    )
    content_items.extend(
        {"kind": "image_occurrence", "value": occurrence}
        for occurrence in image_occurrences
    )
    content_items.sort(key=_canonical_json)
    sequence = {
        "physical_lines": list(lines),
        "image_occurrences": list(image_occurrences),
        "table_topologies": table_topologies,
    }
    return (
        _sha256_text(_canonical_json(content_items)),
        _sha256_text(_canonical_json(sequence)),
    )


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
