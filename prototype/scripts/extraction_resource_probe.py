from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.private_eval import private_eval_path

from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.preflight import DocumentFormat
from scripts.extraction_case_runner import (
    CorpusPathError,
    load_manifest,
    require_report_destination_outside_corpus,
    resolve_document_path,
)

try:
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment]


PROJECT_DIR = Path(__file__).resolve().parents[1]
RESOURCE_PROBE_SCHEMA_VERSION = "extraction-resource-probe/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ExtractionResourceProbeRejected(RuntimeError):
    """Fail-closed input or measurement rejection without source content."""

    def __init__(self, code: str) -> None:
        token = str(code or "").strip()
        if not token:
            raise ValueError("resource probe rejection code must not be blank")
        self.code = token
        super().__init__(token)


@dataclass(frozen=True, slots=True)
class _UsageSnapshot:
    wall_seconds: float
    user_cpu_seconds: float
    system_cpu_seconds: float
    process_peak_rss_bytes: int


def measure_source_structure_resources(
    source: Path,
    *,
    document_id: str,
    expected_source_sha256: str,
    process_mode: str = "CALLER_PROCESS",
) -> dict[str, Any]:
    """Measure native/off SourceIR and Structure in the current process.

    The returned contract deliberately excludes source paths, filenames, source
    text, locators, diagnostics messages, page images, and asset bytes. RSS is
    the process cumulative high-water mark observed after each stage, matching
    the operating system's peak working-set semantics. The CLI marks its own
    invocation as a dedicated process; library callers are labeled separately.
    """

    document_token = str(document_id or "").strip()
    expected_hash = str(expected_source_sha256 or "").strip().lower()
    selected_process_mode = str(process_mode or "").strip().upper()
    if not document_token:
        raise ExtractionResourceProbeRejected("resource_probe_document_id_invalid")
    if _SHA256_PATTERN.fullmatch(expected_hash) is None:
        raise ExtractionResourceProbeRejected("resource_probe_source_hash_invalid")
    if selected_process_mode not in {"CALLER_PROCESS", "DEDICATED_CLI_PROCESS"}:
        raise ExtractionResourceProbeRejected("resource_probe_process_mode_invalid")

    source_path = Path(source)
    if not source_path.is_file():
        raise ExtractionResourceProbeRejected("resource_probe_source_unavailable")

    total_started = _usage_snapshot()
    stage_started = total_started
    preflight = LocalDocumentPreflight().inspect(source_path)
    preflight_finished = _usage_snapshot()
    if preflight.identity.detected_format is not DocumentFormat.PDF:
        raise ExtractionResourceProbeRejected("resource_probe_pdf_required")
    if not preflight.processable:
        raise ExtractionResourceProbeRejected("resource_probe_preflight_blocked")
    if preflight.source_sha256 != expected_hash:
        raise ExtractionResourceProbeRejected("resource_probe_source_hash_mismatch")
    preflight_metrics = _stage_metrics(stage_started, preflight_finished)

    stage_started = preflight_finished
    source_ir = build_source_ir(source_path, preflight_report=preflight)
    source_ir_finished = _usage_snapshot()
    if source_ir.source_sha256 != expected_hash:
        raise ExtractionResourceProbeRejected("resource_probe_source_ir_mismatch")
    source_ir_metrics = _stage_metrics(stage_started, source_ir_finished)

    stage_started = source_ir_finished
    structure = build_structure_tree(source_ir)
    structure_finished = _usage_snapshot()
    if structure.source_ir_id != source_ir.source_ir_id:
        raise ExtractionResourceProbeRejected("resource_probe_structure_mismatch")
    structure_metrics = _stage_metrics(stage_started, structure_finished)

    report: dict[str, Any] = {
        "schema_version": RESOURCE_PROBE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "measurement_scope": {
            "pipeline": "PREFLIGHT_SOURCE_IR_STRUCTURE",
            "visual_provider": "off",
            "process_mode": selected_process_mode,
            "rss_semantics": "PROCESS_CUMULATIVE_HIGH_WATER_MARK",
        },
        "runtime": {
            "python_version": platform.python_version(),
            "platform_system": platform.system(),
            "machine": platform.machine(),
        },
        "document": {
            "document_id": document_token,
            "source_sha256": expected_hash,
            "source_size_bytes": preflight.source_size,
            "page_count": preflight.page_count,
            "format": preflight.identity.detected_format.value,
        },
        "pipeline": {
            "source_ir": {
                "schema_version": source_ir.schema_version,
                "adapter_id": source_ir.adapter_id,
                "adapter_version": source_ir.adapter_version,
                "status": source_ir.status.value,
                "counts": {
                    "nodes": len(source_ir.nodes),
                    "assets": len(source_ir.assets),
                    "diagnostics": len(source_ir.diagnostics),
                    "visual_text_snapshots": len(source_ir.visual_text_snapshots),
                },
            },
            "structure": {
                "schema_version": structure.schema_version,
                "resolver_id": structure.resolver_id,
                "resolver_version": structure.resolver_version,
                "status": "REVIEW_REQUIRED"
                if structure.findings
                else "COMPLETE",
                "counts": {
                    "nodes": len(structure.nodes),
                    "relations": len(structure.relations),
                    "findings": len(structure.findings),
                    "blocking_findings": sum(
                        1 for finding in structure.findings if finding.blocking
                    ),
                },
            },
        },
        "measurements": {
            "baseline_process_peak_rss_bytes": (
                total_started.process_peak_rss_bytes
            ),
            "stages": {
                "preflight": preflight_metrics,
                "source_ir": source_ir_metrics,
                "structure": structure_metrics,
            },
            "total": _stage_metrics(total_started, structure_finished),
        },
        "privacy": {
            "contains_customer_text": False,
            "contains_source_path": False,
            "contains_source_filename": False,
            "contains_locators": False,
            "contains_asset_bytes": False,
            "is_fidelity_or_quality_evidence": False,
            "is_human_gold": False,
            "is_serving_activation_gate": False,
        },
    }
    report["report_sha256"] = canonical_resource_report_sha256(report)
    return report


def canonical_resource_report_sha256(report: dict[str, Any]) -> str:
    payload = dict(report)
    payload.pop("report_sha256", None)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def write_resource_report(
    report: dict[str, Any],
    destination: Path,
    *,
    corpus_root: Path,
) -> Path:
    output_path = require_report_destination_outside_corpus(
        Path(destination),
        corpus_root,
    )
    if output_path.suffix.lower() != ".json":
        raise ExtractionResourceProbeRejected("resource_probe_output_must_be_json")
    if output_path.exists():
        raise ExtractionResourceProbeRejected("resource_probe_output_exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, output_path)
        except FileExistsError as exc:
            raise ExtractionResourceProbeRejected(
                "resource_probe_output_exists"
            ) from exc
        temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def _usage_snapshot() -> _UsageSnapshot:
    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        user_cpu_seconds = float(usage.ru_utime)
        system_cpu_seconds = float(usage.ru_stime)
    else:
        process_times = os.times()
        user_cpu_seconds = float(process_times.user)
        system_cpu_seconds = float(process_times.system)
    return _UsageSnapshot(
        wall_seconds=time.perf_counter(),
        user_cpu_seconds=user_cpu_seconds,
        system_cpu_seconds=system_cpu_seconds,
        process_peak_rss_bytes=_current_process_peak_rss_bytes(),
    )


def _peak_rss_bytes(raw_value: int | float, *, platform_name: str) -> int:
    value = max(0, int(raw_value))
    return value if platform_name in {"darwin", "win32"} else value * 1024


def _current_process_peak_rss_bytes() -> int:
    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return _peak_rss_bytes(usage.ru_maxrss, platform_name=sys.platform)
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows
        return _windows_process_peak_rss_bytes()
    raise ExtractionResourceProbeRejected("resource_probe_rss_unsupported")


def _windows_process_peak_rss_bytes() -> int:
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process_handle = ctypes.windll.kernel32.GetCurrentProcess()
    succeeded = ctypes.windll.psapi.GetProcessMemoryInfo(
        process_handle,
        ctypes.byref(counters),
        counters.cb,
    )
    if not succeeded:
        raise ExtractionResourceProbeRejected("resource_probe_rss_unavailable")
    return int(counters.peak_working_set_size)


def _stage_metrics(
    started: _UsageSnapshot,
    finished: _UsageSnapshot,
) -> dict[str, Any]:
    user_cpu_ms = max(
        0.0,
        (finished.user_cpu_seconds - started.user_cpu_seconds) * 1000,
    )
    system_cpu_ms = max(
        0.0,
        (finished.system_cpu_seconds - started.system_cpu_seconds) * 1000,
    )
    return {
        "wall_ms": round(
            max(0.0, finished.wall_seconds - started.wall_seconds) * 1000,
            3,
        ),
        "user_cpu_ms": round(user_cpu_ms, 3),
        "system_cpu_ms": round(system_cpu_ms, 3),
        "cpu_ms": round(user_cpu_ms + system_cpu_ms, 3),
        "process_peak_rss_bytes_after_stage": finished.process_peak_rss_bytes,
        "process_peak_rss_growth_bytes_during_stage": max(
            0,
            finished.process_peak_rss_bytes - started.process_peak_rss_bytes,
        ),
    }


def _resolve_cli_path(value: Path) -> Path:
    return value if value.is_absolute() else PROJECT_DIR / value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure native/off ReqPilotX SourceIR and Structure resources "
            "without emitting customer content."
        )
    )
    parser.add_argument("--document-id", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=private_eval_path("extraction_cases/manifest.json"),
    )
    parser.add_argument("--corpus-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        manifest_path = _resolve_cli_path(args.manifest).resolve()
        manifest = load_manifest(
            manifest_path,
            oracle_path=manifest_path.with_name("oracles.json"),
        )
        document = (manifest.get("documents") or {}).get(args.document_id)
        if not isinstance(document, dict):
            raise ExtractionResourceProbeRejected(
                "resource_probe_document_not_registered"
            )
        environment_name = str(
            (manifest.get("corpus") or {}).get("mapping_environment_variable")
            or "REQPILOTX_TRAINING_CORPUS_ROOT"
        )
        corpus_value = str(
            args.corpus_root or os.getenv(environment_name) or ""
        ).strip()
        if not corpus_value:
            raise ExtractionResourceProbeRejected(
                "resource_probe_corpus_root_required"
            )
        corpus_root = Path(corpus_value)
        source = resolve_document_path(
            corpus_root,
            str(document.get("document_ref") or ""),
        )
        report = measure_source_structure_resources(
            source,
            document_id=args.document_id,
            expected_source_sha256=str(document.get("source_sha256") or ""),
            process_mode="DEDICATED_CLI_PROCESS",
        )
        if args.output is None:
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            output_path = write_resource_report(
                report,
                _resolve_cli_path(args.output),
                corpus_root=corpus_root,
            )
            print(f"JSON report: {output_path}")
    except (CorpusPathError, ExtractionResourceProbeRejected) as exc:
        code = getattr(exc, "code", "resource_probe_path_rejected")
        print(f"[ERROR] {code}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] resource_probe_failed:{type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
