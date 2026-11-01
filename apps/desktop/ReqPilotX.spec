# -*- mode: python ; coding: utf-8 -*-

import argparse
import platform
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


_project_dir = Path(SPECPATH).resolve()
_repo_root = _project_dir.parents[1]
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

from scripts.release_preflight import validate_qdrant_artifact
from build_desktop import validate_postgres_distribution


_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--with-qdrant", action="store_true")
_parser.add_argument("--qdrant-manifest")
_parser.add_argument("--postgres-root")
_options = _parser.parse_args()

_binaries = []
_runtime_datas = [
    *collect_data_files("reqpilotx"),
    *collect_data_files("reqpilotx_server"),
]
_runtime_hiddenimports = [
    *collect_submodules("reqpilotx"),
    *collect_submodules("reqpilotx.adapters.documents"),
    *collect_submodules("reqpilotx.domain"),
    *collect_submodules("reqpilotx_server.resources"),
    *collect_submodules("reqpilotx_server.worker"),
    "local_worker_entry",
]
if not _options.postgres_root:
    raise SystemExit("A validated --postgres-root is required for a desktop build")
_postgres_root = validate_postgres_distribution(Path(_options.postgres_root))
for _part in ("bin", "lib", "share"):
    _runtime_datas.append((str(_postgres_root / _part), f"postgres/{_part}"))
if _options.with_qdrant:
    _manifest = (
        Path(_options.qdrant_manifest).expanduser().resolve()
        if _options.qdrant_manifest
        else (_project_dir / "release" / "qdrant-artifacts.json").resolve()
    )
    _qdrant = validate_qdrant_artifact(
        _project_dir,
        _manifest,
        target_platform=sys.platform,
        target_arch=platform.machine(),
    )
    _binaries.append((str(_qdrant.path), _qdrant.bundle_destination))


a = Analysis(
    [str(_project_dir / "app_desktop.py")],
    pathex=[
        str(_repo_root / "packages" / "engine" / "src"),
        str(_repo_root / "apps" / "server" / "src"),
        str(_project_dir),
    ],
    binaries=_binaries,
    datas=[
        (str(_project_dir.parent / "web" / "dist"), "frontend/dist"),
        *_runtime_datas,
    ],
    hiddenimports=_runtime_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "app",
        "torch",
        "sentence_transformers",
        "transformers",
        "tokenizers",
        "safetensors",
        "scipy",
        "sklearn",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ReqPilotX",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ReqPilotX",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ReqPilotX.app",
        icon=None,
        bundle_identifier=None,
    )
