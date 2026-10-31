# Shared workbench UI

This is the single source for business components, types, API clients, host
contract and styles. Web imports the `./web` composition surface; host adapters
consume the generated `dist-panel` CJS/ES/CSS and `host-contract.json`.

From the repository root: `npm ci`, `npm test`, `npm run typecheck`,
`npm run build:panel`, `npm run test:css`. The root lock pins the same dependencies
as the former frontend. React is external in Panel builds and supplied by the host.
Contract `reqpilot-host-ui/v1` remains major 1 / minor 3; the additive minor adds the optional fixed-workflow intake/status host fields.

## Complete embedded application

`ReqPilotXApplication` renders the same `HomePage` and `WorkspacePage` used by Web, including project/document navigation and the shared structure, source, response and delivery views. `ReqPilotXReadonlyPanel` remains available for explicit saved-work diagnostic consumers.

The adapter installs `installApplicationTransport({ id, resourceUrl, fetch })` before rendering. One bundle generation owns one active application transport. Disposal aborts its requests and refuses fallback into the parent application's `/api`; it never replaces global `fetch`. The stable application ID namespaces pending entry commands. Standalone Web retains normal browser transport and navigation.

Embedded navigation and preferences are scoped to the panel. The parent URL, document title and global theme/language remain with the host. Shared business copy is owned by the workbench dictionaries: standalone keeps simplified Chinese as the stable default, while a host passes its native `t` slot and the same mounted views follow the host language (simplified Chinese or English) without remounting or dropping drafts, selections, revisions or scope. Source text, filenames, customer evidence, citations and model output are never translated. A ResizeObserver supplies the available panel width to the incumbent responsive navigation. The full application keeps the WorkspaceShell grid; legacy saved-panel flex rules exclude its marker.

In the PAIR edition the WorkspaceShell exposes four core destinations only: Projects, Requirement extraction, Requirement analysis and Matrix export. The separate AI-assisted reading and domain-knowledge modules are not mounted; PAIR owns knowledge. Original PDF/text/table/image inspection remains inside extraction and analysis, and legacy reading/knowledge links fold back into the supported core with the existing project/document context. Standalone Web keeps its independent data and existing entry points.

The extraction work surface treats the document tree/source ledger and original/detail inspection as the primary body. The fixed-workflow overview is a compact single status row by default, with real blockers visible and details in a bounded, focus-returning disclosure. Layout responds to the actual embedding container width, keeps explicit list/detail selection instead of stacking height-starved panes, and lets the tree body own its scrolling. The PDF viewer is a separate page and receives only an explicit supported `locale`; its source/parent-origin checks are unchanged.

The host supplies task start, exact-work restoration, control and Session opening through `WorkbenchApplicationHost`. `HostedAgentPanel` owns only these host controls. Business feedback, original-source linkage, human opinions, versions and exports continue through the existing workbench components and API. Host configuration is shown without a second model/provider editor. Unsupported source removal and knowledge import remain explicitly disabled; program capture requires a declared capability.

A host may additionally supply `workflow` (the server-owned fixed process description) and `taskSetup` (an actionable missing-model/capability state). The task panel then explains required source, unavailable optional evidence and expected outputs, and starts without an authored goal. Receipts may carry `workflowState`, a program projection from saved artifacts; the panel and `PluginWorkbench` keep it separate from execution status and never treat it as human acceptance. A null restore is rendered as Not started rather than a persistent status failure.

Pending observations and controls belong to one committed host/work/revision selection. The route's selected revision invalidates them before its reader has finished loading; returning to the same work does not reactivate an older observation. Restoration accepts an optional `AbortSignal`, which discards stale reads and prevents them from replacing newer durable intentions. This does not cancel an admitted task. Control acknowledgements update execution state while preserving the current work and inspected version; only explicit task start or return navigates to another work. An observation begun before a control cannot replace that control's newer receipt.

## Native Web original evidence

The standalone `WorkbenchHostContext` enables `nativeEvidence: true`. This is an
optional presentation flag: external providers that omit it retain the existing
Pair/plugin contract and never show or call the native evidence flow. Read-only
providers do not expose native registration or preparation writes.

`ProjectKnowledgePanel` registers selected file bytes (including browser folder
selections with relative names) through `/api/projects/{id}/openkb-evidence`.
Metadata defaults to unknown authority, purpose and applicability. Registration
does not request indexing, paid models, or live OpenKB retrieval. Per-file results
distinguish complete success, partial success, and failure; retries retain failed
files only. Aborting a request does not claim that server-side registration was
rolled back.

The existing native `SourceItemReasoningDetail` opens source-bound original
inspection for the current analysis unit. The session is bound to the SOR source
hash, baseline hash, numeric knowledge version, global directory generation/hash,
and selected packet/catalog hash. Original references must match the registered
knowledge document and source hash; returned locators are displayed without
inventing page/region locations. Project, source, baseline, unit or knowledge-scope
changes clear the current evidence selection and abort stale reads.

Preparing selected originals saves only a `PENDING_EVIDENCE` working draft:
`semantic_validation=NOT_PERFORMED`, `live_openkb=NOT_RUN`, and
`human_acceptance=NOT_CLAIMED`. It refreshes the existing canonical reasoning;
`EngineerOpinion`, native saved versions, and native Matrix exports remain the
human review and delivery paths. No queued Agent job or compliance claim is implied.

Focused checks: `npm test -- --run packages/workbench-ui/src/features/knowledge`
and `npm run typecheck`. Aggregate checks remain `npm test`, `npm run build`,
`npm run build:panel`, and `npm run test:css` from the repository root.
