from __future__ import annotations

from pathlib import Path
import unittest

from spikes.codex_app_server.probe import (
    build_command,
    build_environment,
    safe_account_summary,
    safe_model_summary,
)


class CodexAppServerProbeTests(unittest.TestCase):
    def test_account_summary_excludes_identity_and_credentials(self) -> None:
        summary = safe_account_summary(
            {
                "requiresOpenaiAuth": True,
                "account": {
                    "type": "chatgpt",
                    "planType": "pro",
                    "email": "must-not-escape@example.com",
                    "accessToken": "must-not-escape",
                },
            }
        )

        self.assertEqual(
            summary,
            {
                "account_type": "chatgpt",
                "plan_type": "pro",
                "requires_openai_auth": True,
            },
        )
        self.assertNotIn("email", summary)
        self.assertNotIn("accessToken", summary)

    def test_model_summary_keeps_only_explicit_image_models(self) -> None:
        summary = safe_model_summary(
            {
                "data": [
                    {"id": "text-only", "inputModalities": ["text"]},
                    {
                        "id": "vision-b",
                        "inputModalities": ["image", "text", "image"],
                    },
                    {
                        "id": "vision-a",
                        "inputModalities": ["text", "image"],
                    },
                    {"id": "unknown"},
                ]
            },
            preferred_model="vision-a",
        )

        self.assertEqual(summary["image_capable_count"], 2)
        self.assertEqual(
            [item["id"] for item in summary["image_capable_models"]],
            ["vision-a", "vision-b"],
        )
        self.assertTrue(summary["preferred_model_available"])

    def test_command_disables_ambient_agent_capabilities(self) -> None:
        command = build_command(Path("/opt/codex"))
        joined = " ".join(command)

        self.assertIn("features.shell_tool=false", joined)
        self.assertIn("features.unified_exec=false", joined)
        self.assertIn("features.apps=false", joined)
        self.assertIn("tools.web_search=false", joined)
        self.assertIn('history.persistence="none"', joined)
        self.assertIn('sandbox_mode="read-only"', joined)
        self.assertIn("--strict-config", command)

    def test_environment_drops_ambient_secrets_and_preserves_real_home(self) -> None:
        environment = build_environment(
            {
                "HOME": "/Users/example",
                "PATH": "/usr/bin",
                "DASHSCOPE_API_KEY": "must-not-escape",
                "SOME_TOKEN": "must-not-escape",
            },
            codex_home=Path("/tmp/reqpilotx-codex"),
        )

        self.assertEqual(environment["HOME"], "/Users/example")
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(
            environment["CODEX_HOME"],
            str(Path("/tmp/reqpilotx-codex").resolve()),
        )
        self.assertNotIn("DASHSCOPE_API_KEY", environment)
        self.assertNotIn("SOME_TOKEN", environment)


if __name__ == "__main__":
    unittest.main()
