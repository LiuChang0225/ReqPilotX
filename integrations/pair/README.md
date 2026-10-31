# ReqPilotX application for Pair

The bundle starts its owned ReqPilotX service using an explicitly configured,
non-editable engine/server installation and an independent Web artifact. It
renders the shared complete workbench in managed mode. Task preparation, persistent host binding and Session request submission use an explicitly configured frozen capability package. UI fixture verification, real model execution, business-result acceptance and installer qualification are separate evidence.

## Complete workbench composition

Managed mode mounts `ReqPilotXApplication` from the selected shared UI build. Its HomePage, WorkspacePage, project/document table, document work picker, structure/source views, collaboration, human opinions and delivery views are the same components as Web. The PAIR edition exposes four core destinations only: Projects, Requirement extraction, Requirement analysis and Matrix export. AI-assisted reading and the separate domain-knowledge module are not mounted; knowledge remains PAIR-owned, and original source inspection stays in extraction/analysis. Existing confirmed Requirement Sets retain their exact set ID, review/analysis view and same-version export rather than being rewritten to the latest source. `catalog.cjs` handles service discovery, host theme and application binding; it does not maintain another business catalogue form. Explicit readonly-connect mode retains the diagnostic saved-work panel.

The Client registers the shared workbench copy under the native PAIR locale slot. English or simplified Chinese selected in PAIR Settings changes the same mounted project, extraction, source/map/ledger, analysis, feedback and delivery views without a second selector or guessed global language; standalone Chinese remains the fallback. The PDF viewer receives an explicit supported locale and keeps its existing source and parent-origin checks.

The adapter consumes shared UI contract minor 2 and checks `reqpilot-workbench-host/v1` before rendering managed controls. Package Host, Client, shared UI and installed server/frontend artifacts together; an older Host is not treated as a working full application. `workbenchCapture: true` explicitly declares that the matching installed server supports `/source-capture/fast`; omission disables that action. The broker enforces this operation again. Native-model/backend settings remain with Pair, and `agentPreset: standard` plus the requested task model remain independent of the UI composition.

`GET /reqpilotx/tasks?projectId=...&documentId=...&sourceSha256=...` optionally filters an exact `workRef` and recovers durable launch identities. It returns public bindings and original request metadata, never credentials. A fresh browser origin can locate an existing task before generating another command. Multiple matching tasks require work selection; mismatched local intentions refuse submission. Selection and unsent opinion drafts still use browser-origin storage; same-origin refresh recovery and cross-port Desktop storage need separate acceptance.

Source removal, knowledge-file import and backend-native Agent execution are not silently enabled by mounting the complete UI. Missing host actions retain an unavailable state. Pair-owned Agent controls use the existing task API and open the exact acknowledged Session. See [workbench verification](../../docs/validation/pair-workbench-fidelity-20261027.md).

## Current capabilities

- Installable Cordis bundle with separate Host and Client leaves. The R4 adapter is independently tested; no external Pair profile is modified by its verification.
- On activation, the Host checks the non-editable Python installation, acquires its own data-directory lease, applies the application-owned SQLite migrations and starts the shared Python API on an OS-assigned loopback port. It does not probe or start PostgreSQL and needs no PostgreSQL binary or driver. No worker or model process is launched.
- Native Pair project creation, project selection, PDF upload and original-page viewing. Reads/creates use the existing ReqPilotX API and storage; no second project/document ledger.
- Existing saved works are listed under the selected document and open the same shared workbench. Managed Pair enables source-bound feedback, Map confirmation, engineer opinions and immutable delivery saves through the application's persisted plugin-workbench API; connect mode remains readonly. Pair automatically provides internal addresses and scope. A bound task may stop its live Pair turn or submit an explicit continuation to the same Session. Endpoint reconnection and push Agent events remain pending.
- Disable/stop terminates only owned DSH subprocess handles. Data remains. Restart reuses the same SQLite data root and project/document identities. Unknown nonempty directories, competing live owners, unversioned/unknown SQLite files and historical PostgreSQL data roots are rejected, not adopted.

## Guided intake and fixed workflow (RX02)

The Requirement workbench now explains the required customer source, unavailable
project-reference/team-knowledge inputs, expected outputs and the fixed stage
order before the user starts. The user clicks Start once; no authored goal is
required. Optional supplementary instructions are retained as notes but cannot
remove the Map, items, self-check or delivery prerequisites. The existing
"check structure before items" option remains the declared confirmation branch.

The server-owned brief (`reqpilot-fixed-workflow v1.0.0`) is prepared by the
frozen capability package, not by browser text. The six declared stages are
preparation, source/Map, items/classification with explicitly scoped derivation,
evidence analysis, self-check/correction and saved deliverable. Completion is
projected only from saved task artifacts and receipts; model prose, idle
sessions and exports do not advance a stage. With no internal evidence, the
declared branch marks evidence analysis skipped. The public `publish_current_review` task sync now completes a fixed-workflow
handoff by previewing the exact current server snapshot and registering or
reusing one DRAFT delivery version through the existing CAS/idempotency
endpoints. A successful checked `task_deliver` invokes the same path when the
task is bound; unbound portable tasks remain local. Newer manual responses or
revisions conflict without resetting or targeting latest, and no FORMAL
acceptance is created. The workbench can list and download that exact DRAFT
workbook.

Unchanged verification retains its receipt identity, so repeated delivery/check requests do not create result or DRAFT versions from timestamps alone. Partial drafts remain downloadable; fixed-workflow handoff requires complete declared scope and final local/remote currentness. Portable tasks without a fixed-workflow context retain their source-bound content verification.

Task state is rederived through the pinned task read API. Host inspect validates
task/source/workflow/brief identity, stage order, SAVED evidence and readiness;
the task's `workflow-status.json` is only a derived cache and is not trusted.
Corrupt or foreign cache/read state is shown as unconfirmed while saved work is
retained. The workbench registration boundary likewise ignores sender-supplied
stage status/readiness and derives from uploaded artifacts plus retained
delivery-version receipts. Historical tasks without a fixed-workflow context
keep their existing brief and do not acquire a synthetic stage record.

New tasks use the currently selected PAIR default model and effort. The exact
selection is frozen as `modelSelection` in launch record v3 before preparation
and confirmed through `sessionController.selectSessionModel` before the first
prompt. An explicit `agentModel` deployment configuration remains an override.
Missing model selection or Session model capability reports an actionable setup
state and never starts a second runtime. The bundle no longer pins a Codex
model. Receipt schema `reqpilot-pair-launch/v2` identifies v3 records with a
source-bound workflow state; v1/v2 histories retain their original meaning.
New Clients reject v2 receipts without a valid workflow projection, and new
Hosts reject unknown workflow/receipt versions without rewriting prior records.

The Host adapter registers `reqpilotx` through the optional
`ctx.pairSkillCenter.registerApplication` discovery seam with Requirement-stage
metadata and a `readStatus` snapshot of the actual managed runtime status.
Registration runs in a Cordis effect scoped to the Host fiber; a raw DSH
deployment without skill-center still mounts the Client sidebar and main panel.
This is discovery availability, not model readiness or business acceptance.

## Code and ownership

- `host/src/managed-runtime.js`: runtime identity, private data lease, installed-package checks, SQLite/API preparation, readiness and teardown.
- `host/src/managed_api.py`: checks installed distribution origins and rejects editable/legacy `app` imports; invokes installed migration capabilities and serves `reqpilotx_server.main` on a child-allocated loopback socket, with no worker.
- `host/src/index.js`: authenticated Pair route, runtime status and fixed-target transport. Managed mode allows project creation, document import, and feedback/Map-confirmation, engineer-response and immutable delivery-save POSTs only for a concrete plugin work ID. Other writes remain unavailable. Pair cookies/auth are not forwarded to the backend; redirects are rejected. Range and revision headers are preserved.
- `client/src/catalog.cjs`: native projects/documents entry and service readiness, consuming shared APIs. Its exact-path write policy is embedded from Host discovery and grants each result operation separately; a feedback grant cannot authorize human opinions or delivery saves.
- `client/src/client.cjs`: shared-panel host adaptation and the explicit connect-mode development probe.
- `scripts/build.mjs`: packages the existing shared UI CJS/CSS into DSH lazy-CJS; React, JSX and ReactDOM use the Host instance.

Default data is under `~/.local/share/reqpilotx/pair`. The ownership marker, local SQLite database, source/object files and runtime receipts stay there with private permissions and are absent from public configuration responses. Ports are dynamic. Changing dataRoot selects a different owned application store; it does not import other stores automatically.

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
export REQPILOT_PAIR_DATA_ROOT=/absolute/private/reqpilotx-pair-data
```

The adapter has no checkout or `.venv` fallback and never installs dependencies.
Its child explicitly receives storage, object, scratch and frontend paths. Runtime
Python uses `-I`; editable packages and visible legacy `app` are rejected. The
three local Cordis packages are not a registry release or fresh-machine installer.

Config accepts `python`, `frontendRoot` and `dataRoot` (absolute paths). Former
`appRoot` checkout configuration and the retired `postgresBinDir` dependency are
explicitly rejected. For a development server use explicit readonly `mode:
connect` with `serviceOrigin`; failed installed setup never selects connect mode
automatically.

Only the legacy transport probe uses `REQPILOT_SERVICE_ORIGIN`; setting it selects readonly connect mode with the development link form. It is not the normal application flow.

## Verification and limits

Contract tests cover lazy factory/shared React, style disposal, fixed proxy origin,
authentication/Range/revision/credential isolation, write allowlist, exclusive
ownership, invalid roots and dependencies, teardown and retained ownership on failure.
The React 18 test fixture is a module host, not the external Pair UI.

`scripts/installed-smoke.mjs` uses real OS children implementing the subprocess
handle interface, relocated host files, non-editable wheels, the matching built
Web/shared-panel artifacts and an application-owned SQLite data root with no
PostgreSQL executable on `PATH`. It verifies ordinary local API startup before
the managed migrate step (fresh root and unknown-DB refusal), project/import
command replay, source bytes, saved version snapshot/export, frontend, readiness,
competing owner rejection and restart identity. It also exercises the actual managed
response/delivery POST allowlist: source-bound human opinions, exact command
replay, stale-write rejection, explicit formal acceptance and unchanged frozen
snapshot/Excel bytes after restart. An optional fifth argument selects a new
retained smoke-output directory; owned processes stop but synthetic data remains
for inspection. The 2026-10-01 cloud Linux PostgreSQL receipt remains historical
evidence for the separate cloud route. The local smoke is not a fresh-machine
installer or published release. See
[Pair lightweight SQLite](../../docs/architecture/pair-lightweight-sqlite.md)
and the R4 architecture record for exact commands and evidence. Historical
DSH/UI checks on the previous checkout-based adapter are not acceptance of the
current external Pair UI.

Unverified: abrupt-crash/orphan recovery, fresh-machine dependency installation, release distribution, existing-data import, full business writes, push runtime events, Office/Windows/Desktop. Historical PostgreSQL data roots are refused untouched pending the explicit migration slice. The current source candidate adds persistent project/import command IDs, same-command replay and read-only commit recovery; JSON/multipart carry the ID through the existing proxy. Pending intentions survive reload and service-port changes under the same application ID. This requires the matching server/migration and has not been released or verified in the real employee host. See the [entry command contract and focused evidence](../../docs/architecture/project-source-entry-commands.md).

## Upgrade with shared artifacts

Host 0.1.1 requires `@deepseek-ai/dsh-mcp-client` exactly `0.2.1-alpha.2` as a peer, with the same version declared for development checks. The profile supplies the matching framework core through ordinary DSH resolution; the adapter does not install a private framework copy. Keep the profile's `autoInstallPeers: false`. After replacing an older Host, use the normal plugin/package-manager update to remove its obsolete `0.1.5-rc.2` dependency resolution. Verify the default import from the installed Host under the packaged DSH profile; a source-checkout import or manually selected core path does not establish that integration.

On macOS, run the import check against an installed PAIR application and a new private output directory:

```sh
node integrations/pair/scripts/sealed-core-smoke.mjs \
  /path/to/PAIR.app/Contents/Resources/runtime/cli/bin/dsh \
  /new/private/peer-check
```

The check installs the Host through the packaged CLI into an isolated profile with fresh HOME/DSH_HOME and records `receipt.json`. A synthetic lifecycle trigger executes the installed Host's unchanged default bridge import, stopping before plugin registration. The resolver observer returns `nextResolve` results unchanged and checks that the loaded package belongs to the application with the exact required version. PASS covers this import and absence of a physical framework copy in the private profile; it does not establish MCP transport, tool registration, model execution, business acceptance or Windows compatibility. The user's active profile is not changed. Every CLI command must exit successfully before its timeout; a written PASS receipt cannot override a timeout, including a graceful exit after SIGTERM.

Project/document requests now come from the same `createProjectDocumentClient` that the standalone app composes. The Pair catalog only supplies routing and transport. Shared UI is versioned by its generated `host-contract.json`; this adapter declares supported major/required exports in `client/adapter-contract.json`. Build rejects incompatible/missing capabilities before packaging and records `sharedArtifactIdentity`.

A release pipeline can build the shared UI once and supply it independently of the checkout:

```sh
node integrations/pair/scripts/build.mjs --shared-ui /path/to/shared-ui-output
```

Relocation tests copy adapter build sources and shared artifacts outside the
checkout and reproduce the same client hash. The installed service lifecycle has
its own real process test; neither test establishes external Pair UI acceptance.

## Runtime events versus business acceptance

`handoff_ready` means a program artifact is ready for handoff, not source completeness or semantic verification. Host `SUCCEEDED`/`COMPLETED` remains a run-terminal observation. The shared panel already labels it as the external turn ending; no runtime observation means unreported. In a PAIR-hosted view the same status comes from the native Session, so the work picker and workbench name the PAIR task instead of claiming an external task; standalone/portable views keep their external meaning. Machine stage reasons such as `ITEMS_REQUIRED` and `WORKFLOW_CHECK` render as localized labels, and an explicit empty native task (`NOT_STARTED`) reads as “尚未发起任务” rather than an unreadable task.

The pending real event adapter must project actual step starts, waiting duration, retries, attempt failures and terminal errors from public host events. Keep model/provider/client attribution unknown unless transport evidence separates them. A successful export or a large item count cannot promote a source-grounded FAIL to completeness PASS. Existing SourceVisual/locator contracts remain authoritative; no Pair-specific coordinate or business model is introduced.

## RX02 verification entry points

```sh
node integrations/pair/scripts/host-discovery-smoke.mjs \
  "/absolute/PAIR/source" reports/pair-lightweight/work/02-host-discovery

node integrations/pair/scripts/dsh-native-snapshot.mjs \
  "/absolute/PAIR/source" <installed-python> <frozen-bundle> \
  reports/pair-lightweight/work/02-native-session-final
```

The first uses the real Cordis service/fiber from the PAIR source checkout for
Host registration/disposal and runtime-status changes. The second boots the
source-supported `pair` profile in an isolated `DSH_HOME`, approves only one
loopback OpenAI-compatible route, and records a real Workspace/Session/MCP turn
whose scripted `task_open` and early `task_check` calls end without saving
business artifacts. The early `UNCONFIRMED` check receipt is retained and the
workflow stays non-ready. It is keyless/scripted, not a provider-model or
business-acceptance run. The source
checkout currently lacks its prebuilt native system addon, so the runner copies
the matching installed prebuilt into the private output and records that exact
dependency; production policy and profiles are untouched.

## RX03 verification entry point

```sh
node integrations/pair/scripts/dsh-native-rx03.mjs   "/absolute/PAIR/source" <installed-python> <frozen-bundle>   reports/pair-lightweight/work/03-native-session
```

This fixture creates a synthetic source, prepares a real task/Map/Items/review
through the frozen capability, then lets a scripted native turn call
`task_open`, `review_apply` with `changes.map_revision.decisions`, and
`task_deliver` through the actual MCP protocol. It records the public Session
events, work/task/revision identity and final workflow state. The RX02 native
fixture is also extended with an early `task_check` before Map/Items; its
`UNCONFIRMED` receipt remains in place and the later typed operation continues. The route is the
same explicitly approved loopback OpenAI-compatible profile as RX02; it is not
a provider-model, browser-rendering or human-acceptance proof.

## Application discovery contract

The authenticated `/reqpilotx/config` response now implements `reqpilot-application-host/v1`. `application-discovery.cjs` is embedded from the Host source into the Client build; the build receipt records its hash. This is a Pair transport contract, not a new business schema or an implementation of MCP Apps.

Managed `applicationId` derives from persistent storage ownership and is independent of process `instanceId` and service port. It is not user/tenant authentication. Connect mode has no verified persistent identity and reports null. The Client checks protocol, required capability fields and managed identity before showing controls. Its host identity stays stable while its transport identity changes with the endpoint, invalidating old requests.

Service `READY` may precede task-launch readiness. The mounted application applies subsequent capability, model and workbench-lookup changes from `/config`, including capability withdrawal. These updates replace the task controller without reinstalling the business transport for the same application and endpoint. Selection-scoped observation and control behavior is defined in the [shared workbench contract](../../packages/workbench-ui/README.md#complete-embedded-application).

Only implemented project/document/result operations are advertised; Agent start is advertised only when the frozen task package passes doctor and the Pair services are available; resume/cancel and real Agent events remain false. The server still enforces authentication, method/route policy and readiness; advertised capabilities are not an authorization token. Public configuration uses a whitelist and does not serialize private installation data or raw failure text. READY is not semantic acceptance.

This increment has contract/module tests and synthetic installed service-process verification. External Pair UI verification must be performed against a matching release. The source candidate restores pending project/import results and saved document selection after reload; recovery of a running business task remains unverified. Research, architectural decisions and the next implementation slices are recorded in `docs/需求提取/Pair应用插件最佳实践与实施20260922.md`.

## Task launch increment

Set `REQPILOT_PAIR_CAPABILITY_ROOT` to a built bundle containing `scripts/host_launch.py`; optional `REQPILOT_PAIR_AGENT_PRESET` defaults to the shipped `standard` preset. The capability package is explicitly supplied, never downloaded or silently replaced. Its Python dependencies must be satisfied by the configured installed interpreter.

The native document form supplies an exact user goal and optional Map-confirmation choice. Host source bytes are checked against the service identity, then the shared package prepares the task and brief. The Host registers the execution workspace, adopts a preassigned Session ID and submits a stable DSH requestId. No model is dispatched by merely opening the application or inspecting a receipt. `/reqpilotx/tasks/start` returns acceptance only; `/reqpilotx/tasks/{commandId}` is read-only.

Host receipts pin the bundle SHA, interpreter location, preset, task/work, Session and request identity. Conflicting reuse is rejected. Pair master's `session.create` now reuses only a named **blank** Session and may mint a new identity for a started Session, so a retried record is first reconciled through `sessions.inspect`: an absent Session may be created under the preassigned identity, while a started/busy/completed Session is never passed to `create` again. Started sessions resume through the supported prompt/model APIs; a busy Session without an exact recorded request, an unconfirmed model, or a different cwd/workspace fails closed instead of duplicating the work or switching context. Same-task initial workbench synchronization now uses a deterministic session ID in the common portable transport. Existing bindings are preserved. A newly registered Workspace receives a readable registry title from the customer file name through the public `workspaces.rename` contract; its directory and identity do not change, and an existing UUID Workspace from a retry is never renamed. The rename call passes the actual `WorkspaceRenameRequest` object (`{workspaceId,title}`); a wrong call fails instead of being swallowed.

The actual PAIR startup prompt embeds the prepared `task_id` in the first `task_open` resume request; `request.md` remains available after that resume for optional user notes. New workflow/status/intake/control copy resolves through the native slot `t` binding, while standalone Web keeps its Chinese defaults. Source-heading composition preserves brackets, parentheses and meaningful punctuation such as `.NET`; only separators belonging to the number boundary are de-duplicated.

The Client navigates through `ctx.uiWorkspace.openSession`, the owner of view navigation. View-only “open task conversation” calls it without touching the composer. Explicit source/result discussion retains a Session through `ctx.sessions.using`, reads the actual `binding.ctx` scope, appends the versioned reference to an existing draft through the documented input event, and rejects a superseded navigation before insertion. It never calls the removed `ctx.sessions.open` and never auto-sends.

25 Pair checks (including built React18 form interaction), 9 common task/workbench tests and 16 transport tests passed. `scripts/task-launch-smoke.mjs` executes a real frozen-package CLI against synthetic HTTP but mocks the DSH Session controller; it never invokes a model. This is not live Pair acceptance. Changed service endpoints require explicit reconnection before resubmitting an unfinished launch; semantic resume and Agent events remain unavailable.

Full implementation and private proof references: `docs/需求提取/Pair任务发起与会话绑定20260922.md`.

## Native DSH admission probe

`scripts/dsh-binding-smoke.mjs <Pair checkout> <python> <frozen bundle> <new private output>` starts only the supported `dsh --profile web` surface in a private DSH_HOME. Default mode calls real Workspace/Session creation and blocks prompt admission. `--admission` uses the real prompt controller with DSH's scripted LLM adapter seam; `--recover` confirms acceptance from the real Session journal after a test endpoint change. Nonlocal fetch is refused, production credentials are not copied and title-model calls are disabled.

Observed: real admission retry leaves one user request and one scripted turn; journal-based acceptance recovery makes no second prompt. Remote inference calls are zero. The source HTTP service is synthetic, and these probes are not external Pair UI, PostgreSQL integration, extraction or semantic acceptance tests. See `docs/需求提取/Pair真实DSH绑定与接收验证20260922.md`.

TaskLauncher confirms an ambiguous START_REQUESTED record from an exact persisted user rpcId when possible, without sending another prompt or changing the task binding. Context-injection messages do not count. On a retry/restart it inspects the recorded Session before any create: absent sessions use the known identity; blank sessions may be reused; started/busy/completed sessions are restored without a second identity and with the frozen model/cwd/workspace checked. This is acknowledgement/session recovery, not business-task resume; changed endpoints without acceptance evidence still require explicit reconnection.


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

## Explicit trusted task reconnect (development candidate, 2026-10-05 UTC)

An accepted MCP task can explicitly reconnect after its Pair-owned API restarts.
`POST /reqpilotx/tasks/{commandId}/control` accepts `action: "reconnect"` with
exactly the same saved command, Pair Session, work reference and source hash as
continue/cancel. The matched Client discovers `agent.reconnect`, refreshes the
saved task's control capabilities and offers “Verify and reconnect this task”.
A reconnect performs read-only service checks and changes the existing local
transport binding; it never creates work or submits a model prompt. Continue is
still a separate action on the original Session.

Both origin and managed-process generation are pinned. Reusing the old port does
not bypass reconnect. The Host authenticates the live owned lease/descriptor and
an ephemeral challenge proof from the API listener, then checks the fixed
bundle's complete source closure, workspace preparation/creation receipts,
original bytes, task, deterministic work reference and service source/revision.
Reads bypass proxies and cannot redirect. Cached bytecode cannot replace checked
source; undeclared source, sourceless shadow packages and symlinks are rejected.

The Pair `sessions.inspect` adapter accepts the actual master `SessionInspection`
shape (`meta` plus `events`) or the older `session` projection. The header must
expose the exact Session id, cwd and, when present, workspace id; durable events
drive acceptance and turn-state recovery. Missing or conflicting identity
metadata fails closed. The exact original user RPC must be present, the latest
turn must be closed, and the journal/live state must remain unchanged through
reconnect. An accepted retry does not resubmit a lost-ack continuation. Unknown
prompt/cancel outcomes, incomplete local mutations, unestablished publication
receipts or a divergent remote revision block reconnect instead of replaying a
mutation. Repair those original outcomes before retrying.

A frozen-identity reconnect intent is saved before changing the portable binding.
This permits recovery if the helper commits but the Host stops before saving its
new origin, including another subsequent lease/port change. The helper follows
publication → binding → task lock order and has a bounded command timeout. An
accepted reconnect control ID belongs to its target generation; a later restart
requires a fresh reconnect control ID.

Offline verification builds the unchanged shared portable package and matched
shared panel/Client. Set `PAIR_RECONNECT_PYTHON` to an existing dependency-ready
Python and `PAIR_RECONNECT_BUNDLE` to the new built bundle, then run
`node --test integrations/pair/tests/trusted-reconnect.test.mjs`. These tests use
actual loopback listeners, actual owned-directory lease release/reclaim and the
real fixed shared task/compile/publication/resume/feedback facade around synthetic
HTTP state. They cover new continuation, lost acknowledgement, same-port and
repeated restart, helper/Host crash recovery, identity/import/proxy rejection,
unknown publication/cancel outcomes and overlapping locks. The wrapper challenge
contract also has a standard-library ASGI test. The built Client has a synthetic
DOM reconnect/two-generation test using `PAIR_UI_REACT_ROOT`.

These are development program checks, not an installed Pair/DSH acceptance claim.
The actual DSH inspect schema/stable journal, original-Session MCP remount after
Host restart, PostgreSQL restart, target-platform employee UI, real providers and
source semantics still require their own same-candidate evidence. Rebuild the
Host, shared panel and Client together. The new Client rejects Host
configurations missing `agent.reconnect` before displaying controls.
