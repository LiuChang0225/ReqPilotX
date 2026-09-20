#!/usr/bin/env python3
"""Fail closed when repository-tracked files look like customer or secret data.

The default output redacts file names because a leaked customer file name is itself
sensitive. Use ``--show-paths`` only in a trusted local terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_REPORTED_FINDINGS = 100

FORBIDDEN_PREFIXES = (
    "prototype/eval/private/",
    "prototype/eval/private_oracles/",
    "prototype/runtime-data/",
    "output/",
    "tmp/",
    "prototype/eval/local/",
    "prototype/eval/extraction_batches/",
    "prototype/eval/extraction_diagnose/",
    "prototype/eval/phase0/",
    "prototype/eval/iterations/",
    "prototype/eval/regression_suite/",
    "prototype/prototype/",
    "prototype/storage/",
)
FORBIDDEN_EXACT_PATHS = {
    "prototype/eval/extraction_batches.json",
    "prototype/eval/extraction_cases/manifest.json",
    "prototype/eval/extraction_cases/oracles.json",
    "prototype/eval/extraction_cases/silver.json",
    "prototype/eval/extraction_cases/llm_silver.json",
    "prototype/eval/extraction_cases/llm_experience.json",
    "prototype/eval/extraction_cases/visual_routes.json",
}
FORBIDDEN_DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".eml",
    ".msg",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}
SAFE_EVAL_CLASSIFICATIONS = {
    "public_configuration",
    "synthetic",
    "synthetic_fixture",
    "synthetic_template",
}
CUSTOMER_DATA_KEYS = {
    "extracted_text",
    "file_path",
    "input_path",
    "pdf_path",
    "preview_text",
    "review_note",
    "review_notes",
    "source_path",
    "text_preview",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".lock",
    ".sql",
    ".txt",
    ".yaml",
    ".yml",
}

MACHINE_PATH_RE = re.compile(
    rb"(?:/Users/[^/\s]+/|/home/(?!site/)[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(rb"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "openai_key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "alibaba_access_key": re.compile(rb"\bLTAI[A-Za-z0-9]{12,}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "azure_storage_key": re.compile(rb"\bAccountKey=[A-Za-z0-9+/=]{32,}", re.IGNORECASE),
}


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    detail: str

    @property
    def path_id(self) -> str:
        return hashlib.sha256(self.path.encode("utf-8", errors="surrogateescape")).hexdigest()[:12]


def _path_is_forbidden(path: str) -> bool:
    if set(Path(path).parts) & {"node_modules", ".venv", "__pycache__", ".vite", "dist", "dist-panel"}:
        return True
    name = Path(path).name
    if name.startswith(".env") and name != ".env.example":
        return True
    if path in FORBIDDEN_EXACT_PATHS or path.startswith(FORBIDDEN_PREFIXES):
        return True
    if path.startswith("prototype/eval/etc_"):
        return True
    if path.startswith("prototype/eval/irps_rag/") and path not in {
        "prototype/eval/irps_rag/manifest.example.json", "prototype/eval/irps_rag/README.md"
    }:
        return True
    return False


def _collect_json_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_collect_json_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_collect_json_keys(child))
    return keys


def audit_file(
    path: str,
    data: bytes,
    *,
    blocked_identifiers: tuple[str, ...] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    suffix = Path(path).suffix.lower()

    if _path_is_forbidden(path):
        findings.append(Finding("forbidden_tracked_path", path, "local/customer artifact path is tracked"))

    if suffix in FORBIDDEN_DOCUMENT_EXTENSIONS:
        findings.append(Finding("document_binary", path, f"tracked source-document extension: {suffix}"))

    if suffix in TEXT_SUFFIXES and len(data) > MAX_TEXT_BYTES:
        findings.append(Finding("oversized_text_not_scanned", path, "text exceeds scan limit"))
        return findings
    if suffix not in TEXT_SUFFIXES:
        return findings

    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(data):
            findings.append(Finding("credential_pattern", path, name))

    for match in EMAIL_RE.findall(data):
        email = match.decode("ascii", errors="ignore").lower()
        if not email.endswith(("@example.com", "@example.invalid")):
            findings.append(Finding("personal_email", path, "non-placeholder email address in tracked text"))
            break

    if blocked_identifiers:
        decoded = data.decode("utf-8", errors="ignore").casefold()
        if any(_contains_blocked_identifier(decoded, identifier) for identifier in blocked_identifiers):
            findings.append(Finding("blocked_identifier", path, "private incident blocklist match"))

    if suffix in {".csv", ".json", ".md", ".txt", ".yaml", ".yml"}:
        if MACHINE_PATH_RE.search(data):
            findings.append(Finding("machine_path", path, "machine-specific absolute path in tracked text"))

    if path.startswith("prototype/eval/") and suffix == ".json":
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            findings.append(Finding("invalid_eval_json", path, "evaluation JSON is not valid UTF-8 JSON"))
            return findings

        classification = ""
        if isinstance(payload, dict):
            classification = str(payload.get("data_classification") or "").strip().lower()
        sensitive_keys = sorted(_collect_json_keys(payload) & CUSTOMER_DATA_KEYS)
        if sensitive_keys:
            findings.append(
                Finding(
                    "unclassified_customer_fields",
                    path,
                    "sensitive fields require an explicitly reviewed synthetic fixture path",
                )
            )

    return findings


def _contains_blocked_identifier(text: str, identifier: str) -> bool:
    if identifier.isascii() and identifier.isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(identifier)}(?![a-z0-9])", text))
    return identifier in text


def audit_entries(
    entries: Iterable[tuple[str, bytes]],
    *,
    blocked_identifiers: tuple[str, ...] = (),
    reviewed_exceptions: dict[str, dict] | None = None,
) -> tuple[int, list[Finding]]:
    count = 0
    findings: list[Finding] = []
    for path, data in entries:
        count += 1
        detected = audit_file(path, data, blocked_identifiers=blocked_identifiers)
        review = (reviewed_exceptions or {}).get(path, {})
        # A reviewed fixture or upstream attribution is exempt only at the
        # exact reviewed bytes; changing the file requires reviewing it again.
        allowed = set(review.get("rules", [])) & {
            "credential_pattern", "personal_email", "machine_path",
            "blocked_identifier", "unclassified_customer_fields",
        } if (
            review.get("sha256") == hashlib.sha256(data).hexdigest()
            and str(review.get("reason", "")).strip()
        ) else set()
        findings.extend(f for f in detected if f.rule not in allowed)
    return count, findings


def tracked_entries(repo_root: Path = REPO_ROOT) -> Iterable[tuple[str, bytes]]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
    )
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        file_path = repo_root / path
        try:
            yield path, file_path.read_bytes()
        except FileNotFoundError:
            # A worktree deletion is safe; the index still lists it until staging.
            continue


def index_entries(repo_root: Path = REPO_ROOT) -> Iterable[tuple[str, bytes]]:
    """Read the exact blobs to be committed, never their working-tree copies."""
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=repo_root)
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=repo_root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split()
            if stage != b"0":
                raise ValueError("Cannot audit an unmerged index")
            if mode == b"160000":
                raise ValueError("Nested repositories need an explicit reviewed distribution policy")
            process.stdin.write(object_id + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError("Index blob could not be read")
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            yield raw_path.decode("utf-8", errors="surrogateescape"), data
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait(timeout=10)


def history_entries(repo_root: Path = REPO_ROOT) -> Iterable[tuple[str, bytes]]:
    """Yield every path/blob pair reachable from any local Git reference."""
    paths_by_object: dict[str, set[str]] = {}
    commits = subprocess.check_output(["git", "rev-list", "--all"], cwd=repo_root).splitlines()
    for commit in commits:
        tree = subprocess.check_output(
            ["git", "ls-tree", "-r", "-z", "--full-tree", commit.decode("ascii")],
            cwd=repo_root,
        )
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or fields[1] != b"blob":
                continue
            object_id = fields[2].decode("ascii")
            path = raw_path.decode("utf-8", errors="surrogateescape")
            paths_by_object.setdefault(object_id, set()).add(path)

    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for object_id, paths in paths_by_object.items():
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip().split()
            if len(header) != 3:
                continue
            _, object_type, size_text = header
            size = int(size_text)
            data = process.stdout.read(size)
            process.stdout.read(1)
            if object_type != "blob":
                continue
            for path in sorted(paths):
                yield path, data
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait(timeout=10)


def build_report(
    *,
    tracked_files: int,
    findings: list[Finding],
    show_paths: bool,
    scope: str = "tracked_worktree",
    max_reported_findings: int | None = MAX_REPORTED_FINDINGS,
) -> dict[str, object]:
    serialized: list[dict[str, str]] = []
    selected_findings = findings if max_reported_findings is None else findings[:max_reported_findings]
    for finding in selected_findings:
        item = {
            "rule": finding.rule,
            "path_id": finding.path_id,
            "detail": finding.detail,
        }
        if show_paths:
            item["path"] = finding.path
        serialized.append(item)
    return {
        "status": "PASS" if not findings else "FAIL",
        "scope": scope,
        "tracked_files": tracked_files,
        "finding_count": len(findings),
        "reported_finding_count": len(serialized),
        "findings_truncated": len(findings) > len(serialized),
        "rule_counts": {
            rule: sum(finding.rule == rule for finding in findings)
            for rule in sorted({finding.rule for finding in findings})
        },
        "paths_redacted": not show_paths,
        "findings": serialized,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-paths", action="store_true", help="Show violating paths in trusted local output.")
    parser.add_argument("--staged", action="store_true", help="Audit exact index blobs that will be committed.")
    parser.add_argument(
        "--history",
        action="store_true",
        help="Scan every blob reachable from every local Git ref instead of the candidate worktree.",
    )
    parser.add_argument(
        "--all-findings",
        action="store_true",
        help="Do not truncate findings. Use only with protected local output.",
    )
    parser.add_argument("--json-report", type=Path, help="Optional report destination; keep incident reports ignored.")
    parser.add_argument(
        "--blocklist",
        type=Path,
        help="Optional ignored UTF-8 file with one private customer identifier per line.",
    )
    return parser.parse_args(argv)


def load_blocklist(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    source = path if path.is_absolute() else REPO_ROOT / path
    values: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value.casefold())
    return tuple(dict.fromkeys(values))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.history and args.staged:
        raise ValueError("Choose either staged or history scope")
    entries = history_entries() if args.history else index_entries() if args.staged else tracked_entries()
    blocked_identifiers = load_blocklist(args.blocklist)
    review_path = REPO_ROOT / "prototype/contracts/repository-safety-reviews.json"
    if args.staged:
        reviewed_blob = subprocess.run(
            ["git", "show", ":prototype/contracts/repository-safety-reviews.json"],
            cwd=REPO_ROOT, capture_output=True, check=False,
        )
        reviews = json.loads(reviewed_blob.stdout) if reviewed_blob.returncode == 0 else {}
    else:
        reviews = json.loads(review_path.read_text()) if review_path.is_file() else {}
    tracked_files, findings = audit_entries(
        entries, blocked_identifiers=blocked_identifiers,
        reviewed_exceptions=reviews,
    )
    report = build_report(
        tracked_files=tracked_files,
        findings=findings,
        show_paths=args.show_paths,
    scope="reachable_history" if args.history else "staged_index" if args.staged else "tracked_worktree",
        max_reported_findings=None if args.all_findings else MAX_REPORTED_FINDINGS,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.json_report:
        destination = args.json_report
        if not destination.is_absolute():
            destination = REPO_ROOT / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
