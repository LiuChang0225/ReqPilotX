"""Read-only checks used before any pilot service starts."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def runtime_preflight() -> None:
    from reqpilotx_server.saas.config import SaaSSettings
    from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings
    from reqpilotx_server.infrastructure.postgres import PostgresDatabase
    from reqpilotx_server.resource_paths import postgres_migration_dir

    SaaSSettings.from_environment().validate(enabled_required=True)
    settings = RuntimeSettings.from_environment()
    if sys.version_info < (3, 11):
        raise ValueError("Python >=3.11 is required.")
    if settings.database_backend.value != "postgresql":
        raise ValueError("PostgreSQL is required.")
    if settings.object_store_backend.value != "local":
        raise ValueError("Pilot uses existing local disk only.")
    listen = os.environ.get("REQPILOT_LISTEN_HOST", "127.0.0.1")
    container = os.environ.get("REQPILOT_DATABASE_HOST") == "db"
    if listen != ("0.0.0.0" if container else "127.0.0.1"):
        raise ValueError("Unexpected private listener.")
    if not container:
        database = urlsplit(settings.database_url)
        query = parse_qs(database.query)
        if (database.hostname or database.username or database.password
                or database.path != "/reqpilotx"
                or query != {"host": ["/var/run/postgresql"]}):
            raise ValueError("Native pilot must use Unix socket and peer auth.")
    for directory in (settings.object_root.parent, settings.scratch_root):
        if not directory.is_dir() or not os.access(directory, os.W_OK):
            raise ValueError("Private application state is missing or unwritable.")
    if not (settings.frontend_root / "index.html").is_file():
        raise ValueError("Prebuilt frontend is missing.")
    if not tuple(postgres_migration_dir().glob("*.sql")):
        raise ValueError("Versioned migrations are missing.")
    for optional in ("torch", "sentence_transformers", "haystack"):
        if importlib.util.find_spec(optional) is not None:
            raise ValueError("Heavy optional adapters are not part of this 2GiB profile.")
    database = PostgresDatabase(settings.database_url)
    try:
        database.healthcheck()
        with database.connection() as connection:
            for table in ("saas_accounts", "saas_sessions", "saas_project_owners", "saas_invitations"):
                if not connection.execute("SELECT to_regclass(%s) AS relation", (table,)).fetchone()["relation"]:
                    raise ValueError("SaaS identity migrations are missing")
    finally:
        database.close()
    # Exercise the real installed composition, not just environment parsing.
    from reqpilotx_server.api.app_factory import create_app
    app = create_app()
    if not app.state.saas_settings.enabled:
        raise ValueError("Authentication composition was disabled")
    print(json.dumps({"ok": True, "database": "postgresql", "object_store": "local", "auth_required": True, "frontend_built": True}))
