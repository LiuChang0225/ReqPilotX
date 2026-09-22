from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any, Mapping

from reqpilotx_server.adapters.agents.codex_app_server.native_atomization_reference import (
    _normalize_reference_codes,
)
from reqpilotx_server.application.extraction.native_atomization_reference import (
    NativeAtomizationReferenceV1,
    native_atomization_reference_output_schema,
    validate_native_atomization_reference,
)
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
)


MCP_SERVER_NAME = "reqpilotx_atomization"
MCP_SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
DESCRIPTOR_SCHEMA = "codex-atomization-mcp-session/v1"
TOOL_GET_TASK = "atomization.get_task"
TOOL_VALIDATE = "atomization.validate_reference"
TOOL_NAMES = (TOOL_GET_TASK, TOOL_VALIDATE)
MAX_LINE_BYTES = 1_048_576
MAX_DESCRIPTOR_BYTES = 16 * 1024 * 1024


class AtomizationMcpError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class AtomizationDescriptor:
    run_handle: str
    request: RequirementAtomizationProviderRequest


@dataclass
class AtomizationToolSession:
    descriptor: AtomizationDescriptor
    call_counts: Counter[str] = field(default_factory=Counter)
    task_read: bool = False

    def call(self, tool: str, arguments: object) -> dict[str, Any]:
        if tool not in TOOL_NAMES or not isinstance(arguments, Mapping):
            raise AtomizationMcpError("TOOL_NOT_ALLOWED")
        self.call_counts[tool] += 1
        if tool == TOOL_GET_TASK:
            return self._get_task(arguments)
        return self._validate(arguments)

    def _get_task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _exact(arguments, {"run_handle"})
        _handle(arguments, self.descriptor)
        if self.call_counts[TOOL_GET_TASK] > 2:
            raise AtomizationMcpError("TOOL_CALL_BUDGET_EXCEEDED")
        self.task_read = True
        return {
            "schema_version": "atomization-task/v1",
            "request": self.descriptor.request.model_dump(mode="json"),
            "output_schema": native_atomization_reference_output_schema(),
            "source_content_is_untrusted_data": True,
            "parent_is_preserved_by_host": True,
            "required_next_tool": TOOL_VALIDATE,
        }

    def _validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _exact(arguments, {"run_handle", "candidate"})
        _handle(arguments, self.descriptor)
        if not self.task_read:
            raise AtomizationMcpError("TOOL_SEQUENCE_INVALID")
        if self.call_counts[TOOL_VALIDATE] > 2:
            raise AtomizationMcpError("TOOL_CALL_BUDGET_EXCEEDED")
        try:
            candidate = NativeAtomizationReferenceV1.model_validate(
                _normalize_reference_codes(arguments["candidate"])
            )
        except Exception as exc:
            raise AtomizationMcpError(
                "PROPOSAL_REJECTED_ATOMIZATION_SCHEMA_INVALID"
            ) from exc
        try:
            receipt = validate_native_atomization_reference(
                self.descriptor.request,
                candidate,
            )
        except Exception as exc:
            raise AtomizationMcpError(
                "PROPOSAL_REJECTED_ATOMIZATION_GROUNDING_INVALID"
            ) from exc
        payload = candidate.model_dump(mode="json")
        candidate_sha256 = _canonical_sha256(payload)
        return {
            "schema_version": "atomization-candidate-validation/v1",
            "candidate_sha256": candidate_sha256,
            "proposal_sha256": candidate_sha256,
            "validated_reference": payload,
            "validation_receipt": receipt.model_dump(mode="json"),
            "write_audit": {
                "schema_version": "reqpilotx-read-only-write-audit/v1",
                "counts": {
                    "business_state": 0,
                    "job_or_event": 0,
                    "runtime_cache": 0,
                },
                "total_writes": 0,
            },
        }


def write_atomization_descriptor(
    destination: Path,
    *,
    request: RequirementAtomizationProviderRequest,
) -> AtomizationDescriptor:
    if destination.exists() or destination.is_symlink():
        raise AtomizationMcpError("DESCRIPTOR_PATH_INVALID")
    _private_directory(destination.parent)
    descriptor = AtomizationDescriptor(
        run_handle="rpxa_" + secrets.token_urlsafe(24),
        request=request,
    )
    payload = {
        "schema_version": DESCRIPTOR_SCHEMA,
        "run_handle": descriptor.run_handle,
        "request": request.model_dump(mode="json"),
    }
    encoded = _canonical_json(payload).encode()
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise AtomizationMcpError("DESCRIPTOR_TOO_LARGE")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return descriptor


def load_atomization_descriptor(path: Path) -> AtomizationDescriptor:
    _private_file(path)
    _private_directory(path.parent)
    raw = path.read_bytes()
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise AtomizationMcpError("DESCRIPTOR_TOO_LARGE")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "run_handle",
            "request",
        }:
            raise ValueError("descriptor shape invalid")
        if payload["schema_version"] != DESCRIPTOR_SCHEMA:
            raise ValueError("descriptor schema invalid")
        run_handle = payload["run_handle"]
        if not isinstance(run_handle, str) or not run_handle.startswith("rpxa_"):
            raise ValueError("run handle invalid")
        return AtomizationDescriptor(
            run_handle=run_handle,
            request=RequirementAtomizationProviderRequest.model_validate(
                payload["request"]
            ),
        )
    except Exception as exc:
        raise AtomizationMcpError("DESCRIPTOR_INVALID") from exc


def tool_definitions() -> list[dict[str, Any]]:
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    return [
        _tool(
            TOOL_GET_TASK,
            "Resolve an atomization run handle to one complete parent requirement.",
            {"run_handle": {"type": "string"}},
            ["run_handle"],
            annotations,
        ),
        _tool(
            TOOL_VALIDATE,
            "Validate exact-cover child obligations and typed context without persisting.",
            {
                "run_handle": {"type": "string"},
                "candidate": {"type": "object"},
            },
            ["run_handle", "candidate"],
            annotations,
        ),
    ]


class StdioAtomizationMcpServer:
    def __init__(self, session: AtomizationToolSession) -> None:
        self.session = session
        self.initialized = False

    def serve(self) -> int:
        for raw in sys.stdin.buffer:
            if len(raw) > MAX_LINE_BYTES:
                return 2
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self._error(None, -32700, "PARSE_ERROR")
                continue
            self._dispatch(message)
        return 0

    def _dispatch(self, message: object) -> None:
        if not isinstance(message, Mapping):
            self._error(None, -32600, "INVALID_REQUEST")
            return
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if method == "notifications/initialized" and "id" not in message:
            self.initialized = True
            return
        if "id" not in message or not isinstance(params, Mapping):
            return
        if method == "initialize":
            self._result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": MCP_SERVER_NAME,
                        "version": MCP_SERVER_VERSION,
                    },
                    "instructions": (
                        "Read one complete parent, validate exact-cover obligations "
                        "and context, return the accepted receipt, and never write."
                    ),
                },
            )
        elif method == "tools/list" and self.initialized:
            self._result(request_id, {"tools": tool_definitions()})
        elif method == "tools/call" and self.initialized:
            try:
                result = self.session.call(
                    str(params.get("name")), params.get("arguments", {})
                )
            except AtomizationMcpError as exc:
                error = {"reason_code": exc.reason_code}
                self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": _canonical_json(error)}],
                        "structuredContent": error,
                        "isError": True,
                    },
                )
                return
            self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": _canonical_json(result)}],
                    "structuredContent": result,
                    "isError": False,
                },
            )
        else:
            self._error(request_id, -32601, "METHOD_NOT_FOUND")

    def _result(self, request_id: object, result: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: object, code: int, message: str) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    @staticmethod
    def _write(payload: Mapping[str, Any]) -> None:
        sys.stdout.write(_canonical_json(payload) + "\n")
        sys.stdout.flush()


def _tool(name, description, properties, required, annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


def _exact(value: Mapping[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise AtomizationMcpError("TOOL_ARGUMENTS_INVALID")


def _handle(arguments: Mapping[str, Any], descriptor: AtomizationDescriptor) -> None:
    if arguments.get("run_handle") != descriptor.run_handle:
        raise AtomizationMcpError("RUN_HANDLE_UNKNOWN")


def _private_directory(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != os.getuid()
        or stat.S_IMODE(path.stat().st_mode) & 0o077
    ):
        raise AtomizationMcpError("PRIVATE_DIRECTORY_INVALID")


def _private_file(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or stat.S_IMODE(path.stat().st_mode) & 0o077
    ):
        raise AtomizationMcpError("PRIVATE_FILE_INVALID")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        descriptor = load_atomization_descriptor(args.session_file)
        return StdioAtomizationMcpServer(AtomizationToolSession(descriptor)).serve()
    except (OSError, AtomizationMcpError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AtomizationDescriptor",
    "AtomizationMcpError",
    "AtomizationToolSession",
    "MCP_SERVER_NAME",
    "MCP_SERVER_VERSION",
    "StdioAtomizationMcpServer",
    "TOOL_GET_TASK",
    "TOOL_NAMES",
    "TOOL_VALIDATE",
    "load_atomization_descriptor",
    "tool_definitions",
    "write_atomization_descriptor",
]
