"""Explicit, fixture-only PG/HTTP/SDK integration. No live provider is contacted."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def assess_result(text: str, exit_code: int, *, worktree_clean: bool, allow_dirty: bool = False) -> dict:
    count = re.search(r"^Ran (\d+) tests? in ([0-9.]+)s$", text, re.MULTILINE)
    skips = re.findall(r"\bskipped=(\d+)", text)
    strong = re.search(r"^test_complete_sdk_agent_worker_review_export_without_snapshot_seeding \(.*\) \.\.\.", text, re.MULTILINE) is not None
    passed = exit_code == 0 and count is not None and int(count.group(1)) >= 23 and not skips and strong
    formal = passed and worktree_clean
    status = "PASS" if formal else "DEVELOPMENT_PASS" if passed and allow_dirty else "FAIL"
    return {"status": status, "formal_candidate": formal, "test_count": int(count.group(1)) if count else None,
            "mandatory_sdk_case_executed": strong, "worktree_clean": worktree_clean}


@contextmanager
def managed_postgres(postgres_bin: Path, shared: Path, data: Path, output: Path, port: int):
    """Cleanup includes failed/partial starts and is limited to this owned cluster."""
    try:
        with (output / "postgres-setup.log").open("w") as setup:
            subprocess.run([str(postgres_bin / "initdb"), "-D", str(data), "-L", str(shared),
                            "--no-locale", "--encoding=UTF8", "--auth-local=trust", "--auth-host=trust"],
                           stdout=setup, stderr=subprocess.STDOUT, check=True)
            subprocess.run([str(postgres_bin / "pg_ctl"), "-D", str(data), "-l", str(output / "postgres.log"),
                            "-o", f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''", "-w", "start"],
                           stdout=setup, stderr=subprocess.STDOUT, check=True)
        yield
    finally:
        status = subprocess.run([str(postgres_bin / "pg_ctl"), "-D", str(data), "status"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=False)
        if status.returncode == 0 or (data / "postmaster.pid").exists():
            subprocess.run([str(postgres_bin / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--postgres-share", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty-development", action="store_true",
                        help="Label fixture evidence DEVELOPMENT_PASS; never sign a formal dirty candidate")
    args = parser.parse_args()
    postgres_bin = args.postgres_bin.resolve(strict=True)
    bundle = args.bundle.resolve(strict=True)
    receipt = json.loads((bundle / "build-receipt.json").read_text())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    engine = receipt.get("engine_release", {})
    if receipt.get("release_mode") != "ENGINE_WHEEL" or engine.get("source_commit") != commit:
        parser.error("Use a genuine engine/plugin release built from the exact current HEAD")
    # The test also registers and rechecks every bundle file; this is not a
    # substitute receipt or a switch that relaxes provider/registry guards.
    shared = args.postgres_share or postgres_bin.parents[3] / "share/postgresql" / postgres_bin.parent.name
    shared = shared.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    log = output / "saas-postgres.log"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("REQPILOT_", "REQPILOTX_", "OPENAI_", "AZURE_", "DASHSCOPE_", "ALIYUN_", "ALIBABA_CLOUD_"))
                   and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
    started = time.time()
    dirty = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT, text=True).splitlines()
    if dirty and not args.allow_dirty_development:
        parser.error("Commit and freeze the candidate first; dirty source cannot produce formal acceptance")
    with tempfile.TemporaryDirectory(prefix="saas-pg-", dir=output) as temporary:
        data = Path(temporary) / "database"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with managed_postgres(postgres_bin, shared, data, output, port):
            environment.update({"REQPILOT_TEST_MODE": "1", "REQPILOT_ENV": "test", "REQPILOT_STORAGE_DIR": temporary,
                                "REQPILOT_TEST_STORAGE_DIR": temporary, "REQPILOT_OBJECT_STORE": "local",
                                "REQPILOT_OBJECT_ROOT": temporary, "REQPILOT_AGENT_RUNTIME": "none",
                                "REQPILOT_SAAS_POSTGRES_URL": f"postgresql://{getpass.getuser()}@127.0.0.1:{port}/postgres",
                                "REQPILOT_SAAS_REAL_BUNDLE": str(bundle), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                "PYTHONPATH": os.pathsep.join(str(ROOT / path) for path in ("packages/engine/src", "apps/server/src", "prototype"))})
            with log.open("w") as stream:
                completed = subprocess.run([sys.executable, "-m", "unittest", "-v",
                                            "tests.test_saas_acceptance.PostgresSaaSAcceptanceTests"],
                                           cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=False)
    text = log.read_text()
    result = {**assess_result(text, completed.returncode, worktree_clean=not dirty, allow_dirty=args.allow_dirty_development), "source_commit": commit,
              "bundle_sha256": receipt["bundle_sha256"], "engine_core_identity": engine["core_identity"],
              "fixture_only": True, "model_transport": "httpx.MockTransport", "live_provider": "NOT_RUN",
              "postgres_stopped": True,
              "dirty_paths": [line[3:] for line in dirty],
              "exit_code": completed.returncode, "elapsed_seconds": round(time.time() - started, 3), "log": log.name}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] in {"PASS", "DEVELOPMENT_PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
