"""Adapter around the frozen plugin's existing Codex execution primitives."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from .contracts import (
    MODELS,
    ROOT,
    bundle_integrity,
    digest,
    file_sha,
    freeze_run,
    load_run,
    new_run_dir,
    read,
    save,
    selected_case,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def cold_module(bundle):
    spec = importlib.util.spec_from_file_location(
        "fixed_reqpilot_cold", Path(bundle) / "scripts/cold_run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_raw_only(workspace):
    forbidden = {
        "source_ir.json",
        "document-map.json",
        "source-requirement-baseline.json",
        "matrix-snapshot.json",
        "matrix.xlsx",
        "task-context.json",
    }
    if any(p.name in forbidden for p in (workspace / "runtime-data").rglob("*")):
        raise ValueError(
            "Fresh run contains prior captures or results; use a new run directory"
        )


DISCOVER = """import asyncio,json,sys
from mcp import Client,StdioServerParameters
async def main():
    args=[sys.argv[1],"--working-root",sys.argv[2],"--source-root",sys.argv[2]]
    async with Client(StdioServerParameters(command=sys.executable,args=args)) as client:
        listing=await client.list_tools()
        print(json.dumps({"tools":[{"name":t.name,"read_only":bool(t.annotations and t.annotations.read_only_hint),"schema":t.input_schema} for t in listing.tools]}))
asyncio.run(main())
"""


def prepare(
    case_id,
    bindings,
    bundle,
    out,
    python,
    model,
    transport="MCP",
    workbench_url=None,
    codex_binary="codex",
    observation_seconds=1200,
    host_binary=None,
    pair_repo=None,
):
    from .hosts import HOSTS, configure

    host = HOSTS.get(model)
    case, rubrics = selected_case(case_id)
    if case["status"] != "READY_WITH_PRIVATE_BINDING":
        raise ValueError(
            f"Case is {case['status']}; use a ready source and preparation version"
        )
    if model not in MODELS:
        raise ValueError("Unknown model profile")
    if host != "codex" and transport != "MCP":
        raise ValueError("Cross-host comparison currently uses the shared MCP entry")
    if transport not in {"CLI", "MCP"}:
        raise ValueError("Unknown transport")
    private_bindings = read(bindings)
    source_binding = private_bindings["sources"][case["source_key"]]
    source = Path(source_binding["path"]).expanduser().resolve(strict=True)
    if (
        file_sha(source) != case["source_sha256"]
        or source_binding["sha256"] != case["source_sha256"]
    ):
        raise ValueError("Private source binding differs from the frozen case")
    package = Path(bundle).expanduser().resolve(strict=True)
    receipt = bundle_integrity(package)
    python = (
        Path(python).expanduser().absolute()
    )  # Preserve the venv executable symlink.
    doctor = subprocess.run(
        [str(python), str(package / "scripts/reqpilot.py"), "doctor"],
        capture_output=True,
        text=True,
        check=True,
    )
    if json.loads(doctor.stdout)["status"] != "READY":
        raise ValueError("Fixed plugin Python environment is not ready")
    cold = cold_module(package)
    cold.workbench_network_configuration(workbench_url)
    out = new_run_dir(out, [source.parent, package])
    workspace = out / "workspace"
    operator = out / "operator"
    operator.mkdir()
    subprocess.run(
        [
            str(python),
            str(package / "scripts/reqpilot.py"),
            "bootstrap",
            "--workspace",
            str(workspace),
            "--source",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    copied = workspace / ".reqpilot"
    bundle_integrity(copied, receipt["bundle_sha256"])
    assert_raw_only(workspace)
    (workspace / "request.md").write_text(case["task_goal"] + "\n")
    config, discovery = (
        cold.prepare(codex_binary, workspace, operator / "discovery")
        if host == "codex"
        else ([], {})
    )
    host_config = None
    config += [
        "-c",
        "agents.enabled=false",
        "-c",
        "features.memories=false",
        "-c",
        "features.hooks=false",
        "-c",
        'web_search="disabled"',
    ]
    tool_contract = None
    if transport == "MCP":
        process = subprocess.run(
            [
                str(python),
                "-c",
                DISCOVER,
                str(copied / "scripts/mcp_server.py"),
                str(workspace),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        tool_contract = json.loads(process.stdout)
        if not {"task_open", "source_read", "task_deliver"} <= {
            t["name"] for t in tool_contract["tools"]
        }:
            raise ValueError("Required task tools not advertised")
        config += [
            "-c",
            "mcp_servers.reqpilot.command=" + json.dumps(str(python)),
            "-c",
            "mcp_servers.reqpilot.args="
            + json.dumps(
                [
                    str(copied / "scripts/mcp_server.py"),
                    "--working-root",
                    str(workspace),
                    "--source-root",
                    str(workspace),
                ]
            ),
            "-c",
            "mcp_servers.reqpilot.startup_timeout_sec=30",
            "-c",
            "mcp_servers.reqpilot.tool_timeout_sec=120",
        ]
        # Only this task's advertised local tools. No global approval/config edits.
        for tool in tool_contract["tools"]:
            if not tool["name"].replace("_", "").isalnum():
                raise ValueError("Invalid advertised tool name")
            if not tool["read_only"]:
                config += [
                    "-c",
                    "mcp_servers.reqpilot.tools."
                    + tool["name"]
                    + '.approval_mode="approve"',
                ]
        save(operator / "tool-contract.json", tool_contract)
    if host == "codex":
        probe = subprocess.run(
            [
                codex_binary,
                "debug",
                "prompt-input",
                "--disable",
                "plugins",
                "--disable",
                "multi_agent",
                *config,
                "Inspect the isolated tool and skill inventory only. Do not execute document work.",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        effective = json.loads(probe.stdout)
        if any(
            marker in "\n".join(cold.text_values(effective))
            for marker in ("<multi_agent_role>", "spawn_agent")
        ):
            raise ValueError("Host still exposes delegation; no model started")
        save(operator / "effective-prompt.json", effective)
    else:
        host_config = configure(
            host,
            workspace,
            operator,
            python,
            host_binary or ("agy" if host == "antigravity" else "node"),
            pair_repo,
        )
    route = (
        "主要任务操作使用当前ReqPilot MCP工具，业务工件由同一任务保存。"
        if transport == "MCP"
        else "主要任务操作使用打包CLI：<python> .reqpilot/scripts/reqpilot.py task <action> --working-root . --input <request.json>。"
    )
    prompt = (
        "Workspace instructions:\n"
        + (workspace / "AGENTS.md").read_text()
        + "\n用户任务：\n"
        + case["task_goal"]
    )
    prompt += "\n" + route + "\nPython: " + str(python)
    prompt += "\n只用本工作区原件与打包能力，不查其它任务、评测规则或旧答案；不委派子Agent。原文自由回查，按真实业务成果自查。"
    prompt += "\n没有整次时长截止；观察时点不要求停止或等待。完整保存后给出实际成果与未决项，不声明未做的浏览器核验。"
    if workbench_url:
        prompt += "\n本原件已注册的工作台：" + workbench_url
    (operator / "prompt.txt").write_text(prompt)
    source_copy = (
        workspace
        / read(workspace / "runtime-data/input-manifest.json")["inputs"][0]["path"]
    )
    stage_fixture = None
    preparation = case.get("preparation", "SOURCE_ONLY")
    if preparation == "STAGE_FIXTURE":
        from .stage_fixtures import seed

        binding = private_bindings["stage_fixtures"][case["fixture_key"]]
        recipe_path = Path(binding["path"]).expanduser().resolve(strict=True)
        if (
            file_sha(recipe_path) != case["fixture_sha256"]
            or binding["sha256"] != case["fixture_sha256"]
        ):
            raise ValueError("Stage recipe differs from the frozen case")
        stage_fixture = seed(
            workspace,
            source_copy,
            recipe_path,
            python,
            workbench_url,
            case["task_goal"],
            operator,
        )
        prompt += "\n本次为现有任务的阶段复核，输入说明见 stage-input.md；准备程序提供的Map和条目不计为本轮提取成果。"
        (operator / "prompt.txt").write_text(prompt)
    runner_files = {p.name: file_sha(p) for p in ROOT.glob("*.py")}
    run = freeze_run(
        out,
        case,
        rubrics,
        run_kind="MODEL_RUN",
        origin="NEW_STAGE_FIXTURE" if stage_fixture else "NEW_SOURCE_ONLY",
        model=model,
        host=host,
        host_config=host_config,
        effort=MODELS[model],
        transport=transport,
        source_path=str(source_copy),
        bundle_path=str(copied),
        bundle_sha256=receipt["bundle_sha256"],
        bundle_version=read(copied / ".codex-plugin/plugin.json")["version"],
        workspace=str(workspace),
        python=str(python),
        codex_binary=codex_binary,
        config=config,
        prompt_sha256=file_sha(operator / "prompt.txt"),
        agent_instructions_sha256=file_sha(workspace / "AGENTS.md"),
        request_sha256=file_sha(workspace / "request.md"),
        runner_files=runner_files,
        tool_contract_sha256=digest(tool_contract) if tool_contract else None,
        observation_seconds=observation_seconds,
        overall_timeout_seconds=None,
        host_timeout_policy=(
            "Native CLI wait ceiling 12h; 0 means immediate partial return, not unlimited"
            if host == "antigravity"
            else "No overall host deadline configured"
        ),
        workbench_url=workbench_url,
        prior_answers_supplied=False,
        preparation=preparation,
        stage_fixture=stage_fixture,
        prepared_at=now(),
        execution="NOT_STARTED",
        isolation=(
            "VERIFIED_PROMPT_AND_SKILL_INVENTORY; not a filesystem read-access boundary"
            if host == "codex"
            else "Native host tools and instructions; shared task-local skills; not a pure model-only comparison"
        ),
    )
    return {
        "status": "PREPARED",
        "run_id": run["run_id"],
        "run_dir": str(out),
        "models_called": 0,
        "workbench_bound": bool(workbench_url),
        "next_command": "run --run-dir " + str(out),
    }


def verify_prepared(root, run):
    bundle_integrity(run["bundle_path"], run["bundle_sha256"])
    for path, sha in [
        (Path(run["source_path"]), run["source_sha256"]),
        (root / "operator/prompt.txt", run["prompt_sha256"]),
        (Path(run["workspace"]) / "AGENTS.md", run["agent_instructions_sha256"]),
        (Path(run["workspace"]) / "request.md", run["request_sha256"]),
    ]:
        if file_sha(path) != sha:
            raise ValueError("Prepared input changed: " + path.name)
    for name, sha in run["runner_files"].items():
        if file_sha(ROOT / name) != sha:
            raise ValueError("Runner changed after preparation; prepare a new run")
    if run.get("preparation") == "STAGE_FIXTURE":
        from .stage_fixtures import verify_seed

        verify_seed(root, run)
    else:
        assert_raw_only(Path(run["workspace"]))


def _check_workbench(run):
    url = run.get("workbench_url")
    if not url:
        raise ValueError(
            "A fresh model run needs an explicit isolated project/document workbench URL"
        )
    parsed = urlsplit(url)
    q = parse_qs(parsed.query)
    if q.get("pluginSessionId") or q.get("resultRevision"):
        raise ValueError("Fresh run must not reuse a prior external task/result")
    api = (
        parsed.scheme
        + "://"
        + parsed.netloc
        + "/api/projects/"
        + q["projectId"][0]
        + "/sor-documents/"
        + q["documentId"][0]
        + "/file"
    )
    with urlopen(api, timeout=30) as response:
        import hashlib

        if hashlib.sha256(response.read()).hexdigest() != run["source_sha256"]:
            raise ValueError("Workbench source differs from frozen case")


def execute_process(
    command,
    prompt,
    workspace,
    output,
    observation_seconds=1200,
    host="codex",
    environment=None,
):
    """No overall deadline. Preserve public events; an observation never kills work."""
    output.mkdir(parents=True, exist_ok=False)
    started = now()
    began = time.monotonic()
    counts = {"public_events": 0, "reasoning_events_omitted": 0}
    save(
        output / "start.json", {"started_at": started, "overall_timeout_seconds": None}
    )
    terminal = False
    reader_error = []
    interrupted = False
    with (
        (output / "events.jsonl").open("w") as events,
        (
            open(os.devnull, "w")
            if host == "pair"
            else (output / "stderr.log").open("w")
        ) as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            start_new_session=True,
            env=environment,
        )

        def collect():
            nonlocal terminal
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = (event.get("item") or {}).get("type", "")
                    if (
                        "reasoning"
                        in (str(event.get("type", "")) + " " + str(kind)).lower()
                    ):
                        counts["reasoning_events_omitted"] += 1
                        continue
                    from .hosts import public_event

                    for public in public_event(host, event):
                        terminal |= public.get("type") == "turn.completed"
                        if (
                            host != "codex"
                            and (public.get("item") or {}).get("type")
                            == "agent_message"
                        ):
                            (output / "final.md").write_text(
                                public["item"].get("text", "")
                            )
                        public["_host_elapsed_seconds"] = round(
                            time.monotonic() - began, 3
                        )
                        events.write(json.dumps(public, ensure_ascii=False) + "\n")
                        events.flush()
                        counts["public_events"] += 1
            except Exception as exc:
                reader_error.append(type(exc).__name__)

        collector = threading.Thread(target=collect, daemon=True)
        collector.start()
        process.stdin.write(prompt)
        process.stdin.close()
        observed = False
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - began
                if not observed and elapsed >= observation_seconds:
                    # An explicit non-atomic observation, not a frozen final-state copy.
                    indexes = {
                        str(p.relative_to(workspace)): file_sha(p)
                        for p in Path(workspace).glob(
                            "runtime-data/projects/*/documents/*/tasks/*/workspace-index.json"
                        )
                    }
                    save(
                        output / "observation.json",
                        {
                            "elapsed_seconds": elapsed,
                            "public_events": counts["public_events"],
                            "status": "STILL_RUNNING",
                            "index_observations": indexes,
                            "atomic_snapshot": False,
                            "process_stopped": False,
                        },
                    )
                    observed = True
                time.sleep(0.25)
        except KeyboardInterrupt:
            interrupted = True
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                if process.poll() is not None:
                    break
                os.killpg(process.pid, sig)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        collector.join(timeout=10)
        if not collector.is_alive():
            process.stdout.close()
    host_wait_expired = (
        host == "antigravity"
        and "print timeout after" in (output / "stderr.log").read_text()
    )
    return {
        "started_at": started,
        "elapsed_seconds": round(time.monotonic() - began, 3),
        "exit_code": process.returncode,
        "timed_out": False,
        "interrupted": interrupted,
        "natural_completion": process.returncode == 0
        and terminal
        and not host_wait_expired
        and not reader_error
        and not collector.is_alive(),
        "public_turn_completed": terminal,
        "trace_collection_errors": reader_error,
        "overall_timeout_seconds": None,
        "host_wait_expired": host_wait_expired,
        **counts,
    }


def run(run_dir):
    root, record = load_run(run_dir)
    if record["origin"] not in {"NEW_SOURCE_ONLY", "NEW_STAGE_FIXTURE"}:
        raise ValueError("Historical/replay imports cannot be executed as fresh runs")
    verify_prepared(root, record)
    _check_workbench(record)
    output = root / "operator/execution"
    cold = cold_module(record["bundle_path"])
    prompt = (root / "operator/prompt.txt").read_text()
    host = record.get("host", "codex")
    environment = None
    if host == "codex":
        command = cold.execution_command(
            record["codex_binary"],
            Path(record["workspace"]),
            output,
            record["config"],
            model=record["model"],
            effort=record["effort"],
            allow_delegation=False,
            workbench_url=record["workbench_url"],
        )
    else:
        from .hosts import invocation

        command, prompt, environment = invocation(record, prompt)
    save(
        root / "operator/invocation.json",
        {"argv": command, "prompt_sha256": record["prompt_sha256"]},
    )
    result = execute_process(
        command,
        prompt,
        Path(record["workspace"]),
        output,
        record["observation_seconds"],
        host,
        environment,
    )
    result.update(
        run_id=record["run_id"],
        run_kind="MODEL_RUN",
        model=record["model"],
        host=host,
        effort=record["effort"],
        source_sha256=record["source_sha256"],
        bundle_sha256=record["bundle_sha256"],
        public_events_path=str(output / "events.jsonl"),
        semantic_status="REVIEW",
        browser="NOT_VERIFIED",
    )
    try:
        bundle_integrity(record["bundle_path"], record["bundle_sha256"])
        result["package_integrity"] = "UNCHANGED"
    except (ValueError, OSError):
        result["package_integrity"] = "CHANGED_OR_UNAVAILABLE"
    result["source_integrity"] = (
        "UNCHANGED"
        if file_sha(record["source_path"]) == record["source_sha256"]
        else "CHANGED"
    )
    save(root / "result.json", result)
    return result
