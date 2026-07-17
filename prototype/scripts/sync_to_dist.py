"""Sync latest source code to the portable distribution directory.

Run this before launching the desktop app to ensure it uses the latest code.

Usage:
    python scripts/sync_to_dist.py
"""
import shutil
from pathlib import Path

PROTOTYPE_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = PROTOTYPE_DIR / "dist" / "ReqPilotX-Portable"

# Files to sync (source -> dist relative paths)
SYNC_FILES = [
    "app/main.py",
    "app/db.py",
    "app/runtime_paths.py",
    "app/runtime_readiness.py",
    "app/services/ai_copilot.py",
    "app/services/assessor.py",
    "app/services/rag.py",
    "app/services/parser.py",
    "app/services/extractor.py",
    "app/services/extraction_preview.py",
    "app/services/table_assets.py",
    "app/services/complex_table_mode.py",
    "app/services/exporter.py",
    "app/services/preview_cache.py",
    "app/services/phase0_review.py",
    "app/services/iteration_insights.py",
    "app/services/appendix_scopes.py",
    "app/services/progress_tracker.py",
    "app/static/app.js",
    "app/static/index.html",
    "app/static/home.html",
    "app/static/home.js",
    "app/static/home.css",
    "app/static/style.css",
]


def main() -> None:
    if not DIST_DIR.exists():
        print(f"ERROR: Distribution directory not found: {DIST_DIR}")
        return

    synced = 0
    skipped = 0
    for rel_path in SYNC_FILES:
        src = PROTOTYPE_DIR / rel_path
        dst = DIST_DIR / rel_path
        if not src.exists():
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        synced += 1
        print(f"  synced: {rel_path}")

    # Clear __pycache__ in dist to force reload
    for pycache in DIST_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)

    print(f"\nDone: {synced} files synced, {skipped} skipped, __pycache__ cleared.")
    print("You can now restart the desktop app to use the latest code.")


if __name__ == "__main__":
    main()
