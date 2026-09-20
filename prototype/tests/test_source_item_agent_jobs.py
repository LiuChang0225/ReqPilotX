from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from app.application.agent_workflow.source_item_agent_jobs import (
    bind_decomposition_quotes,
    decomposition_output_schema,
    _recover_analysis_checkpoint,
)
from tests.test_source_item_analysis import packet, decision
from app.application.agent_workflow.source_item_analysis import compile_source_item_assessments
from app.application.agent_workflow.source_item_decomposition import (
    SourceItemDecompositionError,
)
from app.adapters.agents.codex_app_server.json_stage_driver import (
    LocalCodexAppServerJsonStageDriver,
    SourceBoundImage,
    _StageLedger,
)
from jsonschema import Draft202012Validator


class SourceItemAgentJobTests(unittest.TestCase):
    def test_checkpoint_retry_retains_a_newer_unselected_analysis_and_receipt(self):
        original = compile_source_item_assessments(
            packet(), {"decisions": [decision("a"), decision("b")]},
            input_packet_ref={"object_key": "original"},
        )
        selected = packet(selected=("a",))
        checkpoint = compile_source_item_assessments(
            selected, {"decisions": [decision("a")]}, previous=original,
            input_packet_ref={"object_key": "failed-before-publication"},
        )
        newer = compile_source_item_assessments(
            packet(selected=("b",)),
            {"decisions": [decision("b", "PENDING_EVIDENCE")]},
            previous=original, input_packet_ref={"object_key": "newer"},
        )
        recovered = _recover_analysis_checkpoint(
            selected, checkpoint, previous=newer,
            input_packet_ref={"object_key": "failed-before-publication"},
        )
        self.assertEqual(recovered["records"]["a"], checkpoint["records"]["a"])
        self.assertEqual(recovered["records"]["b"], newer["records"]["b"])
        self.assertEqual(recovered["unit_input_receipts"]["b"], newer["unit_input_receipts"]["b"])
        self.assertEqual(recovered["predecessor_sha256"], newer["artifact_sha256"])

    def test_quote_binding_preserves_exact_unicode_positions_and_context(self):
        packet = {
            "parents": [
                {"source_item_id": "source-a", "segments": [{"text": "甲要求；乙要求"}]}
            ]
        }
        candidate = {
            "decisions": [
                {
                    "source_item_id": "source-a",
                    "action": "SPLIT",
                    "reason": "独立义务",
                    "logic": "ALL_OF",
                    "children": [
                        {
                            "text": text,
                            "derivation_type": "VERBATIM_SPAN",
                            "requirement_kinds": ["FUNCTIONAL"],
                            "source_quotes": [{"segment_index": 0, "quote": text}],
                        }
                        for text in ("甲要求", "乙要求")
                    ],
                    "context_quotes": [
                        {
                            "segment_index": 0,
                            "quote": "；",
                            "disposition": "PRESENTATION",
                        }
                    ],
                }
            ]
        }
        Draft202012Validator(decomposition_output_schema(["source-a"])).validate(
            candidate
        )
        decision = bind_decomposition_quotes(packet, candidate)[0]
        self.assertEqual(
            decision["children"][0]["source_ranges"],
            [{"segment_index": 0, "start_offset": 0, "end_offset": 3}],
        )
        self.assertEqual(
            decision["children"][1]["source_ranges"],
            [{"segment_index": 0, "start_offset": 4, "end_offset": 7}],
        )
        self.assertEqual(
            decision["context_ranges"][0]["source_range"]["start_offset"], 3
        )

    def test_ambiguous_quote_is_rejected_without_inventing_a_location(self):
        packet = {
            "parents": [
                {"source_item_id": "source-a", "segments": [{"text": "same and same"}]}
            ]
        }
        child = {
            "text": "same",
            "derivation_type": "VERBATIM_SPAN",
            "requirement_kinds": ["QUALITY"],
            "source_quotes": [{"segment_index": 0, "quote": "same"}],
        }
        candidate = {
            "decisions": [
                {
                    "source_item_id": "source-a",
                    "action": "SPLIT",
                    "reason": "test",
                    "logic": "ALL_OF",
                    "children": [child, child],
                    "context_quotes": [],
                }
            ]
        }
        with self.assertRaisesRegex(SourceItemDecompositionError, "exactly once"):
            bind_decomposition_quotes(packet, candidate)

    def test_image_source_root_and_hash_are_checked_before_any_model_start(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "evidence.png"
            path.write_bytes(b"owned image")
            driver = LocalCodexAppServerJsonStageDriver(
                codex_bin=Path(sys.executable), codex_home=root, repository_root=root
            )
            value = SourceBoundImage(
                path=path,
                expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                source_sha256="a" * 64,
                fact_id="fact-a",
            )
            kwargs = dict(
                packet={},
                output_schema={"type": "object"},
                skill_name="reqpilotx-test",
                skill_markdown="Test",
                task_directive="Test",
                image_inputs=(value,),
            )
            with self.assertRaisesRegex(ValueError, "authorized source roots"):
                driver.complete(**kwargs)
            path.write_bytes(b"changed image")
            with self.assertRaisesRegex(ValueError, "SHA changed"):
                driver.complete(**kwargs, authorized_image_roots=(root,))

    def test_usage_receipt_retains_numbers_without_reasoning_text(self):
        ledger = _StageLedger()
        ledger.observe(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-a",
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 10,
                            "outputTokens": 4,
                            "reasoningOutputTokens": 2,
                            "raw": "not retained",
                        }
                    },
                },
            }
        )
        self.assertEqual(
            ledger.token_usage,
            {"inputTokens": 10, "outputTokens": 4, "reasoningOutputTokens": 2},
        )


if __name__ == "__main__":
    unittest.main()
