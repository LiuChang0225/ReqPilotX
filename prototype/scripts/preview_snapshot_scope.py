from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.services.extraction_preview import build_extraction_preview


def build_preview_in_isolated_workspace(
    file_path: str | Path,
    *,
    working_dir: Path | None = None,
    document_id: str = "",
) -> dict[str, Any]:
    """Build a legacy preview without ever selecting a source-adjacent snapshot root.

    Legacy preview enrichment needs a writable directory for transient table and
    image snapshots.  The product default deliberately prefers a directory next
    to the input file, which is appropriate for some interactive serving paths
    but unsafe for read-only corpus regression.  Script callers use this helper
    so preview assets live under their explicit output directory, or in a
    short-lived system temporary directory when the script has no output root.
    """

    parent: Path | None = None
    if working_dir is not None:
        parent = Path(working_dir).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)

    if parent is not None:
        return build_extraction_preview(
            str(file_path),
            document_id=document_id,
            snapshot_root=parent / "table_snapshots",
        )

    with tempfile.TemporaryDirectory(
        prefix=".reqpilotx-preview-",
        ignore_cleanup_errors=True,
    ) as temporary_directory:
        return build_extraction_preview(
            str(file_path),
            document_id=document_id,
            snapshot_root=Path(temporary_directory) / "table_snapshots",
        )
