"""Local admin invitation issuance. Never prints the token or creates a default user."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

from reqpilotx_server import db
from reqpilotx_server.saas.config import SaaSSettings
from reqpilotx_server.saas.store import add_invitation, ensure_schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private file for one invite; never overwrite")
    parser.add_argument("--seconds", type=int, default=86400)
    args = parser.parse_args()
    SaaSSettings.from_environment().validate(enabled_required=True)
    if not args.output.is_absolute() or not 300 <= args.seconds <= 604800:
        parser.error("Use an absolute private path and 300..604800 seconds")
    token = secrets.token_urlsafe(32)
    # O_EXCL/O_NOFOLLOW prevents overwriting a secret or following a symlink.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        db.init_db()
        ensure_schema()
        add_invitation(token, seconds=args.seconds)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        args.output.unlink(missing_ok=True)
        raise
    print("Created one expiring, single-use invitation in the requested private file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
