"""Isolated real PostgreSQL/API/worker smoke with Azure SDK and HTTP-only model mock.

Uses invented source, model and dummy key. This is deterministic integration
evidence, never live provider authentication or semantic-quality acceptance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.parse import urlsplit

import smoke_workbench as shared

ROOT = shared.ROOT
MODEL = "reqpilot-smoke-model"
DUMMY_KEY = "invented-loopback-smoke-key-not-a-credential"


def _tool(reply_id, name, args):
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": reply_id,
        "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


class NativeProviderMock:
    """Choose actual registered tools; all outputs come from production compilers."""

    def __init__(self, directory, map_input, source_sha):
        self.directory, self.map_input, self.source_sha = directory, map_input, source_sha
        self.calls = []
        self.errors = []
        self.fixture = shared.fixture_module()

    def respond(self, payload):
        shared.require(payload.get("model") == MODEL, "Unexpected mock model")
        replies = {}
        for item in payload["messages"]:
            if item.get("role") != "tool":
                continue
            envelope = json.loads(item["content"])
            shared.require(envelope["success"] is True, f"Production tool failed: {envelope}")
            shared.require(len(envelope["text"]) == 1, "Expected one structured tool result")
            replies[item["tool_call_id"]] = json.loads(envelope["text"][0])
        step = len(replies)

        def result(index):
            return replies[f"smoke-{index:03d}"]

        def call(name, args):
            self.calls.append({"step": step, "name": name, "arguments_sha256": shared.sha(
                json.dumps(args, sort_keys=True).encode())})
            return _tool(f"smoke-{step:03d}", name, args)

        def run(operation, **arguments):
            return call("reqpilot_run", {"operation": operation, "arguments": arguments})

        def candidate(name, value):
            return call("reqpilot_candidate", {"name": name, "value": value})

        if step == 0:
            reply = call("reqpilot_context", {})
        elif step == 1:
            source = result(0)["source_intake"]["arguments"]["source"]
            reply = call("reqpilot_read", {"path": source, "page": 1})
        elif step == 2:
            reply = run("capture", **result(0)["source_intake"]["arguments"])
        elif step == 3:
            reply = candidate("synthetic-map", deepcopy(self.map_input))
        elif step == 4:
            reply = run("compile-map", source=result(0)["source_intake"]["arguments"]["source"],
                source_ir="capture/source_ir.json", input=result(3)["path"], output_dir="map")
        elif step == 5:
            reply = call("reqpilot_read", {"path": "map/scope-requests.json", "json_pointer": ""})
        elif step == 6:
            reply = call("reqpilot_read", {"path": "map/document-map.json", "json_pointer": ""})
        elif step == 7:
            mapping = result(6)
            shared.require(mapping["source_sha256"] == self.source_sha, "Mock source drifted")
            projection = self.directory / "mock-input-projection"
            projection.mkdir(exist_ok=False)
            shared.save(projection / "document-map.json", mapping)
            shared.save(projection / "scope-requests.json", result(5))
            reply = candidate("synthetic-items", self.fixture.item_decisions(projection))
        elif step == 8:
            reply = run("compile-items", map_dir="map", input=result(7)["path"], output_dir="items")
        elif step == 9:
            reply = call("reqpilot_read", {"path": "items/source-requirement-baseline.json", "json_pointer": ""})
        elif step == 10:
            reply = run("review-open", source=result(0)["source_intake"]["arguments"]["source"], baseline_dir="items")
        elif (step == 18 and payload["messages"][-1]["role"] == "tool") or step == 27:
            self.calls.append({"step": step, "name": "finish"})
            reply = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "Synthetic draft completed through checked native tools."}}]}
        elif step == 18:
            reply = call("reqpilot_context", {})
        elif step == 19:
            current = result(18)["workspace_manifest"]["current"]["version_path"]
            reply = call("reqpilot_read", {"path": f"review/{current}/source-requirement-baseline.json", "json_pointer": ""})
        elif step in {11, 20}:
            first = step == 11
            baseline = result(9 if first else 19)
            head = result(10) if first else result(18)["workspace_manifest"]
            review_sha = head["review_sha256"]
            record = next(row for row in baseline["records"] if row["node_id"] == "s1")
            if first:
                shared.save(self.directory / "native-initial-baseline.json", baseline)
            reply = candidate("synthetic-review-initial" if first else "synthetic-review-continuation", {
                "schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": review_sha,
                "expected_baseline_sha256": baseline["artifact_sha256"], "model_id": MODEL,
                "decisions": [{"source_record_id": record["source_record_id"],
                    "requirementness": "REQUIREMENT", "requirement_kinds": ["CONSTRAINT", "QUALITY"] if first else ["QUALITY"],
                    "confidence": 0.9, "decision_summary": "Synthetic source-bound classification revision checks stable identity and publication.",
                    "reason_codes": ["synthetic_integration_revision"]}], "addressed_feedback_ids": []})
        elif step in {12, 21}:
            first = step == 12
            reply = run("review-revise", input=result(11 if first else 20)["path"],
                output_dir="review/versions/candidate-1" if first else "review/versions/candidate-2")
        elif step in {13, 22}:
            first = step == 13
            candidate_path = result(12 if first else 21)["candidate_path"]
            reply = run("deliver", baseline_dir="review/" + candidate_path,
                source=result(0)["source_intake"]["arguments"]["source"],
                output_dir="review/deliveries/final-1" if first else "review/deliveries/final-2")
        elif step in {14, 23}:
            first = step == 14
            compiled = result(12 if first else 21)
            reply = candidate("synthetic-close-1" if first else "synthetic-close-2", {
                "expected_review_sha256": compiled["review_sha256"], "candidate_path": compiled["candidate_path"],
                "delivery_path": "deliveries/final-1" if first else "deliveries/final-2", "resolutions": []})
        elif step in {15, 24}:
            reply = run("review-close", input=result(14 if step == 15 else 23)["path"])
        elif step in {16, 25}:
            reply = run("review-verify")
        elif step in {17, 26}:
            reply = call("reqpilot_publish", {"review_sha256": result(16 if step == 17 else 25)["review_sha256"]})
        else:
            raise RuntimeError(f"Unexpected synthetic tool step {step}")
        reply.update(id=f"synthetic-completion-{step}", created=0, object="chat.completion", model=MODEL)
        return reply


def start_mock(mock, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                shared.require(urlsplit(self.path).path == f"/openai/deployments/{MODEL}/chat/completions", "Unexpected provider HTTP route")
                shared.require(self.headers.get("api-key") == DUMMY_KEY, "Unexpected provider dummy key")
                length = int(self.headers.get("Content-Length") or "0")
                shared.require(0 < length < 16_000_000, "Provider payload size rejected")
                value = mock.respond(json.loads(self.rfile.read(length)))
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                mock.errors.append(f"{type(exc).__name__}: {exc}")
                body = json.dumps({"error": {"message": "Synthetic mock contract failed", "type": "smoke_contract"}}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_api(directory, port, env):
    with (directory / "api.log").open("ab") as log:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_serve", "--port", str(port)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def run(args):
    directory = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix="reqpilotx-native-provider-smoke-"))
    if args.output:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    database_port, api_port, mock_port = shared.unused_port(), shared.unused_port(), shared.unused_port()
    shared.require(len({database_port, api_port, mock_port}) == 3, "Random ports collided; rerun")
    database_url = f"postgresql://reqpilot_smoke@127.0.0.1:{database_port}/reqpilot_smoke"
    env = shared.isolate(directory, database_url)
    for key in tuple(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            env.pop(key)
    env.update({"REQPILOT_AGENT_RUNTIME": "provider-tool-loop", "REQPILOT_AGENT_PROVIDER": "azure",
        "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "1", "REQPILOT_AGENT_MODEL_ID": MODEL,
        "REQPILOT_NATIVE_CAPABILITY_MODE": "development-source", "REQPILOT_AZURE_AUTH_MODE": "api_key",
        "AZURE_OPENAI_ENDPOINT": f"http://127.0.0.1:{mock_port}", "AZURE_OPENAI_API_KEY": DUMMY_KEY,
        "AZURE_OPENAI_CHAT_DEPLOYMENT": MODEL, "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "AZURE_OPENAI_MAX_RETRIES": "0", "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1,localhost,::1"})
    audit_root = directory / "python-loopback-audit"
    audit_root.mkdir(mode=0o700)
    (audit_root / "sitecustomize.py").write_text(
        "import sys\n"
        "def _loopback_only(event, args):\n"
        "    if event == 'socket.connect':\n"
        "        address = args[1]\n"
        "        if not (isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}):\n"
        "            raise RuntimeError('Synthetic native smoke permits loopback connections only')\n"
        "sys.addaudithook(_loopback_only)\n", encoding="utf-8")
    env["PYTHONPATH"] = str(audit_root) + os.pathsep + env["PYTHONPATH"]
    os.environ.clear(); os.environ.update(env)
    sys.path[:0] = [str(shared.PLUGIN / "scripts"), str(ROOT / "packages/engine/src"),
                   str(ROOT / "apps/server/src"), str(ROOT / "prototype")]
    sys.addaudithook(shared.block_external_connections)
    import httpx
    from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
    from revision_tools import published_workbook_snapshot_sha256

    receipt = {"schema_version": "reqpilotx-real-pg-native-provider-smoke/v1", "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(), "output_directory": str(directory),
        "database_backend": "postgresql", "provider_sdk": "AzureOpenAI", "provider_http": "LOOPBACK_SYNTHETIC_MOCK",
        "credentials": "INVENTED_DUMMY_ONLY", "live_model_dispatch": "NOT_PERFORMED",
        "semantic_model_acceptance": "NOT_CLAIMED", "requests": [], "workers": []}
    process, mock_server, mock_thread, mock = None, None, None, None
    client = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=100, trust_env=False)

    def request(method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        receipt["requests"].append({"method": method, "path": path, "status_code": response.status_code,
            "response_sha256": shared.sha(response.content)})
        shared.require(response.status_code == expected, f"{method} {path}: {response.status_code} {response.text[:1800]}")
        return response

    def ready():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            shared.require(process.poll() is None, "Owned API exited; see api.log")
            try:
                if client.get("/api/projects").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Owned API did not become ready")

    def worker(job_id):
        worker_env = {**env, "REQPILOT_PROCESS_ROLE": "worker"}
        with (directory / "worker.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_worker", "--job-id", job_id],
                cwd=ROOT, env=worker_env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        observations = []
        try:
            deadline = time.monotonic() + 300
            while child.poll() is None:
                shared.require(time.monotonic() < deadline, "Owned scoped worker timed out")
                state = request("GET", task_path).json()
                shared.require(state["job_id"] == job_id, "Polled another native durable job")
                observations.append({"job_status": state["job_status"], "runtime_status": state["runtime_status"],
                    "progress_stage": state.get("progress", {}).get("stage")})
                time.sleep(1)
            exit_code = child.returncode
        finally:
            if child.poll() is None:
                shared.stop_server(child)
        receipt["workers"].append({"pid": child.pid, "job_id": job_id, "exit_code": exit_code,
            "http_poll_observations": observations})
        shared.require(exit_code == 0, "Owned scoped worker failed; see worker.log")

    def export(root, snapshot, filename):
        baseline_sha = snapshot["source_item_baseline"]["artifact_sha256"]
        response = request("GET", root + "/export", params={"expected_baseline_sha256": baseline_sha})
        shared.require(response.headers.get("x-reqpilotx-baseline-sha256") == baseline_sha, "Native Excel baseline header mismatch")
        shared.require(response.headers.get("x-reqpilotx-delivery-sha256") == snapshot["delivery_sha256"], "Native Excel delivery header mismatch")
        path = directory / filename; path.write_bytes(response.content)
        shared.require(published_workbook_snapshot_sha256(path) == canonical_json_sha256(snapshot), "Native Excel embedded snapshot mismatch")
        return {"path": str(path), "file_sha256": shared.sha(response.content), "baseline_sha256": baseline_sha,
                "delivery_sha256": snapshot["delivery_sha256"]}

    try:
        receipt["postgres"] = shared.start_postgres(directory, shared.postgres_tools(args.postgres_bin), database_port, env)
        fixture = shared.fixture_module()
        source, _, map_input = fixture.make_fixture(directory / "synthetic-input")
        source_sha = shared.sha(source.read_bytes())
        mock = NativeProviderMock(directory, map_input, source_sha)
        mock_server, mock_thread = start_mock(mock, mock_port)
        process = start_api(directory, api_port, env); ready()
        shared.require(request("GET", "/api/projects").json() == [], "Fresh owned PostgreSQL was not empty")
        import psycopg
        with psycopg.connect(database_url) as connection:
            migrations = connection.execute("SELECT version, name, sha256 FROM schema_migrations ORDER BY version").fetchall()
        expected_migrations = sorted((ROOT / "apps/server/src/reqpilotx_server/resources/migrations/postgresql").glob("*.sql"))
        shared.require(len(migrations) == len(expected_migrations) and len(migrations) > 0,
                       "Native API did not apply every PostgreSQL migration")
        receipt["migrations"] = [{"version": row[0], "name": row[1], "sha256": row[2]} for row in migrations]
        project = request("POST", "/api/projects", json={"name": "Synthetic native provider smoke", "customer_name": "Invented demonstration only"}).json()
        documents = f"/api/projects/{project['id']}/sor-documents"
        with source.open("rb") as stream:
            document = request("POST", documents, files={"file": (source.name, stream, "application/pdf")}).json()
        source_root = documents + "/" + document["id"]
        delivery = source_root + "/agent-structure/delivery"
        start_payload = {"command_id": "synthetic-native-cold-start", "expected_source_sha256": source_sha,
            "goal": "Complete the invented source draft and source-bound classification self-review through registered tools.",
            "origin": "USER_REQUEST", "analysis_mode": "SKIP", "require_map_confirmation": False}
        task = request("POST", delivery + "/extraction-loop", expected=202, json=start_payload).json()
        receipt.update(project_id=project["id"], document_id=document["id"], task_id=task["task_id"],
            source_sha256=source_sha, first_job_id=task["job_id"])
        repeated = request("POST", delivery + "/extraction-loop", expected=202, json=start_payload).json()
        shared.require(repeated["job_id"] == task["job_id"] and repeated["task_id"] == task["task_id"], "Native creation replay duplicated task/job")
        task_path = delivery + "/extraction-loop/" + task["task_id"]
        worker(task["job_id"])
        completed = request("GET", task_path).json()
        shared.require(completed["job_status"] == "SUCCEEDED" and completed["result"]["application_saved"] is True,
                       f"Native cold start did not publish: {completed}")
        snapshot = request("GET", delivery).json()["snapshot"]
        shared.require(snapshot["source_item_baseline"]["source_item_count"] == 8, "Expected 8 native SourceItems")
        initial_ids = [node["source_item"]["source_item_id"] for node in snapshot["nodes"] if node.get("source_item")]
        initial_baseline = shared.read(directory / "native-initial-baseline.json")
        shared.require(initial_ids == [row["source_record_id"] for row in initial_baseline["records"]],
                       "Native self-review changed stable source identities or ordering")
        initial_display_ids = [node["source_item"]["source_display_id"] for node in snapshot["nodes"] if node.get("source_item")]
        shared.require(initial_display_ids == [row["source_display_id"] for row in initial_baseline["records"]],
                       "Native self-review changed stable displayed source numbers")
        from app.infrastructure.runtime_settings import RuntimeSettings
        object_root = RuntimeSettings.from_environment().object_root
        pointer = completed["result"]["native_source_graph_artifact"]
        graph_path = object_root / pointer["object_key"]
        shared.require(graph_path.resolve().is_relative_to(object_root.resolve())
            and shared.sha(graph_path.read_bytes()) == pointer["object_sha256"], "Native published graph object mismatch")
        graph = shared.read(graph_path)
        page_returns = graph["host_verification"]["source_page_returns"]
        shared.require(len(page_returns) == 1 and page_returns[0]["page"] == 1
            and page_returns[0]["source_sha256"] == source_sha
            and page_returns[0]["evidence_kind"] == "TOOL_IMAGE_RETURNED", "Native original page-read receipt missing")
        receipt["source_page_read_receipts"] = page_returns
        initial_export = export(delivery, snapshot, "native-before-restart.xlsx")
        old_pid = process.pid; shared.stop_server(process)
        process = start_api(directory, api_port, env); ready()
        initial_restart_pid = process.pid
        shared.require(request("GET", task_path).json() == completed, "Native task changed after API restart")
        shared.require(request("GET", delivery).json()["snapshot"] == snapshot, "Native snapshot changed after API restart")
        shared.require(shared.sha(request("GET", source_root + "/file").content) == source_sha, "Uploaded source changed after restart")
        restarted_export = export(delivery, snapshot, "native-after-restart.xlsx")
        shared.save(directory / "native-first-task.json", completed)
        shared.save(directory / "native-first-snapshot.json", snapshot)
        resume_payload = {"command_id": "synthetic-native-exact-continuation",
            "goal": "Continue this same synthetic task and revise the first target back to its QUALITY classification; preserve all source identities.",
            "origin": "USER_REQUEST", "source_item_ids": [initial_ids[0]]}
        resumed = request("POST", task_path + "/resume", expected=202, json=resume_payload).json()
        resume_replay = request("POST", task_path + "/resume", expected=202, json=resume_payload).json()
        shared.require(resumed["task_id"] == task["task_id"] and resumed["job_id"] != task["job_id"]
            and resumed["job_id"] == resume_replay["job_id"], "Exact native continuation identity/replay mismatch")
        worker(resumed["job_id"])
        continued = request("GET", task_path).json()
        shared.require(continued["job_status"] == "SUCCEEDED" and continued["result"]["application_saved"] is True,
                       f"Native continuation did not publish: {continued}")
        shared.require(continued["thread_id"] == completed["thread_id"] and continued["turn_id"] != completed["turn_id"]
            and continued["result"]["runtime_receipt"]["resumed"] is True
            and continued["plugin_bundle_sha256"] == completed["plugin_bundle_sha256"], "Native runtime lost its pinned thread/capability")
        next_snapshot = request("GET", delivery).json()["snapshot"]
        next_ids = [node["source_item"]["source_item_id"] for node in next_snapshot["nodes"] if node.get("source_item")]
        next_display_ids = [node["source_item"]["source_display_id"] for node in next_snapshot["nodes"] if node.get("source_item")]
        shared.require(next_ids == initial_ids and next_snapshot["source_item_baseline"]["revision"] == snapshot["source_item_baseline"]["revision"] + 1,
                       "Native continuation changed IDs or skipped a baseline revision")
        shared.require(next_display_ids == initial_display_ids, "Native continuation changed displayed source numbers")
        shared.require(next_snapshot["source_item_baseline"]["source_sha256"] == source_sha
            and next_snapshot["source_item_baseline"]["source_item_count"] == 8, "Native continuation changed source/count")
        continuation_export = export(delivery, next_snapshot, "native-continuation.xlsx")
        shared.save(directory / "native-continued-task.json", continued)
        shared.save(directory / "native-continued-snapshot.json", next_snapshot)

        opinions = request("GET", delivery + "/engineer-responses").json()
        decisions = [{"unit_id": unit_id, "expected_unit_context_sha256": unit["unit_context_sha256"],
            "decision": "COMPLIANT", "reason": "Invented synthetic human-response integration test; no customer approval."}
            for unit_id, unit in opinions["units"].items()]
        opinion_payload = {"command_id": "synthetic-native-opinions", "expected_source_sha256": source_sha,
            "expected_baseline_sha256": opinions["baseline_sha256"], "expected_response_revision": opinions["response_revision"],
            "decisions": decisions}
        saved_opinions = request("POST", delivery + "/engineer-responses", json=opinion_payload).json()
        shared.require(request("POST", delivery + "/engineer-responses", json=opinion_payload).json() == saved_opinions,
                       "Native human-response retry duplicated or changed opinions")
        shared.require(saved_opinions["response_revision"] == opinions["response_revision"] + 1
            and len(saved_opinions["records"]) == len(decisions), "Native synthetic opinions not saved")
        preview = request("GET", delivery + "/versions/preview").json()
        shared.require(preview["readiness"]["can_lock"] is True, f"Native formal delivery not ready: {preview}")
        freeze_payload = {"command_id": "synthetic-native-formal-freeze", "expected_source_sha256": source_sha,
            "expected_delivery_sha256": preview["delivery_sha256"], "expected_business_basis_sha256": preview["business_basis_sha256"],
            "mode": "FORMAL", "accept_snapshot": False}
        request("POST", delivery + "/versions", expected=409, json=freeze_payload)
        freeze_payload["accept_snapshot"] = True
        frozen = request("POST", delivery + "/versions", json=freeze_payload).json()
        shared.require(request("POST", delivery + "/versions", json=freeze_payload).json() == frozen,
                       "Native formal freeze replay duplicated delivery")
        frozen_path = delivery + "/versions/" + str(frozen["revision"])
        frozen_view = request("GET", frozen_path).json()
        frozen_excel = request("GET", frozen_path + "/export")
        frozen_file = directory / "native-formal.xlsx"; frozen_file.write_bytes(frozen_excel.content)
        shared.require(shared.sha(frozen_excel.content) == frozen["export_sha256"]
            and published_workbook_snapshot_sha256(frozen_file) == canonical_json_sha256(frozen_view["snapshot"]),
            "Native frozen Excel hash/snapshot mismatch")
        shared.require(frozen["mode"] == "FORMAL" and frozen["response_revision"] == saved_opinions["response_revision"],
                       "Native formal delivery lost exact human responses")
        withdrawal_payload = {"command_id": "synthetic-native-withdraw", "expected_source_sha256": source_sha,
            "expected_baseline_sha256": saved_opinions["baseline_sha256"], "expected_response_revision": saved_opinions["response_revision"],
            "decisions": [{"unit_id": decisions[0]["unit_id"], "expected_unit_context_sha256": decisions[0]["expected_unit_context_sha256"],
                           "action": "WITHDRAW"}]}
        withdrawn = request("POST", delivery + "/engineer-responses", json=withdrawal_payload).json()
        shared.require(withdrawn["response_revision"] == saved_opinions["response_revision"] + 1, "Native opinion withdrawal did not advance")
        shared.require(request("GET", delivery + "/versions/preview").json()["readiness"]["can_lock"] is False,
                       "Native withdrawal did not invalidate current formal readiness")
        shared.require(request("GET", frozen_path).json() == frozen_view
            and request("GET", frozen_path + "/export").content == frozen_excel.content,
            "Native withdrawal altered frozen history")
        old_final_pid = process.pid; shared.stop_server(process)
        process = start_api(directory, api_port, env); ready()
        shared.require(request("GET", task_path).json() == continued
            and request("GET", delivery + "/engineer-responses").json() == withdrawn
            and request("GET", frozen_path).json() == frozen_view
            and request("GET", frozen_path + "/export").content == frozen_excel.content,
            "Native continuation/opinions/formal delivery changed after final restart")
        receipt.update(continuation={"job_id": resumed["job_id"], "same_thread": True, "same_capability": True,
            "source_ids_preserved": True, "baseline_revision": next_snapshot["source_item_baseline"]["revision"]},
            human_response={"synthetic": True, "unit_count": len(decisions), "saved_revision": saved_opinions["response_revision"],
                            "withdrawn_revision": withdrawn["response_revision"]},
            formal_delivery={"revision": frozen["revision"], "export_sha256": frozen["export_sha256"],
                "delivery_sha256": frozen["delivery_sha256"], "path": str(frozen_file), "unchanged_after_withdrawal_and_restart": True},
            final_restart={"old_api_pid": old_final_pid, "new_api_pid": process.pid, "persisted_state_unchanged": True})
        receipt.update(project_id=project["id"], document_id=document["id"], task_id=task["task_id"],
            source_sha256=source_sha, first_job_id=task["job_id"], exports=[initial_export, restarted_export, continuation_export],
            restart={"old_api_pid": old_pid, "new_api_pid": initial_restart_pid, "task_and_snapshot_unchanged": True})
        receipt["status"] = "PASS"
        receipt["limits"] = ["Deterministic synthetic tool choices, not provider authentication or semantic acceptance",
            "Local owned PostgreSQL, not RDS/OSS or deployment failover", "No rendered browser/employee UI validation"]
    except Exception as exc:
        receipt.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
    finally:
        if mock is not None:
            receipt.update(provider_mock_calls=mock.calls, provider_mock_errors=mock.errors)
        client.close()
        if process is not None:
            try:
                shared.stop_server(process)
            except Exception as exc:
                receipt.update(status="FAIL", api_shutdown_error=str(exc))
        if mock_server is not None:
            mock_server.shutdown(); mock_server.server_close(); mock_thread.join(timeout=5)
        try:
            shared.stop_postgres(directory, env)
        except Exception as exc:
            receipt.update(status="FAIL", postgres_shutdown_error=str(exc))
        receipt["process_cleanup"] = {"api_stopped": process is None or process.poll() is not None,
            "scoped_workers_stopped": all(item["exit_code"] == 0 for item in receipt["workers"]),
            "provider_mock_stopped": mock_thread is None or not mock_thread.is_alive(),
            "postgres_stopped": not (directory / "postgres-data/postmaster.pid").exists()}
        if not all(receipt["process_cleanup"].values()):
            receipt.update(status="FAIL", cleanup_error="An owned smoke process did not stop")
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        shared.save(directory / "receipt.json", receipt)
        print(json.dumps({key: receipt.get(key) for key in ("status", "output_directory", "task_id", "first_job_id", "error")}, indent=2), flush=True)
    return 0 if receipt["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--postgres-bin", type=Path)
    parser.add_argument("--_serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--job-id", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._serve or args._worker:
        shared.require(os.environ.get("REQPILOT_ENV") == "local" and os.environ.get("REQPILOT_AGENT_RUNTIME") == "provider-tool-loop"
            and os.environ.get("AZURE_OPENAI_API_KEY") == DUMMY_KEY and "reqpilot_smoke@127.0.0.1:" in os.environ.get("REQPILOT_DATABASE_URL", ""),
            "Internal service requires isolated local PostgreSQL and synthetic provider configuration")
        sys.addaudithook(shared.block_external_connections)
        if args._serve:
            import uvicorn
            uvicorn.run("reqpilotx_server.main:app", host="127.0.0.1", port=args.port, log_level="info")
            return 0
        from reqpilotx_server.worker.__main__ import main as worker_main
        shared.require(bool(args.job_id), "Scoped worker requires job_id")
        sys.argv = [sys.argv[0], "--once", "--job-id", args.job_id]
        return worker_main()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
