from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import hashlib
import subprocess
import tempfile
from pathlib import Path
import unittest

from scripts import repository_safety_audit


class RepositorySafetyAuditTests(unittest.TestCase):
    def test_tsx_and_module_scripts_are_scanned(self) -> None:
        for suffix in (".tsx", ".mjs", ".cjs", ".lock"):
            findings = repository_safety_audit.audit_file(
                "src/input" + suffix, b"token=gh" + b"p_abcdefghijklmnopqrstuvwxyz1234567890",
            )
            self.assertIn("credential_pattern", {f.rule for f in findings})

    def test_force_added_runtime_and_build_paths_cannot_bypass_scan(self) -> None:
        for path in ("prototype/runtime-data/app.sqlite3", "prototype/frontend/dist/app.js",
                     "plugins/x/node_modules/a.js", "prototype/.venv/config.py", "output/review.json"):
            findings = repository_safety_audit.audit_file(path, b"{}")
            self.assertIn("forbidden_tracked_path", {f.rule for f in findings})

    def test_index_scan_uses_staged_bytes_after_worktree_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            path = root / "source.tsx"
            path.write_bytes(b"staged")
            subprocess.run(["git", "-C", str(root), "add", "source.tsx"], check=True)
            path.write_bytes(b"working copy changed")
            self.assertEqual(list(repository_safety_audit.index_entries(root)),
                             [("source.tsx", b"staged")])

    def test_review_is_bound_to_content_and_does_not_allow_changed_secret(self) -> None:
        path = "tests/synthetic_key.py"
        reviewed = b"token = 'sk-" + b"a" * 32 + b"'"
        exceptions = {path: {
            "sha256": hashlib.sha256(reviewed).hexdigest(),
            "rules": ["credential_pattern"],
            "reason": "Deliberately invalid synthetic credential test input.",
        }}
        _, accepted = repository_safety_audit.audit_entries(
            [(path, reviewed)], reviewed_exceptions=exceptions,
        )
        self.assertEqual(accepted, [])
        _, changed = repository_safety_audit.audit_entries(
            [(path, reviewed + b" changed")], reviewed_exceptions=exceptions,
        )
        self.assertIn("credential_pattern", {f.rule for f in changed})

    def test_review_of_one_rule_does_not_hide_private_data_path(self) -> None:
        path, content = "prototype/eval/private/run.json", b"{}"
        exceptions = {path: {"sha256": hashlib.sha256(content).hexdigest(),
                             "rules": ["machine_path"], "reason": "Only path notation reviewed."}}
        _, findings = repository_safety_audit.audit_entries(
            [(path, content)], reviewed_exceptions=exceptions,
        )
        self.assertIn("forbidden_tracked_path", {f.rule for f in findings})

    def test_forbidden_evaluation_output_path_is_rejected(self) -> None:
        findings = repository_safety_audit.audit_file(
            "prototype/eval/extraction_batches/run/summary.json",
            b"{}",
        )

        self.assertIn("forbidden_tracked_path", {finding.rule for finding in findings})

    def test_unclassified_source_path_is_rejected(self) -> None:
        payload = json.dumps({"source_path": "/outside/customer.pdf"}).encode()

        findings = repository_safety_audit.audit_file("prototype/eval/result.json", payload)

        self.assertIn("unclassified_customer_fields", {finding.rule for finding in findings})

    def test_synthetic_label_on_known_path_still_requires_content_review(self) -> None:
        payload = json.dumps(
            {
                "data_classification": "synthetic_fixture",
                "extracted_text": "Synthetic requirement text.",
            }
        ).encode()

        findings = repository_safety_audit.audit_file(
            "prototype/eval/ai_smoke/ai_smoke_cases.json",
            payload,
        )

        self.assertIn("unclassified_customer_fields", {f.rule for f in findings})

    def test_synthetic_label_cannot_bypass_fixture_path_review(self) -> None:
        payload = json.dumps(
            {
                "data_classification": "synthetic_fixture",
                "extracted_text": "Claimed synthetic text.",
            }
        ).encode()

        findings = repository_safety_audit.audit_file("prototype/eval/unreviewed.json", payload)

        self.assertIn("unclassified_customer_fields", {finding.rule for finding in findings})

    def test_machine_path_in_evaluation_data_is_rejected(self) -> None:
        findings = repository_safety_audit.audit_file(
            "prototype/eval/result.csv",
            b"source,/Users/example/Documents/private.pdf\n",
        )

        self.assertIn("machine_path", {finding.rule for finding in findings})

    def test_secret_pattern_is_rejected(self) -> None:
        findings = repository_safety_audit.audit_file(
            "docs/example.md",
            b"token=gh" + b"p_abcdefghijklmnopqrstuvwxyz1234567890",
        )

        self.assertIn("credential_pattern", {finding.rule for finding in findings})

    def test_non_placeholder_email_is_rejected(self) -> None:
        findings = repository_safety_audit.audit_file(
            "docs/runbook.md",
            b"contact=user@" + b"company.test",
        )

        self.assertIn("personal_email", {finding.rule for finding in findings})

    def test_private_blocklist_reports_without_disclosing_identifier(self) -> None:
        findings = repository_safety_audit.audit_file(
            "docs/legacy.md",
            b"Private-Client-Code",
            blocked_identifiers=("private-client-code",),
        )

        self.assertEqual(findings[0].rule, "blocked_identifier")
        self.assertNotIn("private-client-code", findings[0].detail.lower())

    def test_ascii_blocklist_terms_require_token_boundaries(self) -> None:
        self.assertFalse(repository_safety_audit._contains_blocked_identifier("concatenate", "cat"))
        self.assertTrue(repository_safety_audit._contains_blocked_identifier("use cat here", "cat"))

    def test_report_redacts_paths_by_default(self) -> None:
        finding = repository_safety_audit.Finding(
            "forbidden_tracked_path",
            "prototype/eval/private/customer.json",
            "local/customer artifact path is tracked",
        )

        report = repository_safety_audit.build_report(
            tracked_files=1,
            findings=[finding],
            show_paths=False,
        )

        self.assertNotIn("path", report["findings"][0])
        self.assertEqual(len(report["findings"][0]["path_id"]), 12)


if __name__ == "__main__":
    unittest.main()
