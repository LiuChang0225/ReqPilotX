"""Synthetic-only installed-wheel/real-PG smoke, never pointed at a live pilot DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--frontend", required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.database_url)
    if target.hostname != "127.0.0.1" or target.password or not target.path.startswith("/reqpilotx_release_smoke"):
        raise ValueError("Use only a disposable loopback reqpilotx_release_smoke database")
    if "REQPILOT_TEST_MODE" in os.environ:
        raise ValueError("This is real PostgreSQL, never SQLite test mode")
    with tempfile.TemporaryDirectory(prefix="reqpilotx-package-smoke-") as temporary:
        root = Path(temporary)
        origin = "https://127.0.0.1"
        invitations = ["SYNTHETIC-RELEASE-TEST-ONLY-" + uuid4().hex for _ in range(2)]
        password = "SYNTHETIC-RELEASE-PASSWORD-ONLY-" + uuid4().hex
        os.environ.update({
            "REQPILOT_ENV": "local", "REQPILOT_PROCESS_ROLE": "api",
            "REQPILOT_SAAS_ENABLED": "1", "REQPILOT_SAAS_ORIGIN": origin,
            "REQPILOT_SAAS_INVITE_CODE": invitations[0],
            "REQPILOT_DATABASE_URL": args.database_url,
            "REQPILOT_STORAGE_DIR": str(root), "REQPILOT_OBJECT_ROOT": str(root),
            "REQPILOT_SCRATCH_ROOT": str(root / "scratch"),
            "REQPILOT_FRONTEND_DIR": str(args.frontend.resolve()),
            "REQPILOT_ALLOWED_HOSTS": "127.0.0.1", "REQPILOT_ALLOWED_ORIGINS": origin,
            "REQPILOT_AGENT_RUNTIME": "none", "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "0",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        })
        from fastapi.testclient import TestClient
        from reqpilotx_server.api.app_factory import create_app
        from reqpilotx_server.saas import store
        import reqpilotx_server
        if "site-packages" not in str(reqpilotx_server.__path__):
            raise ValueError("Smoke must consume installed wheels, never checkout source")
        app = create_app()
        stages = []
        with TestClient(app, base_url=origin) as guest:
            reply = guest.get("/api/healthz")
            assert reply.status_code == 200 and reply.json() == {"status": "ok", "authentication": "required"}
            assert guest.get("/api/projects").status_code == 401
            assert guest.get("/openapi.json").status_code == 404
            assert guest.get("/", headers={"Host": "evil.example.invalid"}).status_code == 400
            stages.append("real_pg_migrations_and_sanitized_readiness")
            clients, headers, projects, documents = [], [], [], []
            store.add_invitation(invitations[1])
            for index in range(2):
                client = TestClient(app, base_url=origin)
                reply = client.post("/api/auth/register", headers={"Origin": origin}, json={
                    "email": f"synthetic-{index}@example.invalid", "password": password,
                    "invitation": invitations[index],
                })
                assert reply.status_code == 201, f"Registration status {reply.status_code}"
                for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/", "__Host-"):
                    assert flag in reply.headers["set-cookie"]
                h = {"Origin": origin, "X-ReqPilot-CSRF": reply.json()["csrf_token"]}
                assert client.post("/api/projects", json={"name": "synthetic", "customer_name": "synthetic"}).status_code == 403
                p = client.post("/api/projects", headers=h, json={"name": "Synthetic release smoke", "customer_name": "Synthetic", "command_id": uuid4().hex})
                assert p.status_code == 200
                project = p.json()["id"]
                data = f"The synthetic controller {index} shall start.\n".encode()
                upload = client.post(f"/api/projects/{project}/sor-documents", headers=h, data={"command_id": uuid4().hex}, files={"file": ("synthetic.txt", data, "text/plain")})
                assert upload.status_code == 200
                document = upload.json()["id"]
                downloaded = client.get(f"/api/projects/{project}/sor-documents/{document}/file")
                assert downloaded.status_code == 200
                assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(data).digest()
                clients.append(client); headers.append(h); projects.append(project); documents.append(document)
            stages.append("two_accounts_single_use_invites_secure_sessions_csrf_upload_object_bytes")
            for index in range(2):
                other = 1-index
                assert clients[index].get(f"/api/projects/{projects[other]}/sor-documents/{documents[other]}/file").status_code == 404
                assert {p["id"] for p in clients[index].get("/api/projects").json()} == {projects[index]}
            assert guest.post("/api/auth/register", headers={"Origin": origin}, json={"email": "reused@example.invalid", "password": password, "invitation": invitations[0]}).status_code == 403
            stages.append("cross_account_objects_and_catalogs_blocked_invite_reuse_blocked")
            worker_env = dict(os.environ, REQPILOT_PROCESS_ROLE="worker")
            worker = subprocess.run([sys.executable, "-m", "reqpilotx_server.worker", "--once"], env=worker_env, cwd=temporary, capture_output=True, timeout=60)
            assert worker.returncode == 0, "Installed durable worker initialization failed"
            stages.append("installed_durable_worker_real_pg_initialization_no_jobs")
            cookie = dict(clients[0].cookies)
        # New app lifecycle closes/reopens DB pools and persists accounts/projects/objects.
        restarted = create_app()
        with TestClient(restarted, base_url=origin) as client:
            client.cookies.update(cookie)
            reply = client.get(f"/api/projects/{projects[0]}/sor-documents/{documents[0]}/file")
            assert reply.status_code == 200
            assert client.get("/api/auth/session").json()["user"] is not None
            logout = client.post("/api/auth/logout", headers=headers[0])
            assert logout.status_code == 200
            assert client.get("/api/projects").status_code == 401
        stages.append("app_restart_persistence_session_logout")
        print(json.dumps({"ok": True, "python": sys.version.split()[0], "checks": stages,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "real_model_call": False, "tls_server_tested": False,
            "executed_nonempty_durable_job": False, "synthetic_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
