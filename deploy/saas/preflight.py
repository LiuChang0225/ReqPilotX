"""Read-only checks used before any pilot service starts."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def writable_state_preflight(object_root: Path, scratch_root: Path, *, expected_state=None,
                             configured_scratch=None) -> None:
    if expected_state is not None and object_root.resolve() != expected_state.resolve():
        raise ValueError("Pilot state differs from the documented backup or mounted root.")
    if not scratch_root.resolve().is_relative_to(object_root.resolve()):
        raise ValueError("Pilot scratch must stay inside application state.")
    raw_scratch = Path(configured_scratch or scratch_root)
    anchors = [root for root in (object_root, object_root.resolve()) if raw_scratch.is_relative_to(root)]
    if not anchors or ".." in raw_scratch.parts:
        raise ValueError("Pilot scratch must use its documented state path.")
    current = anchors[0]
    for part in ("", *raw_scratch.relative_to(current).parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("Pilot scratch cannot traverse state symlinks.")
    for directory in (object_root, scratch_root):
        if not directory.is_dir() or not os.access(directory, os.W_OK):
            raise ValueError("Private application state is missing or unwritable.")


def native_capability_preflight(settings, *, state_root=Path("/var/lib/reqpilotx/application"),
                                configured_registry=None) -> dict:
    if settings.agent_runtime_backend.value == "none":
        return {"status": "NOT_REQUIRED_AGENT_DISABLED"}
    if settings.agent_runtime_backend.value != "provider-tool-loop":
        raise ValueError("This pilot does not support another active Agent backend.")
    registry = settings.native_capability_registry_root.resolve()
    state = state_root.resolve()
    raw_registry = Path(configured_registry or settings.native_capability_registry_root)
    anchors = [root for root in (state_root, state) if raw_registry.is_relative_to(root)]
    if not anchors or ".." in raw_registry.parts:
        raise ValueError("Capability registry must use the documented application state.")
    current = anchors[0]
    for part in ("", *raw_registry.relative_to(current).parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("Capability registry cannot traverse state symlinks.")
    identity = settings.native_capability_bundle_sha256
    if (settings.native_capability_mode.value != "registered-release"
            or settings.object_root.resolve() != state
            or registry == state or not registry.is_relative_to(state)
            or not re.fullmatch(r"[0-9a-f]{64}", identity)):
        raise ValueError("Provider pilot needs an exact registered capability inside backed-up application state.")
    from reqpilotx_server.application.agent_workflow.native_loop_capabilities import (
        _registered_bundle_path, _verify_bundle,
    )
    from reqpilotx_engine import core_manifest

    receipt = _verify_bundle(_registered_bundle_path(registry, identity), identity,
                             require_engine_release=True)
    installed = core_manifest()
    if receipt["engine_release"]["core_identity"] != installed["core_identity"]:
        raise ValueError("Registered capability differs from the installed engine.")
    return {"status": "VERIFIED", "bundle_sha256": identity,
            "engine_core_identity": installed["core_identity"]}


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
    capability = native_capability_preflight(settings,
        state_root=Path("/var/lib/reqpilotx" if container else "/var/lib/reqpilotx/application"),
        configured_registry=os.environ.get("REQPILOT_NATIVE_CAPABILITY_REGISTRY"))
    if listen != ("0.0.0.0" if container else "127.0.0.1"):
        raise ValueError("Unexpected private listener.")
    if not container:
        database = urlsplit(settings.database_url)
        query = parse_qs(database.query)
        if (database.hostname or database.username or database.password
                or database.path != "/reqpilotx"
                or query != {"host": ["/var/run/postgresql"]}):
            raise ValueError("Native pilot must use Unix socket and peer auth.")
    writable_state_preflight(settings.object_root, settings.scratch_root,
        expected_state=Path("/var/lib/reqpilotx" if container else "/var/lib/reqpilotx/application"),
        configured_scratch=os.environ.get("REQPILOT_SCRATCH_ROOT"))
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
    print(json.dumps({"ok": True, "database": "postgresql", "object_store": "local", "auth_required": True,
                      "frontend_built": True, "native_capability": capability}))
