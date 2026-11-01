"""Protocol checks for the invented HTTP-only native integration harness."""
from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("smoke_native_provider", ROOT / "scripts/smoke_native_provider.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
finally:
    sys.path.pop(0)


def replies(values, *, success=True):
    return [{"role": "tool", "tool_call_id": f"smoke-{index:03d}",
        "content": json.dumps({"success": success, "text": [json.dumps(value)]})}
        for index, value in enumerate(values)]


class NativeProviderSmokeProtocolTests(unittest.TestCase):
    def mock(self, path):
        return smoke.NativeProviderMock(path, {"source_sha256": "a" * 64}, "a" * 64)

    def test_start_is_a_real_registered_context_call_and_completion_is_sdk_compatible(self):
        with TemporaryDirectory() as raw:
            value = self.mock(Path(raw)).respond({"model": smoke.MODEL, "messages": []})
            call = value["choices"][0]["message"]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "reqpilot_context")
            self.assertEqual(json.loads(call["function"]["arguments"]), {})
            self.assertEqual(value["object"], "chat.completion")
            self.assertEqual(value["model"], smoke.MODEL)

    def test_initial_finish_and_following_turn_continue_the_same_transcript(self):
        with TemporaryDirectory() as raw:
            mock = self.mock(Path(raw))
            messages = replies([{}] * 18)
            initial = mock.respond({"model": smoke.MODEL, "messages": messages})
            self.assertEqual(initial["choices"][0]["finish_reason"], "stop")
            messages.append(initial["choices"][0]["message"])
            messages.append({"role": "user", "content": "Exact same-task continuation"})
            resumed = mock.respond({"model": smoke.MODEL, "messages": messages})
            self.assertEqual(resumed["choices"][0]["message"]["tool_calls"][0]["id"], "smoke-018")
            self.assertEqual(resumed["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "reqpilot_context")

    def test_mock_rejects_failed_tools_and_a_different_model(self):
        with TemporaryDirectory() as raw:
            mock = self.mock(Path(raw))
            with self.assertRaisesRegex(RuntimeError, "Unexpected mock model"):
                mock.respond({"model": "a-live-model", "messages": []})
            with self.assertRaisesRegex(RuntimeError, "Production tool failed"):
                mock.respond({"model": smoke.MODEL, "messages": replies([{}], success=False)})

    def test_map_compile_consumes_the_actual_candidate_returned_path(self):
        with TemporaryDirectory() as raw:
            mock = self.mock(Path(raw))
            messages = replies([{"source_intake": {"arguments": {"source": "source/synthetic.pdf"}}}, {}, {},
                                {"path": "inputs/synthetic-map-a123.json"}])
            value = mock.respond({"model": smoke.MODEL, "messages": messages})
            arguments = json.loads(value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            self.assertEqual(arguments["operation"], "compile-map")
            self.assertEqual(arguments["arguments"]["input"], "inputs/synthetic-map-a123.json")
            self.assertEqual(arguments["arguments"]["source_ir"], "capture/source_ir.json")
