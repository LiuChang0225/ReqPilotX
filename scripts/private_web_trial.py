"""Run an owned, installed private-Web acceptance environment on loopback.

No existing database is used. Live provider calls require --live-provider and
an explicitly supplied existing connection profile. Only invented inputs are
generated here; complete runtime artifacts stay in the private output directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import tarfile
import time
from uuid import uuid4

import smoke_workbench as shared

EMAIL = "trial-engineer@example.invalid"
# Public, disposable-fixture login; never used for an existing service/account.
PASSWORD = "Invented-Fixture-Only-20261016!"


def verify_frontend(root: Path, commit: str, tree: str) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Frontend must be a regular built directory")
    path = root / "reqpilotx-build.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("Matching Web build identity is missing")
    identity = json.loads(path.read_text())
    if (
        identity.get("schema_version") != "reqpilotx-web-build/v1"
        or identity.get("source_commit") != commit
        or identity.get("source_tree") != tree
        or identity.get("source_inputs_clean") is not True
    ):
        raise ValueError("Frontend was not built from this clean candidate")
    actual = {}
    for member in root.rglob("*"):
        if member.is_symlink():
            raise ValueError("Frontend cannot contain symlinks")
        if member.is_file() and member != path:
            actual[member.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
                "bytes": member.stat().st_size,
            }
    expected = {
        row["path"]: {"sha256": row["sha256"], "bytes": row["bytes"]}
        for row in identity["files"]
    }
    if (
        len(expected) != len(identity["files"])
        or "index.html" not in expected
        or actual != expected
    ):
        raise ValueError("Frontend files differ from their build identity")
    return {
        "status": "VERIFIED",
        "source_commit": commit,
        "source_tree": tree,
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_count": len(actual),
    }


def installed_identity(
    interpreter: Path, directory: Path, build: dict, commit: str, env: dict
) -> dict:
    prefix = "apps/server/src/reqpilotx_server/"
    archive = subprocess.check_output(
        ["git", "archive", commit, prefix], cwd=shared.ROOT
    )
    files = {}
    with tarfile.open(fileobj=BytesIO(archive)) as source:
        for member in source:
            if member.isfile():
                files[member.name.removeprefix(prefix)] = hashlib.sha256(
                    source.extractfile(member).read()
                ).hexdigest()
            elif not member.isdir():
                raise ValueError("Committed server source cannot contain links")
    expected = {
        "source_commit": commit,
        "source_tree": subprocess.check_output(
            ["git", "rev-parse", commit + "^{tree}"], cwd=shared.ROOT, text=True
        ).strip(),
        "server_files": files,
        "engine_core_identity": build["engine_release"]["core_identity"],
    }
    descriptor = directory / "expected-installation.json"
    shared.save(descriptor, expected)
    checked = subprocess.run(
        [
            str(interpreter),
            "-I",
            str(shared.ROOT / "scripts/check_trial_installation.py"),
            str(descriptor),
        ],
        env=env,
        cwd=directory,
        capture_output=True,
        text=True,
    )
    shared.require(
        checked.returncode == 0, "Installed engine/server do not match this candidate"
    )
    proof = json.loads(checked.stdout)
    shared.require(
        proof.get("status") == "VERIFIED", "Installation identity was not verified"
    )
    return proof


def stop_owned_resources(directory: Path, env: dict, worker, api) -> dict:
    errors, forced = [], []
    for name, process in (("worker", worker), ("api", api)):
        if process is not None:
            try:
                shared.stop_server(process)
            except Exception as error:
                errors.append({"resource": name, "error_type": type(error).__name__})
            if process.poll() == -getattr(signal, "SIGKILL", 9):
                forced.append(name)
    stopped = {
        "api_stopped": api is None or api.poll() is not None,
        "worker_stopped": worker is None or worker.poll() is not None,
    }
    if all(stopped.values()):
        try:
            shared.stop_postgres(directory, env)
        except Exception as error:
            errors.append({"resource": "postgres", "error_type": type(error).__name__})
    else:
        errors.append({"resource": "postgres", "error_type": "OwnedWriterStillRunning"})
    return {
        **stopped,
        "postgres_stopped": not (directory / "postgres-data/postmaster.pid").exists(),
        "cleanup_errors": errors,
        "forced_stop": forced,
    }


def provider_environment(path: Path, model: str) -> dict[str, str]:
    """Read the existing selected profile in memory, without copying its file."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Supply an explicit regular existing provider profile")
    profile = json.loads(path.read_text())
    secret = profile.get("REQPILOT_DASHSCOPE_API_KEY") or profile.get(
        "DASHSCOPE_API_KEY"
    )
    origin = profile.get("REQPILOT_DASHSCOPE_BASE_URL")
    if not isinstance(secret, str) or not secret.strip() or not isinstance(origin, str):
        raise ValueError("The selected profile does not configure Aliyun chat")
    from urllib.parse import urlsplit

    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.hostname
        not in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
    ):
        raise ValueError(
            "The selected profile does not use an approved DashScope endpoint"
        )
    if model != "qwen3.8-max-0902":
        raise ValueError("This acceptance entry targets the approved Qwen snapshot")
    return {
        "DASHSCOPE_API_KEY": secret,
        "REQPILOT_DASHSCOPE_API_KEY": secret,
        "REQPILOT_DASHSCOPE_BASE_URL": origin,
        "REQPILOT_ALIYUN_CHAT_MODEL": model,
        "REQPILOT_ALIYUN_SCREENING_MODEL": model,
        "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
        "REQPILOT_AGENT_PROVIDER": "aliyun",
        "REQPILOT_AGENT_MODEL_ID": model,
        "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "1",
        "REQPILOT_AGENT_ALIYUN_ENABLE_THINKING": "1",
    }


def invented_source(path: Path) -> None:
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    for y, text in (
        (55, "Controller specification"),
        (105, "1 Functional requirements"),
        (150, "1.1 The controller shall record the fault code."),
        (200, "1.2 The controller shall report readiness."),
    ):
        page.insert_text((40, y), text, fontsize=11)
    pdf.save(path)
    pdf.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="Non-editable matching engine/server installation",
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--frontend-root", type=Path, required=True)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--profile-file", type=Path)
    parser.add_argument("--model", default="qwen3.8-max-0902")
    args = parser.parse_args()
    if args.live_provider != bool(args.profile_file):
        parser.error(
            "--live-provider and the existing --profile-file must be supplied together"
        )
    directory = args.output.expanduser().resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    interpreter = Path(os.path.abspath(args.python.expanduser()))
    shared.require(
        interpreter.is_file() and os.access(interpreter, os.X_OK),
        "Installed Python is unavailable",
    )
    bundle = args.bundle.resolve(strict=True)
    build = shared.read(bundle / "build-receipt.json")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=shared.ROOT, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=shared.ROOT, text=True
    ).strip()
    shared.require(
        not subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=shared.ROOT,
            text=True,
        ).strip(),
        "Freeze a clean candidate before acceptance",
    )
    shared.require(
        build.get("release_mode") == "ENGINE_WHEEL"
        and build["engine_release"]["source_commit"] == commit,
        "Use an engine/plugin release from this exact candidate",
    )
    api_port, pg_port = shared.unused_port(), shared.unused_port()
    origin = f"http://127.0.0.1:{api_port}"
    env = shared.isolate(
        directory, f"postgresql://reqpilot_smoke@127.0.0.1:{pg_port}/reqpilot_smoke"
    )
    env.pop("PYTHONPATH", None)
    env.pop("REQPILOT_DEVELOPMENT_ROOT", None)
    env.update(
        LC_ALL="C",
        TZ="UTC",
        REQPILOT_FRONTEND_DIR=str(args.frontend_root.resolve(strict=True)),
        REQPILOT_SAAS_ENABLED="1",
        REQPILOT_SAAS_ORIGIN=origin,
        REQPILOT_ALLOWED_ORIGINS=origin,
        REQPILOT_SAAS_INVITE_CODE=uuid4().hex + uuid4().hex,
        REQPILOT_LOCAL_WORKER_COUNT="1",
        REQPILOT_NATIVE_CAPABILITY_MODE="registered-release",
        REQPILOT_NATIVE_CAPABILITY_REGISTRY=str(
            directory / "storage/native-loop-capabilities"
        ),
        REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256=build["bundle_sha256"],
    )
    frontend_proof = verify_frontend(args.frontend_root, commit, tree)
    installation_proof = installed_identity(interpreter, directory, build, commit, env)
    if args.live_provider:
        env.update(provider_environment(args.profile_file, args.model))
    else:
        env["REQPILOT_AGENT_RUNTIME"] = "none"
    invented_source(directory / "synthetic-controller.pdf")
    report = {
        "schema_version": "reqpilotx-private-web-trial/v1",
        "status": "STARTING",
        "source_commit": commit,
        "installation": installation_proof,
        "frontend": frontend_proof,
        "bundle_sha256": build["bundle_sha256"],
        "base_url": origin,
        "fixture_only": True,
        "live_provider_enabled": args.live_provider,
        "model": args.model if args.live_provider else None,
        "login_email": EMAIL,
        "source_file": str(directory / "synthetic-controller.pdf"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    api = worker = None
    try:
        shared.start_postgres(
            directory, shared.postgres_tools(args.postgres_bin), pg_port, env
        )
        register = (
            "import json,sys;from pathlib import Path;"
            "from reqpilotx_server.application.agent_workflow.native_loop_capabilities import register_native_loop_capability_bundle;"
            "print(json.dumps(register_native_loop_capability_bundle(Path(sys.argv[1]),registry_root=Path(sys.argv[2]),require_engine_release=True)))"
        )
        registered = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                register,
                str(bundle),
                env["REQPILOT_NATIVE_CAPABILITY_REGISTRY"],
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        shared.require(
            registered.returncode == 0,
            "Installed capability registration failed; no model was started",
        )
        with (directory / "api.log").open("ab") as log:
            api = subprocess.Popen(
                [
                    str(interpreter),
                    "-I",
                    "-m",
                    "uvicorn",
                    "reqpilotx_server.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                    "--no-access-log",
                ],
                env=env,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        import httpx

        with httpx.Client(base_url=origin, trust_env=False, timeout=30) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                shared.require(
                    api.poll() is None, "Installed API stopped; inspect private api.log"
                )
                try:
                    if client.get("/api/healthz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("Installed API did not become ready")
            registered_account = client.post(
                "/api/auth/register",
                headers={"Origin": origin},
                json={
                    "email": EMAIL,
                    "password": PASSWORD,
                    "invitation": env["REQPILOT_SAAS_INVITE_CODE"],
                },
            )
            shared.require(
                registered_account.status_code == 201,
                "Disposable account registration failed",
            )
        if args.live_provider:
            with (directory / "worker.log").open("ab") as log:
                worker = subprocess.Popen(
                    [
                        str(interpreter),
                        "-I",
                        "-m",
                        "reqpilotx_server.worker",
                        "--poll-seconds",
                        "1",
                    ],
                    env={**env, "REQPILOT_PROCESS_ROLE": "worker"},
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        report.update(
            status="READY_FOR_BROWSER",
            api_pid=api.pid,
            worker_pid=worker.pid if worker else None,
        )
        shared.save(directory / "trial.json", report)
        print(json.dumps(report), flush=True)
        while not stopped.wait(1):
            shared.require(api.poll() is None, "Owned API exited")
            if worker is not None:
                shared.require(worker.poll() is None, "Owned worker exited")
    except Exception as error:
        report.update(status="FAILED", error_type=type(error).__name__)
        raise
    finally:
        cleanup = stop_owned_resources(directory, env, worker, api)
        report.update(
            status="FAILED"
            if report.get("error_type") or cleanup["cleanup_errors"]
            else "STOPPED",
            lifecycle_status="STOPPED"
            if all(
                cleanup[key]
                for key in ("api_stopped", "worker_stopped", "postgres_stopped")
            )
            else "CLEANUP_INCOMPLETE",
            **cleanup,
            stopped_at=datetime.now(timezone.utc).isoformat(),
        )
        shared.save(directory / "trial.json", report)
    return 0 if report["status"] != "FAILED" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Provider profile values, credentials and exceptions stay out of stdout.
        print(
            json.dumps({"status": "FAILED", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        raise SystemExit(1)
