# Desktop shell

`app_desktop.py`, `build_desktop.py`, `ReqPilotX.spec`, `scripts/` and `release/`
are the authoritative desktop launcher, packaging policy and verification tools.
The former prototype entries only forward here.

Build the Web artifact at the repository root with `npm ci && npm run build`.
Prepare `apps/desktop/.venv` with `prototype/requirements.txt` and installed
engine/server wheels, plus a complete PostgreSQL distribution whose `bin`,
`lib`, and `share` trees match the target OS. Then run
`.venv/bin/python build_desktop.py --postgres-root /absolute/postgresql` here. The
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

Normal desktop launch now owns one private PostgreSQL cluster under the selected
storage directory. An explicit `REQPILOT_DESKTOP_POSTGRES_BIN` selects a local
binary directory during development; a frozen build uses its bundled
`postgres/bin`. The launcher obtains an exclusive desktop lease, starts the
private database on an assigned loopback port, then starts the shared API and
one or two registered durable workers. It opens the window only after the API
and the worker fleet acknowledge readiness. Shutdown drains workers, closes the
API, stops only the identified PostgreSQL cluster, and releases both leases.
The database, object files and task records remain for restart. A crash leaves
an ownership receipt; the next launch verifies the database PID, start time,
recorded port and SQL data directory before stopping that orphan and recovering
the same data. Unidentified live databases and unknown nonempty directories are
refused. Worker children exit if their launcher heartbeat goes stale. While the
window is open, an exited worker is restarted at most three times per minute;
repeated failure closes the runtime rather than leaving a task queue that
silently appears ready.

On a Mac with local PostgreSQL installed, an empty temporary storage root can
exercise the real database/API/worker launch without opening a GUI:

```sh
REQPILOT_STORAGE_DIR=/absolute/private/empty/storage \
REQPILOT_DESKTOP_POSTGRES_BIN=/absolute/postgresql/bin \
python apps/desktop/app_desktop.py --runtime-smoke-seconds 2
```

The launcher only starts Qdrant when `REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant`
is explicitly configured; a DashScope key alone does not enable retrieval or
imply that Qdrant is bundled. Native task locks use the shared Windows/POSIX
file lease. The artifact self-test still uses isolated test storage; it does not
exercise the bundled PostgreSQL binary, the durable worker or either provider's
Agent tool loop. These must be checked on the built artifact separately.
In the frozen executable, portable tool subprocesses reenter through a restricted
Python child dispatcher. It accepts only scripts inside the registered plugin's
`scripts/` directory, the source capture module and the owned worker entry. The installed Windows
artifact still needs a real tool call test; source Python tests cannot prove that
PyInstaller collected every dependency.
