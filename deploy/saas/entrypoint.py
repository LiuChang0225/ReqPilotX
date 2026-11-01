"""Fail-closed pilot process launcher; credentials are read only at runtime."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote


def read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Required runtime secret is empty or multiline.")
    return value


def configure_environment() -> None:
    if os.environ.get("REQPILOT_SAAS_ENABLED") != "1":
        raise ValueError("This release only runs with REQPILOT_SAAS_ENABLED=1.")
    if os.environ.get("REQPILOT_TEST_MODE") == "1":
        raise ValueError("SQLite test mode is prohibited in this release.")
    # Native socket peer auth needs no password. Container variant builds the DSN
    # inside the process so credentials never enter Compose's rendered config.
    db_host = os.environ.get("REQPILOT_DATABASE_HOST")
    if db_host:
        password = read_secret("/run/secrets/database_password")
        user = quote(os.environ.get("REQPILOT_DATABASE_USER", "reqpilotx"), safe="")
        name = quote(os.environ.get("REQPILOT_DATABASE_NAME", "reqpilotx"), safe="")
        if db_host != "db":
            raise ValueError("Container profile requires the private db service.")
        os.environ["REQPILOT_DATABASE_URL"] = (
            f"postgresql://{user}:{quote(password, safe='')}@db:5432/{name}"
        )
    invite_path = os.environ.get("REQPILOT_SAAS_INVITE_CODE_FILE")
    if not invite_path and Path("/run/secrets/invitation_code").is_file():
        invite_path = "/run/secrets/invitation_code"
    if invite_path:
        os.environ["REQPILOT_SAAS_INVITE_CODE"] = read_secret(invite_path)
    from reqpilotx_server.saas.config import SaaSSettings

    SaaSSettings.from_environment().validate(enabled_required=True)
    from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings

    settings = RuntimeSettings.from_environment()
    if settings.database_backend.value != "postgresql":
        raise ValueError("SaaS pilot requires PostgreSQL.")
    if settings.object_store_backend.value != "local":
        raise ValueError("This no-new-resource profile only supports local disk.")


def main() -> int:
    configure_environment()
    role = sys.argv[1] if len(sys.argv) > 1 else "api"
    if role == "migrate":
        from reqpilotx_server.infrastructure.postgres import PostgresDatabase
        from reqpilotx_server.resource_paths import postgres_migration_dir

        database = PostgresDatabase(os.environ["REQPILOT_DATABASE_URL"])
        try:
            migrations = database.apply_migrations(postgres_migration_dir())
            database.healthcheck()
            print(f"PostgreSQL migration gate passed: {len(migrations)} checked migrations")
        finally:
            database.close()
        return 0
    if role == "preflight":
        from preflight import runtime_preflight

        runtime_preflight()
        return 0
    if role == "api":
        os.environ["REQPILOT_PROCESS_ROLE"] = "api"
        command = [
            sys.executable, "-m", "uvicorn", "reqpilotx_server.main:app",
            "--host", os.environ.get("REQPILOT_LISTEN_HOST", "127.0.0.1"),
            "--port", os.environ.get("REQPILOT_PORT", "8000"),
            "--workers", "1", "--proxy-headers",
            "--forwarded-allow-ips", os.environ.get("REQPILOT_TRUSTED_PROXY_IPS", "127.0.0.1"),
            "--no-access-log",
        ]
    elif role == "worker":
        os.environ["REQPILOT_PROCESS_ROLE"] = "worker"
        command = [
            sys.executable, "-m", "reqpilotx_server.worker",
            "--lease-seconds", os.environ.get("REQPILOT_JOB_LEASE_SECONDS", "120"),
            "--poll-seconds", os.environ.get("REQPILOT_JOB_POLL_SECONDS", "1"),
        ]
    else:
        raise ValueError("Expected api, worker, migrate or preflight.")
    os.execv(sys.executable, command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Do not include exception strings: a DSN, secret path or secret could leak.
        print("Pilot startup refused: missing or invalid approved runtime configuration", file=sys.stderr)
        raise SystemExit(2)
