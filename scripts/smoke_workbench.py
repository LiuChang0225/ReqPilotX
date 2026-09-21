"""Real HTTP, synthetic PDF smoke; no model, worker or existing database.

Run with prototype/.venv/bin/python scripts/smoke_workbench.py.
Use --keep-server to retain an isolated browser review window after success.
"""

from __future__ import annotations

import argparse
from argparse import Namespace
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "reqpilot"


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def isolate(directory: Path, database_url: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("REQPILOT_", "REQPILOTX_", "OPENAI_", "AZURE_", "DASHSCOPE_", "ALIYUN_", "ALIBABA_CLOUD_"))
    }
    env.update({
        "REQPILOT_ENV": "local", "REQPILOT_DATABASE_URL": database_url,
        "REQPILOT_STORAGE_DIR": str(directory / "storage"),
        "REQPILOT_OBJECT_STORE": "local", "REQPILOT_OBJECT_ROOT": str(directory / "storage"),
        "REQPILOT_SCRATCH_ROOT": str(directory / "scratch"),
        "REQPILOT_AGENT_RUNTIME": "none", "REQPILOT_PROCESS_ROLE": "api",
        "REQPILOT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "DO_NOT_TRACK": "1",
        "PYTHONPATH": str(ROOT / "prototype"),
    })
    return env


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def postgres_tools(explicit: Path | None) -> Path:
    located = shutil.which("pg_ctl")
    directory = explicit.expanduser().resolve() if explicit else Path(located).resolve().parent if located else None
    require(directory is not None, "PostgreSQL binaries unavailable; provide --postgres-bin")
    for name in ("initdb", "pg_ctl", "createdb", "postgres"):
        require((directory / name).is_file() and os.access(directory / name, os.X_OK),
                f"Missing executable: {directory / name}")
    return directory


def start_postgres(directory: Path, binaries: Path, port: int, env: dict[str, str]) -> dict:
    data = directory / "postgres-data"
    require(not data.exists(), "Refusing to reuse PostgreSQL data")
    owner = {"schema_version": "reqpilotx-smoke-owned-postgres/v1", "owner_id": uuid.uuid4().hex,
             "directory": str(directory), "data_directory": str(data), "binaries": str(binaries), "port": port}
    save(directory / "postgres-owner.json", owner)
    with (directory / "postgres-init.log").open("ab") as log:
        subprocess.run([str(binaries / "initdb"), "-D", str(data), "--username=reqpilot_smoke",
                        "--encoding=UTF8", "--locale=C", "--auth-local=trust", "--auth-host=trust"],
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        options = shlex.join(["-h", "127.0.0.1", "-p", str(port), "-c", "unix_socket_directories=",
                              "-c", "max_connections=25"])
        subprocess.run([str(binaries / "pg_ctl"), "-D", str(data), "-l", str(directory / "postgres.log"),
                        "-o", options, "-w", "start"], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([str(binaries / "createdb"), "-h", "127.0.0.1", "-p", str(port),
                        "-U", "reqpilot_smoke", "reqpilot_smoke"], env=env,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    return owner


def stop_postgres(directory: Path, env: dict[str, str]) -> None:
    marker = directory / "postgres-owner.json"
    if not marker.is_file():
        return
    owner = read(marker)
    data = Path(owner["data_directory"])
    require(owner.get("schema_version") == "reqpilotx-smoke-owned-postgres/v1"
            and Path(owner["directory"]).resolve() == directory.resolve()
            and data.resolve() == (directory / "postgres-data").resolve() and not data.is_symlink(),
            "PostgreSQL ownership does not match this output directory")
    if (data / "postmaster.pid").is_file():
        with (directory / "postgres-stop.log").open("ab") as log:
            subprocess.run([str(Path(owner["binaries"]) / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def stop_retained(directory: Path) -> int:
    directory = directory.expanduser().resolve(strict=True)
    receipt = read(directory / "receipt.json")
    owner = read(directory / "postgres-owner.json")
    require(Path(receipt["output_directory"]).resolve() == directory
            and owner.get("schema_version") == "reqpilotx-smoke-owned-postgres/v1"
            and Path(owner["directory"]).resolve() == directory,
            "Output receipt and PostgreSQL owner must identify this exact directory")
    pid = receipt.get("server_pid")
    port = urlsplit(receipt["base_url"]).port
    if pid:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=False).stdout.strip()
        if command:
            require(str(Path(__file__).resolve()) in command and f"--_serve --port {port}" in command,
                    "Recorded API PID now belongs to another process; refusing to stop it")
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                       capture_output=True, text=True, check=False).stdout.strip()
                if not state or state.startswith("Z"):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Owned API still running; PostgreSQL was not stopped")
    env = isolate(directory, f"postgresql://reqpilot_smoke@127.0.0.1:{owner['port']}/reqpilot_smoke")
    stop_postgres(directory, env)
    receipt.update(server_pid=None, server_retained=False,
                   stopped_at=datetime.now(timezone.utc).isoformat())
    save(directory / "receipt.json", receipt)
    print(json.dumps({"status": "STOPPED", "output_directory": str(directory), "artifacts_retained": True}, indent=2))
    return 0


def block_external_connections(event: str, args: tuple) -> None:
    if event != "socket.connect":
        return
    address = args[1]
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
        return
    raise RuntimeError("The synthetic smoke permits loopback HTTP only")


def start_server(directory: Path, port: int, env: dict[str, str]) -> subprocess.Popen:
    with (directory / "api.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--_serve", "--port", str(port)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    return process


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise RuntimeError("Owned smoke API did not stop gracefully; see api.log")


def fixture_module():
    # This module creates an invented PDF now; it never loads a frozen answer.
    path = PLUGIN / "tests" / "portable_fixtures.py"
    spec = importlib.util.spec_from_file_location("reqpilot_synthetic_fixture", path)
    require(spec is not None and spec.loader is not None, "Synthetic fixture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_bundle(mapping: Path, baseline: Path | None = None) -> dict:
    result = {name: read(mapping / name) for name in ("document-map.json", "table-previews.json")}
    if baseline:
        for name in ("source-requirement-baseline.json", "validation-receipt.json"):
            result[name] = read(baseline / name)
    return result


def run(args: Namespace) -> int:
    if args.output:
        directory = args.output.expanduser().resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        directory = Path(tempfile.mkdtemp(prefix="reqpilotx-workbench-smoke-"))
    binaries = postgres_tools(args.postgres_bin)
    postgres_port = unused_port()
    database_url = f"postgresql://reqpilot_smoke@127.0.0.1:{postgres_port}/reqpilot_smoke"
    env = isolate(directory, database_url)
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(PLUGIN / "scripts"), str(ROOT / "prototype")]
    sys.addaudithook(block_external_connections)
    import httpx

    port = unused_port()
    require(port != postgres_port, "Random API and database ports collided; rerun")
    base = f"http://127.0.0.1:{port}"
    receipt = {
        "schema_version": "reqpilotx-synthetic-http-smoke/v1", "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(), "source_kind": "INVENTED_SYNTHETIC_PDF",
        "model_dispatch": "NOT_PERFORMED", "worker_started": False,
        "semantic_model_acceptance": "NOT_CLAIMED", "database_backend": "postgresql",
        "output_directory": str(directory), "base_url": base, "requests": [],
    }
    process = None
    successful = False
    client = httpx.Client(base_url=base, timeout=90, trust_env=False)

    def request(method: str, path: str, *, expected: int = 200, **kwargs):
        response = client.request(method, path, **kwargs)
        receipt["requests"].append({
            "method": method, "path": path, "status_code": response.status_code,
            "response_sha256": sha(response.content), "response_bytes": len(response.content),
        })
        require(response.status_code == expected,
                f"Unexpected {method} {path}: HTTP {response.status_code}; {response.text[:1200]}")
        return response

    def ready():
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(process is not None and process.poll() is None, "Smoke API exited; see api.log")
            try:
                if client.get("/api/projects").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Smoke API did not become ready; see api.log")

    try:
        receipt["postgres"] = start_postgres(directory, binaries, postgres_port, env)
        process = start_server(directory, port, env)
        ready()
        require(request("GET", "/api/projects").json() == [], "New isolated database was not empty")
        import psycopg
        with psycopg.connect(database_url) as connection:
            migrations = connection.execute("SELECT version, name, sha256 FROM schema_migrations ORDER BY version").fetchall()
        expected_migrations = sorted((ROOT / "prototype" / "migrations" / "postgresql").glob("*.sql"))
        require(len(migrations) == len(expected_migrations) and len(migrations) > 0,
                "Application migrations were not fully applied")
        receipt["migrations"] = [{"version": row[0], "name": row[1], "sha256": row[2]} for row in migrations]
        fixture = fixture_module()
        import artifact_tools
        from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
        from revision_tools import published_workbook_snapshot_sha256
        from workbench_tools import workbench_result_url

        source, capture, map_input = fixture.make_fixture(directory / "synthetic-input")
        source_sha = sha(source.read_bytes())
        project = request("POST", "/api/projects", json={
            "name": "Synthetic HTTP smoke", "customer_name": "Invented demonstration only",
        }).json()
        project_id = project["id"]
        documents_path = f"/api/projects/{project_id}/sor-documents"
        with source.open("rb") as upload:
            document = request("POST", documents_path,
                               files={"file": (source.name, upload, "application/pdf")}).json()
        document_id = document["id"]
        source_path = f"{documents_path}/{document_id}"
        require(sha(request("GET", source_path + "/file").content) == source_sha,
                "Uploaded source bytes differ")

        save(directory / "map-input.json", map_input)
        mapping = directory / "map"
        map_result = artifact_tools.compile_map(Namespace(
            source=source, source_ir=capture / "source_ir.json", input=directory / "map-input.json", output_dir=mapping,
        ))
        session = "plugin-" + uuid.uuid4().hex
        api = source_path + "/agent-structure/delivery/plugin-workbench"
        identity = {"session_id": session, "expected_source_sha256": source_sha,
                    "require_map_confirmation": False, "goal": "Synthetic no-model HTTP feedback and recovery smoke"}
        map_state = request("POST", api, json={**identity, "artifacts": artifact_bundle(mapping)}).json()
        require(map_state["revision_number"] == 1, "Map was not saved as the first revision")

        decisions = fixture.item_decisions(mapping)
        initial = deepcopy(decisions)
        initial["text_decisions"] = [d for d in decisions["text_decisions"] if d["scope_id"] == "text:s2"]
        initial["table_decisions"] = []
        save(directory / "initial-items.json", initial)
        first = directory / "baseline-1"
        artifact_tools.compile_items(Namespace(map_dir=mapping, input=directory / "initial-items.json",
                                              baseline_dir=None, output_dir=first))
        first_baseline = read(first / "source-requirement-baseline.json")
        require(len(first_baseline["records"]) == 1, "Synthetic first baseline must have one retained item")
        first_state = request("POST", api, json={**identity,
            "expected_revision_id": map_state["revision_id"], "artifacts": artifact_bundle(mapping, first)}).json()
        session_path = api + "/" + session
        first_snapshot = request("GET", session_path + "/snapshot", params={"revision": first_state["revision_id"]}).json()
        selected = first_baseline["records"][0]["source_record_id"]
        feedback_payload = {
            "command_id": "synthetic-complete-remaining-scopes", "expected_source_sha256": source_sha,
            "expected_revision_id": first_state["revision_id"], "source_item_ids": [selected],
            "text": "Retain this original item and add the remaining authorized synthetic scopes, including the complete test table.",
        }
        feedback_state = request("POST", session_path + "/feedback", json=feedback_payload).json()
        feedback_id = feedback_state["feedback"][0]["feedback_id"]
        repeated = request("POST", session_path + "/feedback", json=feedback_payload).json()
        require(len(repeated["feedback"]) == 1, "Idempotent feedback retry duplicated the command")

        additional = deepcopy(decisions)
        additional["text_decisions"] = [d for d in decisions["text_decisions"] if d["scope_id"] != "text:s2"]
        save(directory / "additional-items.json", additional)
        final = directory / "baseline-2"
        item_result = artifact_tools.compile_items(Namespace(map_dir=mapping, input=directory / "additional-items.json",
                                                           baseline_dir=first, output_dir=final))
        final_baseline = read(final / "source-requirement-baseline.json")
        require(len(final_baseline["records"]) == 8 and item_result["unprocessed_scope_count"] == 0,
                "Complete synthetic scopes were not compiled")
        retained = next(r for r in final_baseline["records"] if r["source_record_id"] == selected)
        require(retained["source_display_id"] == first_baseline["records"][0]["source_display_id"],
                "Revision changed the retained source identity")
        final_payload = {**identity, "expected_revision_id": first_state["revision_id"],
                         "artifacts": artifact_bundle(mapping, final), "feedback_resolutions": {
                             feedback_id: "Added the remaining synthetic scopes through the shared compiler; retained the existing source item identity.",
                         }}
        final_state = request("POST", api, json=final_payload).json()
        require(final_state["revision_number"] == 3, "Expected Map, partial Items, complete Items versions")
        require(final_state["feedback_receipts"][0]["state"] == "REVISION_REPORTED", "Feedback resolution was not saved")
        snapshot = request("GET", session_path + "/snapshot", params={"revision": final_state["revision_id"]}).json()
        require(snapshot["delivery_sha256"] != first_snapshot["delivery_sha256"], "Expanded revision has unchanged content")
        request("POST", api, expected=409, json={**identity, "expected_revision_id": map_state["revision_id"],
                                                "artifacts": artifact_bundle(mapping, first)})
        require(request("GET", session_path + "/snapshot", params={"revision": first_state["revision_id"]}).json() == first_snapshot,
                "Historical snapshot changed after a successor revision")

        def export(name: str):
            response = request("GET", session_path + "/export", params={"revision": final_state["revision_id"]})
            require(response.headers.get("x-reqpilot-revision") == final_state["revision_id"], "Excel revision header mismatch")
            require(response.headers.get("x-reqpilot-snapshot") == snapshot["delivery_sha256"], "Excel snapshot header mismatch")
            path = directory / name
            path.write_bytes(response.content)
            # The HTTP header pins delivery content without its self-hash field;
            # Excel pins the complete canonical snapshot, including that field.
            workbook_snapshot_sha = canonical_json_sha256(snapshot)
            require(published_workbook_snapshot_sha256(path) == workbook_snapshot_sha, "Excel embedded snapshot mismatch")
            return {"path": str(path), "file_sha256": sha(response.content),
                    "delivery_sha256": snapshot["delivery_sha256"], "snapshot_sha256": workbook_snapshot_sha}

        before_export = export("matrix-before-restart.xlsx")
        old_pid = process.pid
        stop_server(process)
        process = start_server(directory, port, env)
        ready()
        restored = request("GET", session_path).json()
        require(restored == final_state, "Restart changed saved work, feedback or version history")
        require(request("GET", session_path + "/snapshot", params={"revision": final_state["revision_id"]}).json() == snapshot,
                "Restart changed the saved delivery snapshot")
        require(sha(request("GET", source_path + "/file").content) == source_sha, "Restart changed the original source")
        after_export = export("matrix-after-restart.xlsx")
        listing = request("GET", api + "/sessions").json()
        require(len(listing["sessions"]) == 1 and not listing["unavailable"], "Restored work is not listed")
        reader = request("GET", session_path + "/reader", params={"revision": final_state["revision_id"]}).json()
        request("GET", session_path + "/records/" + selected, params={"revision": final_state["revision_id"]})
        url = workbench_result_url({"base_url": base, "project_id": project_id,
                                   "document_id": document_id, "session_id": session}, final_state["revision_id"])
        page = request("GET", url)
        require("text/html" in page.headers.get("content-type", ""), "Built workbench UI not served")
        save(directory / "final-state.json", final_state)
        save(directory / "matrix-snapshot.json", snapshot)
        save(directory / "reader.json", reader)
        receipt.update({
            "status": "PASS", "source_sha256": source_sha, "project_id": project_id, "document_id": document_id,
            "session_id": session, "revision_id": final_state["revision_id"], "revision_number": 3,
            "baseline_sha256": final_state["baseline_sha256"], "delivery_sha256": snapshot["delivery_sha256"],
            "snapshot_sha256": canonical_json_sha256(snapshot),
            "source_items": 8, "requirements": 7, "feedback_id": feedback_id,
            "map_compilation": map_result, "item_compilation": item_result,
            "exports": [before_export, after_export], "restart": {"old_pid": old_pid, "new_pid": process.pid, "state_unchanged": True},
            "workbench_url": url, "server_pid": process.pid if args.keep_server else None,
            "server_retained": args.keep_server, "browser_interaction": "NOT_RUN",
            "limits": ["Synthetic deterministic decisions, not model quality", "Isolated local PostgreSQL, not cloud/deployment recovery",
                       "HTTP/UI entry checked; browser interactions require separate inspection"],
        })
        successful = True
    except Exception as exc:
        receipt.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
    finally:
        client.close()
        if process is not None and not (successful and args.keep_server):
            try:
                stop_server(process)
            except Exception as exc:
                receipt.update(status="FAIL", shutdown_error=str(exc))
        if not (successful and args.keep_server):
            try:
                stop_postgres(directory, env)
            except Exception as exc:
                receipt.update(status="FAIL", postgres_shutdown_error=str(exc))
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(directory / "receipt.json", receipt)
        print(json.dumps({key: receipt.get(key) for key in (
            "status", "output_directory", "workbench_url", "server_pid", "revision_id", "snapshot_sha256", "error",
        )}, ensure_ascii=False, indent=2), flush=True)
    return 0 if receipt["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Create a new result directory; an existing path is refused")
    parser.add_argument("--keep-server", action="store_true", help="Keep the owned API alive for browser review after PASS")
    parser.add_argument("--postgres-bin", type=Path, help="Directory containing initdb/pg_ctl/createdb; otherwise resolve from PATH")
    parser.add_argument("--stop-output", type=Path, help="Stop only the owned retained API/PostgreSQL recorded in this result directory")
    parser.add_argument("--_serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stop_output:
        require(not args.output and not args.keep_server and not args._serve,
                "--stop-output cannot be combined with a new run")
        return stop_retained(args.stop_output)
    if args._serve:
        require(os.environ.get("REQPILOT_ENV") == "local" and os.environ.get("REQPILOT_AGENT_RUNTIME") == "none"
                and "reqpilot_smoke@127.0.0.1:" in os.environ.get("REQPILOT_DATABASE_URL", ""),
                "Internal server requires the isolated local PostgreSQL configuration")
        sys.path.insert(0, str(ROOT / "prototype"))
        sys.addaudithook(block_external_connections)
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="info")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
