"""No network: existing-profile selection must not widen provider destinations."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from private_web_trial import provider_environment


class PrivateWebTrialProfileTests(unittest.TestCase):
    def test_explicit_profile_is_read_without_rewriting_or_reusing_old_model(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            raw = json.dumps(
                {
                    "DASHSCOPE_API_KEY": "invented-test-only-key",
                    "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "REQPILOT_ALIYUN_CHAT_MODEL": "old-invented-model",
                    "UNRELATED": "must-not-copy",
                }
            )
            path.write_text(raw)
            env = provider_environment(path, "qwen3.8-max-0902")
            self.assertEqual(path.read_text(), raw)
            self.assertEqual(env["REQPILOT_AGENT_MODEL_ID"], "qwen3.8-max-0902")
            self.assertNotIn("UNRELATED", env)

    def test_foreign_credential_urls_and_unapproved_model_are_rejected(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            for origin in (
                "http://dashscope.aliyuncs.com/v1",
                "https://untrusted.invalid/v1",
                "https://user@example.invalid/v1",
                "https://dashscope.aliyuncs.com/v1?key=invented",
            ):
                path.write_text(
                    json.dumps(
                        {
                            "DASHSCOPE_API_KEY": "invented-test-only-key",
                            "REQPILOT_DASHSCOPE_BASE_URL": origin,
                        }
                    )
                )
                with self.assertRaises(ValueError):
                    provider_environment(path, "qwen3.8-max-0902")
            with self.assertRaises(ValueError):
                path.write_text(
                    json.dumps(
                        {
                            "DASHSCOPE_API_KEY": "invented-test-only-key",
                            "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        }
                    )
                )
                provider_environment(path, "another-model")


if __name__ == "__main__":
    unittest.main()
