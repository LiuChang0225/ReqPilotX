#!/usr/bin/env python3
"""Return a stable SHA-256 for one JSON value read from standard input."""

from __future__ import annotations

import hashlib
import json
import sys


def main() -> int:
    value = json.load(sys.stdin)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sys.stdout.write(hashlib.sha256(payload).hexdigest() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
