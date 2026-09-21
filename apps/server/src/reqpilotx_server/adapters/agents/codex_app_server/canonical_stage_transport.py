"""Local App Server transport for one canonical structured extraction stage.

The transport deliberately owns platform concerns only: effective-config
preflight, one-thread lifecycle, a restricted read-only turn, streamed event
validation, structured-output decoding, cancellation, and cleanup.  Stage
methodology remains in the attached Portable Agent Kit Skill and Host schemas.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol

import jsonschema

from reqpilotx_server.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalDynamicToolHost,
    CanonicalDynamicToolSpec,
    canonical_dynamic_tool_specs_sha256,
)
from reqpilotx_server.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
    AgentNativeReadOnlyRuntimePolicyV1,
    agent_native_read_confinement_mode,
    validate_agent_native_effective_config,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageInvocation,
    CanonicalStageTransportResult,
    canonical_sha256,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    EventLedger,
    JsonlRpcSession,
    LifecycleProbeError,
    _delete_thread_with_fresh_session,
    _notification_thread_id,
    _notification_turn_id,
    _start_or_resume_thread,
    _validate_instruction_sources,
    _validate_initialize_result,
    _validate_turn_start_result,
    _verify_thread_absent_after_restart,
)
from reqpilotx_server.adapters.agents.codex_app_server.primary_runtime_pdf import (
    validate_openai_primary_runtime_pdf_environment,
)
from reqpilotx_server.adapters.agents.codex_app_server.pdf_skill_canary import (
    _isolate_runtime_skills,
    _parse_agent_json,
    _verify_skill_allowlist,
    _workspace_manifest,
)
from reqpilotx_server.adapters.agents.codex_app_server.probe import (
    safe_account_summary,
    safe_model_summary,
)


CANONICAL_STAGE_EVENT_LEDGER_SCHEMA_VERSION = (
    "reqpilotx-canonical-stage-event-ledger/v1"
)
DEFAULT_CANONICAL_STAGE_TIMEOUT_SECONDS = 900.0
_CANONICAL_STAGE_PROMPT_MARKER = "REQPILOTX_CANONICAL_STAGE_TURN_V1"

_ALLOWED_STAGE_ITEM_TYPES = frozenset(
    {
        "agentMessage",
        "commandExecution",
        "contextCompaction",
        "imageView",
        "plan",
        "reasoning",
        "userMessage",
    }
)
_FORBIDDEN_ITEM_REASON_CODES = {
    "collabAgentToolCall": "CANONICAL_STAGE_MULTI_AGENT_FORBIDDEN",
    "collabToolCall": "CANONICAL_STAGE_MULTI_AGENT_FORBIDDEN",
    "dynamicToolCall": "CANONICAL_STAGE_DYNAMIC_TOOL_FORBIDDEN",
    "enteredReviewMode": "CANONICAL_STAGE_REVIEW_MODE_FORBIDDEN",
    "exitedReviewMode": "CANONICAL_STAGE_REVIEW_MODE_FORBIDDEN",
    "fileChange": "CANONICAL_STAGE_FILE_CHANGE_FORBIDDEN",
    "hookPrompt": "CANONICAL_STAGE_HOOK_FORBIDDEN",
    "imageGeneration": "CANONICAL_STAGE_IMAGE_GENERATION_FORBIDDEN",
    "mcpToolCall": "CANONICAL_STAGE_MCP_EVENT_FORBIDDEN",
    "sleep": "CANONICAL_STAGE_SLEEP_FORBIDDEN",
    "subAgentActivity": "CANONICAL_STAGE_MULTI_AGENT_FORBIDDEN",
    "webSearch": "CANONICAL_STAGE_WEB_EVENT_FORBIDDEN",
}
_CONTENT_DELTA_METHODS = frozenset(
    {
        "item/commandExecution/outputDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
    }
)
_APPROVAL_METHOD_SUFFIXES = (
    "/requestApproval",
    "/requestUserInput",
)


class CanonicalStageJsonlSession(Protocol):
    """Narrow surface shared by the real JSONL session and contract fakes."""

    ledger: EventLedger

    def configure_dynamic_server_requests(
        self,
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None: ...

    def initialize(self) -> Mapping[str, Any]: ...

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def wait_for_turn(
        self,
        turn_id: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> str: ...

    def wait_for_thread_deleted(self, thread_id: str) -> None: ...

    def drain_notifications_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout_seconds: float,
    ) -> None: ...

    def stop_gracefully(self, *, timeout_seconds: float = 3.0) -> None: ...

    def close(self, *, force: bool = False) -> None: ...


SessionOpener = Callable[
    [CanaryRuntimeState, "CanonicalStageEventLedger", float],
    CanonicalStageJsonlSession,
]
ThreadLifecycleAction = Callable[[CanaryRuntimeState, str, float], None]
RuntimeSkillIsolator = Callable[
    [CanaryRuntimeState, Mapping[str, Path], float],
    tuple[CanaryRuntimeState, int],
]


@dataclass
class CanonicalStageEventLedger(EventLedger):
    """Accept native read tools but retain only content-free event metadata."""

    readable_root: Path = field(default_factory=Path)
    delta_method_counts: Counter[str] = field(default_factory=Counter)
    completed_item_status_counts: Counter[str] = field(default_factory=Counter)
    snapshot_reconciled_item_type_counts: Counter[str] = field(default_factory=Counter)
    completed_turn_snapshot_verified: bool = False
    allowed_dynamic_tool_specs: tuple[CanonicalDynamicToolSpec, ...] = ()
    dynamic_tool_request_ids: set[str] = field(default_factory=set)
    dynamic_tool_item_arguments_sha256: dict[str, str] = field(default_factory=dict)
    dynamic_tool_item_names: dict[str, str] = field(default_factory=dict)
    dynamic_tool_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    contact_sheet_opened: bool = False
    high_resolution_page_numbers: set[int] = field(default_factory=set)
    opened_image_receipts: set[str] = field(default_factory=set)
    other_image_view_count: int = 0

    def __post_init__(self) -> None:
        try:
            root = self.readable_root.resolve(strict=True)
        except OSError as exc:
            raise LifecycleProbeError("CANONICAL_STAGE_READ_ROOT_INVALID") from exc
        if self.readable_root.is_symlink() or not root.is_dir():
            raise LifecycleProbeError("CANONICAL_STAGE_READ_ROOT_INVALID")
        self.readable_root = root

    def observe(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            raise LifecycleProbeError("UNEXPECTED_NOTIFICATION_METHOD")
        if "id" in message or method.endswith(_APPROVAL_METHOD_SUFFIXES):
            raise LifecycleProbeError("CANONICAL_STAGE_APPROVAL_REQUEST_FORBIDDEN")
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise LifecycleProbeError("NOTIFICATION_PARAMS_INVALID")
        if method in _CONTENT_DELTA_METHODS:
            self._observe_content_delta(method, params)
            return
        if method in {"item/started", "item/completed"}:
            self._observe_stage_item(method, params)
            return
        if method in {"turn/diff/updated", "item/fileChange/outputDelta"}:
            raise LifecycleProbeError("CANONICAL_STAGE_FILE_CHANGE_FORBIDDEN")
        if method.startswith("mcpServer/") or method.startswith("item/tool/"):
            raise LifecycleProbeError("CANONICAL_STAGE_MCP_EVENT_FORBIDDEN")
        super().observe(message)

    def _observe_content_delta(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        thread_id = _notification_thread_id(params)
        turn_id = _notification_turn_id(params)
        item_id = params.get("itemId")
        if thread_id is None or turn_id is None or not _nonempty(item_id):
            raise LifecycleProbeError("CANONICAL_STAGE_DELTA_IDENTITY_MISSING")
        self._validate_event_identity(thread_id=thread_id, turn_id=turn_id)
        expected_item_type = (
            "commandExecution"
            if method == "item/commandExecution/outputDelta"
            else "reasoning"
        )
        if self.item_types_by_id.get(str(item_id)) != expected_item_type:
            raise LifecycleProbeError("CANONICAL_STAGE_DELTA_ITEM_MISMATCH")
        self.method_counts[method] += 1
        self.delta_method_counts[method] += 1
        # Intentionally do not read or retain `delta`, command output, or reasoning.

    def _observe_stage_item(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        thread_id = _notification_thread_id(params)
        turn_id = _notification_turn_id(params)
        if thread_id is None or turn_id is None:
            raise LifecycleProbeError("ITEM_EVENT_IDENTITY_MISSING")
        self._validate_event_identity(thread_id=thread_id, turn_id=turn_id)
        item = params.get("item")
        if not isinstance(item, Mapping):
            raise LifecycleProbeError("ITEM_EVENT_INVALID")
        item_type = item.get("type")
        item_id = item.get("id")
        if not isinstance(item_type, str) or not _nonempty(item_id):
            raise LifecycleProbeError("ITEM_TYPE_INVALID")
        forbidden = _FORBIDDEN_ITEM_REASON_CODES.get(item_type)
        if item_type == "dynamicToolCall" and self.allowed_dynamic_tool_specs:
            forbidden = None
        if forbidden is not None:
            raise LifecycleProbeError(forbidden)
        if item_type not in _ALLOWED_STAGE_ITEM_TYPES and not (
            item_type == "dynamicToolCall" and self.allowed_dynamic_tool_specs
        ):
            suffix = _item_type_code(item_type)
            raise LifecycleProbeError(
                f"CANONICAL_STAGE_ITEM_TYPE_FORBIDDEN_{suffix or 'UNKNOWN'}"
            )

        self.method_counts[method] += 1
        self.item_type_counts[item_type] += 1
        item_key = str(item_id)
        known_type = self.item_types_by_id.setdefault(item_key, item_type)
        if known_type != item_type:
            raise LifecycleProbeError("ITEM_TYPE_DRIFT")
        if method == "item/started":
            if item_key in self.started_item_ids:
                raise LifecycleProbeError("ITEM_STARTED_DUPLICATE")
            self.started_item_ids.add(item_key)
        else:
            if item_key not in self.started_item_ids:
                raise LifecycleProbeError("ITEM_COMPLETED_WITHOUT_START")
            if item_key in self.completed_item_ids:
                raise LifecycleProbeError("ITEM_COMPLETED_DUPLICATE")
            self.completed_item_ids.add(item_key)
            status = item.get("status")
            if isinstance(status, str):
                self.completed_item_status_counts[f"{item_type}:{status}"] += 1

        if item_type == "dynamicToolCall":
            self._validate_dynamic_tool_item(method=method, item=item)
        elif item_type == "commandExecution":
            self._validate_command_item(method=method, item=item)
        elif item_type == "imageView":
            self._validate_image_item(method=method, item=item)
        elif method == "item/completed" and item_type == "agentMessage":
            text = item.get("text")
            phase = item.get("phase")
            if not isinstance(text, str) or phase not in {
                None,
                "commentary",
                "final_answer",
            }:
                raise LifecycleProbeError("AGENT_MESSAGE_INVALID")
            if phase != "commentary":
                self.final_agent_message = text

    def _validate_event_identity(self, *, thread_id: str, turn_id: str) -> None:
        self.observed_thread_ids.add(thread_id)
        self.observed_turn_ids.add(turn_id)
        if self.expected_thread_id is not None and thread_id != self.expected_thread_id:
            raise LifecycleProbeError("EVENT_THREAD_ID_MISMATCH")
        if self.expected_turn_id is not None and turn_id != self.expected_turn_id:
            raise LifecycleProbeError("EVENT_TURN_ID_MISMATCH")

    def _validate_command_item(
        self,
        *,
        method: str,
        item: Mapping[str, Any],
    ) -> None:
        command = item.get("command")
        cwd = item.get("cwd")
        status = item.get("status")
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(cwd, str)
        ):
            raise LifecycleProbeError("CANONICAL_STAGE_COMMAND_ITEM_INVALID")
        _require_path_within_root(
            Path(cwd),
            root=self.readable_root,
            require_file=False,
            reason_code="CANONICAL_STAGE_COMMAND_CWD_OUTSIDE_ROOT",
        )
        expected_statuses = (
            {"inProgress"} if method == "item/started" else {"completed", "failed"}
        )
        if status not in expected_statuses:
            raise LifecycleProbeError("CANONICAL_STAGE_COMMAND_STATUS_INVALID")

    def _validate_image_item(
        self,
        *,
        method: str,
        item: Mapping[str, Any],
    ) -> None:
        path = item.get("path")
        if not isinstance(path, str):
            raise LifecycleProbeError("CANONICAL_STAGE_IMAGE_ITEM_INVALID")
        resolved = _require_path_within_root(
            Path(path),
            root=self.readable_root,
            require_file=True,
            reason_code="CANONICAL_STAGE_IMAGE_PATH_OUTSIDE_ROOT",
        )
        if method != "item/completed":
            return
        relative = resolved.relative_to(self.readable_root).as_posix()
        if relative.endswith("/evidence-workspace/contact-sheet.png") or relative == (
            "evidence-workspace/contact-sheet.png"
        ):
            self.contact_sheet_opened = True
        else:
            matched = re.search(
                r"(?:^|/)evidence-workspace/renders/high/P([0-9]{4})\.png$",
                relative,
            )
            if matched is not None:
                self.high_resolution_page_numbers.add(int(matched.group(1)))
            else:
                self.other_image_view_count += 1
        self.opened_image_receipts.add(
            canonical_sha256(
                {
                    "relative_path_sha256": hashlib.sha256(
                        relative.encode("utf-8")
                    ).hexdigest(),
                    "image_sha256": _sha256_file(resolved),
                }
            )
        )

    def _validate_dynamic_tool_item(
        self,
        *,
        method: str,
        item: Mapping[str, Any],
    ) -> None:
        item_id = str(item.get("id") or "")
        tool_name = item.get("tool")
        namespace = item.get("namespace")
        arguments = item.get("arguments")
        status = item.get("status")
        allowed = {spec.name: spec for spec in self.allowed_dynamic_tool_specs}
        if (
            not item_id
            or not isinstance(tool_name, str)
            or tool_name not in allowed
            or namespace is not None
            or not isinstance(arguments, Mapping)
        ):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_ITEM_INVALID")
        arguments_sha256 = canonical_sha256(dict(arguments))
        if method == "item/started":
            if status != "inProgress":
                raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_STATUS_INVALID")
            self.dynamic_tool_item_names[item_id] = tool_name
            self.dynamic_tool_item_arguments_sha256[item_id] = arguments_sha256
            return
        if (
            status not in {"completed", "failed"}
            or self.dynamic_tool_item_names.get(item_id) != tool_name
            or self.dynamic_tool_item_arguments_sha256.get(item_id) != arguments_sha256
        ):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_COMPLETION_INVALID")
        receipt = self.dynamic_tool_receipts.get(item_id)
        if receipt is None:
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_RECEIPT_MISSING")
        success = item.get("success")
        content_items = item.get("contentItems")
        if (
            success is not receipt["success"]
            or status != ("completed" if success is True else "failed")
            or not isinstance(content_items, list)
            or len(content_items) != 1
            or not isinstance(content_items[0], Mapping)
            or content_items[0].get("type") != "inputText"
            or not isinstance(content_items[0].get("text"), str)
            or hashlib.sha256(str(content_items[0]["text"]).encode("utf-8")).hexdigest()
            != receipt["result_sha256"]
        ):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_RESULT_DRIFT")

    def handle_dynamic_tool_server_request(
        self,
        message: Mapping[str, Any],
        *,
        host: CanonicalDynamicToolHost,
    ) -> Mapping[str, Any]:
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_REQUEST_INVALID")
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        call_id = params.get("callId")
        tool_name = params.get("tool")
        namespace = params.get("namespace")
        arguments = params.get("arguments")
        if (
            thread_id != self.expected_thread_id
            or turn_id != self.expected_turn_id
            or not isinstance(call_id, str)
            or call_id not in self.started_item_ids
            or call_id in self.completed_item_ids
            or call_id in self.dynamic_tool_request_ids
            or self.dynamic_tool_item_names.get(call_id) != tool_name
            or namespace is not None
            or not isinstance(arguments, Mapping)
            or self.dynamic_tool_item_arguments_sha256.get(call_id)
            != canonical_sha256(dict(arguments))
        ):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_REQUEST_INVALID")
        specs = {spec.name: spec for spec in self.allowed_dynamic_tool_specs}
        spec = specs.get(str(tool_name))
        if spec is None:
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_NOT_ALLOWED")
        try:
            jsonschema.validate(dict(arguments), dict(spec.input_schema))
        except jsonschema.ValidationError as exc:
            raise LifecycleProbeError(
                "CANONICAL_DYNAMIC_TOOL_ARGUMENTS_INVALID"
            ) from exc
        execution = host.invoke(
            tool_name=spec.name,
            arguments=dict(arguments),
        )
        result_text = json.dumps(
            dict(execution.result),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(result_text.encode("utf-8")) > 950_000:
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_RESULT_TOO_LARGE")
        result_sha256 = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
        self.dynamic_tool_request_ids.add(call_id)
        self.dynamic_tool_receipts[call_id] = {
            "tool_name": spec.name,
            "arguments_sha256": canonical_sha256(dict(arguments)),
            "result_sha256": result_sha256,
            "success": execution.success,
        }
        return {
            "success": execution.success,
            "contentItems": [{"type": "inputText", "text": result_text}],
        }

    def content_free_summary(self) -> dict[str, Any]:
        unique_item_counts = Counter(self.item_types_by_id.values())
        return {
            "schema_version": CANONICAL_STAGE_EVENT_LEDGER_SCHEMA_VERSION,
            "event_counts": dict(sorted(self.method_counts.items())),
            "item_event_counts": dict(sorted(self.item_type_counts.items())),
            "unique_item_counts": dict(sorted(unique_item_counts.items())),
            "delta_event_counts": dict(sorted(self.delta_method_counts.items())),
            "completed_item_status_counts": dict(
                sorted(self.completed_item_status_counts.items())
            ),
            "snapshot_reconciled_item_type_counts": dict(
                sorted(self.snapshot_reconciled_item_type_counts.items())
            ),
            "terminal_orphaned_item_type_counts": dict(
                sorted(
                    Counter(
                        self.item_types_by_id.get(item_id, "unknown")
                        for item_id in self.terminal_orphaned_item_ids
                    ).items()
                )
            ),
            "dynamic_tools": {
                "enabled": bool(self.allowed_dynamic_tool_specs),
                "specs_sha256": canonical_dynamic_tool_specs_sha256(
                    self.allowed_dynamic_tool_specs
                ),
                "call_count": len(self.dynamic_tool_receipts),
                "tool_call_counts": dict(
                    sorted(
                        Counter(
                            item["tool_name"]
                            for item in self.dynamic_tool_receipts.values()
                        ).items()
                    )
                ),
                "success_count": sum(
                    1
                    for item in self.dynamic_tool_receipts.values()
                    if item["success"] is True
                ),
                "receipt_root_sha256": canonical_sha256(
                    [
                        {"call_id": call_id, **receipt}
                        for call_id, receipt in sorted(
                            self.dynamic_tool_receipts.items()
                        )
                    ]
                ),
            },
            "visual_inspection": {
                "contact_sheet_opened": self.contact_sheet_opened,
                "high_resolution_page_numbers": sorted(
                    self.high_resolution_page_numbers
                ),
                "high_resolution_page_count": len(
                    self.high_resolution_page_numbers
                ),
                "other_image_view_count": self.other_image_view_count,
                "image_view_count": len(self.opened_image_receipts),
                "receipt_root_sha256": canonical_sha256(
                    sorted(self.opened_image_receipts)
                ),
            },
        }

    def reconcile_completed_turn_snapshot(
        self,
        result: Mapping[str, Any],
        *,
        thread_id: str,
        turn_id: str,
    ) -> None:
        open_item_ids = self.started_item_ids - self.completed_item_ids
        if not open_item_ids:
            return
        thread = result.get("thread")
        if not isinstance(thread, Mapping) or thread.get("id") != thread_id:
            raise LifecycleProbeError("CANONICAL_STAGE_THREAD_SNAPSHOT_INVALID")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise LifecycleProbeError("CANONICAL_STAGE_THREAD_SNAPSHOT_INVALID")
        matching_turns = [
            item
            for item in turns
            if isinstance(item, Mapping) and item.get("id") == turn_id
        ]
        if len(matching_turns) != 1:
            raise LifecycleProbeError("CANONICAL_STAGE_TURN_SNAPSHOT_INVALID")
        turn = matching_turns[0]
        if turn.get("status") != "completed" or turn.get("itemsView", "full") != "full":
            raise LifecycleProbeError("CANONICAL_STAGE_TURN_SNAPSHOT_NOT_COMPLETE")
        items = turn.get("items")
        if not isinstance(items, list):
            raise LifecycleProbeError("CANONICAL_STAGE_TURN_SNAPSHOT_INVALID")
        self.completed_turn_snapshot_verified = True
        items_by_id: dict[str, Mapping[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping) or not _nonempty(item.get("id")):
                raise LifecycleProbeError("CANONICAL_STAGE_TURN_SNAPSHOT_INVALID")
            item_id = str(item["id"])
            if item_id in items_by_id:
                raise LifecycleProbeError("CANONICAL_STAGE_TURN_SNAPSHOT_INVALID")
            items_by_id[item_id] = item

        for item_id in tuple(sorted(open_item_ids)):
            item = items_by_id.get(item_id)
            if item is None:
                continue
            item_type = item.get("type")
            if item_type != self.item_types_by_id.get(item_id):
                raise LifecycleProbeError("CANONICAL_STAGE_SNAPSHOT_ITEM_TYPE_DRIFT")
            if item_type == "commandExecution" and item.get("status") not in {
                "completed",
                "failed",
            }:
                continue
            if item_type not in _ALLOWED_STAGE_ITEM_TYPES:
                suffix = _item_type_code(str(item_type))
                raise LifecycleProbeError(
                    f"CANONICAL_STAGE_ITEM_TYPE_FORBIDDEN_{suffix or 'UNKNOWN'}"
                )
            self.completed_item_ids.add(item_id)
            self.snapshot_reconciled_item_type_counts[str(item_type)] += 1
            self.completed_item_status_counts[
                f"{item_type}:thread_snapshot_completed"
            ] += 1

    def quarantine_terminal_reasoning_protocol_anomaly(self) -> None:
        open_item_ids = self.started_item_ids - self.completed_item_ids
        if not open_item_ids:
            return
        if (
            not self.completed_turn_snapshot_verified
            or self.expected_turn_id is None
            or self.terminal_turns.get(self.expected_turn_id) != "completed"
            or self.final_agent_message is None
            or any(
                self.item_types_by_id.get(item_id) != "reasoning"
                for item_id in open_item_ids
            )
        ):
            return
        self.terminal_orphaned_item_ids.update(open_item_ids)

    def validate_normal_sequence(
        self,
        *,
        thread_id: str,
        turn_id: str,
        require_thread_started: bool = True,
        require_thread_deleted: bool = True,
    ) -> None:
        accepted_item_ids = self.completed_item_ids | self.terminal_orphaned_item_ids
        if self.started_item_ids != accepted_item_ids:
            incomplete_types = tuple(
                sorted(
                    {
                        self.item_types_by_id.get(item_id, "unknown")
                        for item_id in self.started_item_ids - accepted_item_ids
                    }
                )
            )
            suffix = "_".join(_item_type_code(item) for item in incomplete_types)
            raise LifecycleProbeError(
                f"CANONICAL_STAGE_ITEM_LIFECYCLE_INCOMPLETE_{suffix or 'UNKNOWN'}"
            )
        super().validate_normal_sequence(
            thread_id=thread_id,
            turn_id=turn_id,
            require_thread_started=require_thread_started,
            require_thread_deleted=require_thread_deleted,
        )


class LocalCodexAppServerStructuredStageTransport:
    """Run one canonical stage through a local Codex App Server JSONL session."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_CANONICAL_STAGE_TIMEOUT_SECONDS,
        cancellation_requested: Callable[[], bool] | None = None,
        session_opener: SessionOpener | None = None,
        runtime_skill_isolator: RuntimeSkillIsolator | None = None,
        thread_absence_verifier: ThreadLifecycleAction | None = None,
        emergency_thread_delete: ThreadLifecycleAction | None = None,
        dynamic_tool_host: CanonicalDynamicToolHost | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("canonical stage timeout must be finite and positive")
        self.timeout_seconds = float(timeout_seconds)
        self.cancellation_requested = cancellation_requested or (lambda: False)
        self.dynamic_tool_host = dynamic_tool_host
        self._real_session = session_opener is None
        self._session_opener = session_opener or _open_real_session
        self._runtime_skill_isolator = (
            runtime_skill_isolator or _isolate_stage_runtime_skills
            if self._real_session
            else runtime_skill_isolator or _identity_stage_runtime_skills
        )
        self._thread_absence_verifier = (
            thread_absence_verifier or _verify_absent_with_fresh_session
            if self._real_session
            else thread_absence_verifier
        )
        self._emergency_thread_delete = (
            emergency_thread_delete or _emergency_delete_with_fresh_session
            if self._real_session
            else emergency_thread_delete
        )

    @property
    def cleanup_timeout_seconds(self) -> float:
        return min(max(self.timeout_seconds, 5.0), 30.0)

    def invoke(
        self,
        invocation: CanonicalStageInvocation,
    ) -> CanonicalStageTransportResult:
        if self.cancellation_requested():
            raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED_BEFORE_START")
        (
            attestation,
            skill_paths,
            readable_root,
            additional_readable_roots,
            native_runtime_policy,
        ) = _validate_invocation(invocation)
        dynamic_tool_specs = (
            self.dynamic_tool_host.specs if self.dynamic_tool_host is not None else ()
        )
        dynamic_tool_names = tuple(sorted(item.name for item in dynamic_tool_specs))
        dynamic_tool_specs_sha256 = (
            canonical_dynamic_tool_specs_sha256(dynamic_tool_specs)
            if dynamic_tool_specs
            else None
        )
        if (
            invocation.dynamic_tool_names != dynamic_tool_names
            or invocation.dynamic_tool_specs_sha256 != dynamic_tool_specs_sha256
            or (
                bool(dynamic_tool_specs)
                and (
                    self.dynamic_tool_host is None
                    or invocation.stage not in self.dynamic_tool_host.supported_stages
                    or native_runtime_policy.confinement_mode != "PERMISSION_PROFILE"
                    or invocation.runtime.protocol.get("dynamic_tools") != "true"
                    or len({item.name for item in dynamic_tool_specs})
                    != len(dynamic_tool_specs)
                )
            )
        ):
            raise LifecycleProbeError("CANONICAL_DYNAMIC_TOOL_POLICY_INVALID")
        runtime, ambient_skill_disabled_count = self._runtime_skill_isolator(
            invocation.runtime,
            skill_paths,
            min(self.timeout_seconds, 30.0),
        )
        if (
            runtime.sterile_cwd.resolve(strict=True)
            != invocation.runtime.sterile_cwd.resolve(strict=True)
            or canonical_sha256(_workspace_manifest(readable_root))
            != attestation.workspace_manifest_sha256
        ):
            raise LifecycleProbeError("CANONICAL_STAGE_SKILL_ISOLATION_DRIFT")
        workspace_before = _workspace_manifest(readable_root)
        ledger = CanonicalStageEventLedger(
            readable_root=readable_root,
            allowed_dynamic_tool_specs=dynamic_tool_specs,
        )
        session = self._session_opener(runtime, ledger, self.timeout_seconds)
        thread_id: str | None = None
        turn_id: str | None = None
        thread_deleted = False
        try:
            if self.dynamic_tool_host is not None:
                configure = getattr(
                    session,
                    "configure_dynamic_server_requests",
                    None,
                )
                if not callable(configure):
                    raise LifecycleProbeError(
                        "CANONICAL_DYNAMIC_TOOL_SESSION_UNSUPPORTED"
                    )
                configure(
                    lambda message: ledger.handle_dynamic_tool_server_request(
                        message,
                        host=self.dynamic_tool_host,  # type: ignore[arg-type]
                    )
                )
            _preflight_agent_native_stage(
                session,
                runtime=runtime,
                model=attestation.model_id,
                skill_paths=skill_paths,
                readable_root=readable_root,
                additional_readable_roots=additional_readable_roots,
                native_runtime_policy=native_runtime_policy,
            )
            if native_runtime_policy.confinement_mode == "PERMISSION_PROFILE":
                thread_id = _start_permission_profile_thread(
                    session,
                    ledger,
                    model=attestation.model_id,
                    sterile_cwd=runtime.sterile_cwd,
                    developer_instructions=invocation.developer_context,
                    permission_profile_id=(
                        native_runtime_policy.permission_profile_id or ""
                    ),
                    dynamic_tools=tuple(
                        item.app_server_payload() for item in dynamic_tool_specs
                    ),
                )
            else:
                thread_id = _start_or_resume_thread(
                    session,  # type: ignore[arg-type]
                    ledger,
                    model=attestation.model_id,
                    sterile_cwd=runtime.sterile_cwd,
                    developer_instructions=invocation.developer_context,
                )
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [dict(item) for item in invocation.input_items],
                "cwd": str(runtime.sterile_cwd),
                "approvalPolicy": "never",
                "model": attestation.model_id,
                "effort": attestation.reasoning_effort,
                "summary": "none",
                "outputSchema": dict(invocation.turn_output_schema),
            }
            if native_runtime_policy.confinement_mode == "READ_ONLY_RESTRICTED_ROOT":
                turn_params["sandboxPolicy"] = dict(invocation.turn_sandbox_policy)
            turn = session.request(
                "turn/start",
                turn_params,
            )
            turn_id = _validate_turn_start_result(turn)
            ledger.bind_turn(turn_id)
            terminal_status = session.wait_for_turn(
                turn_id,
                cancellation_requested=self.cancellation_requested,
            )
            if terminal_status == "interrupted" and self.cancellation_requested():
                raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED")
            if terminal_status != "completed":
                raise LifecycleProbeError("CANONICAL_STAGE_TURN_NOT_COMPLETED")
            structured_output = _parse_and_validate_structured_output(
                ledger.final_agent_message,
                schema=invocation.turn_output_schema,
            )
            thread_snapshot = session.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            ledger.reconcile_completed_turn_snapshot(
                thread_snapshot,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if ledger.started_item_ids != ledger.completed_item_ids:
                try:
                    session.drain_notifications_until(
                        lambda: (ledger.started_item_ids == ledger.completed_item_ids),
                        timeout_seconds=min(self.cleanup_timeout_seconds, 5.0),
                    )
                except LifecycleProbeError as exc:
                    if exc.reason_code != "APP_SERVER_TIMEOUT":
                        raise
            ledger.quarantine_terminal_reasoning_protocol_anomaly()
            ledger.validate_normal_sequence(
                thread_id=thread_id,
                turn_id=turn_id,
                require_thread_deleted=False,
            )
            session.request("thread/delete", {"threadId": thread_id})
            session.wait_for_thread_deleted(thread_id)
            thread_deleted = True
            ledger.validate_normal_sequence(thread_id=thread_id, turn_id=turn_id)
            session.stop_gracefully()
            _require_workspace_unchanged(readable_root, workspace_before)
            if self._thread_absence_verifier is not None:
                self._thread_absence_verifier(
                    runtime,
                    thread_id,
                    self.cleanup_timeout_seconds,
                )
            stage_binding = next(
                item
                for item in attestation.stages
                if item.stage is invocation.stage
            )
            summary = {
                **ledger.content_free_summary(),
                "skill_inputs": {
                    "primary_skill_id": stage_binding.skill_id,
                    "capability_skill_ids": [
                        item.skill_id for item in stage_binding.capability_skills
                    ],
                    "skill_count": len(skill_paths),
                    "closure_sha256": canonical_sha256(
                        {
                            name: _sha256_file(path)
                            for name, path in skill_paths.items()
                        }
                    ),
                },
                "pdf_primary_runtime": (
                    {
                        "attestation_id": attestation.pdf_primary_runtime.attestation_id,
                        "distribution_id": attestation.pdf_primary_runtime.distribution_id,
                        "distribution_version": (
                            attestation.pdf_primary_runtime.distribution_version
                        ),
                        "runtime_bundle_version": (
                            attestation.pdf_primary_runtime.runtime_bundle_version
                        ),
                        "python_version": (
                            attestation.pdf_primary_runtime.python_version
                        ),
                        "python_packages": {
                            item.name: item.version
                            for item in (
                                attestation.pdf_primary_runtime.python_packages
                            )
                        },
                        "render_tools": {
                            item.name: item.version
                            for item in attestation.pdf_primary_runtime.render_tools
                        },
                        "read_only": True,
                    }
                    if attestation.pdf_primary_runtime is not None
                    else None
                ),
            }
            return CanonicalStageTransportResult(
                stage=invocation.stage,
                structured_output=structured_output,
                output_schema_sha256=invocation.turn_output_schema_sha256,
                response_sha256=canonical_sha256(structured_output),
                tool_receipt_sha256=canonical_sha256(
                    {
                        "stage": invocation.stage.value,
                        "ambient_skill_disabled_count": ambient_skill_disabled_count,
                        "events": summary,
                    }
                ),
                runtime_thread_id=thread_id,
                runtime_turn_id=turn_id,
                tool_activity_summary=summary,
            )
        except BaseException as exc:
            if thread_id is None and ledger.expected_thread_id is not None:
                # A profile mismatch is detected after App Server has created and
                # announced the thread. Preserve the existing cleanup contract.
                thread_id = ledger.expected_thread_id
            cleanup_failure = self._cleanup_failed_invocation(
                session,
                runtime=runtime,
                ledger=ledger,
                thread_id=thread_id,
                turn_id=turn_id,
                thread_deleted=thread_deleted,
            )
            workspace_failure: BaseException | None = None
            try:
                _require_workspace_unchanged(readable_root, workspace_before)
            except BaseException as workspace_exc:
                workspace_failure = workspace_exc
            if cleanup_failure is not None:
                raise LifecycleProbeError("CANONICAL_STAGE_CLEANUP_FAILED") from (
                    cleanup_failure
                )
            if workspace_failure is not None:
                raise workspace_failure
            raise exc
        finally:
            session.close()

    def _cleanup_failed_invocation(
        self,
        session: CanonicalStageJsonlSession,
        *,
        runtime: CanaryRuntimeState,
        ledger: CanonicalStageEventLedger,
        thread_id: str | None,
        turn_id: str | None,
        thread_deleted: bool,
    ) -> BaseException | None:
        if thread_id is None or thread_deleted:
            session.close(force=True)
            return None
        try:
            if turn_id is not None and turn_id not in ledger.terminal_turns:
                session.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                )
                terminal = session.wait_for_turn(turn_id)
                if terminal not in {"interrupted", "failed", "completed"}:
                    raise LifecycleProbeError("CANONICAL_STAGE_INTERRUPT_FAILED")
            session.request("thread/delete", {"threadId": thread_id})
            session.wait_for_thread_deleted(thread_id)
            session.close(force=True)
            if self._thread_absence_verifier is not None:
                self._thread_absence_verifier(
                    runtime,
                    thread_id,
                    self.cleanup_timeout_seconds,
                )
            return None
        except BaseException as cleanup_exc:
            session.close(force=True)
            if self._emergency_thread_delete is None:
                return cleanup_exc
            try:
                self._emergency_thread_delete(
                    runtime,
                    thread_id,
                    min(self.cleanup_timeout_seconds, 20.0),
                )
                return None
            except BaseException as emergency_exc:
                return emergency_exc


def _preflight_agent_native_stage(
    session: CanonicalStageJsonlSession,
    *,
    runtime: CanaryRuntimeState,
    model: str,
    skill_paths: Mapping[str, Path],
    readable_root: Path,
    additional_readable_roots: tuple[Path, ...],
    native_runtime_policy: AgentNativeReadOnlyRuntimePolicyV1,
    expected_shell_environment: Mapping[str, str] | None = None,
) -> None:
    _validate_initialize_result(session.initialize(), codex_home=runtime.codex_home)
    account = safe_account_summary(
        session.request("account/read", {"refreshToken": False})
    )
    if account.get("account_type") != "chatgpt":
        raise LifecycleProbeError("AUTH_REQUIRED")
    model_result = session.request(
        "model/list",
        {"limit": 100, "includeHidden": False},
    )
    if model_result.get("nextCursor") is not None:
        raise LifecycleProbeError("MODEL_LIST_INCOMPLETE")
    model_summary = safe_model_summary(model_result, preferred_model=model)
    if not model_summary.get("preferred_model_available"):
        raise LifecycleProbeError("MODEL_UNAVAILABLE")
    validate_agent_native_effective_config(
        session.request("config/read", {"includeLayers": False}),
        confinement_mode=native_runtime_policy.confinement_mode,
        permission_profile_id=native_runtime_policy.permission_profile_id,
        expected_readable_root=readable_root,
        expected_additional_readable_roots=additional_readable_roots,
        expected_shell_environment=expected_shell_environment,
    )
    mcp_status = session.request(
        "mcpServerStatus/list",
        {"limit": 100, "detail": "toolsAndAuthOnly"},
    )
    if mcp_status.get("data") != [] or mcp_status.get("nextCursor") is not None:
        raise LifecycleProbeError("CANONICAL_STAGE_MCP_CLOSURE_MISMATCH")
    _verify_skill_allowlist(
        session,  # type: ignore[arg-type]
        runtime=runtime,
        expected_paths=skill_paths,
    )


def _validate_invocation(
    invocation: CanonicalStageInvocation,
) -> tuple[
    CanonicalAppServerDocumentSessionAttestationV1,
    dict[str, Path],
    Path,
    tuple[Path, ...],
    AgentNativeReadOnlyRuntimePolicyV1,
]:
    if not isinstance(invocation, CanonicalStageInvocation):
        raise LifecycleProbeError("CANONICAL_STAGE_INVOCATION_INVALID")
    confinement_mode = agent_native_read_confinement_mode(invocation.runtime)
    if confinement_mode is None:
        raise LifecycleProbeError("CANONICAL_NATIVE_READ_RESTRICTED_ROOT_UNSUPPORTED")
    try:
        attestation = CanonicalAppServerDocumentSessionAttestationV1.model_validate(
            invocation.session_attestation.model_dump(mode="json")
        )
        stage_binding = next(
            item for item in attestation.stages if item.stage is invocation.stage
        )
    except Exception as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_ATTESTATION_INVALID") from exc
    if (
        not stage_binding.inference_available
        or not attestation.agent_native_read_capability.real_transport_eligible
        or attestation.native_runtime_policy is None
        or attestation.native_runtime_policy.network_access is not False
        or not attestation.native_runtime_policy.shell_tool_enabled
        or not attestation.native_runtime_policy.unified_exec_enabled
        or not attestation.native_runtime_policy.view_image_enabled
        or not attestation.native_runtime_policy.local_image_input_enabled
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_NATIVE_RUNTIME_UNATTESTED")
    native_runtime_policy = attestation.native_runtime_policy
    if native_runtime_policy is None:
        raise LifecycleProbeError("CANONICAL_STAGE_NATIVE_RUNTIME_UNATTESTED")
    if (
        native_runtime_policy.confinement_mode != confinement_mode
        or (
            confinement_mode == "PERMISSION_PROFILE"
            and native_runtime_policy.permission_profile_id
            != AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
        )
        or (
            confinement_mode == "READ_ONLY_RESTRICTED_ROOT"
            and native_runtime_policy.permission_profile_id is not None
        )
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_NATIVE_RUNTIME_UNATTESTED")
    try:
        cwd = invocation.runtime.sterile_cwd.resolve(strict=True)
        state_path = invocation.runtime.state_path.resolve(strict=True)
        cwd.relative_to(state_path)
    except OSError as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_READ_ROOT_INVALID") from exc
    except ValueError as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_READ_ROOT_INVALID") from exc
    if (
        invocation.runtime.sterile_cwd.is_symlink()
        or invocation.runtime.state_path.is_symlink()
        or not cwd.is_dir()
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_READ_ROOT_INVALID")
    policy = invocation.turn_sandbox_policy
    access = policy.get("access")
    readable_roots = (
        access.get("readableRoots") if isinstance(access, Mapping) else None
    )
    try:
        if (
            not isinstance(readable_roots, list)
            or not readable_roots
            or not isinstance(readable_roots[0], str)
        ):
            raise ValueError
        readable_root = Path(readable_roots[0]).resolve(strict=True)
        readable_root.relative_to(state_path)
        cwd.relative_to(readable_root)
        additional_readable_roots = tuple(
            Path(raw).resolve(strict=True) for raw in readable_roots[1:]
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_SANDBOX_POLICY_INVALID") from exc
    if Path(str(readable_roots[0])).is_symlink() or not readable_root.is_dir():
        raise LifecycleProbeError("CANONICAL_STAGE_SANDBOX_POLICY_INVALID")
    if any(
        not isinstance(raw, str)
        or Path(raw).is_symlink()
        or not resolved.is_dir()
        for raw, resolved in zip(
            readable_roots[1:], additional_readable_roots, strict=True
        )
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_SANDBOX_POLICY_INVALID")
    expected_root_sha256 = hashlib.sha256(
        str(readable_root).encode("utf-8")
    ).hexdigest()
    additional_root_hashes = tuple(
        sorted(
            hashlib.sha256(str(root).encode("utf-8")).hexdigest()
            for root in additional_readable_roots
        )
    )
    pdf_runtime = attestation.pdf_primary_runtime
    if pdf_runtime is not None:
        if len(additional_readable_roots) != 1:
            raise LifecycleProbeError("CANONICAL_STAGE_SANDBOX_POLICY_INVALID")
        validate_openai_primary_runtime_pdf_environment(
            invocation.runtime.environment,
            dependencies_root=additional_readable_roots[0],
            attestation=pdf_runtime,
        )
    if (
        set(policy) != {"type", "access"}
        or policy.get("type") != "readOnly"
        or not isinstance(access, Mapping)
        or set(access) != {"type", "includePlatformDefaults", "readableRoots"}
        or access.get("type") != "restricted"
        or access.get("includePlatformDefaults") is not True
        or readable_roots
        != [str(item) for item in (readable_root, *additional_readable_roots)]
        or native_runtime_policy.readable_root_path_sha256 != expected_root_sha256
        or native_runtime_policy.additional_readable_root_path_sha256s
        != additional_root_hashes
        or (
            pdf_runtime is not None
            and additional_root_hashes
            != (pdf_runtime.dependency_root_path_sha256,)
        )
        or invocation.stage_context_sha256 != stage_binding.context_sha256
        or invocation.final_artifact_schema_id != stage_binding.artifact_schema_id
        or stage_binding.artifact_schema is None
        or invocation.final_artifact_schema_sha256
        != stage_binding.artifact_schema.sha256
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_SANDBOX_POLICY_INVALID")
    if invocation.turn_output_schema_sha256 != canonical_sha256(
        dict(invocation.turn_output_schema)
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_OUTPUT_SCHEMA_DRIFT")
    if (
        invocation.runner_output_schema_variants_sha256
        != stage_binding.runner_output_schema_variants_sha256
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH")
    if invocation.runner_output_execution_mode is None:
        runner_schema_id = stage_binding.runner_output_schema_id
        runner_schema = stage_binding.runner_output_schema
    else:
        selected_variant = next(
            (
                item
                for item in stage_binding.runner_output_schema_variants
                if item.execution_mode == invocation.runner_output_execution_mode
            ),
            None,
        )
        runner_schema_id = (
            selected_variant.schema_id if selected_variant is not None else None
        )
        runner_schema = (
            selected_variant.schema_file if selected_variant is not None else None
        )
    if runner_schema is None or runner_schema_id != invocation.turn_output_schema_id:
        raise LifecycleProbeError("CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH")
    try:
        runner_schema_path = (cwd / runner_schema.path).resolve(strict=True)
        runner_schema_payload = json.loads(
            runner_schema_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_RUNNER_SCHEMA_INVALID") from exc
    if (
        not isinstance(runner_schema_payload, Mapping)
        or _sha256_file(runner_schema_path) != runner_schema.sha256
        or dict(runner_schema_payload) != dict(invocation.turn_output_schema)
        or canonical_sha256(dict(runner_schema_payload))
        != invocation.turn_output_schema_sha256
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH")
    if (
        canonical_sha256(_workspace_manifest(readable_root))
        != attestation.workspace_manifest_sha256
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_WORKSPACE_ATTESTATION_DRIFT")
    text_items = tuple(
        item for item in invocation.input_items if item.get("type") == "text"
    )
    skill_items = tuple(
        item for item in invocation.input_items if item.get("type") == "skill"
    )
    image_items = tuple(
        item for item in invocation.input_items if item.get("type") == "localImage"
    )
    if (
        len(text_items) != 1
        or not skill_items
        or len(invocation.input_items) != 1 + len(skill_items) + len(image_items)
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_SKILL_INPUT_INVALID")
    expected_skills = {
        stage_binding.skill_id: stage_binding.skill,
        **{
            item.skill_id: item.skill_file
            for item in stage_binding.capability_skills
        },
    }
    if tuple(item.get("name") for item in skill_items) != tuple(expected_skills):
        raise LifecycleProbeError("CANONICAL_STAGE_SKILL_INPUT_INVALID")
    resolved_skill_paths: dict[str, Path] = {}
    for skill_item in skill_items:
        skill_name = skill_item.get("name")
        skill_path_value = skill_item.get("path")
        if (
            not isinstance(skill_name, str)
            or not isinstance(skill_path_value, str)
            or set(skill_item) != {"type", "name", "path"}
        ):
            raise LifecycleProbeError("CANONICAL_STAGE_SKILL_INPUT_INVALID")
        resolved_skill_paths[skill_name] = _require_path_within_root(
            Path(skill_path_value),
            root=readable_root,
            require_file=True,
            reason_code="CANONICAL_STAGE_SKILL_PATH_INVALID",
        )
    try:
        expected_skill_paths = {
            name: (cwd / digest.path).resolve(strict=True)
            for name, digest in expected_skills.items()
        }
    except OSError as exc:
        raise LifecycleProbeError("CANONICAL_STAGE_SKILL_PATH_INVALID") from exc
    if any(
        resolved_skill_paths[name] != expected_skill_paths[name]
        or _sha256_file(resolved_skill_paths[name]) != expected_skills[name].sha256
        for name in expected_skills
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_SKILL_DIGEST_MISMATCH")
    prompt = text_items[0].get("text")
    if (
        not isinstance(prompt, str)
        or _CANONICAL_STAGE_PROMPT_MARKER not in prompt
        or any(f"${name}" not in prompt for name in expected_skills)
        or invocation.developer_context in prompt
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_TEXT_INPUT_INVALID")
    for item in invocation.input_items:
        item_type = item.get("type")
        if item_type == "text" and set(item) != {"type", "text"}:
            raise LifecycleProbeError("CANONICAL_STAGE_TEXT_INPUT_INVALID")
        if item_type == "localImage":
            if set(item) != {"type", "path"}:
                raise LifecycleProbeError("CANONICAL_STAGE_LOCAL_IMAGE_INVALID")
            _require_path_within_root(
                Path(str(item.get("path") or "")),
                root=readable_root,
                require_file=True,
                reason_code="CANONICAL_STAGE_LOCAL_IMAGE_INVALID",
            )
    return (
        attestation,
        resolved_skill_paths,
        readable_root,
        additional_readable_roots,
        native_runtime_policy,
    )


def _start_permission_profile_thread(
    session: CanonicalStageJsonlSession,
    ledger: CanonicalStageEventLedger,
    *,
    model: str,
    sterile_cwd: Path,
    developer_instructions: str,
    permission_profile_id: str,
    dynamic_tools: tuple[Mapping[str, Any], ...] = (),
) -> str:
    """Start a profile-governed thread without any legacy sandbox parameter."""

    if permission_profile_id != AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID:
        raise LifecycleProbeError("CANONICAL_STAGE_PERMISSION_PROFILE_INVALID")
    params: dict[str, Any] = {
        "model": model,
        "cwd": str(sterile_cwd),
        "approvalPolicy": "never",
        "ephemeral": False,
        "developerInstructions": developer_instructions,
        "serviceName": "reqpilotx_canonical_stage",
    }
    if dynamic_tools:
        params["dynamicTools"] = [dict(item) for item in dynamic_tools]
    result = session.request("thread/start", params)
    thread = result.get("thread")
    if not isinstance(thread, Mapping) or not _nonempty(thread.get("id")):
        raise LifecycleProbeError("CANONICAL_STAGE_THREAD_START_RESULT_INVALID")
    thread_id = str(thread["id"])
    ledger.bind_thread(thread_id)
    active_profile = result.get("activePermissionProfile")
    if (
        result.get("model") != model
        or result.get("cwd") != str(sterile_cwd)
        or result.get("approvalPolicy") != "never"
        or not isinstance(active_profile, Mapping)
        or set(active_profile) not in ({"id"}, {"id", "extends"})
        or active_profile.get("id") != permission_profile_id
        or active_profile.get("extends") is not None
        or thread.get("cwd") != str(sterile_cwd)
        or thread.get("ephemeral") is not False
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_PERMISSION_PROFILE_MISMATCH")
    _validate_instruction_sources(result, sterile_cwd=sterile_cwd)
    return thread_id


def _parse_and_validate_structured_output(
    value: str | None,
    *,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(_parse_agent_json(value))
    try:
        jsonschema.Draft202012Validator.check_schema(dict(schema))
        jsonschema.Draft202012Validator(
            dict(schema),
            format_checker=jsonschema.FormatChecker(),
        ).validate(payload)
    except (jsonschema.SchemaError, jsonschema.ValidationError):
        # A jsonschema ValidationError retains the failing instance.  Do not chain
        # it into content-free transport diagnostics.
        raise LifecycleProbeError("CANONICAL_STAGE_STRUCTURED_OUTPUT_INVALID") from None
    return payload


def _require_path_within_root(
    path: Path,
    *,
    root: Path,
    require_file: bool,
    reason_code: str,
) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise LifecycleProbeError(reason_code)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LifecycleProbeError(reason_code) from exc
    if require_file and not resolved.is_file():
        raise LifecycleProbeError(reason_code)
    if not require_file and not resolved.is_dir():
        raise LifecycleProbeError(reason_code)
    return resolved


def _require_workspace_unchanged(
    root: Path,
    before: Mapping[str, str],
) -> None:
    if _workspace_manifest(root) != dict(before):
        raise LifecycleProbeError("MODEL_WORKSPACE_MUTATED")


def _open_real_session(
    runtime: CanaryRuntimeState,
    ledger: CanonicalStageEventLedger,
    timeout_seconds: float,
) -> JsonlRpcSession:
    return JsonlRpcSession(
        command=runtime.command,
        environment=runtime.environment,
        cwd=runtime.sterile_cwd,
        ledger=ledger,
        timeout_seconds=timeout_seconds,
        experimental_api=runtime.protocol.get("permission_profiles") == "true",
    )


def _isolate_stage_runtime_skills(
    runtime: CanaryRuntimeState,
    skill_paths: Mapping[str, Path],
    timeout_seconds: float,
) -> tuple[CanaryRuntimeState, int]:
    return _isolate_runtime_skills(
        runtime,
        skill_paths=skill_paths,
        timeout_seconds=timeout_seconds,
    )


def _identity_stage_runtime_skills(
    runtime: CanaryRuntimeState,
    skill_paths: Mapping[str, Path],
    timeout_seconds: float,
) -> tuple[CanaryRuntimeState, int]:
    del skill_paths, timeout_seconds
    return runtime, 0


def _verify_absent_with_fresh_session(
    runtime: CanaryRuntimeState,
    thread_id: str,
    timeout_seconds: float,
) -> None:
    _verify_thread_absent_after_restart(
        command=runtime.command,
        environment=runtime.environment,
        cwd=runtime.sterile_cwd,
        timeout_seconds=timeout_seconds,
        thread_id=thread_id,
        codex_home=runtime.codex_home,
    )


def _emergency_delete_with_fresh_session(
    runtime: CanaryRuntimeState,
    thread_id: str,
    timeout_seconds: float,
) -> None:
    _delete_thread_with_fresh_session(
        runtime,
        thread_id=thread_id,
        timeout_seconds=timeout_seconds,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _item_type_code(value: str) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Z0-9]+", "_", separated.upper()).strip("_")


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "CANONICAL_STAGE_EVENT_LEDGER_SCHEMA_VERSION",
    "DEFAULT_CANONICAL_STAGE_TIMEOUT_SECONDS",
    "CanonicalStageEventLedger",
    "CanonicalStageJsonlSession",
    "LocalCodexAppServerStructuredStageTransport",
]
