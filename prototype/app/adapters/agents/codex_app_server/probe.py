"""Safe, no-inference capability probe for the local Codex App Server.

The probe performs only protocol initialization, ``account/read``, and
``model/list``. It never starts a model turn, reads an auth file, or prints
account email/token fields.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


PROBE_SCHEMA = "reqpilotx-codex-capability-probe/v1"
DEFAULT_CODEX_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_PREFERRED_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_SECONDS = 10.0

_PASSTHROUGH_ENV = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TERM",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

_HARDENING_OVERRIDES = (
    'forced_login_method="chatgpt"',
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.skill_mcp_dependency_install=false",
    "features.apps=false",
    "tools.web_search=false",
    "feedback.enabled=false",
    'history.persistence="none"',
    'file_opener="none"',
    "hide_agent_reasoning=true",
    "show_raw_agent_reasoning=false",
    'model_reasoning_effort="medium"',
    'approval_policy="never"',
    'sandbox_mode="read-only"',
    "allow_login_shell=false",
    'shell_environment_policy.inherit="none"',
    "shell_environment_policy.ignore_default_excludes=false",
)


class ProbeError(RuntimeError):
    """A safe probe failure that contains no provider payload or credential."""


def configured_auth_store() -> str:
    """Use the explicitly configured login store without copying credentials."""
    value = str(os.environ.get("REQPILOT_CODEX_AUTH_STORE") or "keyring").strip()
    if value not in {"file", "keyring", "auto"}:
        raise ProbeError("CODEX_AUTH_STORE_INVALID")
    return value


def build_environment(
    source: Mapping[str, str], *, codex_home: Path | None = None
) -> dict[str, str]:
    """Build a minimal child environment without ambient API secrets."""

    environment = {name: source[name] for name in _PASSTHROUGH_ENV if source.get(name)}
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home.resolve())
    return environment


def build_command(codex_bin: Path) -> list[str]:
    """Return the pinned stable-surface App Server invocation."""

    command = [
        str(codex_bin),
        "app-server",
        "--stdio",
        "--strict-config",
    ]
    for override in _HARDENING_OVERRIDES:
        command.extend(("--config", override))
    command.extend(("--config", f'cli_auth_credentials_store="{configured_auth_store()}"'))
    return command


def safe_account_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project an account response without identity or credential fields."""

    account = result.get("account")
    if not isinstance(account, Mapping):
        account = {}
    return {
        "account_type": _optional_string(account.get("type")),
        "plan_type": _optional_string(account.get("planType")),
        "requires_openai_auth": bool(result.get("requiresOpenaiAuth")),
    }


def safe_model_summary(
    result: Mapping[str, Any], *, preferred_model: str
) -> dict[str, Any]:
    """Project only model ids and declared input modalities."""

    data = result.get("data")
    if not isinstance(data, list):
        data = []
    image_models: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        model_id = _optional_string(item.get("id"))
        modalities = item.get("inputModalities")
        if not model_id or not isinstance(modalities, list):
            continue
        normalized_modalities = sorted(
            {
                value
                for raw in modalities
                if (value := _optional_string(raw)) is not None
            }
        )
        if "image" not in normalized_modalities:
            continue
        image_models.append(
            {
                "id": model_id,
                "input_modalities": normalized_modalities,
            }
        )
    image_models.sort(key=lambda item: str(item["id"]))
    return {
        "image_capable_count": len(image_models),
        "image_capable_models": image_models,
        "preferred_model": preferred_model,
        "preferred_model_available": any(
            item["id"] == preferred_model for item in image_models
        ),
    }


def run_probe(
    *,
    codex_bin: Path,
    codex_home: Path | None,
    preferred_model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run the no-inference App Server capability probe."""

    if not codex_bin.is_file():
        raise ProbeError("Codex binary is not available")
    if timeout_seconds <= 0:
        raise ProbeError("Probe timeout must be positive")

    process = subprocess.Popen(
        build_command(codex_bin),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=build_environment(os.environ, codex_home=codex_home),
    )
    if process.stdin is None or process.stdout is None:
        process.terminate()
        raise ProbeError("Codex App Server stdio is unavailable")

    try:
        _send(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "reqpilotx_capability_probe",
                        "title": "ReqPilotX Capability Probe",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            },
        )
        _read_response(process, request_id=1, timeout_seconds=timeout_seconds)
        _send(process, {"method": "initialized", "params": {}})
        _send(
            process,
            {
                "method": "account/read",
                "id": 2,
                "params": {"refreshToken": False},
            },
        )
        account_result = _read_response(
            process, request_id=2, timeout_seconds=timeout_seconds
        )
        _send(
            process,
            {"method": "model/list", "id": 3, "params": {}},
        )
        model_result = _read_response(
            process, request_id=3, timeout_seconds=timeout_seconds
        )
    finally:
        _stop_process(process)

    account_summary = safe_account_summary(account_result)
    model_summary = safe_model_summary(
        model_result,
        preferred_model=preferred_model,
    )
    return {
        "schema": PROBE_SCHEMA,
        "status": "PASS",
        "inference_started": False,
        "account": account_summary,
        "models": model_summary,
        "hardening": {
            "experimental_api": False,
            "shell_tool": False,
            "unified_exec": False,
            "apps": False,
            "web_search": False,
            "history_persistence": "none",
        },
    }


def _send(process: subprocess.Popen[str], message: Mapping[str, Any]) -> None:
    if process.stdin is None:
        raise ProbeError("Codex App Server stdin closed unexpectedly")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[str], *, request_id: int, timeout_seconds: float
) -> Mapping[str, Any]:
    if process.stdout is None:
        raise ProbeError("Codex App Server stdout closed unexpectedly")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.0)
            if not selector.select(remaining):
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError("Codex App Server emitted invalid JSON") from exc
            if not isinstance(message, Mapping) or message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise ProbeError(f"Codex App Server request {request_id} failed")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise ProbeError(
                    f"Codex App Server request {request_id} returned no object"
                )
            return result
    finally:
        selector.close()
    raise ProbeError(f"Codex App Server request {request_id} timed out")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe Codex auth and image-model availability without inference."
    )
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path(os.environ.get("REQPILOT_CODEX_BIN", DEFAULT_CODEX_BIN)),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("REQPILOT_CODEX_HOME", "").strip())
            else None
        ),
    )
    parser.add_argument(
        "--preferred-model",
        default=DEFAULT_PREFERRED_MODEL,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_probe(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            preferred_model=str(args.preferred_model),
            timeout_seconds=float(args.timeout_seconds),
        )
    except ProbeError as exc:
        print(
            json.dumps(
                {
                    "schema": PROBE_SCHEMA,
                    "status": "FAIL",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2

    account = result["account"]
    models = result["models"]
    if account["account_type"] != "chatgpt":
        result["status"] = "AUTH_REQUIRED"
        print(json.dumps(result, sort_keys=True))
        return 3
    if not models["preferred_model_available"]:
        result["status"] = "MODEL_UNAVAILABLE"
        print(json.dumps(result, sort_keys=True))
        return 4
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
