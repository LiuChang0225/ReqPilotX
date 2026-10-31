# Pair lightweight local SQLite (RX01)

Status: RX01 implementation slice. This document describes the storage and
composition contract proven by the current branch; it is not acceptance of
RX02–RX07 or of the complete Pair migration.

## Scope

An ordinary Pair-managed ReqPilotX activation selects the existing complete
workbench and an application-owned SQLite database. It does not set
`REQPILOT_TEST_MODE`, does not construct the in-memory/test repositories, does
not start PostgreSQL, and does not need a PostgreSQL executable or Python
driver in the local environment.

The shared business API, source identity, command receipts, plugin-workbench
feedback, engineer responses, immutable delivery versions and frozen Excel
bytes remain owned by the existing ReqPilotX code paths.

## Backend selection

`RuntimeSettings.from_environment` selects one explicit backend:

| Deployment | Configuration | Storage |
|---|---|---|
| local | default, or `REQPILOT_DATABASE_BACKEND=sqlite-local` | `sqlite-local` |
| local | `REQPILOT_DATABASE_URL` set, or `REQPILOT_DATABASE_BACKEND=postgresql` | `postgresql` |
| test | `REQPILOT_TEST_MODE=1` | `sqlite-test` (unchanged) |
| cloud | `REQPILOT_ENV=cloud` plus `REQPILOT_DATABASE_URL` | `postgresql` |

A local `REQPILOT_DATABASE_URL` together with
`REQPILOT_DATABASE_BACKEND=sqlite-local` is rejected instead of silently
choosing one. Cloud mode keeps its existing host/origin/OSS/PostgreSQL
requirements. `psycopg[binary,pool]` is the optional `postgres` extra of
`reqpilotx-server`, not a dependency of the local runtime.

## Database ownership and migrations

The local database is `<REQPILOT_STORAGE_DIR>/app.db`, owned by the ReqPilotX
server. Pair does not put business tables in DSH or Session SQLite.

Package resource `reqpilotx_server/resources/migrations/sqlite/` is the
lightweight schema source. Startup records applied files in
`reqpilotx_local_schema_migrations`, rejects a changed checksum for an applied
migration, and runs additive DDL only for unapplied versions. PostgreSQL keeps
its separate `resources/migrations/postgresql/` path and ledger.

Each connection enables foreign keys, WAL, a 30-second busy timeout and
`BEGIN IMMEDIATE` for multi-writer business sections. Commits remain short;
model or long-running parsing work is not held inside a write transaction.
Project creation and document import use the existing command-receipt table:
same input replays the original resource, different input conflicts with 409,
and an uncommitted/failed write is not reported as saved.

Only the supported local backend uses `app.db`. The test backend remains
separate and retains test-only host/path allowances; none of those allowances
are enabled by local mode.

## Files and source identity

Uploaded source bytes stay in the application storage tree (`uploads/` for the
local direct-file route). The database stores the immutable SHA-256, byte size,
declared extension, detected format and preflight schema version. Authenticated
download returns the original bytes, and export/plugin-workbench reads are bound
to that source identity. Plugin-workbench drafts, feedback, response versions
and delivery Excel live under the configured application object root, never in
DSH storage.

An older data root containing `postgres/PG_VERSION` or `database-password` is
refused and left untouched. Moving such data requires the explicit RX06
migration path; the lightweight runtime does not adopt, mutate or silently
reinterpret a PostgreSQL store.

## Pair activation contract

`ManagedRuntime` requires an absolute installed Python, an absolute built
frontend root and an owned data directory. It runs a non-editable installation
probe, applies the SQLite migrations through `managed_api.py`, starts the API on
an OS-assigned loopback socket and verifies readiness before reporting READY.
It never resolves or spawns `initdb`, `postgres`, `psql`, `createdb` or
`pg_isready`.

The existing runtime.lock ownership, owner marker, authenticated challenge
proof, fixed-target proxy, restart identity and owned-subprocess cleanup are
preserved. A stopped runtime terminates only the API subprocess it started and
retains project data.

## Proven in this slice

`integrations/pair/scripts/installed-smoke.mjs` builds a real installed Python
environment, removes PostgreSQL from `PATH`, and runs the actual managed
subprocess. It observes project creation/import, original byte digest, command
replay, saved work, feedback, one engineer-response revision and formal delivery
version, a service restart with the same project/document/version identity, the
same frozen Excel bytes, competing-owner rejection and the complete frontend
route. See `reports/pair-lightweight/work/01-result.md` for the exact run.

## Not implemented here

RX01 does not claim the fixed RX02 workflow, native Session continuation,
project references, team knowledge, old PostgreSQL data migration, or complete
Web acceptance. Deterministic parsing/OCR/job execution that still requires the
PostgreSQL durable worker remains deferred and is surfaced as unavailable rather
than reported as complete. The legacy `prototype/start.sh` worker pool remains a
PostgreSQL development path and is not the Pair local activation path. Existing
direct-file source identities are bound to the configured storage root, so
relocating a populated root is not supported in RX01. The installed smoke uses
synthetic material and no model.
