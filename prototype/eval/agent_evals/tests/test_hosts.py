import json
import tempfile
import unittest
from pathlib import Path
import sys

from prototype.eval.agent_evals.hosts import public_event
from prototype.eval.agent_evals.runner import execute_process


class HostTraceTests(unittest.TestCase):
    def test_success_envelope_with_native_timeout_is_not_natural_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = "import sys,json;print('[agy] print timeout after 0s with turn in progress; returning partial output',file=sys.stderr);print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':''}}))"
            result = execute_process(
                [sys.executable, "-c", code],
                "",
                root,
                root / "execution",
                host="antigravity",
            )
            self.assertFalse(result["natural_completion"])
            self.assertTrue(result["host_wait_expired"])

    def test_opaque_reasoning_steps_never_store_payload(self):
        output = public_event(
            "antigravity",
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "reasoning",
                    "step_index": 2,
                    "state": "DONE",
                    "text_delta": "PRIVATE_SENTINEL",
                    "reasoning": "PRIVATE_SENTINEL",
                },
            },
        )
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(output))

    def test_native_terminal_is_required_and_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = "import json;print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'done','usage':{'input_tokens':12}}}))"
            result = execute_process(
                [sys.executable, "-c", code],
                "",
                root,
                root / "execution",
                host="antigravity",
            )
            self.assertTrue(result["natural_completion"])
            events = (root / "execution/events.jsonl").read_text()
            self.assertIn('"turn.completed"', events)
            self.assertIn('"input_tokens": 12', events)

    def test_pair_reasoning_stderr_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = "import sys,json;print('PRIVATE_SENTINEL',file=sys.stderr);print(json.dumps({'type':'turn.completed'}))"
            result = execute_process(
                [sys.executable, "-c", code], "", root, root / "execution", host="pair"
            )
            self.assertTrue(result["natural_completion"])
            self.assertFalse((root / "execution/stderr.log").exists())
