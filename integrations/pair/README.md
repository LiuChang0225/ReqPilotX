# ReqPilotX application for Pair

The default bundle now **starts its own ReqPilotX service through Pair** and exposes native projects/document import. Users do not start another website or paste a workbench link. This is the managed runtime and document-entry increment; Agent execution/binding is not implemented yet.

## Current capabilities

- Installable Cordis bundle with separate Host and Client leaves. It is installed in the current development machine's normal Pair Web profile.
- On activation, the Host checks existing Python/PostgreSQL dependencies, acquires its own data-directory lease, initializes a private password-protected PostgreSQL cluster when needed, applies the shared migrations and starts the shared Python API on an OS-assigned loopback port. No worker or model process is launched.
- Native Pair project creation, project selection, PDF upload and original-page viewing. Reads/creates use the existing ReqPilotX API and storage; no second project/document ledger.
- Existing saved works are listed under the selected document and open the same shared readonly workbench. Pair automatically provides internal addresses and scope. Real Session binding, task creation/execution and progress are still pending.
- Disable/stop terminates only owned DSH subprocess handles; the API closes before the database. Data remains. Restart reuses the same cluster and project/document identities. Unknown nonempty directories and competing live owners are rejected, not adopted.

## Code and ownership

- `host/src/managed-runtime.js`: runtime identity, private data lease, dependency checks, cluster/API preparation, readiness and teardown.
- `host/src/managed_api.py`: socket allocated inside the child process; writes a private instance descriptor; serves the existing `app.main` with no worker.
- `host/src/index.js`: authenticated Pair route, runtime status and fixed-target transport. Managed mode allows project creation and document import POSTs; other writes remain unavailable. Pair cookies/auth are not forwarded to the backend; redirects are rejected. Range and revision headers are preserved.
- `client/src/catalog.cjs`: native projects/documents entry and service readiness, consuming shared APIs.
- `client/src/client.cjs`: shared-panel host adaptation and the explicit connect-mode development probe.
- `scripts/build.mjs`: packages the existing shared UI CJS/CSS into DSH lazy-CJS; React, JSX and ReactDOM use the Host instance.

Default data is under `~/.local/share/reqpilotx/pair`. Runtime credentials stay there with private permissions and are absent from public configuration responses/receipts. Ports are dynamic. Changing dataRoot selects a different owned application store; it does not import other stores automatically.

The existing PDF viewer is embedded with the correct `pdf` and `parentOrigin` parameters. It currently uses the owned API's loopback origin directly. A fully authenticated remote viewer/worker proxy is not yet supported.

## Development installation

Build from ReqPilotX root:

```sh
npm --prefix prototype/frontend run build:panel
node integrations/pair/scripts/build.mjs
PAIR_REPO=/absolute/path/to/PAIR node --test integrations/pair/tests/*.test.mjs
```

Through the supported `dsh` launcher in the Pair checkout, install all three local development directories:

```sh
pnpm dsh plugin --profile web add \
  "$REQPILOTX_ROOT/integrations/pair/host" \
  "$REQPILOTX_ROOT/integrations/pair/client" \
  "$REQPILOTX_ROOT/integrations/pair/bundle"
pnpm dsh web
```

Reload the Pair application after changing its profile package roster. Host/Client are plain dependencies, only the bundle is a profile layer. The default managed mode requires no backend URL. Application files are currently resolved from the development checkout, and its existing virtual environment supplies Python dependencies. These private/local-link packages are **not a self-contained registry release**; new-machine dependency distribution remains to be built.

For isolated development, use a separate profile and set `REQPILOT_PAIR_DATA_ROOT` to a new dedicated directory. Config additionally accepts `appRoot`, `python`, `postgresBinDir` for an explicit development installation. A missing dependency reports a startup error instead of changing another service or installing system software.

Only the legacy transport probe uses `REQPILOT_SERVICE_ORIGIN`; setting it selects readonly connect mode with the development link form. It is not the normal application flow.

## Verification and limits

Eight focused tests pass: lazy factory/shared React; slots/style disposal; fixed proxy origin; authentication/read-only mode/Range/credential isolation; managed project/import write allowlist; exclusive data ownership; unknown-directory refusal; teardown order and retained ownership on failed quiescence.

Actual DSH/Pair process validation: auto-created owned PostgreSQL/API, native project creation and synthetic PDF upload, downloaded source bytes identical, PDF page visible, controlled shutdown of both owned processes, restart with unchanged project/document records, and unchanged 18080/55472/5432 listeners. Normal Pair Web profile also loaded the bundle and automatically started its own store; sidebar shows projects instead of a link form. No models were invoked.

Unverified: abrupt-crash/orphan recovery, fresh-machine dependency installation, release distribution, existing-data import, full business writes, Agent preset/tools/work-Session binding and execution, real runtime events, Office/Windows/Desktop. An unexplained live database process is refused even under the owned directory; it is not silently adopted or killed. Project/import writes are not yet idempotent across an ambiguous network failure; UI suppresses concurrent submissions but does not promise retry deduplication.

## Upgrade with shared artifacts

Project/document requests now come from the same `createProjectDocumentClient` that the standalone app composes. The Pair catalog only supplies routing and transport. Shared UI is versioned by its generated `host-contract.json`; this adapter declares supported major/required exports in `client/adapter-contract.json`. Build rejects incompatible/missing capabilities before packaging and records `sharedArtifactIdentity`.

A release pipeline can build the shared UI once and supply it independently of the checkout:

```sh
node integrations/pair/scripts/build.mjs --shared-ui /path/to/shared-ui-output
```

Relocation tests copied both adapter build sources and shared artifacts outside the checkout and reproduced the same client hash. Eleven integration tests now pass. This demonstrates frontend artifact/build portability only: the Python app/venv and local-link npm install still need release packaging. Do not claim a complete application migration from this test.

## Runtime events versus business acceptance

`handoff_ready` means a program artifact is ready for handoff, not source completeness or semantic verification. Host `SUCCEEDED`/`COMPLETED` remains a run-terminal observation. The shared panel already labels it as the external turn ending; no runtime observation means unreported.

The pending real event adapter must project actual step starts, waiting duration, retries, attempt failures and terminal errors from public host events. Keep model/provider/client attribution unknown unless transport evidence separates them. A successful export or a large item count cannot promote a source-grounded FAIL to completeness PASS. Existing SourceVisual/locator contracts remain authoritative; no Pair-specific coordinate or business model is introduced.
