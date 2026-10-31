# ReqPilotX 2026-10-01 cloud integration

This source candidate continues the explicitly allowlisted migration of the current
ReqPilotX Workspace baseline, not an older saved project. Base provenance is
`a7d9d29e081b1be8c73e72e57bf67cbd44ec5903`; the imported integrated candidate also
contains the uncommitted Workspace changes and preceding integration fixes.
No Git metadata, credentials, customer documents, databases, logs or dependencies
were transferred. No commit, push or modification to the Mac is part of this run.

## Migration and environment

- Archive SHA-256: `acb8d9bf7b2ad0094d4eeb66816c6248c11641798c968c571d71d34beef22173`
- All 2362 manifest entries verified before implementation: 2360 regular files
  (size and SHA-256), two symbolic links (exact targets), no unexpected/missing files
- Cloud-only Python 3.13.5 virtual environment; focused dependencies installed
  under the existing Linux Python 3.13 lock constraints. This is a tested subset,
  not a claim that the entire distribution dependency lock has been installed
- Root npm lock installed; type checking and production Web build passed
- Tests use the repository isolation runner, temporary synthetic data, socket
  blocking and disabled model downloads. No live model or credential transmission

## Joint mixed-source publication

The durable table job now resolves unfinished sibling tables on the same document
node before the companion text executor settles exact ownership. Completed table
candidates are hash-bound private checkpoints, in addition to their existing
window checkpoints. A text interruption resumes those candidates rather than
requesting the table models again. Sibling window checkpoints cannot overwrite
the selected table's checkpoint.

The text request receives all exact table-owned spans and the candidate identities.
Overlapping ownership, changed source/map/node identity, and damaged checkpoints
stop before publication. The domain compiler validates all table and text scopes
against the current predecessor before the publication fence; one product baseline
revision includes the entire group. Existing source IDs and unaffected predecessor
records remain intact. Shared context still grants no ownership.

This structured extension requires an existing product baseline. When it is absent,
a mixed result fails explicitly instead of publishing a table-only scope receipt
that would discard its text. The source-document tool loop remains the separate
cold-start route. Reassigning already-published text requires an explicit revision.

## Verification boundaries

The initial transferred tests were reproduced in cloud: mixed source-item 8 tests,
provider tool-loop 11 tests. New joint-worker tests exercise two tables plus their
text scope, interruption/retry, same-version Excel, stable previous identities,
conflicting ownership, corrupted candidates, source drift, cancellation, and
absence of a product predecessor. These tests are synthetic contract evidence.

The transferred text-stage implementation and locked compatibility kit schemas
were found inconsistent during expanded tests. Their compatibility treatment and
provider structured-adapter regression are tracked in the companion provider
foundation documentation; a broad pass must use the final matching candidate.

Azure live authentication, Bailian live responses, actual Pair/plugin host behavior,
Mac user-interface acceptance, and Windows installation are not established by
these cloud tests. No live customer extraction or model semantic-quality claim is
made. Full target-platform acceptance remains necessary for the trial release.

## Shared human-response and delivery integration

The external workbench API now scopes human responses and frozen deliveries to
the project, document, plugin session, source and exact result revision. A response
save creates a new immutable result revision. The response view's opaque version
token, baseline and unit-context hashes, response revision and retained command
ID protect writes/retries; an external producer upload cannot author human
opinions. Changed or removed unit contexts preserve prior opinions as stale and
do not automatically revive them.

The existing shared EngineerOpinion and DeliveryVersions UI now consumes those
session-bound endpoints. Human-response editing is independent of native
analysis execution; external edits cannot accidentally start the native Agent.
A successful or replayed response follows its exact returned revision, not a
subsequently newer head. Response-only revisions retain source selection,
expansion and source/unit-bound drafts. Historical results and unmanaged host
panels remain read-only, while saved opinion details and frozen downloads remain
readable. Managed Pair transport reuses the same component and command contract.
Formal delivery requires explicit acceptance and complete effective responses.

Additional negative tests cover wrong-session/version replies, late completion
following navigation, uncertain command retry across remount, delivery
cross-session misuse, changed input with a reused command, concurrent replay,
and response-versus-delivery races. Actual employee-host/UI and live model
acceptance remain separate gates.

## Packaging limitation found during this run

The verified source-only migration intentionally omitted non-allowlisted
resources. The engine packaging closure requires
`packages/engine/src/reqpilotx/resources/branding/schaeffler-logo.png.base64`,
which is absent from that manifest. Distribution checks initially failed closed on this missing resource. The exact
9042-byte asset was subsequently recovered with the authorized read-only GitHub
connector at pinned commit `a7d9d29e081b1be8c73e72e57bf67cbd44ec5903`. Its Git blob is
`612a208828ec4a730de202f26e3f45298817aeda`, file SHA-256 is
`bb9a56df094f681eb6d4f2a94a2b44b68b5277e53650536407dab5f94e931443`. The PNG payload
also validates. It is supplemental source provenance; the original migration
manifest remains unchanged. No replacement was fabricated and the guard was
retained. Legitimate release builds still require clean committed Git provenance;
this cloud candidate intentionally has no Git metadata and is not a published
release.

## Final cloud checkpoint

The final integrated candidate was checked after the response/delivery UI changes:

| Check | Result |
|---|---|
| Shared frontend | 83 files, 909 tests passed |
| TypeScript | Typecheck passed |
| Web / shared panel / Pair adapter | All three production builds passed |
| Panel CSS scope | 4 tests passed |
| Source-requirement aggregate | 69 tests passed |
| Source-item aggregate | 163 tests passed |
| Provider aggregate, including actual SDK-mocked joint worker and real-SDK lifecycle | 25 tests passed |
| Joint mixed worker | 7 tests passed |
| Native durable checkpoint regression | 6 tests passed |
| External response/delivery API | 26 tests passed |
| Compatibility kit | 16 tests passed; exporter/lock checks passed |
| Agent asset/frozen inventory guards | 21 tests passed |
| Engine distribution contracts | 14 tests passed |
| Pair proxy / discovery / relocated shared artifact | 13 tests passed |
| Plugin workbench / transport | 11 / 16 tests passed |
| Actual MCP stdio / fixed CLI | 6 tests passed |
| AI configuration/client regression | 149 tests passed |
| Test runtime isolation | 1 test passed |

Counts are separate suites and must not be added as unique-test totals.
The existing Vite large-chunk warning and jsdom canvas limitation remain;
they do not establish actual browser rendering or PDF canvas acceptance.
Asset packaging checks also corrected the explicit 0.16.0 compatibility group's
catalog identity and exact release assertion; current business/vendor groups
were not silently regenerated.

### Next acceptance on the canonical Workspace

The cloud delta applies only to the migrated **integrated 145-dirty** snapshot.
The latest canonical Workspace remained the earlier **95-dirty** source. The
required chain is Workspace95 → reconcile initial local fixes → verified
integrated145 migration snapshot → cloud delta. The transferred
`workspace-changes.patch` describes HEAD → integrated145 and does not establish
Workspace95 preimages; do not apply it blindly over existing dirty files.
The first stage requires a separately reviewed reconciliation and retained
backups. Only after that stage matches the migrated manifest may each cloud
changed file be replaced under its exact preimage hash. Abort on mismatch and
never overwrite later local changes. No automatic Mac application occurred. The
supplemental branding file is an exact pinned source asset and may already be
present on the Mac with the same hash. Preserve existing local data, identities,
private credentials and prior task/capability bundles. Rebuild the Web, panel,
Pair adapter and governed release packages from the final integrated source.

Then verify the actual Mac application / installed Pair / plugin workflow:
create or reopen a synthetic task, interruption and exact resume, mixed-source
publication, source item/derived-unit selection across a response-only revision,
human response and withdrawal, historical read-only view, explicit formal freeze,
and same-version Matrix/Excel download. Use an explicitly supplied locked kit
for the provider window route. Bailian live acceptance needs its authorized
synthetic input/model/cost boundary. Azure authentication is not established on
the Mac, and Windows installation/runtime remain unverified. No claim of a full
employee trial release or customer semantic acceptance follows from this
checkpoint.

The attempted synthetic shared-workbench cloud-browser harness could not open
its loopback URL: the browser client returned `net::ERR_BLOCKED_BY_CLIENT`.
That policy was not bypassed; both temporary server sessions were stopped.
Actual rendered browser/PDF acceptance remains pending.

## Follow-on real service validation

After preserving this initial cloud delta, unprivileged workspace-only extraction
of signed Debian PostgreSQL 17.11 packages enabled actual service validation.
The external HTTP/PG lifecycle, stopped-service backup/restore into a different
database/copied storage root, native Azure-SDK loopback HTTP mock through two
production durable workers, native human-response/formal delivery lifecycle, and
installed Pair managed service/proxy all passed. The original storage root is
actively unreadable to the restored external API, proving the copy is used.
Native continuation preserves the same task/thread/capability and source IDs.
These are synthetic service receipts, not live provider, rendered browser or
employee-host acceptance. See [cloud service acceptance](cloud-service-acceptance-20261001.md)
for exact scope, trusted dependency provenance and reproduction commands. The
first delta remains immutable; follow-on scripts/tests/docs require a separately
hashed successor or incremental delta under their exact preimages.
