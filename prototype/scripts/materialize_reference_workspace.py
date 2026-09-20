#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.reference_workspace import (  # noqa: E402
    materialize_reference_workspace,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--document-role", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus-root", required=True, type=Path)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=PROTOTYPE_ROOT.parent,
    )
    args = parser.parse_args()
    result = materialize_reference_workspace(
        args.source,
        expected_source_sha256=args.expected_source_sha256,
        document_role=args.document_role,
        output_directory=args.output,
        corpus_root=args.corpus_root,
        repository_root=args.repository_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
