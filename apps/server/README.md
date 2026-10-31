# ReqPilotX server

This package owns the API, durable worker, configuration, persistence, object
storage and concrete Agent-host adapters. It depends on `reqpilotx-engine` and
never carries a second copy of the engine implementation.

Installed entrypoints are `reqpilotx-api` and `reqpilotx-worker`.
`reqpilotx-saas-invite` is an optional local administrator command that writes
one expiring single-use invite to a new private file without printing its token.
The Web account boundary is opt-in; see
[SaaS ownership contract](../../docs/architecture/saas-account-boundary.md) and
[private pilot deployment](../../docs/deployment/saas-pilot.md). Optional heavyweight
retrieval adapters are installed with `reqpilotx-server[retrieval]`; OSS uses
`reqpilotx-server[oss]`. The lightweight local-object profile does not include them.

The legacy
`prototype/start.sh` and `prototype/app` imports remain development compatibility
routes. Desktop packaging composes `reqpilotx` and `reqpilotx_server` directly;
the PyInstaller search roots and release preflight do not consume the legacy
`app` namespace.

Writable state is always host configured with `REQPILOT_STORAGE_DIR` and
`REQPILOT_OBJECT_ROOT`; installed code never writes beside site-packages.
`REQPILOT_FRONTEND_DIR` selects the separately built frontend. Checkout-only
Agent Kit and development-source bundle operations require the explicit absolute
`REQPILOT_DEVELOPMENT_ROOT`.

Local mode defaults to the application-owned `sqlite-local` backend at
`<REQPILOT_STORAGE_DIR>/app.db` with package migrations under
`resources/migrations/sqlite/`. Startup rejects unknown applied migrations and
changed/missing migration files before schema changes, and commits each pending
migration atomically with its ledger row. Existing SQLite files are preflighted
read-only before writable PRAGMAs; unversioned/unknown stores are refused
unchanged, while a new empty destination initializes. A new SQLite store is also
refused beside historical PostgreSQL cluster/password markers. Set
`REQPILOT_DATABASE_URL` or `REQPILOT_DATABASE_BACKEND=postgresql` to keep the
explicit PostgreSQL route; an omitted URL keeps the existing
`postgresql://localhost/reqpilotx` default, and `psycopg[binary,pool]` is then
installed with the `postgres` extra. Test mode
(`REQPILOT_TEST_MODE=1`) remains a separate `sqlite-test` backend. PostgreSQL
migrations, SQLite migrations and the App Server protocol lock are read-only
package resources. See [Pair lightweight SQLite](../../docs/architecture/pair-lightweight-sqlite.md).

Project creation and source import accept an optional body/form `command_id`.
Migration `0038_entry_command_receipts.sql` binds unique command receipts to the
business record in the same transaction. Same-input replay returns the original
record; conflicting input returns 409. Read-only command routes recover an
ambiguously acknowledged commit. Shared source objects are retained on failed or
unknown DB saves and explicit document removal until a durable deletion fence
can protect concurrent imports. Document bindings and private derived caches are
still removed. See [entry command contract](../../docs/architecture/project-source-entry-commands.md)
for routes, compatibility, cleanup and the current single-owner boundary.

Provider-backed windowed table/text execution uses the separately released compatibility kit, configured by absolute `REQPILOT_STRUCTURED_AGENT_KIT_ROOT`; the bare server wheel does not embed it. Exact kit identity and source/provider/model binding are checked before execution and resume. See `docs/issues/provider-tool-loop-foundation.md` for the current synthetic evidence and live-acceptance boundaries.
