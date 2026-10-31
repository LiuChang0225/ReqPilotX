"""Independent account/ownership acceptance with actual isolated SQLite + ASGI.

No provider, OSS, ECS, PostgreSQL or customer-corpus acceptance is claimed here.
The account endpoints, scrypt, cookies, CSRF, ownership SQL and upload bytes are real.
"""
from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from reqpilotx_server import db
from reqpilotx_server.api.app_factory import create_app
from reqpilotx_server.application import runtime
from reqpilotx_server.saas import store
from reqpilotx_server.saas.authorization import authorize, bind_job_payload, durable_job_scope
from reqpilotx_server.saas.config import SaaSSettings
from reqpilotx_server.saas.context import account_scope, current_scope
from reqpilotx_server.ports.job_queue import NonRetryableJobError

ORIGIN = "https://127.0.0.1"
INVITATION = "test-only-invitation-never-a-live-secret-0000"
PASSWORD = "test-only-password-never-a-live-secret"


class _DeterministicRawAgent:
    """Synthetic model decisions from actual tool returns, through a real SDK.

    This never writes files or final snapshots directly. Registered host tools
    capture the uploaded source, compile decisions, revise, export and publish.
    """
    def __init__(self):
        self.phase = "initial"
        self.cursor = 0
        self.results = {}
        self.calls = []
        self.errors = []
        self.interrupt_once = None
        self.source = None

    def begin_review(self):
        self.phase = "review"
        self.cursor = 0
        self.results = {}

    def _map(self):
        ir = self.results["ir"]
        text_nodes = [n for n in ir["nodes"] if n.get("raw_text")]
        title = next(n for n in text_nodes if n["raw_text"] == "Controller specification")
        heading = next(n for n in text_nodes if n["raw_text"] == "1 Functional requirements")
        body = [n["source_node_id"] for n in text_nodes if n["source_node_id"] not in {title["source_node_id"], heading["source_node_id"]}]
        return {"schema_version": "reqpilot-map-decisions/v1", "source_sha256": ir["source_sha256"],
            "nodes": [{"node_id": "main", "parent_id": None, "kind": "DOCUMENT", "title": title["raw_text"], "label": "",
                       "title_source_ids": [title["source_node_id"]], "content_source_ids": []},
                      {"node_id": "requirements", "parent_id": "main", "kind": "SECTION", "title": "Functional requirements", "label": "1",
                       "title_source_ids": [heading["source_node_id"]], "content_source_ids": body}],
            "logical_tables": [], "source_dispositions": []}

    def _items(self):
        mapping, scopes = self.results["map"], self.results["scopes"]
        decisions = {"schema_version": "reqpilot-item-decisions/v1", "source_sha256": mapping["source_sha256"],
                     "document_map_artifact_sha256": mapping["artifact_sha256"], "text_decisions": [], "table_decisions": []}
        for request in scopes["text_requests"]:
            items, structural = [], []
            for index, content in enumerate(request["source_content"]):
                span = {"content_id": content["content_id"], "start_offset": 0, "end_offset": len(content["text"])}
                if content["is_title"]:
                    structural.append({"text_span": span, "reason_codes": ["source_heading"]})
                else:
                    items.append({"item_key": "body-" + str(index), "label": content["text"], "source_form": "NUMBERED_CLAUSE", "text_spans": [span],
                                  "requirementness": "REQUIREMENT", "requirement_kinds": ["FUNCTIONAL"], "confidence": 0.9,
                                  "decision_summary": "Synthetic model keeps each complete original obligation.", "reason_codes": ["source_meaning"]})
            decisions["text_decisions"].append({"scope_id": request["scope_id"], "items": items, "structure_only_spans": structural})
        return decisions

    def _revision(self):
        head = self.results["feedback"] if self.phase == "initial" else self.results["head"]
        baseline = self.results["baseline"]
        feedback_ids = head["added_feedback_ids"] if self.phase == "initial" else [f["feedback_id"] for f in head["feedback"] if f["status"] in {"OPEN", "UNRESOLVED"}]
        return {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"],
                "expected_baseline_sha256": baseline["artifact_sha256"], "model_id": "test-provider-model",
                "addressed_feedback_ids": feedback_ids, "decisions": [{"source_record_id": baseline["records"][0]["source_record_id"],
                "requirementness": "REQUIREMENT", "requirement_kinds": ["FUNCTIONAL" if self.phase == "initial" else "QUALITY"],
                "confidence": 0.95, "decision_summary": "Synthetic self-review preserves complete source." if self.phase == "initial" else "Synthetic user feedback requests quality review classification.",
                "reason_codes": ["source_meaning"]}]}

    def _close(self):
        head = self.results["feedback"] if self.phase == "initial" else self.results["head"]
        ids = head["added_feedback_ids"] if self.phase == "initial" else [f["feedback_id"] for f in head["feedback"] if f["status"] in {"OPEN", "UNRESOLVED"}]
        return {"expected_review_sha256": head["review_sha256"], "candidate_path": self.results["revised"]["candidate_path"],
                "delivery_path": "delivery-" + self.phase, "resolutions": [{"feedback_id": value, "disposition": "ADDRESSED",
                "reason": "Registered compiler applied this source-bound synthetic review."} for value in ids]}

    def _steps(self):
        def run(operation, arguments): return ("reqpilot_run", {"operation": operation, "arguments": arguments})
        def read(path): return ("reqpilot_read", {"path": path, "json_pointer": ""})
        def candidate(name, value): return ("reqpilot_candidate", {"name": name + "-" + self.phase, "value": value})
        if self.phase == "initial":
            steps = [
                ("context", lambda: ("reqpilot_context", {})),
                ("page", lambda: ("reqpilot_read", {"path": self.results["context"]["source_intake"]["arguments"]["source"], "page": 1})),
                ("capture", lambda: run("capture", self.results["context"]["source_intake"]["arguments"])),
                ("ir", lambda: read("capture/source_ir.json")),
                ("map_candidate", lambda: candidate("map", self._map())),
                ("mapped", lambda: run("compile-map", {"source": self.source, "source_ir": "capture/source_ir.json", "input": self.results["map_candidate"]["path"], "output_dir": "map"})),
                ("map", lambda: read("map/document-map.json")),
                ("scopes", lambda: read("map/scope-requests.json")),
                ("item_candidate", lambda: candidate("items", self._items())),
                ("items", lambda: run("compile-items", {"map_dir": "map", "input": self.results["item_candidate"]["path"], "output_dir": "items"})),
                ("opened", lambda: run("review-open", {"source": self.source, "baseline_dir": "items"})),
                ("baseline", lambda: read("review/" + self.results["opened"]["current_version_path"] + "/source-requirement-baseline.json")),
                ("delivery", lambda: run("deliver", {"source": self.source, "baseline_dir": "items", "output_dir": "review/delivery-initial"})),
                ("attached", lambda: run("review-attach-delivery", {"delivery_dir": "review/delivery-initial", "expected_review_sha256": self.results["opened"]["review_sha256"]})),
                ("verified", lambda: run("review-verify", {})),
                ("published", lambda: ("reqpilot_publish", {"review_sha256": self.results["verified"]["review_sha256"]})),
            ]
            return steps
        else:
            steps = [("context", lambda: ("reqpilot_context", {})), ("head", lambda: read("review/workspace-manifest.json")),
                     ("baseline", lambda: read("review/" + self.results["head"]["current"]["version_path"] + "/source-requirement-baseline.json"))]
        return steps + [
            ("revision_candidate", lambda: candidate("revision", self._revision())),
            ("revised", lambda: run("review-revise", {"input": self.results["revision_candidate"]["path"], "output_dir": "review/revised-" + self.phase})),
            ("delivery", lambda: run("deliver", {"source": self.source, "baseline_dir": "review/" + self.results["revised"]["candidate_path"], "output_dir": "review/delivery-" + self.phase})),
            ("close_candidate", lambda: candidate("close", self._close())),
            ("closed", lambda: run("review-close", {"input": self.results["close_candidate"]["path"]})),
            ("verified", lambda: run("review-verify", {})),
            ("published", lambda: ("reqpilot_publish", {"review_sha256": self.results["verified"]["review_sha256"]})),
        ]

    def respond(self, request):
        import httpx
        packet = json.loads(request.content)
        self.calls.append(packet)
        steps = self._steps()
        for message in packet["messages"]:
            if message.get("role") != "tool" or not str(message.get("tool_call_id", "")).startswith("synthetic-" + self.phase + "-"):
                continue
            index = int(message["tool_call_id"].rsplit("-", 1)[1])
            wrapper = json.loads(message["content"])
            if not wrapper["success"]:
                self.errors.append(wrapper["text"])
                return httpx.Response(200, json={"id": "synthetic-error", "object": "chat.completion", "created": 0, "model": "test-provider-model",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Synthetic tool chain rejected an input"}}]})
            value = json.loads(wrapper["text"][0])
            self.results[steps[index][0]] = value
        if "context" in self.results and self.phase == "initial":
            self.source = self.results["context"]["source_intake"]["arguments"]["source"]
        if self.cursor == len(steps):
            message, finish = {"role": "assistant", "content": "Deterministic synthetic SDK task completed with registered tool receipts"}, "stop"
        else:
            name, args = steps[self.cursor][1]()
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "synthetic-" + self.phase + "-" + str(self.cursor), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            self.cursor += 1; finish = "tool_calls"
        if self.interrupt_once:
            callback, self.interrupt_once = self.interrupt_once, None
            callback()
        return httpx.Response(200, json={"id": "synthetic-" + str(len(self.calls)), "object": "chat.completion", "created": 0,
            "model": "test-provider-model", "choices": [{"index": 0, "finish_reason": finish, "message": message}]})


class SaaSAcceptanceTests(unittest.TestCase):
    USE_POSTGRES = False

    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(TemporaryDirectory(prefix="saas-independent-")))
        cls.stack.enter_context(patch.object(db, "STORAGE_DIR", cls.root))
        cls.stack.enter_context(patch.object(db, "DB_PATH", cls.root / "business.sqlite"))
        cls.stack.enter_context(patch.object(runtime, "UPLOADS_DIR", cls.root / "uploads"))
        if cls.USE_POSTGRES:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import conninfo_to_dict, make_conninfo
            from reqpilotx_server.infrastructure.postgres import PostgresDatabase
            from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
            from reqpilotx_server.adapters.storage.local_object_store import LocalObjectStore
            from reqpilotx_server.application.storage.runtime_files import RuntimeObjectFiles
            params = conninfo_to_dict(os.environ["REQPILOT_SAAS_POSTGRES_URL"])
            host = params.get("host", "")
            if host not in {"", "localhost", "127.0.0.1", "::1"} and not host.startswith("/"):
                raise ValueError("Only a disposable local PostgreSQL target is allowed")
            schema = "saas_acceptance_" + uuid4().hex
            admin = cls.stack.enter_context(psycopg.connect(make_conninfo(**params), autocommit=True))
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cls.stack.callback(lambda: admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))))
            dsn = make_conninfo(**{**params, "options": "-c search_path=" + schema})
            cls.database = PostgresDatabase(dsn, min_pool_size=1, max_pool_size=6)
            cls.stack.callback(cls.database.close)
            settings = replace(runtime.RUNTIME_SETTINGS, database_backend=DatabaseBackend.POSTGRESQL, database_url=dsn)
            cls.stack.enter_context(patch.object(db, "_RUNTIME_SETTINGS", settings))
            cls.stack.enter_context(patch.object(db, "_POSTGRES_DATABASE", cls.database))
            cls.stack.enter_context(patch.object(runtime, "RUNTIME_SETTINGS", settings))
            objects = LocalObjectStore(cls.root / "source-objects")
            cls.stack.enter_context(patch.object(runtime.RUNTIME_STATE, "container", SimpleNamespace(object_store=objects, settings=settings)))
            cls.stack.enter_context(patch.object(runtime.RUNTIME_STATE, "files", RuntimeObjectFiles(objects, cls.root / "materialized", allow_legacy_paths=False)))
        db.init_db()
        cls.legacy = db.create_project("Legacy private data", "Synthetic")
        cls.stack.enter_context(patch.dict(os.environ, {
            "REQPILOT_SAAS_ENABLED": "1", "REQPILOT_SAAS_ORIGIN": ORIGIN,
            "REQPILOT_SAAS_INVITE_CODE": INVITATION,
        }))
        store.ensure_schema()
        cls.app = create_app()
        cls.accounts = []
        for email in ("alice@example.invalid", "bob@example.invalid"):
            invitation = INVITATION + "-" + uuid4().hex
            store.add_invitation(invitation)
            client = TestClient(cls.app, base_url=ORIGIN)
            reply = client.post("/api/auth/register", headers={"Origin": ORIGIN},
                                json={"email": email, "password": PASSWORD, "invitation": invitation})
            if reply.status_code != 201:
                raise AssertionError(f"Real registration failed: {reply.status_code} {reply.text}")
            cls.accounts.append(reply.json()["user"])

    def setUp(self):
        # Each case gets a new server-issued opaque session and fresh owned rows.
        with db.get_conn() as conn:
            conn.execute("DELETE FROM saas_auth_attempts")
        self.clients, self.csrf = [], []
        for user in self.accounts:
            token, csrf = store.issue_session(user["id"], 3600)
            client = TestClient(self.app, base_url=ORIGIN)
            client.cookies.set(self.app.state.saas_settings.cookie_name, token, domain="127.0.0.1", path="/")
            self.clients.append(client); self.csrf.append(csrf)
        self.a, self.b = self.clients
        self.projects = [self.create_project(index)["id"] for index in (0, 1)]
        self.documents = [self.upload(index).json()["id"] for index in (0, 1)]

    def headers(self, index=0):
        return {"Origin": ORIGIN, "X-ReqPilot-CSRF": self.csrf[index]}

    def create_project(self, index=0, command=None, name="Synthetic project"):
        reply = self.clients[index].post("/api/projects", headers=self.headers(index), json={
            "name": name, "customer_name": "Synthetic", "command_id": command or uuid4().hex,
        })
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    def upload(self, index=0, command=None, data=b"The synthetic controller shall start.\n", project=None):
        return self.clients[index].post(f"/api/projects/{project or self.projects[index]}/sor-documents",
            headers=self.headers(index), data={"command_id": command or uuid4().hex},
            files={"file": ("synthetic.txt", data, "text/plain")})

    def document_path(self, index=0):
        return f"/api/projects/{self.projects[index]}/sor-documents/{self.documents[index]}"

    def test_registration_session_and_stored_password_are_real_and_redacted(self):
        client = TestClient(self.app, base_url=ORIGIN)
        email = f"new-{uuid4().hex}@example.invalid"
        invitation = INVITATION + "-" + uuid4().hex
        store.add_invitation(invitation)
        reply = client.post("/api/auth/register", headers={"Origin": ORIGIN},
                            json={"email": email, "password": PASSWORD, "invitation": invitation})
        self.assertEqual(reply.status_code, 201)
        value = reply.json(); self.assertEqual(value["user"]["email"], email)
        cookie = reply.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "__Host-"):
            self.assertIn(flag, cookie)
        self.assertEqual(reply.headers.get("cache-control"), "no-store")
        with db.get_conn() as conn:
            account = conn.execute("SELECT * FROM saas_accounts WHERE id=?", (value["user"]["id"],)).fetchone()
            session = conn.execute("SELECT * FROM saas_sessions WHERE account_id=?", (value["user"]["id"],)).fetchone()
        self.assertTrue(account["password_digest"].startswith("scrypt:131072:8:1:"))
        self.assertNotIn(PASSWORD, str(dict(account)))
        raw_cookie = client.cookies.get(self.app.state.saas_settings.cookie_name)
        self.assertEqual(session["token_sha256"], store.token_hash(raw_cookie))
        self.assertNotEqual(raw_cookie, session["token_sha256"])
        self.assertNotIn("password_digest", json.dumps(value))

    def test_registration_requires_invitation_same_origin_and_server_owned_actor(self):
        client = TestClient(self.app, base_url=ORIGIN)
        payload = {"email": "rejected@example.invalid", "password": PASSWORD, "invitation": INVITATION}
        self.assertEqual(client.post("/api/auth/register", json=payload).status_code, 403)
        self.assertEqual(client.post("/api/auth/register", headers={"Origin": "https://evil.example.invalid"}, json=payload).status_code, 403)
        self.assertEqual(client.post("/api/auth/register", headers={"Origin": ORIGIN}, json={**payload, "invitation": "wrong-test-invitation-00000000000000"}).status_code, 403)
        self.assertEqual(client.post("/api/auth/register", headers={"Origin": ORIGIN}, json={**payload, "account_id": self.accounts[0]["id"]}).status_code, 422)

    def test_login_checks_actual_password_and_rotates_previous_session(self):
        old_cookie = self.a.cookies.get(self.app.state.saas_settings.cookie_name)
        reply = self.a.post("/api/auth/login", headers={"Origin": ORIGIN},
                            json={"email": self.accounts[0]["email"], "password": PASSWORD})
        self.assertEqual(reply.status_code, 200)
        self.assertNotEqual(self.a.cookies.get(self.app.state.saas_settings.cookie_name), old_cookie)
        self.assertIsNone(store.read_session(old_cookie))
        bad = TestClient(self.app, base_url=ORIGIN).post("/api/auth/login", headers={"Origin": ORIGIN},
            json={"email": self.accounts[0]["email"], "password": "wrong-but-long-test-password"})
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn(PASSWORD, bad.text)

    def test_single_use_invitation_has_exactly_one_concurrent_account_winner(self):
        invitation = INVITATION + "-" + uuid4().hex
        store.add_invitation(invitation)
        barrier = Barrier(2)
        emails = [f"invite-race-{uuid4().hex}@example.invalid" for _ in range(2)]
        def register(email):
            client = TestClient(self.app, base_url=ORIGIN)
            barrier.wait()
            return client.post("/api/auth/register", headers={"Origin": ORIGIN},
                               json={"email": email, "password": PASSWORD, "invitation": invitation})
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(register, emails))
        self.assertEqual(sorted(r.status_code for r in replies), [201, 403])
        with db.get_conn() as conn:
            rows = conn.execute("SELECT id FROM saas_accounts WHERE email IN (?,?)", tuple(emails)).fetchall()
            claimed = conn.execute("SELECT used_account_id FROM saas_invitations WHERE token_sha256=?", (store.token_hash(invitation),)).fetchone()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], claimed["used_account_id"])
        self.assertFalse(store.invitation_valid(invitation))

    def test_consumed_and_expired_invites_never_reopen_on_restart_or_reseed(self):
        invitation = INVITATION + "-" + uuid4().hex
        store.add_invitation(invitation)
        user = store.create_account(f"single-use-{uuid4().hex}@example.invalid", PASSWORD, invitation)
        self.assertTrue(user["id"])
        with patch.dict(os.environ, {"REQPILOT_SAAS_INVITE_CODE": invitation}):
            store.ensure_schema(); store.add_invitation(invitation)
        self.assertFalse(store.invitation_valid(invitation))
        expired = INVITATION + "-" + uuid4().hex
        store.add_invitation(expired)
        with db.get_conn() as conn:
            conn.execute("UPDATE saas_invitations SET expires_at=0 WHERE token_sha256=?", (store.token_hash(expired),))
        with patch.dict(os.environ, {"REQPILOT_SAAS_INVITE_CODE": expired}):
            store.ensure_schema(); store.add_invitation(expired)
        self.assertFalse(store.invitation_valid(expired))

    def test_anonymous_and_forged_identity_headers_cannot_enter_business_api(self):
        guest = TestClient(self.app, base_url=ORIGIN)
        for path in ("/api/projects", "/api/reading/projects", self.document_path() + "/file"):
            self.assertEqual(guest.get(path, headers={"X-Account-Id": self.accounts[0]["id"], "X-Tenant-Id": self.accounts[0]["id"]}).status_code, 401)
        self.assertEqual(guest.get("/api/auth/session").json(), {"enabled": True, "user": None, "csrf_token": None})

    def test_catalogs_filter_both_accounts_and_never_adopt_legacy_projects(self):
        a_ids = {p["id"] for p in self.a.get("/api/projects").json()}
        b_ids = {p["id"] for p in self.b.get("/api/projects").json()}
        self.assertIn(self.projects[0], a_ids); self.assertNotIn(self.projects[1], a_ids)
        self.assertIn(self.projects[1], b_ids); self.assertNotIn(self.projects[0], b_ids)
        self.assertNotIn(self.legacy["id"], a_ids | b_ids)
        read_ids = {p["id"] for p in self.a.get("/api/reading/projects").json()["items"]}
        self.assertIn(self.projects[0], read_ids); self.assertNotIn(self.projects[1], read_ids)
        self.assertEqual(self.a.get(f"/api/reading/projects/{self.projects[1]}/documents").status_code, 404)

    def test_two_accounts_cannot_read_upload_remove_or_rebind_each_others_sources(self):
        for path in (self.document_path(1) + "/file", self.document_path(1) + "/extraction/structure",
                     self.document_path(1) + "/agent-structure/delivery/versions",
                     f"/api/projects/{self.projects[1]}/sor-documents"):
            self.assertEqual(self.a.get(path).status_code, 404, path)
        self.assertEqual(self.upload(project=self.projects[1]).status_code, 404)
        self.assertEqual(self.a.delete(self.document_path(1), headers=self.headers()).status_code, 404)
        nested = f"/api/projects/{self.projects[0]}/sor-documents/{self.documents[1]}/file"
        self.assertEqual(self.a.get(nested).status_code, 404)
        self.assertEqual(self.b.get(self.document_path(1) + "/file").content, b"The synthetic controller shall start.\n")

    def test_csrf_requires_matching_session_and_origin_before_any_mutation(self):
        payload = {"name": "Rejected", "customer_name": "Synthetic", "command_id": uuid4().hex}
        self.assertEqual(self.a.post("/api/projects", json=payload).status_code, 403)
        self.assertEqual(self.a.post("/api/projects", headers={"Origin": ORIGIN}, json=payload).status_code, 403)
        self.assertEqual(self.a.post("/api/projects", headers={"Origin": ORIGIN, "X-ReqPilot-CSRF": self.csrf[1]}, json=payload).status_code, 403)
        self.assertEqual(self.a.post("/api/projects", headers={"X-ReqPilot-CSRF": self.csrf[0]}, json=payload).status_code, 403)
        self.assertEqual(self.a.post("/api/projects", headers={**self.headers(), "Origin": "https://evil.example.invalid"}, json=payload).status_code, 403)

    def test_creation_command_ids_are_private_and_replay_survives_app_recreation(self):
        command = uuid4().hex
        a = self.create_project(command=command)
        b = self.create_project(index=1, command=command, name="Bob's distinct project")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.a.get(f"/api/projects/creation-commands/{command}").json()["id"], a["id"])
        other_command = uuid4().hex; private = self.create_project(command=other_command)
        self.assertEqual(self.b.get(f"/api/projects/creation-commands/{other_command}").status_code, 404)
        fresh = TestClient(create_app(), base_url=ORIGIN); fresh.cookies.update(self.a.cookies)
        self.assertEqual(fresh.get("/api/auth/session").json()["user"], self.accounts[0])
        reply = fresh.post("/api/projects", headers=self.headers(), json={"name": "Synthetic project", "customer_name": "Synthetic", "command_id": other_command})
        self.assertEqual(reply.json()["id"], private["id"])

    def test_upload_replay_conflict_restart_and_cross_account_recovery(self):
        command = uuid4().hex; first = self.upload(command=command)
        self.assertEqual(first.status_code, 200)
        second = self.upload(command=command); self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(self.upload(command=command, data=b"Changed source").status_code, 409)
        fresh = TestClient(create_app(), base_url=ORIGIN); fresh.cookies.update(self.a.cookies)
        recovery = f"/api/projects/{self.projects[0]}/sor-documents/import-commands/{command}"
        self.assertEqual(fresh.get(recovery).json()["id"], first.json()["id"])
        self.assertEqual(self.b.get(recovery).status_code, 404)

    def test_legacy_global_surfaces_runtime_mutations_and_plugin_writes_are_denied(self):
        for method, path in (("GET", "/api/exports/file.xlsx"), ("POST", "/api/exports/file.xlsx/reveal"),
            ("GET", "/api/evidences/evidence/context"), ("GET", "/api/evidences/evidence/file"),
            ("GET", "/api/requirements/requirement/review-logs"), ("POST", "/api/runtime/ai-provider-setup"),
            ("POST", "/api/runtime/ai-provider-setup/aliyun-probe"), ("POST", "/api/artifact-smoke/shutdown"),
            ("POST", self.document_path() + "/agent-structure/delivery/plugin-workbench")):
            reply = self.a.request(method, path, headers=self.headers(), json={} if method == "POST" else None)
            self.assertEqual(reply.status_code, 403, f"{method} {path}: {reply.text}")
        self.assertEqual(self.a.get("/openapi.json").status_code, 404)

    def test_unclassified_route_with_familiar_tag_stays_disabled(self):
        app = create_app(); router = APIRouter(tags=["ingestion"])
        @router.get("/api/projects/{project_id}/unclassified-dangerous-surface")
        def unclassified(project_id: str):
            return {"unexpected": "enabled"}
        app.include_router(router, dependencies=[Depends(authorize)])
        client = TestClient(app, base_url=ORIGIN); client.cookies.update(self.a.cookies)
        self.assertEqual(client.get(f"/api/projects/{self.projects[0]}/unclassified-dangerous-surface").status_code, 403)

    def test_unclassified_api_registered_without_domain_dependency_stays_disabled(self):
        app = create_app()
        @app.get("/api/projects/{project_id}/another-unclassified-surface")
        def unclassified(project_id: str):
            return {"unexpected": "enabled"}
        client = TestClient(app, base_url=ORIGIN); client.cookies.update(self.a.cookies)
        self.assertEqual(client.get(f"/api/projects/{self.projects[0]}/another-unclassified-surface").status_code, 403)

    def test_body_query_actor_and_foreign_document_references_are_rejected(self):
        route = self.document_path() + "/agent-structure/delivery/extraction-loop"
        for payload in ({"tenant_id": self.accounts[1]["id"]}, {"nested": {"document_id": self.documents[1]}},
                        {"nested": {"project_id": self.projects[1]}}, {"owner_id": ""}):
            reply = self.a.post(route, headers=self.headers(), json=payload)
            self.assertIn(reply.status_code, (404, 422), reply.text)
        self.assertEqual(self.a.get(self.document_path() + "/file", params={"project_id": self.projects[1]}).status_code, 404)

    def test_logout_revokes_the_server_session_and_expiration_survives_recreation(self):
        raw = self.a.cookies.get(self.app.state.saas_settings.cookie_name)
        self.assertEqual(self.a.post("/api/auth/logout", headers=self.headers()).status_code, 200)
        self.assertIsNone(store.read_session(raw))
        self.assertEqual(self.a.get("/api/projects").status_code, 401)
        token = self.b.cookies.get(self.app.state.saas_settings.cookie_name)
        with db.get_conn() as conn:
            conn.execute("UPDATE saas_sessions SET expires_at=0 WHERE token_sha256=?", (store.token_hash(token),))
        fresh = TestClient(create_app(), base_url=ORIGIN); fresh.cookies.update(self.b.cookies)
        self.assertEqual(fresh.get("/api/projects").status_code, 401)
        self.assertIsNone(fresh.get("/api/auth/session").json()["user"])

    def test_job_enqueue_requires_trusted_account_and_preserves_immutable_scope(self):
        payload = {"project_id": self.projects[0], "document_id": self.documents[0]}
        with self.assertRaises(PermissionError):
            bind_job_payload(payload, aggregate_type="sor-document", aggregate_id=self.documents[0])
        with account_scope(self.accounts[0]["id"], self.projects[0]):
            result = bind_job_payload(payload, aggregate_type="sor-document", aggregate_id=self.documents[0])
            self.assertEqual(result["saas_scope"], {"account_id": self.accounts[0]["id"], "project_id": self.projects[0]})
            with self.assertRaises(PermissionError):
                bind_job_payload({**payload, "saas_scope": {"account_id": self.accounts[1]["id"], "project_id": self.projects[0]}}, aggregate_type="sor-document", aggregate_id=self.documents[0])
            with self.assertRaises(Exception):
                bind_job_payload(payload, aggregate_type="sor-document", aggregate_id=self.documents[1])

    def test_durable_restart_restores_principal_and_rejects_missing_or_foreign_owner(self):
        payload = {"project_id": self.projects[0], "document_id": self.documents[0],
                   "saas_scope": {"account_id": self.accounts[0]["id"], "project_id": self.projects[0]}}
        job = SimpleNamespace(payload=payload, aggregate_type="sor-document", aggregate_id=self.documents[0])
        self.assertIsNone(current_scope())
        with durable_job_scope(job):
            self.assertEqual(current_scope().account_id, self.accounts[0]["id"])
            self.assertIsNone(db.get_project(self.projects[1]))
        self.assertIsNone(current_scope())
        for changes in ({"saas_scope": {}}, {"saas_scope": {"account_id": self.accounts[1]["id"], "project_id": self.projects[0]}},
                        {"project_id": self.projects[1]}, {"document_id": self.documents[1]}):
            job = SimpleNamespace(payload={**payload, **changes}, aggregate_type="sor-document", aggregate_id=self.documents[0])
            with self.assertRaises(NonRetryableJobError):
                with durable_job_scope(job):
                    self.fail("untrusted durable job entered handler scope")

    def test_actual_worker_dispatch_restores_owner_and_rejects_foreign_binding(self):
        from reqpilotx_server.worker.runtime import WorkerRuntime
        calls = []
        class Queue:
            def __init__(self, job): self.job, self.claimed, self.failure, self.success = job, False, None, None
            def claim(self, **kwargs):
                if self.claimed: return None
                self.claimed = True; return self.job
            def heartbeat(self, **kwargs): return self.job
            def succeed(self, **kwargs): self.success = kwargs; return self.job
            def fail(self, **kwargs): self.failure = kwargs; return self.job
        def handler(job, context):
            calls.append(current_scope())
            self.assertIsNone(db.get_project(self.projects[1]))
            return {"completed": True}
        payload = {"project_id": self.projects[0], "document_id": self.documents[0],
                   "saas_scope": {"account_id": self.accounts[0]["id"], "project_id": self.projects[0]}}
        def job(value):
            return SimpleNamespace(job_id="job-" + uuid4().hex, kind="synthetic.acceptance",
                                   aggregate_type="sor-document", aggregate_id=self.documents[0], payload=value)
        trusted = Queue(job(payload))
        worker = WorkerRuntime(queue=trusted, handlers={"synthetic.acceptance": handler}, worker_id="synthetic-worker")
        self.assertTrue(worker.run_once()); self.assertEqual(calls[0].account_id, self.accounts[0]["id"])
        self.assertIsNotNone(trusted.success); self.assertIsNone(current_scope())
        untrusted = Queue(job({**payload, "saas_scope": {"account_id": self.accounts[1]["id"], "project_id": self.projects[0]}}))
        worker = WorkerRuntime(queue=untrusted, handlers={"synthetic.acceptance": handler}, worker_id="synthetic-worker-restarted")
        self.assertTrue(worker.run_once()); self.assertEqual(len(calls), 1)
        self.assertEqual(untrusted.failure["error_code"], "SAAS_OWNER_BINDING_REJECTED")
        self.assertIsNone(untrusted.failure["retry_delay_seconds"])

    def test_seeded_delivery_http_export_is_owner_bound_immutable_and_survives_recreation(self):
        # Synthetic business output is seeded here. Authentication, authorization,
        # SQL receipts, XLSX generation and on-disk object bytes are not mocked.
        from .test_source_item_delivery_numbers import fixture
        from reqpilotx_server.adapters.storage.local_object_store import LocalObjectStore
        from reqpilotx_server.application.agent_workflow import source_item_delivery_bridge as bridge
        from reqpilotx_server.application.agent_workflow.source_item_delivery import build_delivery_snapshot
        from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
        m, b, d, a = fixture()
        snapshot = build_delivery_snapshot(m, b, {}, decomposition=d, annotations=a)
        document = db.get_sor_document(self.documents[0])
        snapshot["source_item_baseline"]["source_sha256"] = document["source_sha256"]
        snapshot["delivery_sha256"] = canonical_json_sha256({k: v for k, v in snapshot.items() if k != "delivery_sha256"})
        objects = LocalObjectStore(self.root / "versioned-objects")
        root = self.document_path() + "/agent-structure/delivery/versions"
        payload = {"command_id": uuid4().hex, "expected_source_sha256": document["source_sha256"],
                   "expected_delivery_sha256": snapshot["delivery_sha256"], "expected_business_basis_sha256": "b" * 64}
        with patch.object(bridge, "_build_current_delivery", side_effect=lambda *args: (deepcopy(snapshot), "", document, {"business_basis_sha256": "b" * 64, "active_jobs": []})), \
             patch.object(bridge, "_container", return_value=SimpleNamespace(object_store=objects)):
            saved = self.a.post(root, headers=self.headers(), json=payload)
            self.assertEqual(saved.status_code, 200, saved.text)
            first = self.a.get(root + "/1/export"); self.assertEqual(first.status_code, 200, first.text[:200])
            self.assertIn("ComplianceMatrix", load_workbook(BytesIO(first.content)).sheetnames)
            self.assertEqual(first.headers.get("cache-control"), "no-store, max-age=0")
            self.assertEqual(self.b.get(root + "/1/export").status_code, 404)
            self.assertEqual(self.b.post(root, headers=self.headers(1), json=payload).status_code, 404)
            snapshot["nodes"][-1]["display_text"] = "New working content must not replace a saved version"
            snapshot["delivery_sha256"] = canonical_json_sha256({k: v for k, v in snapshot.items() if k != "delivery_sha256"})
            replay = self.a.post(root, headers=self.headers(), json=payload)
            self.assertEqual(replay.json(), saved.json())
            fresh = TestClient(create_app(), base_url=ORIGIN); fresh.cookies.update(self.a.cookies)
            self.assertEqual(sha256(fresh.get(root + "/1/export").content).hexdigest(), sha256(first.content).hexdigest())
            frozen = fresh.get(root + "/1").json()
            self.assertNotEqual(frozen["snapshot"]["nodes"][-1]["display_text"], snapshot["nodes"][-1]["display_text"])

    def test_safe_config_requires_https_nonempty_invitation_and_bounded_session(self):
        for changes in ({"REQPILOT_SAAS_ORIGIN": "http://public.example.invalid"},
                        {"REQPILOT_SAAS_INVITE_CODE": "short"}, {"REQPILOT_SAAS_SESSION_SECONDS": "86401"},
                        {"REQPILOT_SAAS_ORIGIN": "https://127.0.0.1/"}):
            with self.assertRaises(ValueError):
                SaaSSettings.from_environment({"REQPILOT_SAAS_ENABLED": "1", "REQPILOT_SAAS_ORIGIN": ORIGIN,
                    "REQPILOT_SAAS_INVITE_CODE": INVITATION, **changes})


@unittest.skipUnless(os.environ.get("REQPILOT_SAAS_POSTGRES_URL"), "requires explicit disposable local PostgreSQL")
class PostgresSaaSAcceptanceTests(SaaSAcceptanceTests):
    """The same real account/API/ownership tests against native PostgreSQL."""
    USE_POSTGRES = True

    def test_all_native_migrations_apply_repeatably_with_saas_identity(self):
        migrations = Path(__file__).resolve().parents[2] / "apps/server/src/reqpilotx_server/resources/migrations/postgresql"
        first = self.database.apply_migrations(migrations)
        second = self.database.apply_migrations(migrations)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(tuple(migrations.glob("*.sql"))))
        self.assertIn("0039", {m.version for m in first})

    @unittest.skipUnless(os.environ.get("REQPILOT_SAAS_REAL_BUNDLE"), "requires a genuine registered engine/plugin bundle")
    def test_complete_sdk_agent_worker_review_export_without_snapshot_seeding(self):
        import httpx
        import pymupdf
        from openai import OpenAI
        from reqpilotx_server.adapters.persistence.postgres_jobs import PostgresJobQueue
        from reqpilotx_server.adapters.persistence.postgres_agent_structure_loop import PostgresAgentStructureLoopRepository
        from reqpilotx_server.infrastructure.runtime_container import RuntimeContainer
        from reqpilotx_server.infrastructure.runtime_settings import AgentProvider, AgentRuntimeBackend, NativeCapabilityMode
        from reqpilotx_server.application.agent_workflow import native_loop_capabilities as caps
        from reqpilotx_server.application.agent_workflow import source_document_loop_jobs as raw
        from reqpilotx_server.services import ai_copilot
        from reqpilotx_server.application.source_runtime import bind_source_runtime, clear_source_runtime
        from reqpilotx_server.worker.runtime import WorkerRuntime

        release = Path(os.environ["REQPILOT_SAAS_REAL_BUNDLE"]).resolve(strict=True)
        registered = caps.register_native_loop_capability_bundle(release, registry_root=self.root / "capability-registry", require_engine_release=True)
        objects = runtime.RUNTIME_STATE.container.object_store
        settings = replace(runtime.RUNTIME_SETTINGS, object_root=objects.root,
            agent_runtime_backend=AgentRuntimeBackend.PROVIDER_TOOL_LOOP, agent_provider=AgentProvider.ALIYUN,
            agent_model_id="test-provider-model", agent_customer_data_enabled=True,
            native_capability_mode=NativeCapabilityMode.REGISTERED_RELEASE,
            native_capability_registry_root=self.root / "capability-registry", native_capability_bundle_sha256=registered["bundle_sha256"])
        queue = PostgresJobQueue(self.database)
        container = RuntimeContainer(settings=settings, object_store=objects, database=self.database, jobs=queue,
            llm_review_repository=None, requirement_enrichment_repository=None,
            agent_structure_loop_repository=PostgresAgentStructureLoopRepository(self.database))
        model = _DeterministicRawAgent()
        transport = httpx.MockTransport(model.respond)
        client = OpenAI(api_key="test-only-placeholder-key", base_url="https://synthetic-provider.example.invalid/v1", http_client=httpx.Client(transport=transport))
        self.addCleanup(client.close)
        profile = replace(ai_copilot.load_ai_copilot_config("aliyun"), provider="aliyun", profile_id="aliyun",
            chat_model="test-provider-model", screening_model="test-provider-model",
            dashscope_api_key="test-only-placeholder-key", dashscope_base_url="https://synthetic-provider.example.invalid/v1")
        bind_source_runtime(settings=settings, object_store=objects, files=runtime.RUNTIME_STATE.files)
        self.addCleanup(clear_source_runtime)
        with patch.object(runtime, "RUNTIME_SETTINGS", settings), \
             patch.object(runtime.RUNTIME_STATE, "container", container), \
             patch.object(ai_copilot, "load_ai_copilot_config", return_value=profile), \
             patch.object(ai_copilot, "_aliyun_openai_client", return_value=client), \
             patch.object(ai_copilot, "_new_aliyun_openai_client", return_value=client):
            login = self.a.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"email": self.accounts[0]["email"], "password": PASSWORD})
            self.assertEqual(login.status_code, 200, login.text)
            self.csrf[0] = login.json()["csrf_token"]
            project = self.create_project(name="SDK synthetic integration")
            pdf = pymupdf.open(); page = pdf.new_page(width=595, height=842)
            for y, text in ((50, "Controller specification"), (95, "1 Functional requirements"),
                            (130, "The controller shall record the fault code."), (165, "The controller shall report readiness.")):
                page.insert_text((40, y), text, fontsize=11)
            source = pdf.tobytes(); pdf.close()
            uploaded = self.a.post(f"/api/projects/{project['id']}/sor-documents", headers=self.headers(),
                data={"command_id": uuid4().hex}, files={"file": ("synthetic-controller.pdf", source, "application/pdf")})
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            document = uploaded.json()
            base = f"/api/projects/{project['id']}/sor-documents/{document['id']}"
            start_path = base + "/agent-structure/delivery/extraction-loop"
            started = self.a.post(start_path, headers=self.headers(), json={"command_id": uuid4().hex,
                "expected_source_sha256": sha256(source).hexdigest(), "goal": "Extract this synthetic source through registered tools, review and export.",
                "analysis_mode": "SKIP", "model_id": "test-provider-model"})
            self.assertEqual(started.status_code, 202, started.text)
            task = started.json(); task_path = start_path + "/" + task["task_id"]
            self.assertEqual(self.b.get(task_path).status_code, 404)
            self.assertEqual(self.b.post(task_path + "/interrupt", headers=self.headers(1), json={"command_id": uuid4().hex}).status_code, 404)
            model.interrupt_once = lambda: self.assertEqual(self.a.post(task_path + "/interrupt", headers=self.headers(), json={"command_id": uuid4().hex}).status_code, 200)

            def consume(job_id):
                worker = WorkerRuntime(queue=queue, handlers={raw.EXTRACTION_JOB_KIND: raw.run_durable_source_document_loop},
                    worker_id="sdk-test-worker-" + uuid4().hex[:12], job_id=job_id, required_entrypoint=raw.ENTRYPOINT)
                self.assertTrue(worker.run_once(), "Real PostgreSQL worker did not claim the requested job")
                result = queue.get(job_id)
                self.assertIn(result.status.value, {"SUCCEEDED", "CANCELLED"}, str((result.status, result.error_code, result.error_message, model.errors)))
                return result

            consume(task["job_id"])
            interrupted = self.a.get(task_path).json()
            self.assertEqual(interrupted["runtime_status"], "INTERRUPTED")
            resume = self.a.post(task_path + "/resume", headers=self.headers(), json={"command_id": uuid4().hex, "goal": "Continue the synthetic source task after interruption."})
            self.assertEqual(resume.status_code, 202, resume.text)
            first = consume(resume.json()["job_id"])
            self.assertEqual(model.errors, [])
            self.assertTrue(first.result["application_saved"], str(first.result))
            self.assertEqual(first.result["transport_kind"], "PROVIDER_TOOL_LOOP")
            self.assertEqual(first.payload["saas_scope"], {"account_id": self.accounts[0]["id"], "project_id": project["id"]})
            self.assertEqual(first.result["runtime_receipt"]["identity"]["provider"], "aliyun")
            root = raw._task_root(container, project["id"], document["id"], task["task_id"])
            self.assertEqual(json.loads((root / "task.json").read_text())["account_id"], self.accounts[0]["id"])
            self.assertGreater(len(list((root / "runtime/calls").glob("*.json"))), 8)
            self.assertTrue(any(isinstance(message.get("content"), list) for call in model.calls for message in call["messages"]))

            versions_path = base + "/agent-structure/delivery/versions"
            preview = self.a.get(versions_path + "/preview"); self.assertEqual(preview.status_code, 200, preview.text)
            pins = preview.json()
            saved = self.a.post(versions_path, headers=self.headers(), json={"command_id": uuid4().hex,
                "expected_source_sha256": pins["source_sha256"], "expected_delivery_sha256": pins["delivery_sha256"],
                "expected_business_basis_sha256": pins["business_basis_sha256"], "mode": "DRAFT"})
            self.assertEqual(saved.status_code, 200, saved.text)
            frozen = self.a.get(versions_path + "/1/export"); self.assertEqual(frozen.status_code, 200, frozen.text[:200])
            book = load_workbook(BytesIO(frozen.content)); self.addCleanup(book.close)
            visible = "\n".join(str(cell.value) for row in book["ComplianceMatrix"] for cell in row if cell.value is not None)
            self.assertIn("The controller shall record the fault code.", visible)
            self.assertIn("The controller shall report readiness.", visible)
            self.assertEqual(self.b.get(versions_path + "/1/export").status_code, 404)
            target = model.results["baseline"]["records"][0]["source_record_id"]
            feedback = self.a.post(task_path + "/steer", headers=self.headers(), json={"command_id": uuid4().hex,
                "feedback": "Use QUALITY classification for the selected synthetic obligation; preserve its exact source wording.",
                "origin": "USER_REQUEST", "source_item_ids": [target]})
            self.assertEqual(feedback.status_code, 200, feedback.text)
            model.begin_review()
            resume_payload = {"command_id": uuid4().hex, "goal": "Apply the saved user feedback, review and save the same-source successor."}
            resumed = self.a.post(task_path + "/resume", headers=self.headers(), json=resume_payload)
            self.assertEqual(resumed.status_code, 202, resumed.text)
            successor = consume(resumed.json()["job_id"])
            self.assertEqual(model.errors, [])
            self.assertTrue(successor.result["application_saved"], str(successor.result))
            self.assertNotEqual(successor.result["baseline_sha256"], first.result["baseline_sha256"])
            self.assertEqual(successor.result["source_sha256"], sha256(source).hexdigest())
            self.assertEqual(model.results["baseline"]["records"][0]["source_record_id"], target)
            calls = len(model.calls)
            replay = self.a.post(task_path + "/resume", headers=self.headers(), json=resume_payload)
            self.assertEqual(replay.json()["job_id"], successor.job_id)
            self.assertEqual(len(model.calls), calls)
            fresh = TestClient(create_app(), base_url=ORIGIN); fresh.cookies.update(self.a.cookies)
            after_restart = fresh.get(versions_path + "/1/export")
            self.assertEqual(after_restart.status_code, 200, after_restart.text[:200])
            self.assertEqual(sha256(after_restart.content).hexdigest(), sha256(frozen.content).hexdigest())
            manifest = json.loads((root / "workspace/review/workspace-manifest.json").read_text())
            self.assertTrue(any(entry["origin"] == "USER" and entry["status"] == "ADDRESSED" for entry in manifest["feedback"]))
