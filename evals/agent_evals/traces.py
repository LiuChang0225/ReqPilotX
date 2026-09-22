"""Read-only metrics from public events and sealed task artifacts.

This module observes execution and byte integrity, never model reasoning or
semantic acceptance. It deliberately emits no command, output or error text.
Elapsed clocks are local to a segment; resumed clocks are never added together.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

TOOL_KINDS = {
    "command_execution",
    "mcp_tool_call",
    "tool_call",
    "function_call",
    "image_view",
}
TERMINALS = {"item.completed", "item.failed", "item.cancelled", "item.canceled"}
FAILED_STATUSES = {"FAILED", "NOT_READY", "CANCELLED", "CANCELED", "DENIED", "REJECTED"}
BUSINESS_STATUSES = FAILED_STATUSES | {
    "UNKNOWN",
    "COMPLETED",
    "READY",
    "OK",
    "COMMITTED",
    "SAVED_CANDIDATE",
    "CANDIDATE_CONFLICT",
}
USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "reasoning_tokens",
}
ARTIFACT_KINDS = {
    "VISUAL_SOURCE",
    "READING_WORKSPACE",
    "REVIEW_HEAD",
    "SOURCE_IR",
    "DOCUMENT_MAP",
    "SOURCE_ITEMS",
    "SOURCE_FINDINGS",
    "ANNOTATIONS",
    "DECOMPOSITION",
    "DELIVERY",
    "REVIEW_APPLICATION",
}
OPERATION_KINDS = {
    "read-pdf-visual",
    "revise-visual-text",
    "reading-workspace",
    "review-open",
    "capture",
    "compile-map",
    "compile-items",
    "compile-images",
    "compile-annotations",
    "compile-decomposition",
    "deliver",
    "review-revise",
    "review-apply",
    "compile-findings",
}
STATES = {
    "COMMITTED",
    "SAVED_CANDIDATE",
    "CANDIDATE_CONFLICT",
    "PREPARED",
    "RUNNING",
    "FAILED",
    "SAVED_PENDING_RECOVERY",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def _identifier(value, fallback="UNREPORTED"):
    if value is None or value == "":
        return fallback
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
        return value
    return "sha256:" + _sha(_canonical(value))


def _number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def _elapsed(event):
    return _number(event.get("_host_elapsed_seconds"))


def _view_image(name):
    return isinstance(name, str) and bool(re.search(r"(^|[._:/])view_image$", name))


def _error_code(value, default="TOOL_EXECUTION_FAILURE"):
    # Inspect only to classify; none of this text is returned or paraphrased.
    error = _object(value)
    if isinstance(error.get("error"), dict):
        error = error["error"]
    if (
        error.get("error_code") == "HOST_LLM_STREAM_IDLE_TIMEOUT"
        or error.get("code") == "HOST_LLM_STREAM_IDLE_TIMEOUT"
    ):
        return "HOST_LLM_STREAM_IDLE_TIMEOUT"
    if (
        error.get("message")
        == "MCP tool call requires approval, but approval policy is never"
    ):
        return "HOST_APPROVAL_DENIED"
    encoded = json.dumps(value, ensure_ascii=False).lower()
    if ("approval" in encoded or "permission" in encoded) and any(
        word in encoded
        for word in (
            "denied",
            "rejected",
            "required",
            "blocked",
            "not allowed",
            "reject",
            "cannot",
        )
    ):
        return "HOST_APPROVAL_DENIED"
    if any(
        word in encoded
        for word in (
            "unauthorized",
            "authentication",
            "invalid api key",
            "missing api key",
        )
    ):
        return "HOST_AUTHENTICATION_FAILURE"
    if any(
        word in encoded
        for word in ("connection refused", "connection reset", "network unreachable")
    ):
        return "HOST_CONNECTIVITY_FAILURE"
    return default


def _image_results(result):
    pictures = []
    structured = _object(
        result.get("structured_content") or result.get("structuredContent")
    )
    blocks = result.get("content")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        mime = block.get("mimeType") or block.get("mime_type")
        picture = {
            "mime_type": mime
            if mime
            in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
            else "UNREPORTED_OR_OTHER",
            "data_present": isinstance(block.get("data"), str),
        }
        if not picture["data_present"]:
            picture["evidence"] = "IMAGE_BLOCK_WITHOUT_BYTES"
        else:
            try:
                raw = base64.b64decode(block["data"], validate=True)
                picture.update(
                    bytes=len(raw),
                    sha256=_sha(raw),
                    evidence="NATIVE_IMAGECONTENT_BYTES_RETURNED"
                    if raw
                    else "IMAGE_BLOCK_WITHOUT_BYTES",
                )
                expected = _object(structured.get("image")).get("sha256")
                if expected is not None:
                    picture["declared_sha256_matches"] = expected == picture["sha256"]
            except (ValueError, binascii.Error):
                picture["evidence"] = "INVALID_IMAGECONTENT_ENCODING"
        if picture not in pictures:
            pictures.append(picture)
    return pictures


def _terminal(item, event_type, result):
    structured = _object(
        result.get("structured_content") or result.get("structuredContent")
    )
    status = str(item.get("status", "")).upper()
    business = str(structured.get("status", "")).upper()
    failed = (
        status in FAILED_STATUSES
        or event_type in TERMINALS - {"item.completed"}
        or bool(item.get("error"))
        or bool(result.get("isError") or result.get("is_error"))
        or business in FAILED_STATUSES
        or item.get("type") == "command_execution"
        and item.get("exit_code") not in (None, 0)
    )
    code = (
        _error_code(
            {
                "error": item.get("error"),
                "result": result,
                "output": item.get("aggregated_output"),
            }
        )
        if failed
        else None
    )
    outcome = (
        "failed"
        if failed
        else "unknown"
        if business == "UNKNOWN"
        else "completed"
        if event_type == "item.completed" or status == "COMPLETED"
        else "unknown"
    )
    return outcome, code, business if business in BUSINESS_STATUSES else None


def public_events(path: Path) -> dict:
    data = path.read_bytes()
    calls, turns, segments = {}, [], []
    errors, observations = Counter(), []
    malformed = omitted_reasoning = unkeyed = 0
    thread, segment, turn = "UNREPORTED_THREAD", None, None
    last_elapsed = None

    def new_segment(line, reason, time):
        nonlocal segment, turn, last_elapsed
        segment = {
            "segment_id": len(segments) + 1,
            "start_line": line,
            "boundary_reason": reason,
            "thread_id": thread,
            "first_observed_seconds": time,
            "last_observed_seconds": time,
        }
        segments.append(segment)
        turn, last_elapsed = None, None

    def new_turn(line, reason, turn_id=None):
        nonlocal turn
        turn = {
            "segment_id": segment["segment_id"],
            "thread_id": thread,
            "turn_occurrence": len(turns) + 1,
            "turn_id": _identifier(turn_id, None),
            "start_line": line,
            "boundary_reason": reason,
            "terminal_observations": [],
            "usage_observations": [],
        }
        turns.append(turn)

    for line_number, line in enumerate(data.splitlines(), 1):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        event_type = event.get("type") if isinstance(event.get("type"), str) else ""
        item = _object(event.get("item"))
        kind = item.get("type") if isinstance(item.get("type"), str) else ""
        if "reasoning" in str(event_type).lower() or "reasoning" in str(kind).lower():
            omitted_reasoning += 1
            continue
        time = _elapsed(event)
        if event_type == "thread.started":
            thread = _identifier(event.get("thread_id"), thread)
            if segment is None or line_number > segment["start_line"]:
                new_segment(line_number, "THREAD_STARTED", time)
        elif segment is None:
            new_segment(line_number, "FIRST_OBSERVED_EVENT", time)
        elif time is not None and last_elapsed is not None and time < last_elapsed:
            new_segment(line_number, "HOST_ELAPSED_RESET", time)
        if time is not None:
            last_elapsed = time
            segment["last_observed_seconds"] = time
            if segment["first_observed_seconds"] is None:
                segment["first_observed_seconds"] = time
        turn_id = event.get("turn_id") or item.get("turn_id")
        if event_type == "turn.started":
            new_turn(line_number, "TURN_STARTED", turn_id)
        elif turn_id and turn and turn["turn_id"] not in (None, _identifier(turn_id)):
            new_turn(line_number, "EXPLICIT_TURN_ID_CHANGED", turn_id)
        name = item.get("tool") or item.get("name") or item.get("tool_name")
        is_tool = kind in TOOL_KINDS or _view_image(name)
        is_turn_terminal = event_type in {
            "turn.completed",
            "turn.failed",
            "turn.cancelled",
            "turn.canceled",
        }
        if is_tool or is_turn_terminal or event.get("usage") is not None:
            late_duplicate = (
                turn is not None
                and event_type in TERMINALS
                and (
                    segment["segment_id"],
                    thread,
                    turn["turn_occurrence"],
                    _identifier(item.get("id")),
                )
                in calls
            )
            if turn is None or (
                is_tool and turn["terminal_observations"] and not late_duplicate
            ):
                new_turn(
                    line_number,
                    "IMPLICIT_AFTER_TERMINAL" if turn else "TURN_START_NOT_RECORDED",
                    turn_id,
                )
            elif turn_id and turn["turn_id"] is None:
                turn["turn_id"] = _identifier(turn_id)
        if is_turn_terminal:
            outcome = "completed" if event_type == "turn.completed" else "failed"
            turn["terminal_observations"].append(
                {"line": line_number, "outcome": outcome}
            )
            if outcome == "failed":
                code = _error_code(
                    event.get("error") or event, "TURN_EXECUTION_FAILURE"
                )
                errors[code] += 1
                observations.append(
                    {
                        "line": line_number,
                        "error_code": code,
                        "category": "ENVIRONMENT"
                        if code.startswith("HOST_")
                        else "EXECUTION_UNATTRIBUTED",
                    }
                )
        if event_type == "error":
            code = _error_code(event, "PUBLIC_EVENT_ERROR")
            errors[code] += 1
            observations.append(
                {
                    "line": line_number,
                    "error_code": code,
                    "category": "ENVIRONMENT"
                    if code.startswith("HOST_")
                    else "EXECUTION_UNATTRIBUTED",
                }
            )
        if event.get("usage") is not None:
            usage = _object(event["usage"])
            metrics = {
                key: usage[key]
                for key in sorted(USAGE_FIELDS & usage.keys())
                if _number(usage[key]) is not None
            }
            turn["usage_observations"].append({"line": line_number, "usage": metrics})
        if not is_tool:
            continue
        identifier = item.get("id")
        if not identifier:
            unkeyed += 1
            continue
        key = (
            segment["segment_id"],
            thread,
            turn["turn_occurrence"],
            _identifier(identifier),
        )
        record = calls.setdefault(
            key,
            {
                "segment_id": key[0],
                "thread_id": key[1],
                "turn_occurrence": key[2],
                "turn_id": turn["turn_id"],
                "item_id": key[3],
                "kind": kind if kind in TOOL_KINDS else "tool_call",
                "tool": _identifier(name, None),
                "server": _identifier(item.get("server"), None),
                "lines": [],
                "started_observed": False,
                "terminal_observations": [],
                "event_elapsed": [],
                "image_results": [],
                "image_requested": False,
                "builtin_view_image_observed": _view_image(name)
                or kind == "image_view",
            },
        )
        if (
            record["kind"] != kind
            or name
            and record["tool"] not in (None, _identifier(name))
        ):
            record["identity_conflict"] = True
        record["lines"].append(line_number)
        record["event_elapsed"].append(time)
        record["started_observed"] |= event_type == "item.started"
        if kind == "command_execution":
            command = item.get("command")
            if isinstance(command, str):
                record["command_sha256"] = _sha(command.encode())
                record["typed_cli_action_hints"] = sorted(
                    set(
                        re.findall(
                            r"\btask\s+(open|inspect|prepare|read|map|items|review|revise|images|annotations|decomposition|check|deliver|sync|recover)\b",
                            command,
                        )
                    )
                )
                record["reqpilot_cli_mentioned"] = bool(
                    re.search(
                        r"(?:reqpilot\.py|\.tools/reqpilot|reqpilot\s+task)", command
                    )
                )
        arguments = _object(item.get("arguments"))
        arguments = (
            _object(arguments.get("request"))
            if set(arguments) == {"request"}
            else arguments
        )
        record["image_requested"] |= arguments.get("view") in (
            "image",
            "item_image",
            "image_scope_image",
        )
        result = _object(item.get("result"))
        for picture in _image_results(result):
            if picture not in record["image_results"]:
                record["image_results"].append(picture)
        if event_type in TERMINALS or str(
            item.get("status", "")
        ).upper() in FAILED_STATUSES | {"COMPLETED"}:
            outcome, code, business = _terminal(item, event_type, result)
            terminal = {
                "line": line_number,
                "outcome": outcome,
                "business_status": business,
                "exit_code": item.get("exit_code")
                if type(item.get("exit_code")) is int
                else None,
                "elapsed_seconds": time,
            }
            if code:
                terminal.update(
                    error_code=code,
                    error_category="ENVIRONMENT"
                    if code.startswith("HOST_")
                    else "EXECUTION_UNATTRIBUTED",
                )
            record["terminal_observations"].append(terminal)

    groups, named = {}, {}
    for record in calls.values():
        outcomes = {value["outcome"] for value in record["terminal_observations"]}
        record["terminal_state"] = (
            next(iter(outcomes))
            if len(outcomes) == 1 and not record.get("identity_conflict")
            else "unknown"
        )
        if not outcomes:
            record["unknown_reason"] = "NO_TERMINAL_OBSERVATION"
        elif len(outcomes) > 1 or record.get("identity_conflict"):
            record["unknown_reason"] = "CONFLICTING_CALL_OBSERVATIONS"
        if record["terminal_state"] == "unknown":
            errors[record.get("unknown_reason", "UNKNOWN_TOOL_OUTCOME")] += 1
        for code in {
            entry["error_code"]
            for entry in record["terminal_observations"]
            if "error_code" in entry
        }:
            errors[code] += 1
        for picture in record["image_results"]:
            if picture["evidence"] == "INVALID_IMAGECONTENT_ENCODING":
                errors["INVALID_IMAGECONTENT_ENCODING"] += 1
            if picture.get("declared_sha256_matches") is False:
                errors["IMAGE_DECLARED_HASH_MISMATCH"] += 1
        times = [value for value in record.pop("event_elapsed") if value is not None]
        record.update(
            first_observed_seconds=min(times) if times else None,
            last_observed_seconds=max(times) if times else None,
        )
        bucket = (
            "shell_command_calls"
            if record["kind"] == "command_execution"
            else "mcp_calls"
            if record["kind"] == "mcp_tool_call"
            else "other_explicit_tool_calls"
        )
        group = groups.setdefault(
            bucket,
            {
                "unique_item_ids": 0,
                "started_observed": 0,
                "completed": 0,
                "failed": 0,
                "unknown": 0,
                "event_records": 0,
            },
        )
        group["unique_item_ids"] += 1
        group["started_observed"] += int(record["started_observed"])
        group[record["terminal_state"]] += 1
        group["event_records"] += len(record["lines"])
        if record["kind"] == "mcp_tool_call":
            name = (record["server"] or "") + "/" + (record["tool"] or "UNKNOWN")
            group = named.setdefault(
                name, {"unique_calls": 0, "completed": 0, "failed": 0, "unknown": 0}
            )
            group["unique_calls"] += 1
            group[record["terminal_state"]] += 1
    usage_records, totals = [], Counter()
    complete_usage = bool(turns)
    for turn in turns:
        outcomes = {entry["outcome"] for entry in turn["terminal_observations"]}
        turn["terminal_state"] = (
            next(iter(outcomes)) if len(outcomes) == 1 else "unknown"
        )
        if turn["terminal_state"] == "unknown":
            errors[
                "TURN_TERMINAL_NOT_RECORDED"
                if not outcomes
                else "CONFLICTING_TURN_TERMINALS"
            ] += 1
        usage = turn.pop("usage_observations")
        latest = usage[-1] if usage else None
        turn["usage_status"] = (
            "REPORTED"
            if latest and {"input_tokens", "output_tokens"} <= latest["usage"].keys()
            else "PARTIAL"
            if usage
            else "NOT_RECORDED"
        )
        complete_usage &= (
            turn["usage_status"] == "REPORTED" and turn["terminal_state"] != "unknown"
        )
        if latest:
            usage_records.append(
                {
                    "segment_id": turn["segment_id"],
                    "thread_id": turn["thread_id"],
                    "turn_occurrence": turn["turn_occurrence"],
                    **latest,
                }
            )
            totals.update(latest["usage"])
    details = list(calls.values())
    first_times = [
        record["first_observed_seconds"]
        for record in details
        if record["segment_id"] == 1 and record["first_observed_seconds"] is not None
    ]
    if malformed:
        errors["MALFORMED_EVENT_LINE"] += malformed
    if unkeyed:
        errors["UNKEYED_TOOL_EVENT"] += unkeyed
    return {
        "events_path": str(path),
        "events_snapshot_sha256": _sha(data),
        "events_snapshot_bytes": len(data),
        "line_count": len(data.splitlines()),
        "malformed_or_partial_lines_ignored": malformed,
        "reasoning_events_omitted": omitted_reasoning,
        "unique_tool_calls": len(details),
        "unkeyed_tool_events_not_counted_as_unique_calls": unkeyed,
        "groups": groups,
        "mcp_by_tool": named,
        "segments": segments,
        "turns": turns,
        "terminal_integrity": "COMPLETE"
        if turns
        and all(turn["terminal_state"] != "unknown" for turn in turns)
        and all(call["terminal_state"] != "unknown" for call in details)
        and not malformed
        and not unkeyed
        else "UNKNOWN",
        "first_tool_seconds": min(first_times) if first_times else None,
        "first_tool_timing_basis": "FIRST_SEGMENT_HOST_OBSERVED_ELAPSED"
        if first_times
        else "NOT_RECORDED",
        "usage": {
            "status": "REPORTED"
            if complete_usage
            else "PARTIAL"
            if usage_records
            else "NOT_RECORDED",
            "totals": dict(totals) if usage_records else None,
            "aggregation": "LAST_REPORTED_USAGE_PER_TURN; MISSING_FIELDS_ARE_NOT_ZERO",
            "records": usage_records,
        },
        "usage_if_reported": usage_records,
        "calls": details,
        "error_codes": sorted(errors),
        "error_counts": dict(errors),
        "error_observations": observations,
        "visual_evidence": {
            "calls_with_native_image_bytes": sum(
                any(p.get("bytes", 0) > 0 for p in r["image_results"]) for r in details
            ),
            "explicit_builtin_view_image_calls": sum(
                r["builtin_view_image_observed"] for r in details
            ),
            "mcp_image_requests": sum(
                r["kind"] == "mcp_tool_call" and r["image_requested"] for r in details
            ),
            "image_paths_or_rendered_files_alone": "NOT_PROOF_OF_VISUAL_TOOL_DELIVERY",
            "builtin_view_image_absence": "NOT_PROOF_OF_NO_VISUAL_READING",
            "semantic_image_understanding": "NOT_CLAIMED",
            "browser_rendering": "NOT_VERIFIED",
        },
    }


class _IntegrityError(ValueError):
    pass


def _read(path):
    try:
        value = json.loads(path.read_bytes())
    except OSError as exc:
        raise _IntegrityError("ARTIFACT_FILE_UNAVAILABLE") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise _IntegrityError("ARTIFACT_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise _IntegrityError("ARTIFACT_OBJECT_REQUIRED")
    return value


def _safe_path(root, relative):
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise _IntegrityError("ARTIFACT_PATH_OUTSIDE_BUNDLE")
    value = (root / relative).resolve()
    if value == root.resolve() or not value.is_relative_to(root.resolve()):
        raise _IntegrityError("ARTIFACT_PATH_OUTSIDE_BUNDLE")
    return value


def _verify_file(path, expected, code):
    try:
        actual = _sha(path.read_bytes())
    except OSError as exc:
        raise _IntegrityError("ARTIFACT_FILE_UNAVAILABLE") from exc
    if actual != expected:
        raise _IntegrityError(code)


def _seal(value, key, code):
    if value.get(key) != _sha(_canonical({k: v for k, v in value.items() if k != key})):
        raise _IntegrityError(code)


def _epoch(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise _IntegrityError("TIMESTAMP_INVALID_OR_TIMEZONE_MISSING") from exc


def _verify_receipt(root, context, entry):
    path = _safe_path(root, entry["receipt_path"])
    receipt = _read(path)
    _seal(receipt, "receipt_sha256", "RECEIPT_SEAL_MISMATCH")
    if receipt.get("receipt_sha256") != entry.get("receipt_sha256"):
        raise _IntegrityError("INDEX_RECEIPT_HASH_MISMATCH")
    if receipt.get("schema_version") != "reqpilot-operation-receipt/v1":
        raise _IntegrityError("RECEIPT_SCHEMA_UNSUPPORTED")
    if any(
        receipt.get(key) != expected
        for key, expected in (
            ("work_id", context["work_id"]),
            ("source_sha256", context["source"]["sha256"]),
            ("operation_id", entry["operation_id"]),
            ("operation", entry["operation"]),
        )
    ):
        raise _IntegrityError("RECEIPT_SCOPE_MISMATCH")
    if _safe_path(root, receipt["output_dir"]) != path.parent:
        raise _IntegrityError("RECEIPT_OUTPUT_DIRECTORY_MISMATCH")
    files = receipt["files"]
    artifacts = receipt["validation"]["artifacts"]
    if (
        not isinstance(files, dict)
        or not files
        or not isinstance(artifacts, list)
        or not artifacts
    ):
        raise _IntegrityError("RECEIPT_ARTIFACT_EVIDENCE_MISSING")
    verified = {}
    for relative, digest in files.items():
        target = _safe_path(path.parent, relative)
        _verify_file(target, digest, "SAVED_ARTIFACT_HASH_MISMATCH")
        verified[target] = digest
    for artifact in artifacts:
        if artifact["kind"] not in ARTIFACT_KINDS:
            raise _IntegrityError("ARTIFACT_KIND_UNSUPPORTED")
        target = _safe_path(root, artifact["path"])
        if (
            target not in verified
            or artifact.get("file_sha256", artifact.get("sha256")) != verified[target]
        ):
            raise _IntegrityError("ARTIFACT_NOT_BOUND_TO_RECEIPT_FILES")
    return path, receipt


def saved_artifacts(workspace: Path, start: dict | None) -> dict:
    errors, observations, unavailable, contexts, first = Counter(), [], [], [], {}
    counts = Counter(
        {
            key: 0
            for key in (
                "verified_tasks",
                "unverified_tasks",
                "indexed_operations",
                "effective_saved_operations",
                "candidate_operations",
                "conflict_operations",
                "incomplete_operations",
                "integrity_errors",
            )
        }
    )
    start_epoch = (
        _epoch(start["started_at"]) if start and start.get("started_at") else None
    )
    manifest = workspace / "runtime-data/input-manifest.json"
    expected_sources = None
    manifest_error = None
    if manifest.is_file():
        try:
            inputs = _read(manifest)["inputs"]
            if not isinstance(inputs, list) or any(
                not isinstance(value, dict) for value in inputs
            ):
                raise _IntegrityError("INPUT_MANIFEST_INVALID")
            expected_sources = {value["sha256"] for value in inputs}
        except (KeyError, TypeError, _IntegrityError):
            manifest_error = "INPUT_MANIFEST_INVALID"
    for context_path in sorted(workspace.rglob("task-context.json")):
        if any(
            name in context_path.relative_to(workspace).parts
            for name in (".reqpilot", ".agents", "__pycache__")
        ):
            continue
        root, task_errors = context_path.parent, set()
        try:
            if manifest_error:
                raise _IntegrityError(manifest_error)
            context, index = _read(context_path), _read(root / "workspace-index.json")
            if (
                context.get("schema_version") != "reqpilot-task-workspace/v1"
                or index.get("schema_version") != "reqpilot-workspace-index/v1"
            ):
                raise _IntegrityError("TASK_OR_INDEX_SCHEMA_UNSUPPORTED")
            if context.get("context_sha256"):
                _seal(context, "context_sha256", "CONTEXT_SEAL_MISMATCH")
            _seal(index, "index_sha256", "INDEX_SEAL_MISMATCH")
            if (
                index["work_id"] != context["work_id"]
                or index["source_sha256"] != context["source"]["sha256"]
            ):
                raise _IntegrityError("INDEX_TASK_SCOPE_MISMATCH")
            if (
                expected_sources is not None
                and context["source"]["sha256"] not in expected_sources
            ):
                raise _IntegrityError("TASK_SOURCE_NOT_IN_INPUT_MANIFEST")
            _verify_file(
                _safe_path(root, context["source"]["path"]),
                context["source"]["sha256"],
                "SOURCE_BYTES_CHANGED",
            )
            contexts.append(str(context_path))
            known, candidates, conflicts = set(), set(), set()
            entries = index["committed"]
            if not isinstance(entries, list) or not isinstance(
                index.get("candidates", []), list
            ):
                raise _IntegrityError("TASK_INDEX_SHAPE_INVALID")
            # Validate the entire identity set before any entry can count as a
            # save: a sealed but ambiguous index is still invalid evidence.
            if any(
                not isinstance(entry, dict)
                for entry in entries + index.get("candidates", [])
            ):
                raise _IntegrityError("TASK_INDEX_SHAPE_INVALID")
            identifiers = [_identifier(entry["operation_id"]) for entry in entries]
            if len(set(identifiers)) != len(identifiers):
                raise _IntegrityError("DUPLICATE_INDEX_OPERATION")
            for entry in entries:
                identifier = _identifier(entry["operation_id"])
                known.add(identifier)
                counts["indexed_operations"] += 1
                state = (
                    entry.get("state") if entry.get("state") in STATES else "UNKNOWN"
                )
                if state in {"SAVED_CANDIDATE", "CANDIDATE_CONFLICT"}:
                    candidates.add(identifier)
                if state == "CANDIDATE_CONFLICT":
                    conflicts.add(identifier)
                value = {
                    "task_id": _identifier(context["work_id"]),
                    "operation_id": identifier,
                    "operation": entry.get("operation")
                    if entry.get("operation") in OPERATION_KINDS
                    else "OTHER",
                    "state": state,
                    "effective_saved": False,
                    "integrity": "UNVERIFIED",
                    "artifact_kinds": [],
                }
                try:
                    receipt_path, receipt = _verify_receipt(root, context, entry)
                    seconds = (
                        round(_epoch(receipt["saved_at"]) - start_epoch, 3)
                        if start_epoch is not None
                        else None
                    )
                    value.update(
                        receipt_path=str(receipt_path),
                        receipt_sha256=receipt["receipt_sha256"],
                        saved_at=datetime.fromtimestamp(
                            _epoch(receipt["saved_at"]), timezone.utc
                        ).isoformat(),
                        seconds_since_run_start=seconds,
                        during_observed_run=seconds >= 0
                        if seconds is not None
                        else None,
                        effective_saved=state == "COMMITTED",
                        integrity="SOURCE_INDEX_RECEIPT_AND_SAVED_FILES_MATCH",
                        artifact_kinds=sorted(
                            {
                                artifact["kind"]
                                for artifact in receipt["validation"]["artifacts"]
                            }
                        ),
                    )
                    if value["effective_saved"]:
                        counts["effective_saved_operations"] += 1
                        if seconds is not None and seconds >= 0:
                            for kind in value["artifact_kinds"]:
                                first[kind] = min(first.get(kind, seconds), seconds)
                except (KeyError, TypeError, _IntegrityError) as exc:
                    code = (
                        str(exc)
                        if isinstance(exc, _IntegrityError)
                        else "RECEIPT_SHAPE_INVALID"
                    )
                    value["error_code"] = code
                    task_errors.add(code)
                    errors[code] += 1
                observations.append(value)
            for entry in index.get("candidates", []):
                candidates.add(_identifier(entry["operation_id"]))
                if entry.get("state") == "CANDIDATE_CONFLICT":
                    conflicts.add(_identifier(entry["operation_id"]))
            counts["candidate_operations"] += len(candidates)
            counts["conflict_operations"] += len(conflicts)
            for path in sorted((root / "operations").glob("*.json")):
                operation = _read(path)
                if _identifier(operation["operation_id"]) not in known:
                    counts["incomplete_operations"] += 1
        except (KeyError, TypeError, _IntegrityError) as exc:
            code = (
                str(exc)
                if isinstance(exc, _IntegrityError)
                else "TASK_ARTIFACT_SHAPE_INVALID"
            )
            task_errors.add(code)
            errors[code] += 1
        if task_errors:
            counts["unverified_tasks"] += 1
            unavailable.append(
                {"task_context": str(context_path), "error_codes": sorted(task_errors)}
            )
        else:
            counts["verified_tasks"] += 1
    counts["integrity_errors"] = sum(errors.values())
    return {
        "status": "VERIFIED"
        if contexts and not unavailable
        else "PARTIAL"
        if contexts
        else "NOT_RECORDED",
        "definition": "COMMITTED index entry plus matching source/index/receipt/artifact file hashes; semantic validation is separate",
        "semantic_acceptance": "NOT_CLAIMED",
        "first_effective_artifact_seconds": first,
        "timing_basis": "RECEIPT_SAVED_AT_MINUS_EXECUTION_START"
        if start_epoch is not None
        else "RUN_START_NOT_RECORDED",
        "operations": observations,
        "unverified_tasks": unavailable,
        "task_context_files": contexts,
        "counts": dict(counts),
        "error_codes": sorted(errors),
        "error_counts": dict(errors),
        "unregistered_loose_files": "NOT_COUNTED_AS_EFFECTIVE",
    }


def analyze(
    events: Path, workspace: Path | None = None, execution_start: Path | None = None
) -> dict:
    """Return a privacy-minimized supplement without writing to any input path."""
    event_metrics = public_events(Path(events))
    errors = Counter(event_metrics["error_counts"])
    start = None
    if execution_start is not None:
        try:
            start = _read(Path(execution_start))
            _epoch(start["started_at"])
        except (KeyError, _IntegrityError) as exc:
            errors[
                str(exc)
                if isinstance(exc, _IntegrityError)
                else "EXECUTION_START_INVALID"
            ] += 1
            start = None
    artifacts = (
        saved_artifacts(Path(workspace), start)
        if workspace is not None
        else {
            "status": "WORKSPACE_NOT_SUPPLIED",
            "semantic_acceptance": "NOT_CLAIMED",
            "operations": [],
            "unverified_tasks": [],
            "first_effective_artifact_seconds": {},
            "counts": {},
            "error_codes": [],
        }
    )
    errors.update(artifacts.get("error_counts", {}))
    counts = {
        "unique_tool_calls": event_metrics["unique_tool_calls"],
        "segments": len(event_metrics["segments"]),
        "turns": len(event_metrics["turns"]),
        "unknown_tool_calls": sum(
            r["terminal_state"] == "unknown" for r in event_metrics["calls"]
        ),
        "failed_tool_calls": sum(
            r["terminal_state"] == "failed" for r in event_metrics["calls"]
        ),
        **artifacts["counts"],
    }
    return {
        "schema_version": "reqpilot-agent-eval-traces/v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "read_only_supplement": True,
        "frozen_runner_result_modified": False,
        "script_sha256": _sha(Path(__file__).read_bytes()),
        "execution_start_record": str(execution_start) if start else None,
        "events": event_metrics,
        "saved_artifacts": artifacts,
        "counts": counts,
        "error_codes": sorted(errors),
        "error_counts": dict(errors),
        "semantic_acceptance": "NOT_CLAIMED",
        "limitations": [
            "Shell calls count public tool invocations, not hidden subprocesses.",
            "Elapsed times are segment-local; missing resume gaps cannot be reconstructed.",
            "Missing usage is unknown, never zero cost.",
            "Hash integrity and returned image bytes do not establish semantic correctness.",
            "Environment failures are not attributed to model capability.",
        ],
    }
