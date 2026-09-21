"""A provider stall must remain diagnosable without exporting private streams."""

from pathlib import Path
import json
import subprocess
import unittest


class PairFailureEventsTests(unittest.TestCase):
    def test_idle_timeout_metadata_is_public_but_error_prose_and_streams_are_not(self):
        module = (Path(__file__).resolve().parents[1] / "pair_events.mjs").as_uri()
        script = f"""
const {{ apply }} = await import({json.dumps(module)});
let handle;
apply({{on(_event, fn) {{handle = fn;}}}});
const session = {{id: 'test-session'}};
const failure = {{code:'TIMEOUT', message:'pi-ai stream idle timeout after 300000ms', stack:'PRIVATE_SENTINEL'}};
for (const event of [
  {{type:'step/start',time:100,data:{{step:29}}}},
  {{type:'assistant/attempt',time:300100,data:{{step:29,stream:[{{type:'reasoning-chunks',texts:['PRIVATE_SENTINEL']}}]}}}},
  {{type:'llm/retry',time:300101,data:{{step:29,retry:1,maxRetries:5,delayMs:1000,failure}}}},
  {{type:'turn/end',time:600101,data:{{turn:1,reason:{{kind:'error',error:failure}}}}}},
  {{type:'turn/end',time:600102,data:{{turn:2,reason:{{kind:'error',error:{{code:'OTHER',message:'PRIVATE_SENTINEL'}}}}}}}},
]) handle(session,event);
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertNotIn("PRIVATE_SENTINEL", result.stdout)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        retry = next(e for e in events if e["type"] == "host.retry")
        self.assertEqual(retry["error_code"], "HOST_LLM_STREAM_IDLE_TIMEOUT")
        self.assertEqual(retry["idle_timeout_ms"], 300000)
        self.assertEqual(retry["max_retries"], 5)
        self.assertEqual(events[-2]["error_code"], "HOST_LLM_STREAM_IDLE_TIMEOUT")
        self.assertEqual(events[-1]["host_error_code"], "OTHER")
        self.assertNotIn("message", events[-1])
