# Desktop shell

`app_desktop.py`, `build_desktop.py`, `ReqPilotX.spec`, `scripts/` and `release/`
are the authoritative desktop launcher, packaging policy and verification tools.
The former prototype entries only forward here.

Build the Web artifact at the repository root with `npm ci && npm run build`.
Prepare `apps/desktop/.venv` with `prototype/requirements.txt` and installed
engine/server wheels, then run `.venv/bin/python build_desktop.py` here. The
builder requires committed source inputs and runs the artifact self-test.
Frontend comes from `apps/web/dist`, frozen as `frontend/dist`; server resources
and engine modules retain the existing collection policy. Writable storage is
outside the installed application.

Unfrozen, non-portable checkout launches (including the old prototype entry)
default to that checkout's `prototype/storage`. Explicit `REQPILOT_STORAGE_DIR`
still takes precedence; frozen/portable launches retain their per-user directory,
and artifact smoke requires its isolated absolute root. No data migration occurs.

Independent self-test: `.venv/bin/python -m scripts.artifact_self_test --dist dist`.
This uses isolated test/SQLite/local object storage, no model or worker. It is
separate from Pair's installed PostgreSQL check. macOS signing/notarization and
Windows/Linux/fresh-machine distribution require their own verification.
