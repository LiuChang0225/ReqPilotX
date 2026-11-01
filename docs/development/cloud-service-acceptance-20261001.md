# 2026-10-01 synthetic service acceptance

The preceding cloud source delta remains an immutable milestone. This follow-on
checks the actual service/transport layers without a live provider, customer data
or browser-policy workaround. The authoritative Workspace95 → integrated145 →
cloud source reconciliation requirements are unchanged.

## Scoped PostgreSQL dependency

No PostgreSQL binaries or package cache were initially installed. As uid 1000,
the cloud downloaded signed Debian 13 archive metadata using the existing Debian
archive keyring, then downloaded and extracted two packages **inside the work
scratch directory**. No sudo, host package installation, system service, system
configuration or permission change was used. All required shared libraries were
already present. Tested version: PostgreSQL 17.11 (`17.11-0+deb13u1`).

| Package | Download SHA-256 |
|---|---|
| postgresql-17 | `d2ce1ddffafa783f9acda4c92c86fc21e8288bff3782d739294f14fa797d7886` |
| postgresql-client-17 | `9d8558f8dd57c8e92e218a20698383575d53742ca3f9e7c2b7fe5f246d5216ae` |

Packages were verified against the authenticated apt metadata. Temporary clusters
bind only to loopback, use newly owned data directories and are stopped by their
ownership receipts. They never adopt an existing database. Synthetic output is
retained for inspection and is excluded from source deltas.

## External workbench HTTP and recovery

`scripts/smoke_workbench.py` now checks actual FastAPI HTTP over a disposable
PostgreSQL database with all 37 migrations:

- PDF upload/source-byte download; Map → partial Items → exact feedback/replay →
  full eight source items/seven requirements, stable retained identity
- Immutable result predecessors; all synthetic human opinions through the
  dedicated response endpoint; exact command replay and stale-write rejection
- Explicit formal acceptance; missing acceptance rejected; frozen Excel bytes
  survive opinion withdrawal while current readiness becomes incomplete
- API process restart preserving task, source, feedback, opinions and frozen version
- `pg_dump`/`pg_restore` into a **different, newly created database**, plus a
  hash-verified copied storage root; original storage reads are actively denied
  in the restored API process, preventing an accidental read from the old store
- Same current snapshot and embedded Excel snapshot hash after restart and restore

Final receipt: `real-pg-external-lifecycle-v2-complete/receipt.json` (PASS). Recovery
copied 36 storage files and checked their hashes. This is a coordinated stopped-API
synthetic backup/restore; it is not online backup, hosted deployment disaster
recovery or recovery of the user's production data.

## Native provider and durable worker

`scripts/smoke_native_provider.py` uses the actual AzureOpenAI SDK with an invented
model/dummy value and an audited loopback HTTP mock. The mock dynamically chooses
registered tools; production tools capture the original PDF, compile Map and
Items, check a genuine classification revision, close the review, export and
request native publication. No host validation or publication receipt is forged.

Final receipt: `real-pg-native-provider-20261001-070050/receipt.json` (PASS):

- Native raw-source HTTP task admission and a real scoped PostgreSQL worker
- Original-page return receipt, complete eight-item publication and stable IDs
- A second fresh `--once --job-id` worker continues the same task/thread and frozen
  capability, producing a baseline successor without substituting a task
- Native human responses, exact replay, explicit formal freeze, frozen same-version
  Excel, withdrawal, and two API process restarts
- Both actual worker processes exit zero; API, PostgreSQL and mock are stopped

This final run records 29 actual SDK HTTP completions (27 registered tool calls
and two final messages), one hash-bound original-page image-return receipt,
baseline revision 2 → 3 and queue polling through PENDING/RUNNING/SUCCEEDED.

These replies are synthetic HTTP decisions, not Azure authentication, autonomous
model judgment or customer semantic acceptance. Aliyun's live endpoint allowlist
was not loosened to substitute a loopback endpoint. Existing Bailian SDK-only
contract evidence remains distinct from live Bailian acceptance.

## Installed Pair managed service

`integrations/pair/scripts/installed-smoke.mjs` checks non-editable local
engine/server wheels with independent built frontend, Pair-owned private
password-protected PostgreSQL and the actual managed proxy. The disposable
installed venv reused the already tested dependency site-packages via an explicit
`.pth`; engine/server origins resolve inside the new installed venv, not the
checkout. This saves downloads and is not a fresh-machine dependency installer.
The engine wheel is a transitional `0.0.0` build from the no-Git cloud source, not
a governed published release with clean commit provenance.

Final receipt: `real-pg-pair-installed-complete/receipt.json` (PASS): upload/read,
response CAS/replay, formal freeze/export, stop/restart preserving task data,
opinions/frozen Excel, invalid setup and competing-owner rejection. No model or
worker is launched by this managed service; actual Pair UI and external DSH Session
admission remain separate. Optional sixth argument retains its owned smoke output
rather than removing it after teardown.

## Portable fixed CLI and actual MCP stdio

`scripts/smoke_portable_service.py` uses an explicit development plugin, actual
stdio MCP servers and fixed CLI subprocesses against the real isolated API/PG.
Final receipt: `real-pg-portable-service-complete/receipt.json` (PASS):

- Source-bound task creation, capture command replay, original-page read, shared
  Map/eight Items compilation and actual task-bound HTTP synchronization
- Local review/export registration; real feedback is imported once and the next
  pull does not duplicate it
- Synthetic human opinions/formal acceptance are authored through the separate
  application HTTP interface; MCP only reads pinned opinions/preview/history
  and rejects human-authoring input
- Formal Excel bytes and embedded snapshot match the exact frozen result;
  a new MCP process resumes the explicit task and reads the same delivery,
  and an API restart preserves the frozen bytes

The source/dependency/semantic checks still separate saved, synchronized,
registered and accepted states. No autonomous host model was run, and this is
not the clean governed portable release or a customer-quality test.

## Reproduction

Supply existing dependencies and PostgreSQL binaries explicitly. Each smoke
requires a new output directory and does not install dependencies or use an
existing user database:

```sh
"$PYTHON" scripts/smoke_workbench.py --postgres-bin "$POSTGRES_BIN" --output "$NEW_EXTERNAL_OUTPUT"
"$PYTHON" scripts/smoke_native_provider.py --postgres-bin "$POSTGRES_BIN" --output "$NEW_NATIVE_OUTPUT"
"$PYTHON" scripts/smoke_portable_service.py --postgres-bin "$POSTGRES_BIN" --output "$NEW_PORTABLE_OUTPUT"
node integrations/pair/scripts/installed-smoke.mjs "$INSTALLED_PYTHON" "$FRONTEND_ROOT" "$POSTGRES_BIN" "$SYNTHETIC_FIXTURE" "$REPORT_JSON" "$NEW_PAIR_OUTPUT"
```

The Pair fixture contains `example.pdf`, `map/`, and `baseline/` compiled from the
same invented source. Local credentials stay private inside the disposable Pair
owned store and are absent from public receipts/source deltas. Network audit hooks
limit Python smoke subprocesses to loopback. Browser rendering remains untested:
the cloud browser returned `net::ERR_BLOCKED_BY_CLIENT`, and that restriction was
not bypassed. Actual Mac/Bailian, Windows11, installed employee-host/browser/PDF
and customer-quality acceptance are still required before a complete trial release.
