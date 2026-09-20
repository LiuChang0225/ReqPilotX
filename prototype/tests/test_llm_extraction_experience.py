from __future__ import annotations

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

import json
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1] / "eval" / "extraction_cases"
SCHEMA_PATH = ROOT / "llm_experience.schema.json"
REGISTRY_PATH = ROOT / "llm_experience.json"
FORBIDDEN_KEYS = {
    "raw_text",
    "text",
    "content",
    "file_path",
    "source_path",
    "local_path",
    "filename",
    "file_name",
    "source_uri",
    "prompt_body",
    "response_body",
    "report_path",
}


class LlmExtractionExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.registry = json.loads(require_private_eval("extraction_cases/llm_experience.json").read_text(encoding="utf-8"))

    def test_schema_and_registry_are_strictly_valid(self) -> None:
        jsonschema.Draft202012Validator.check_schema(self.schema)
        jsonschema.Draft202012Validator(
            self.schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(self.registry)

    def test_registry_is_text_free_non_gold_and_non_serving(self) -> None:
        self.assertEqual(
            self.registry["schema_version"],
            "llm-extraction-experience/v2",
        )
        self.assertEqual(self.registry["content_policy"], "TEXT_FREE")
        self.assertFalse(self.registry["is_human_gold"])
        self.assertFalse(self.registry["is_serving_activation_gate"])
        self.assertEqual(_find_forbidden_keys(self.registry), set())
        serialized = json.dumps(self.registry, ensure_ascii=False)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("external://", serialized)
        self.assertNotIn("apple", serialized.lower())

    def test_experience_ids_are_unique_and_gap_is_not_promoted(self) -> None:
        records = self.registry["records"]
        ids = [item["experience_id"] for item in records]
        self.assertEqual(len(ids), len(set(ids)))
        gap = next(
            item
            for item in records
            if item["experience_id"] == "LLM-EXP-QJLY-BILINGUAL-LIST-GAP"
        )
        self.assertEqual(gap["review_disposition"], "OBSERVED_GAP")
        self.assertIn("INTRA_TARGET_SPAN_REVIEW_ENABLED", gap["experience_codes"])
        self.assertIn("UPSTREAM_SEGMENTATION_REQUIRED", gap["experience_codes"])
        review = gap["review_validation"]
        self.assertEqual(
            review["prediction_schema_version"],
            "llm-requirement-prediction/v2",
        )
        self.assertEqual(review["review_schema_version"], "llm-requirement-review/v2")
        self.assertEqual(review["provider_version"], "2.15.0;s=1234;t=0;think=0")
        self.assertEqual(review["model_id"], "qwen3.7-plus")
        self.assertEqual(review["model_target_count"], 15)
        self.assertEqual(review["review_node_count"], 16)
        self.assertEqual(review["split_target_count"], 1)
        self.assertTrue(review["exact_partition_valid"])
        self.assertTrue(review["persisted_round_trip_valid"])
        self.assertFalse(review["requirement_set_mutation"])
        self.assertNotIn(
            "invocations",
            next(item for item in records if item["case_kind"] == "REPEATABILITY"),
        )

    def test_remote_ocr_is_registered_only_and_never_local(self) -> None:
        ocr_records = [
            item for item in self.registry["records"] if "remote_ocr" in item
        ]
        self.assertEqual(len(ocr_records), 1)
        binding = ocr_records[0]["remote_ocr"]
        self.assertEqual(
            binding["allowed_page_numbers"], binding["actual_page_numbers"]
        )
        self.assertEqual(binding["provider_id"], "aliyun-bailian-qwen-ocr")

    def test_alias_failure_and_pinned_ab_result_are_both_preserved(self) -> None:
        records = self.registry["records"]
        failed = next(
            item
            for item in records
            if item["experience_id"] == "LLM-EXP-F06-TAB005-ALIAS-INVALID-V15"
        )
        self.assertEqual(failed["review_disposition"], "OBSERVED_GAP")
        self.assertEqual(failed["provider_binding"]["model_id"], "qwen3.7-plus")
        self.assertEqual(
            [item["status"] for item in failed["invocations"]],
            ["INVALID_OUTPUT", "SKIPPED"],
        )
        self.assertEqual(failed["observation"]["unresolved_semantic_count"], 11)
        self.assertEqual(failed["observation"]["requirement_view_node_count"], 0)
        self.assertIn(
            "FAIL_CLOSED_WITHOUT_RULE_FALLBACK",
            failed["experience_codes"],
        )

        pinned = next(
            item
            for item in records
            if item["experience_id"] == "LLM-EXP-F06-TAB005-PINNED-CURRENT-V15"
        )
        self.assertEqual(
            pinned["provider_binding"]["model_id"],
            "qwen3.7-plus-2026-05-26",
        )
        self.assertEqual(pinned["observation"]["unresolved_semantic_count"], 0)
        self.assertEqual(
            pinned["observation"]["decision_counts"],
            {"DIRECT": 3, "ROLLUP": 1, "NONE": 2},
        )
        self.assertIn("CORE_DECISION_SKELETON_MATCH", pinned["experience_codes"])

    def test_invocation_status_metadata_is_internally_consistent(self) -> None:
        for record in self.registry["records"]:
            for invocation in record.get("invocations", []):
                status = invocation["status"]
                self.assertLess(
                    invocation["retry_count"], max(1, invocation["request_count"])
                )
                if status == "SUCCESS":
                    self.assertIsInstance(invocation["response_sha256"], str)
                    self.assertNotIn("error_code", invocation)
                elif status == "SKIPPED":
                    self.assertEqual(invocation["request_count"], 0)
                    self.assertEqual(invocation["retry_count"], 0)
                    self.assertIsNone(invocation["response_sha256"])
                    self.assertIn("error_code", invocation)
                else:
                    self.assertGreaterEqual(invocation["request_count"], 1)
                    self.assertIn("error_code", invocation)

    def test_repeatability_keeps_strict_failure_and_decision_stability(self) -> None:
        records = [
            item
            for item in self.registry["records"]
            if item["case_kind"] == "REPEATABILITY"
        ]
        self.assertEqual(len(records), 2)
        for record in records:
            repeatability = record["repeatability"]
            self.assertEqual(repeatability["strict_status"], "FAIL")
            self.assertTrue(repeatability["human_review_required"])
            self.assertTrue(repeatability["decision_skeleton_exact_match"])
            self.assertFalse(repeatability["decision_review_required"])
            self.assertTrue(repeatability["non_decision_variance_only"])
            self.assertEqual(
                repeatability["unstable_decision_target_structure_node_ids"], []
            )
        latest = next(
            item
            for item in records
            if item["experience_id"] == "LLM-EXP-F06-TAB005-REPEAT3-V2"
        )["repeatability"]
        self.assertTrue(latest["classification_request_exact_match"])
        self.assertFalse(latest["assembly_request_exact_match"])
        self.assertFalse(latest["provider_request_exact_match"])

    def test_real_document_runtime_boundary_is_preserved(self) -> None:
        records = {
            item["experience_id"]: item for item in self.registry["records"]
        }
        buffered = records["LLM-EXP-AUTHORIZED-DOC-64-BUFFERED"][
            "runtime_observation"
        ]
        stream_64 = records["LLM-EXP-AUTHORIZED-CHUNK-64-STREAM"][
            "runtime_observation"
        ]
        stream_16 = records["LLM-EXP-AUTHORIZED-CHUNK-16-STREAM"][
            "runtime_observation"
        ]

        self.assertEqual(buffered["semantic_target_count"], 1085)
        self.assertEqual(buffered["unresolved_target_count"], 1044)
        self.assertEqual(
            buffered["error_target_counts"],
            {"provider_transport_error": 1044},
        )
        self.assertEqual(stream_64["transport_mode"], "STREAMING")
        self.assertEqual(stream_64["classification_status"], "INVALID_OUTPUT")
        self.assertEqual(stream_64["configured_chunk_node_budget"], 64)
        self.assertEqual(stream_16["configured_chunk_node_budget"], 16)
        self.assertEqual(stream_16["classification_status"], "SUCCESS")
        self.assertEqual(stream_16["assembly_status"], "SUCCESS")
        self.assertEqual(stream_16["unresolved_target_count"], 0)


def _find_forbidden_keys(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        result.update(str(key) for key in value if key in FORBIDDEN_KEYS)
        for item in value.values():
            result.update(_find_forbidden_keys(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_find_forbidden_keys(item))
    return result


if __name__ == "__main__":
    unittest.main()
