"""Native Antigravity/Pair adapters around the same ReqPilot bundle and MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .contracts import MODELS, ROOT, file_sha, save

HOSTS = {
    "gpt-5.6-sol": "codex",
    "gpt-5.6-luna": "codex",
    "gemini-3.8-flash-high": "antigravity",
    "qwen3.8-max-0902": "pair",
}


def configure(host, workspace, operator, python, binary, pair_repo=None):
    args = [
        str(workspace / ".reqpilot/scripts/mcp_server.py"),
        "--working-root",
        str(workspace),
        "--source-root",
        str(workspace),
    ]
    if host == "antigravity":
        path = workspace / ".agents/mcp_config.json"
        save(path, {"mcpServers": {"reqpilot": {"command": str(python), "args": args}}})
        probe = subprocess.run(
            [binary, "-p", "/skills", "--print-timeout", "60s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        names = [line.split("\t")[0] for line in probe.stdout.splitlines()]
        if not all(
            any(name.endswith(skill) for name in names)
            for skill in [
                "reqpilot",
                "reqpilot-source-map",
                "reqpilot-source-items",
                "reqpilot-review",
                "reqpilot-verify",
            ]
        ):
            raise ValueError("Antigravity did not discover the full skill catalog")
        save(
            operator / "host-discovery.json",
            {
                "models_called": 0,
                "skills": names,
                "isolation": "Native host catalog; unrelated global capabilities remain visible",
            },
        )
        return {"binary": binary, "files": {str(path): file_sha(path)}}
    if host != "pair" or pair_repo is None:
        raise ValueError("Pair needs its actual repository CLI path")
    import yaml

    pair_repo = Path(pair_repo).expanduser().resolve(strict=True)
    # Keep provider credential references in place. Do not copy the credential store.
    settings = yaml.safe_load((Path.home() / ".dsh/settings.yaml").read_text())
    selected = settings["agent-default-model"]
    if selected != {
        "provider": "bailian",
        "model": "qwen3.8-max-0902",
        "reasoningEffort": MODELS["qwen3.8-max-0902"],
    }:
        raise ValueError(
            "Pair selection changed; explicitly review the actual model/effort before running"
        )
    # Preserve YAML 1.2 spellings such as `off`: PyYAML's YAML 1.1 round-trip
    # would turn that model capability key into a boolean and break discovery.
    settings_path = operator / "pair-settings.yaml"
    settings_path.write_bytes((Path.home() / ".dsh/settings.yaml").read_bytes())
    settings_path.chmod(0o600)
    loader = subprocess.check_output(
        [
            "node",
            "--input-type=module",
            "-e",
            "console.log(import.meta.resolve('tsx/esm'))",
        ],
        cwd=pair_repo,
        text=True,
    ).strip()
    overlay = [
        {"id": "settings", "config": {"path": str(settings_path)}},
        {"id": "reqpilot-skills", "disabled": True},
        {
            "id": "skill-filesystem",
            "config": {
                "includeDefaultRoots": False,
                "customSkillDirs": [str(workspace / ".reqpilot/skills")],
            },
        },
        {
            "insert": [
                {
                    "id": "reqpilot-mcp",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {
                        "serverName": "reqpilot",
                        "transport": "stdio",
                        "command": str(python),
                        "args": args,
                        "cwd": str(workspace),
                        "toolCallTimeoutMs": 120000,
                        "failOnStartupError": True,
                    },
                },
                {"id": "reqpilot-public-events", "name": str(ROOT / "pair_events.mjs")},
            ]
        },
    ]
    patch = operator / "pair.patch.yml"
    patch.write_text(yaml.safe_dump(overlay, sort_keys=False))
    # JSON/YAML and adapter hashes are frozen separately from domain capabilities.
    probe_patch = operator / "pair-probe.patch.yml"
    probe_patch.write_text(
        yaml.safe_dump(
            [
                {"id": "headless-runner", "disabled": True},
                {
                    "insert": [
                        {
                            "id": "reqpilot-discovery-probe",
                            "name": str(ROOT / "pair_probe.mjs"),
                        }
                    ]
                },
            ],
            sort_keys=False,
        )
    )
    probe = subprocess.run(
        [
            binary,
            "--import",
            loader,
            str(pair_repo / "apps/cli/src/bin.ts"),
            "--profile",
            "headless",
            "--patch",
            str(patch),
            "--patch",
            str(probe_patch),
            "discovery",
        ],
        cwd=workspace,
        env={**os.environ, "TSX_TSCONFIG_PATH": str(pair_repo / "tsconfig.json")},
        capture_output=True,
        text=True,
        check=True,
    )
    discovery = json.loads(probe.stdout)
    if discovery.get("selection") != selected or discovery.get("modelCalls") != 0:
        raise ValueError(
            "Pair runtime model discovery differs from its frozen selection"
        )
    save(operator / "host-discovery.json", discovery)
    return {
        "binary": binary,
        "cli": str(pair_repo / "apps/cli/src/bin.ts"),
        "loader": loader,
        "tsconfig": str(pair_repo / "tsconfig.json"),
        "patch": str(patch),
        "files": {
            str(patch): file_sha(patch),
            str(settings_path): file_sha(settings_path),
            str(ROOT / "pair_events.mjs"): file_sha(ROOT / "pair_events.mjs"),
        },
    }


def invocation(run, prompt):
    host = run["host"]
    cfg = run["host_config"]
    for path, sha in cfg["files"].items():
        if file_sha(path) != sha:
            raise ValueError("Host configuration changed after preparation")
    if host == "antigravity":
        command = [
            cfg["binary"],
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            "12h",
            "--model",
            run["model"],
            "--effort",
            run["effort"],
            "--mode",
            "accept-edits",
            "--dangerously-skip-permissions",
        ]
        # Per-invocation authorization for the user's task; no global policy edits.
        return (
            command,
            json.dumps({"event": "user", "message": {"content": prompt}}) + "\n",
            None,
        )
    command = [
        cfg["binary"],
        "--import",
        cfg["loader"],
        cfg["cli"],
        "--profile",
        "headless",
        "--patch",
        cfg["patch"],
        prompt,
    ]
    return (
        command,
        "",
        {
            **os.environ,
            "DSH_PERMISSION_MODE": "danger-full-access",
            "TSX_TSCONFIG_PATH": cfg["tsconfig"],
        },
    )


def public_event(host, event):
    """Normalize documented public Antigravity fields; omit opaque step payloads."""
    if host != "antigravity":
        return [event] if event.get("type") else []
    kind = event.get("event")
    if kind == "init":
        return [
            {"type": "thread.started", "thread_id": event.get("conversation_id")},
            {"type": "turn.started"},
        ]
    if kind == "result":
        result = event.get("result", {})
        usage = result.get("usage", {})
        return [
            {
                "type": "item.completed",
                "item": {
                    "id": "final",
                    "type": "agent_message",
                    "text": result.get("response", ""),
                },
            },
            {
                "type": "turn.completed"
                if result.get("status") == "SUCCESS"
                else "turn.failed",
                "usage": {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cached_input_tokens": usage.get("cache_read_tokens"),
                    "reasoning_output_tokens": usage.get("thinking_tokens"),
                },
            },
        ]
    if kind == "step_update":
        step = event.get("step_update", {})
        if step.get("step_type") == "agent_response":
            return [{"type": "host.text_delta", "text": step.get("text_delta", "")}]
        return [
            {
                "type": "host.step",
                "step_index": step.get("step_index"),
                "step_type": step.get("step_type"),
                "state": step.get("state"),
            }
        ]
    return []
