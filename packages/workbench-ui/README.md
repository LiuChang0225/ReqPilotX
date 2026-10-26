# Shared workbench UI

This is the single source for business components, types, API clients, host
contract and styles. Web imports the `./web` composition surface; host adapters
consume the generated `dist-panel` CJS/ES/CSS and `host-contract.json`.

From the repository root: `npm ci`, `npm test`, `npm run typecheck`,
`npm run build:panel`, `npm run test:css`. The root lock pins the same dependencies
as the former frontend. React is external in Panel builds and supplied by the host.
Contract `reqpilot-host-ui/v1` remains major 1 / minor 2.

## Complete embedded application

`ReqPilotXApplication` renders the same `HomePage` and `WorkspacePage` used by Web, including project/document navigation and the shared structure, source, response and delivery views. `ReqPilotXReadonlyPanel` remains available for explicit saved-work diagnostic consumers.

The adapter installs `installApplicationTransport({ id, resourceUrl, fetch })` before rendering. One bundle generation owns one active application transport. Disposal aborts its requests and refuses fallback into the parent application's `/api`; it never replaces global `fetch`. The stable application ID namespaces pending entry commands. Standalone Web retains normal browser transport and navigation.

Embedded navigation and preferences are scoped to the panel. The parent URL, document title and global theme/language remain with the host. The current shared business UI is Chinese; the embedding wrapper does not claim an English translation. A ResizeObserver supplies the available panel width to the incumbent responsive navigation. The full application keeps the WorkspaceShell grid; legacy saved-panel flex rules exclude its marker.

The host supplies task start, exact-work restoration, control and Session opening through `WorkbenchApplicationHost`. `HostedAgentPanel` owns only these host controls. Business feedback, original-source linkage, human opinions, versions and exports continue through the existing workbench components and API. Host configuration is shown without a second model/provider editor. Unsupported source removal and knowledge import remain explicitly disabled; program capture requires a declared capability.

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
