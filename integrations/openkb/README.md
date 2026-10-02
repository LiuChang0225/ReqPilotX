# Optional OpenKB → ReqPilotX original-evidence bridge

Candidate dated 2026-10-02, based on ReqPilotX `67bb444cdbdb5d2a2e6e39cde45543b8233b8ea4`.
This is a tested isolated integration candidate, not a deployed server or a live
OpenKB model run. OpenKB is optional; source reading/extraction never depends on
it. Only explicit folder/upload sources are supported. No SharePoint connector,
customer fixture, credential, paid call or production merge is included.

## Actual integration

`OpenKBAnalysisService` calls existing portable public operations
`analysis-packet`, `compile-assessments`, and `deliver`. These share the engine's
SourceItem analysis compiler and Matrix/Excel projection used by the main
workbench. It does not rebuild source extraction or introduce a parallel
requirements model.

The host binds one project, KB, current baseline directory and its exact original
before constructing the service. `create_router(service, authorize=...)` can be
mounted explicitly on an existing FastAPI host; the host must supply its real
identity AND project-authorization dependency. There is no anonymous default
application or automatic main-server mounting.

Runtime import paths must include `packages/engine/src`,
`plugins/reqpilot/scripts`, and `integrations/openkb/src`. Reuse the project's
installed dependencies; optional routes require FastAPI/python-multipart, and
the evidence Excel annex uses the already supported openpyxl dependency.

## API sequence

1. POST `/api/v1/openkb-evidence/sources/upload`: multipart files, explicit host
   declaration, JSON metadata. Identity/hash/status/version are host-generated.
   Upload supports TXT, DOCX, XLSX and PDF; current explicit limits are 64 files,
   10 MiB per file, 20 MiB per API batch. Exceeding a limit fails explicitly.
   Folder import is a host method and accepts no client filesystem path.
2. POST `/sessions`: current `expected_generation` and
   `expected_baseline_sha256`, optionally current unit IDs. The actual native
   analysis packet and original evidence registration are frozen.
3. GET `/sessions/{id}/sources`, then `/originals/{document_id}`. The tool
   recaptures native facts/locators from immutable originals. Issued evidence IDs
   are persisted, allowing the same session to survive service reconstruction.
4. POST `/sessions/{id}/assessments`: submit a
   `reqpilotx-openkb-assessment/v1` envelope bound to project/KB/generation/packet.
   Every selected unit must appear exactly once. Each decision has unit_id,
   decision, reason, evidence_ids, conditions, conflicts, missing and
   clarification_question. The host maps only issued IDs to native fact/exact
   quote pairs; the existing compiler rejects unsupported compliance conclusions.
5. GET `/sessions/{id}/assessments/{sha256}/export-receipt` or `/matrix.xlsx`:
   export exactly that native assessment/baseline/knowledge generation. The
   untouched canonical workbook remains `matrix-native.xlsx`; the delivered
   workbook adds `OpenKB原件依据` with original hashes, exact quotes/locators,
   applicability, gaps, version bindings and explicit NOT_CLAIMED human acceptance.
   Both workbook hashes are recorded; source images and native sheet values are
   tested unchanged. Repeat export reuses checked immutable bytes.

Updates/deletions, changed original bytes, baseline/review changes or expired
sources invalidate current use. Historical artifacts remain immutable, but a
stale session cannot be used to export a current judgment. The API contains no
human-acceptance operation. Model judgments remain proposals requiring actual
engineering scope/coverage review, not automatic source-semantic verification.

## Customer context fix

The existing analysis packet could carry `context_source_ids` as IDs without the
referenced non-ancestor source text. The engine now expands declared same-source
context, including exact text/cell/image locators, shared content and indirect
references, into `referenced_customer_context`. A visited graph exposes cycles
without repeated obligations. Unknown/retired/foreign references fail explicitly.
Dependency content is part of unit hashes; changed indirect conditions stale only
dependent assessment units. No-reference unit contracts remain unchanged. An
associated customer response/state is never promoted into a new AI conclusion.

## Upstream contract and honest runtime boundary

Audited OpenKB pin: `ff54396e575ee6feb0113b631a34caa082b441cc`
(https://github.com/VectifyAI/OpenKB/tree/ff54396e575ee6feb0113b631a34caa082b441cc).
Its REST `/api/v1/query` returns only answer/saved_path and has no document filter
or native evidence envelope. Wiki prose remains discovery, never an original
fact. `runtime.build_analysis_query_agent` extends the real upstream query agent
with original-read/customer-unit tools for an approved host runner. It checks the
audited query module hash and the host-owned generation Wiki path.

The actual OpenKB/Agents/LiteLLM runtime is unavailable in this cloud environment.
The pinned upstream schemas were fetched and verified by Git blob identity;
source-contract and native integration tests are offline synthetic tests. No
mock/contract server result is represented as a full live OpenKB pass. Approved
runtime installation, Wiki compilation, authenticated main-host mounting,
UI/Pair/plugin invocation, actual customer semantic validation and deployment
remain separate validation tasks.

## Tests

From repository root: `python -m pytest -q integrations/openkb/tests`.
The regression fixture is invented, uses the real source capture/Map/SourceItem
compiler, and preserves customer-source vs internal-evidence separation.


## Canonical Mac integration verification (2026-10-02)

Library version 1 candidate ZIP was checked at 117,401 bytes, SHA-256
`227cbe4d40d0a9107016e969f759f084a441be2ee1af03c388e41a3008b52597`.
All 16 overlay paths and both existing-file preimages were checked before applying
on the clean Pair commit `881356852959c5b729db5f6830659f759947b4ea`; the paths do
not overlap the Pair transport change. Existing-file backups were retained.

With existing Mac dependencies, 31 new integration tests passed. The combined
OpenKB/analysis/Map/decomposition/portable/knowledge-original/plugin/native
delivery regression passed 116 tests, including those 31. The existing Pair
suite passed 41 tests in the restricted sandbox; its three localhost HTTP tests
failed to listen with EPERM, then all three passed through the normal approved
numeric 127.0.0.1 test run. This was an environment restriction, not a business
regression. The synthetic test servers were closed after the checks.

These checks made no paid model call and used no customer document. They do not
establish company identity, automatic main-server mounting, live OpenKB runtime,
external Pair browser, canonical delivery-version integration or Windows 11
acceptance. The earlier 68-test PoC result is cloud provenance, not a Mac rerun.
The existing repository CI covers its backend/plugin/engine/Pair workflows;
these new opt-in OpenKB pytest checks were run separately as described above.


### Full CI reconciliation

The first integrated-head frontend CI passed. Its full backend run reported two
failures among 4,811 tests (154 skipped): the new engine context module lacked the
required server compatibility alias, and a lease-heartbeat test depended on a
100 ms wall-clock window. The standard alias now reuses the identical engine
module. The test advances a controlled lease clock past the original expiry,
waits for a real database heartbeat and still rejects a competing owner; it no
longer treats CI scheduling delays as lease expiration. Production lease timing
and ownership rules were not changed. Five productization and 58 atomization
scope tests passed locally after these corrections. Final-head remote CI is
tracked separately; the earlier failed run is retained as evidence.
