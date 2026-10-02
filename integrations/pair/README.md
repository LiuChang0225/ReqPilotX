# ReqPilotX application for Pair

The bundle starts its owned ReqPilotX service using an explicitly configured,
non-editable engine/server installation and an independent Web artifact. It
exposes native projects/document import and saved results. Task preparation, persistent host binding and Session request submission are wired behind an explicitly configured frozen capability package. The first paid native-UI synthetic run has completed; efficiency, MCP-model acceptance and full product release remain pending.

## Current capabilities

- Installable Cordis bundle with separate Host and Client leaves. The R4 adapter is independently tested; no external Pair profile is modified by its verification.
- On activation, the Host checks existing Python/PostgreSQL dependencies, acquires its own data-directory lease, initializes a private password-protected PostgreSQL cluster when needed, applies the shared migrations and starts the shared Python API on an OS-assigned loopback port. No worker or model process is launched.
- Native Pair project creation, project selection, PDF upload and original-page viewing. Reads/creates use the existing ReqPilotX API and storage; no second project/document ledger.
- Existing saved works are listed under the selected document and open the same shared workbench. Managed Pair enables source-bound feedback, Map confirmation, engineer opinions and immutable delivery saves through the application's persisted plugin-workbench API; connect mode remains readonly. Pair automatically provides internal addresses and scope. A bound task may stop its live Pair turn or submit an explicit continuation to the same Session. Business completion, endpoint reconnection and push Agent events remain pending.
- Disable/stop terminates only owned DSH subprocess handles; the API closes before the database. Data remains. Restart reuses the same cluster and project/document identities. Unknown nonempty directories and competing live owners are rejected, not adopted.

## Code and ownership

- `host/src/managed-runtime.js`: runtime identity, private data lease, dependency checks, cluster/API preparation, readiness and teardown.
- `host/src/managed_api.py`: checks installed distribution origins and rejects editable/legacy `app` imports; invokes installed migration capabilities and serves `reqpilotx_server.main` on a child-allocated loopback socket, with no worker.
- `host/src/index.js`: authenticated Pair route, runtime status and fixed-target transport. Managed mode allows project creation, document import, and feedback/Map-confirmation, engineer-response and immutable delivery-save POSTs only for a concrete plugin work ID. Other writes remain unavailable. Pair cookies/auth are not forwarded to the backend; redirects are rejected. Range and revision headers are preserved.
- `client/src/catalog.cjs`: native projects/documents entry and service readiness, consuming shared APIs. Its exact-path write policy is embedded from Host discovery and grants each result operation separately; a feedback grant cannot authorize human opinions or delivery saves.
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
readiness, competing owner rejection and restart identity. It also exercises the
actual managed response/delivery POST allowlist: source-bound human opinions,
exact command replay, stale-write rejection, explicit formal acceptance and
unchanged frozen snapshot/Excel bytes after restart. An optional sixth argument
selects a new retained smoke-output directory; owned processes stop but synthetic
data remains for inspection. The 2026-10-01 cloud Linux service receipt passed
with PostgreSQL 17.11 and non-editable transitional wheels, reusing already
tested dependencies; this is not a fresh-machine installer or published release.
See [cloud service acceptance](../../docs/development/cloud-service-acceptance-20261001.md)
and the R4 architecture
record for exact commands and evidence. Historical DSH/UI checks on the previous
checkout-based adapter are not acceptance of the current external Pair UI.

Unverified: abrupt-crash/orphan recovery, fresh-machine dependency installation, release distribution, existing-data import, full business writes, push runtime events, Office/Windows/Desktop. An unexplained live database process is refused even under the owned directory; it is not silently adopted or killed. Project/import writes are not yet idempotent across an ambiguous network failure; UI suppresses concurrent submissions but does not promise retry deduplication.

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

## Application discovery contract

The authenticated `/reqpilotx/config` response now implements `reqpilot-application-host/v1`. `application-discovery.cjs` is embedded from the Host source into the Client build; the build receipt records its hash. This is a Pair transport contract, not a new business schema or an implementation of MCP Apps.

Managed `applicationId` derives from persistent storage ownership and is independent of process `instanceId` and service port. It is not user/tenant authentication. Connect mode has no verified persistent identity and reports null. The Client checks protocol, required capability fields and managed identity before showing controls. Its host identity stays stable while its transport identity changes with the endpoint, invalidating old requests.

Only implemented project/document/result operations are advertised; Agent start is advertised only when the frozen task package passes doctor and the Pair services are available; resume/cancel and real Agent events remain false. The server still enforces authentication, method/route policy and readiness; advertised capabilities are not an authorization token. Public configuration uses a whitelist and does not serialize private installation data or raw failure text. READY is not semantic acceptance.

This increment has contract/module tests and synthetic installed service-process verification. External Pair UI verification must be performed against a matching release. It does not yet restore a selected document or running business task after disconnection. Research, architectural decisions and the next implementation slices are recorded in `docs/需求提取/Pair应用插件最佳实践与实施20260922.md`.

## Task launch increment

Set `REQPILOT_PAIR_CAPABILITY_ROOT` to a built bundle containing `scripts/host_launch.py`; optional `REQPILOT_PAIR_AGENT_PRESET` defaults to the shipped `standard` preset. The capability package is explicitly supplied, never downloaded or silently replaced. Its Python dependencies must be satisfied by the configured installed interpreter.

The native document form supplies an exact user goal and optional Map-confirmation choice. Host source bytes are checked against the service identity, then the shared package prepares the task and brief. The Host registers the execution workspace, adopts a preassigned Session ID and submits a stable DSH requestId. No model is dispatched by merely opening the application or inspecting a receipt. `/reqpilotx/tasks/start` returns acceptance only; `/reqpilotx/tasks/{commandId}` is read-only.

Host receipts pin the bundle SHA, interpreter location, preset, task/work, Session and request identity. Conflicting reuse is rejected; ambiguous acknowledgements retry the same identity. Same-task initial workbench synchronization now uses a deterministic session ID in the common portable transport. Existing bindings are preserved.

25 Pair checks (including built React18 form interaction), 9 common task/workbench tests and 16 transport tests passed. `scripts/task-launch-smoke.mjs` executes a real frozen-package CLI against synthetic HTTP but mocks the DSH Session controller; it never invokes a model. This is not live Pair acceptance. Changed service endpoints require explicit reconnection before resubmitting an unfinished launch; semantic resume and Agent events remain unavailable.

Full implementation and private proof references: `docs/需求提取/Pair任务发起与会话绑定20260922.md`.

## Native DSH admission probe

`scripts/dsh-binding-smoke.mjs <Pair checkout> <python> <frozen bundle> <new private output>` starts only the supported `dsh --profile web` surface in a private DSH_HOME. Default mode calls real Workspace/Session creation and blocks prompt admission. `--admission` uses the real prompt controller with DSH's scripted LLM adapter seam; `--recover` confirms acceptance from the real Session journal after a test endpoint change. Nonlocal fetch is refused, production credentials are not copied and title-model calls are disabled.

Observed: real admission retry leaves one user request and one scripted turn; journal-based acceptance recovery makes no second prompt. Remote inference calls are zero. The source HTTP service is synthetic, and these probes are not external Pair UI, PostgreSQL integration, extraction or semantic acceptance tests. See `docs/需求提取/Pair真实DSH绑定与接收验证20260922.md`.

TaskLauncher now confirms an ambiguous START_REQUESTED record from an exact persisted user rpcId when possible, without sending another prompt or changing the task binding. Context-injection messages do not count. This is acknowledgement repair, not business-task resume; changed endpoints without such evidence still require explicit reconnection.


## Public execution observation

The task receipt GET can now include `execution`, projected from the exact bound DSH request's public turn/step/tool/retry lifecycle and current Agent status. No message, tool arguments, model streams or raw failures are returned. The native document page polls sequentially every three seconds and passes the observation through the existing shared panel contract. A completed turn is explicitly separate from result acceptance. `events.agent` remains false until a push event adapter exists. Explicit cancel/continue controls use the bound task receipt and never imply result acceptance.

The first paid native-UI run used CLI and uncovered missing MCP attachment. New Bundle launches now set `taskMcp: true`: the common helper publishes a frozen MCP descriptor, and `task-mcp.js` mounts the official DSH bridge within the owning Agent before its first request. Install `plugins/reqpilot/requirements-mcp.txt` into the configured Python environment; incompatible helper protocols or SDKs keep task launch unavailable. Existing CLI task receipts remain unchanged. Real DSH discovery and task_open are verified with a scripted adapter; paid-model extraction through this MCP path remains pending. Current evidence and limitations: `docs/需求提取/Pair原生界面付费模型验收20260922.md`.

Task-scoped MCP implementation and failed/proven probe boundaries: `docs/需求提取/Pair任务隔离MCP接入20260922.md`.


The two-session native DSH probe is `scripts/dsh-mcp-isolation-smoke.mjs <Pair checkout> <MCP-ready Python> <fixed bundle> <new private output>`. It also observes a third unrelated Session and adapter-only disposal while task Agents remain alive. The new Host code releases its per-Agent listeners/bridges on either Host or Agent teardown; it does not dispose the Agents or their other tools. The paid same-case MCP trial completed, but its source-semantic findings failed; elapsed time and call counts are observations, not release or causal performance claims. See `docs/需求提取/PairMCP同案复测20260922.md`.

## Managed feedback boundary (2026-10-01)

Managed Pair advertises feedback and Map confirmation only after its owned service is ready. Its embedded shared panel enables those actions for the selected saved work; the browser and Host each restrict POST to that work's plugin-workbench `feedback` or `confirm-map` endpoint. The backend checks source hash, current immutable revision, referenced source item/node and retry command identity before persisting. Historical versions remain reviewable but cannot receive a new edit. Excel continues to export the pinned immutable version through the same workbench endpoint.

These controls have build, browser transport and authenticated loopback proxy tests. They have not yet been exercised in an external Pair browser against the managed PostgreSQL service after this change. Saved feedback is an inbox entry for the associated task, not proof that an Agent retrieved it or revised the draft. Local Pair authentication protects the route, while the owned single-user service has no separate per-project user authorization.

The explicit task control endpoint checks the saved command, Pair application, Session, work reference and source hash before calling DSH. A continuation reuses its durable request ID and can repair a lost acknowledgement from that Session's exact user message; a cancel calls DSH `sessionController.cancel` for only the bound Session and retains queued inbox work. The UI offers cancel during a reported running turn and continuation after a reported terminal/idle turn. These are Agent-turn controls; a successful control receipt does not claim document-task recovery or a correct revised version. The isolated Mac DSH probe with a scripted model and synthetic PDF recorded one same-Session continuation and an active turn ending `aborted` after cancel at `task/pair-dsh-cancel-fixed-20261001/receipt.json`; remote model calls were zero. Installed Pair UI and managed-service/real-model end-to-end acceptance remain open.


### External response and delivery API (2026-10-01 candidate)

The managed proxy now advertises `results.engineer_responses` and
`results.save_version`. These do not enable writes in readonly-connect mode.
The business service, not the Pair host, validates every source, session,
result revision, response revision and unit-context binding.

Under `/api/projects/{project}/sor-documents/{document}/agent-structure/delivery/plugin-workbench/{session}`:

- `GET/POST engineer-responses`: read pinned responses or save explicit human
  choices. Writes require the read view's opaque `expected_version`, exact
  `expected_revision_id`, source/baseline hashes, response revision, a stable
  command ID and each unit context hash. A successful write creates a new
  immutable result revision; old snapshots and exports stay unchanged.
- `GET versions/preview`: return the exact Matrix snapshot, CAS token and
  readiness; `revision` optionally pins the result being inspected.
- `POST versions`: save DRAFT or explicitly accepted ready FORMAL snapshot plus
  frozen Excel bytes. Require the preview's result/source/response/version and
  delivery hash. A retry with identical command/content returns its original
  version; reusing that command with different content conflicts.
- `GET versions`, `GET versions/{number}` and `GET versions/{number}/export`:
  list, inspect and download the same immutable delivery. Frozen deliveries
  remain readable after the original is replaced; creating a new save against
  an obsolete source is rejected. Byte and snapshot hashes are verified.

External Agent uploads still cannot author manual-opinion artifacts. The server
carries existing opinions through the shared context validator when a new
external result arrives; changed or removed evidence makes them stale, and a
later restoration does not silently revive them. A response save is a working
opinion, not formal acceptance. Source re-extraction and model calls are absent.

Focused synthetic backend/ASGI, task-facade and managed-proxy tests cover these
contracts. This API evidence does not establish installed Pair UI, Windows,
company authorization or real customer-material acceptance.


## Managed result-write transport correction (2026-10-02)

The 67bb444 source snapshot already advertised and proxied human opinions and
immutable delivery saves, but the browser Client rejected their POST routes.
This increment connects those existing business APIs rather than introducing
another response/version store. Four independent discovery flags cover feedback,
Map confirmation, engineer responses and immutable saves. Unknown/missing flags,
malformed work IDs, mutation descendants and other methods stay blocked. The
Client protocol requires both newer result flags; incompatible mixed releases
fail before showing controls. Rebuild the shared panel and Pair Client together.

The embedded shared provider intersects the host's optional `workCapabilities`
with the business service capabilities. A writable feedback surface cannot make
an undiscovered human-response or save-version action writable; historical
versions remain read-only. Legacy `makePairWorkbenchHost(..., true)` retains only
its original feedback/Map grants. Connect mode cannot become writable by passing
flags without a verified managed application identity.

Offline source verification uses actual Client JavaScript, the authenticated
Host proxy and actual FastAPI routes around the existing synthetic compiler
fixture. It exercises exact command replay, stale-write rejection, result/reader
binding, a frozen delivery and unchanged Excel bytes after a later opinion and
reopening. Test storage and project lookup are isolated fixture adapters; this is
not PostgreSQL, RBAC/tenant, external Pair browser or model-semantic acceptance.
The added built React host/provider regressions must run with the matching shared
panel before target-platform acceptance.


### Mac validation of the matched build (2026-10-02)

The existing Mac dependencies passed typecheck, the Web build, the shared panel
build and the Pair Client build. Focused shared React regressions passed 25 tests;
the Pair suite, including its built React 18 host fixture, passed 44 tests; the
affected plugin/native delivery Python regressions passed 29 tests. Six additional
source/Host/FastAPI HTTP checks passed with isolated synthetic data. The original
HTTP fixture stalled in this Mac's reverse DNS lookup; the private test fixture
used numeric loopback binding without changing application or resolver settings.

An additional actual built Client / React 18 DOM / authenticated Host / FastAPI
synthetic regression passed: submit an opinion, select its returned result
revision, freeze and download a draft, edit later, reject writes in the historical
view and re-download identical frozen Excel bytes. Catalog reads and project
lookup were fixture adapters; this was not an external installed Pair browser.
The built Client SHA-256 was
`060b3d62c20733d51d0c6daf64310d709bbd92079f9bd44ca97f4d5a9701cee8`.
No customer document, paid model call, PostgreSQL restart, company identity or
Windows execution was part of these checks. Those acceptance boundaries remain
open.
