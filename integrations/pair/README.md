# ReqPilotX application for Pair

The bundle starts its owned ReqPilotX service using an explicitly configured,
non-editable engine/server installation and an independent Web artifact. It
exposes native projects/document import and saved results. Agent execution and
work-session binding remain unimplemented.

## Current capabilities

- Installable Cordis bundle with separate Host and Client leaves. The R4 adapter is independently tested; no external Pair profile is modified by its verification.
- On activation, the Host checks existing Python/PostgreSQL dependencies, acquires its own data-directory lease, initializes a private password-protected PostgreSQL cluster when needed, applies the shared migrations and starts the shared Python API on an OS-assigned loopback port. No worker or model process is launched.
- Native Pair project creation, project selection, PDF upload and original-page viewing. Reads/creates use the existing ReqPilotX API and storage; no second project/document ledger.
- Existing saved works are listed under the selected document and open the same shared readonly workbench. Pair automatically provides internal addresses and scope. Real Session binding, task creation/execution and progress are still pending.
- Disable/stop terminates only owned DSH subprocess handles; the API closes before the database. Data remains. Restart reuses the same cluster and project/document identities. Unknown nonempty directories and competing live owners are rejected, not adopted.

## Code and ownership

- `host/src/managed-runtime.js`: runtime identity, private data lease, dependency checks, cluster/API preparation, readiness and teardown.
- `host/src/managed_api.py`: checks installed distribution origins and rejects editable/legacy `app` imports; invokes installed migration capabilities and serves `reqpilotx_server.main` on a child-allocated loopback socket, with no worker.
- `host/src/index.js`: authenticated Pair route, runtime status and fixed-target transport. Managed mode allows project creation and document import POSTs; other writes remain unavailable. Pair cookies/auth are not forwarded to the backend; redirects are rejected. Range and revision headers are preserved.
- `client/src/catalog.cjs`: native projects/documents entry and service readiness, consuming shared APIs.
- `client/src/client.cjs`: shared-panel host adaptation and the explicit connect-mode development probe.
- `scripts/build.mjs`: packages the existing shared UI CJS/CSS into DSH lazy-CJS; React, JSX and ReactDOM use the Host instance.

Default data is under `~/.local/share/reqpilotx/pair`. Runtime credentials stay there with private permissions and are absent from public configuration responses/receipts. Ports are dynamic. Changing dataRoot selects a different owned application store; it does not import other stores automatically.

The existing PDF viewer is embedded with the correct `pdf` and `parentOrigin` parameters. It currently uses the owned API's loopback origin directly. A fully authenticated remote viewer/worker proxy is not yet supported.

## Development installation

Build from ReqPilotX root:

```sh
npm ci
npm run build
npm run build:panel
node integrations/pair/scripts/build.mjs
npm --prefix integrations/pair/tests/react-host ci
PAIR_REACT_ROOT="$PWD/integrations/pair/tests/react-host" node --test integrations/pair/tests/*.test.mjs
```

Through the supported `dsh` launcher in the Pair checkout, install all three local development directories:

```sh
pnpm dsh plugin --profile web add \
  "$REQPILOTX_ROOT/integrations/pair/host" \
  "$REQPILOTX_ROOT/integrations/pair/client" \
  "$REQPILOTX_ROOT/integrations/pair/bundle"
pnpm dsh web
```

Reload Pair after changing its profile package roster. Host/Client are dependencies;
only the bundle is a profile layer. Before activation, prepare a non-editable
installation (wheel paths are explicit; this is an operator step, not a runtime download):

```sh
uv venv /absolute/private/reqpilotx-venv
uv pip install --python /absolute/private/reqpilotx-venv/bin/python /release/reqpilotx_engine-VERSION-py3-none-any.whl /release/reqpilotx_server-0.1.0-py3-none-any.whl
export REQPILOT_PAIR_PYTHON=/absolute/private/reqpilotx-venv/bin/python
export REQPILOT_PAIR_FRONTEND_ROOT=/release/web/dist
export REQPILOT_PAIR_POSTGRES_BIN=/absolute/postgresql/bin
export REQPILOT_PAIR_DATA_ROOT=/absolute/private/reqpilotx-pair-data
```

The adapter has no checkout or `.venv` fallback and never installs dependencies.
Its child explicitly receives storage, object, scratch and frontend paths. Runtime
Python uses `-I`; editable packages and visible legacy `app` are rejected. The
three local Cordis packages are not a registry release or fresh-machine installer.

Config accepts `python`, `frontendRoot`, `postgresBinDir` and `dataRoot` (absolute
paths). Former `appRoot` checkout configuration is explicitly rejected. For a
development server use explicit readonly `mode: connect` with `serviceOrigin`;
failed installed setup never selects connect mode automatically.

Only the legacy transport probe uses `REQPILOT_SERVICE_ORIGIN`; setting it selects readonly connect mode with the development link form. It is not the normal application flow.

## Verification and limits

Contract tests cover lazy factory/shared React, style disposal, fixed proxy origin,
authentication/Range/revision/credential isolation, write allowlist, exclusive
ownership, invalid roots and dependencies, teardown and retained ownership on failure.
The React 18 test fixture is a module host, not the external Pair UI.

R4's `scripts/installed-smoke.mjs` uses real OS children implementing the subprocess
handle interface, relocated host files, non-editable wheels and private PostgreSQL.
It verifies project/import, source bytes, saved version snapshot/export, frontend,
readiness, competing owner rejection and restart identity. See the R4 architecture
record for exact commands and evidence. Historical DSH/UI checks on the previous
checkout-based adapter are not acceptance of the current external Pair UI.

Unverified: abrupt-crash/orphan recovery, fresh-machine dependency installation, release distribution, existing-data import, full business writes, Agent preset/tools/work-Session binding and execution, real runtime events, Office/Windows/Desktop. An unexplained live database process is refused even under the owned directory; it is not silently adopted or killed. Project/import writes are not yet idempotent across an ambiguous network failure; UI suppresses concurrent submissions but does not promise retry deduplication.

## Upgrade with shared artifacts

Project/document requests now come from the same `createProjectDocumentClient` that the standalone app composes. The Pair catalog only supplies routing and transport. Shared UI is versioned by its generated `host-contract.json`; this adapter declares supported major/required exports in `client/adapter-contract.json`. Build rejects incompatible/missing capabilities before packaging and records `sharedArtifactIdentity`.

A release pipeline can build the shared UI once and supply it independently of the checkout:

```sh
node integrations/pair/scripts/build.mjs --shared-ui /path/to/shared-ui-output
```

Relocation tests copy adapter build sources and shared artifacts outside the
checkout and reproduce the same client hash. The installed service lifecycle has
its own real process test; neither test establishes external Pair UI acceptance.

## Runtime events versus business acceptance

`handoff_ready` means a program artifact is ready for handoff, not source completeness or semantic verification. Host `SUCCEEDED`/`COMPLETED` remains a run-terminal observation. The shared panel already labels it as the external turn ending; no runtime observation means unreported.

The pending real event adapter must project actual step starts, waiting duration, retries, attempt failures and terminal errors from public host events. Keep model/provider/client attribution unknown unless transport evidence separates them. A successful export or a large item count cannot promote a source-grounded FAIL to completeness PASS. Existing SourceVisual/locator contracts remain authoritative; no Pair-specific coordinate or business model is introduced.
