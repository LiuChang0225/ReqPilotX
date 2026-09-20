# Analysis and delivery

Command details for this topic; [tool index](tools.md) provides the common entry.

Workflow choice comes first: without supplied internal evidence, skip automatic compliance analysis and proceed to source review/decomposition and program delivery. Empty-catalog support below is a compatibility capability, not a default instruction to generate pending assessments for every item.

Persist the declared choice in a source-bound `analysis-policy.json`, for example `{"source_sha256":"<actual source SHA>","status":"SKIPPED","reason":"NO_INTERNAL_EVIDENCE"}`. A file elsewhere in the run folder is not automatically part of a review. Pass it with `review-open --analysis-policy <file>`; later exports inherit the saved review policy. For an existing review use `review-revise --input <version-bound revision request> --analysis-policy <file>` so old versions stay intact. Direct `deliver` also accepts `--analysis-policy <file>` when no review is used. Check `analysis_policy_status` in the open/export result; `UNSPECIFIED` is not SKIPPED. Do not create empty assessment decisions just to express this branch.

Explicit policy imports also accept `reason_code` with prose `reason`; the program keeps the prose as `note` and rejects conflicting codes. If `baseline_sha256` is supplied, it must match the import baseline. Historical saved policies are not reinterpreted or rewritten.

Internal facts must originate in actual user/host supplied files. Register them
with `register-evidence --input <registration-input.json> --output-dir <new-dir>`.
The input is `reqpilot-evidence-input/v1` with `project_id`,
`knowledge_base_version`, `declaration: {origin: USER_SUPPLIED|HOST_REGISTERED,
reference: <actual registration basis>}`, and `documents: [{source_path, record}]`.
Each record supplies the real `id`, `project_id`, `file_name`, file `source_sha256`,
`doc_version`, `doc_type`, `status`, `authority`, `claim_role`, and applicable
effective dates/applicability. These are supplied registration metadata, not
fields for an assessment Agent to infer or upgrade. Source paths are resolved
relative to the input JSON. An optional `as_of` freezes the evidence date.

The shared knowledge capture reads the real original bytes and derives fact
text, hashes and locators. It copies originals into `knowledge-sources/`, writes
`evidence-registration.json` and `knowledge-catalog.json`. A nonempty catalog is
rechecked against these actual files at packet, assessment and export time.
Self-signed fact JSON, an empty receipt around fabricated facts or changed source
bytes are rejected. Declared registration does not prove human approval or the
truth of a test result; the Agent must still judge applicability and evidence
meaning. Missing/unknown authority remains pending under the shared rules.

Prefer the shared, frozen assessment path:

```bash
python3 <plugin>/scripts/reqpilot.py analysis-packet --baseline-dir <current-baseline-dir> --decomposition <sealed-decomposition.json> --output-dir runtime-data/run/analysis
python3 <plugin>/scripts/reqpilot.py compile-assessments --analysis-packet runtime-data/run/analysis/analysis-packet.json --input <assessment-decisions.json> --output-dir runtime-data/run/assessments
python3 <plugin>/scripts/reqpilot.py deliver --baseline-dir <current-baseline-dir> --source <original.pdf> --decomposition <sealed-decomposition.json> --assessments runtime-data/run/assessments/source-item-assessments.json --output-dir runtime-data/run/delivery
```

Omit `--decomposition` when no split was needed. `analysis-packet` accepts an actual registered `--knowledge-catalog <project-knowledge-facts.json>`; without one it creates only an explicitly empty catalog and a missing-catalog gap, not invented evidence or a claim that no internal files exist. Repeat `--unit-id` to assess a bounded subset. Read the resulting original/parent/shared context and image references, and use the generated `assessment-candidate.schema.json`.

Assessment input is only `{"decisions":[{"unit_id":"<current unit>","decision":"PENDING_EVIDENCE","reason":"<source-specific explanation>","evidence":[],"clarification_question":"<concrete missing evidence or customer question>"}]}`. Cover each requested unit once. Valid decisions are COMPLIANT, PARTIALLY_COMPLIANT, NON_COMPLIANT, PENDING_EVIDENCE, NEEDS_CLARIFICATION and NOT_APPLICABLE. Evidence quotes must use the frozen catalog's fact IDs; positive, negative or scope conclusions require eligible, authority-verified internal product/project facts under the shared rules. Pending units require nonempty, useful questions. Do not supply an evidence catalog, source coordinates, authority flags or human approval in the model response. `--previous-assessments` preserves only compatible, unchanged unit inputs. The command seals the result and retains its exact input packet; `deliver --assessments` validates those bindings again.

`deliver --baseline-dir <dir> --source <original.pdf> [--input <review.json>] [--decomposition <sealed-artifact.json>] --output-dir <new-dir>` calls the same `source_item_delivery` pure bridge used by the application. Bind the original with `--source` when source figures or visual crops must enter the workbook. Omitting required image evidence does not create an implicit image-free draft: `SOURCE_IMAGE_EXPORT_INCOMPLETE` names the owner/anchor and rejects a missing projection, unresolved image or payload absent from the actual XLSX package. Its review JSON contains optional `annotations`, `decomposition` and `assessments` under the shared bridge contract; supply a sealed decomposition once, via the flag or embedded object. Read the bundled module's public interface before supplying review inputs; do not invent fields. No evidence cannot establish compliance. A new task with no internal evidence follows the skipped-analysis branch; retain historical pending assessments as history rather than regenerating them.

The receipt's `image_export` records expected references, unique source images,
inserted placements and embedded media, with each original anchor's source SHA,
page/bounds, payload SHA, package part and figure location. `gaps: []` follows
actual package verification; `source_document_bound_for_visual_export` alone
only says the original file was supplied. This checks the snapshot's registered
images, not whether semantic source review found every meaningful image.

All assessment inputs, including embedded `review.json` assessments, require a
sealed artifact and its exact analysis packet; an embedded assessment requires
explicit `--analysis-packet`. Unsealed legacy response maps are rejected.

`analysis-packet` also freezes `analysis-source/` with the actual Map, baseline,
tables, annotations and decomposition. `compile-assessments` rebuilds the whole
packet and unit registry from that context; `deliver` and the review importer
rebuild again from the current actual source artifacts. Changing source text,
image interpretation or context and merely recomputing a packet hash does not
pass. An older packet lacking its frozen context requires explicit
`compile-assessments --baseline-dir <actual-current-source-context>`.

Annotations and assessments each bind `baseline_sha256`. Their `records` object uses exact SourceItem identity; assessments may also target compiled `singular-requirement-*` identities. A decomposed parent remains visible as the original and is no longer an active assessment target; assess the active children, without averaging partial outcomes into full compliance. Annotation records support `source_number` (verified within original bound text/cells), `topics` from the controlled set and `context_source_ids` referencing real SourceItems. Assessment records contain `decision`, `reason` and `evidence`; each citation has `fact_id` and `quote`, resolving against the registered `evidence_catalog`. A fact carries captured exact text and declared authority metadata. A compliance conclusion requires eligible, supplied and read internal product/project evidence; the source requirement alone does not establish product compliance.

`deliver` inherits saved annotations, decomposition, assessments (with their exact packet), engineer opinions and analysis policy from `--baseline-dir`. Use the actual current review version directory; no need to rediscover and repeat every saved artifact as a flag. Explicit flags remain available for candidate inputs. Missing or invalid required assessment evidence fails instead of silently dropping saved analysis.

The internal program produces `matrix-snapshot.json`, `matrix.xlsx` and a delivery receipt bound to the exact source/baseline. Programmatic checks compare IDs, complete values, classification columns, source/child order, links/images and evidence state with that snapshot, including actual derived rows rather than merely finding a decomposition file. Normal export needs no second model reading of Excel, extraction or analysis; representative visual rendering is exporter development QA. A file existing alone does not prove data consistency or engineer acceptance.

For actual user-provided technical opinions, use the [explicit manual decision import](manual-decisions.md). CURRENT user opinions have effective priority while AI suggestions remain visible; neither classification overrides nor Agent-authored JSON are user opinions. Native Agent tools cannot create or withdraw these records.
