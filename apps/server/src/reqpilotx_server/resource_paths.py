"""Explicit installed-resource and host-configured path resolution."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
import os
from pathlib import Path


def package_resource_path(*parts: str) -> Path:
    resource = files("reqpilotx_server.resources")
    for part in parts:
        resource = resource.joinpath(part)
    return Path(str(resource)).resolve(strict=False)


def postgres_migration_dir() -> Path:
    path = package_resource_path("migrations", "postgresql")
    if not path.is_dir():
        raise FileNotFoundError(f"Installed PostgreSQL migrations are missing: {path}")
    return path


def configured_frontend_dir(
    environment: Mapping[str, str] | None = None,
) -> Path:
    source = os.environ if environment is None else environment
    configured = str(source.get("REQPILOT_FRONTEND_DIR") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("REQPILOT_FRONTEND_DIR must be absolute")
        return path.resolve(strict=False)
    return package_resource_path("frontend")


def configured_development_root(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    source = os.environ if environment is None else environment
    configured = str(source.get("REQPILOT_DEVELOPMENT_ROOT") or "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError("REQPILOT_DEVELOPMENT_ROOT must be absolute")
    return path.resolve(strict=False)


def development_path(*parts: str) -> Path:
    """Locate an explicitly configured checkout-only resource.

    A non-existent package-local sentinel keeps imports side-effect free when the
    optional development assets are not configured; the feature boundary must
    still verify the concrete file or directory before use.
    """

    root = configured_development_root()
    if root is None:
        return package_resource_path("development-root-not-configured", *parts)
    return root.joinpath(*parts)


__all__ = [
    "configured_development_root",
    "configured_frontend_dir",
    "development_path",
    "package_resource_path",
    "postgres_migration_dir",
]
