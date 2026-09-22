# Image SourceItems

Command details for this topic; [tool index](tools.md) provides the common entry.

Native capture is evidence acquisition, not a limit on what the Agent can read. Every meaningful drawing/figure needs verified ownership/disposition: supporting table/clause images and whole-front-matter images stay with their owners, while genuine independent image items receive SRC. Whole image requirements can be analyzed without forcing their labels into a fake verbatim text field.

Choose the route before authoring image items. For a figure supporting an existing clause/table, retain its Map ownership and use `compile-annotations`/review `visual_readings` for its source-bound interpretation; do not create another NON_REQUIREMENT or IMAGE SRC to preserve it. Use `compile-images` below only for genuinely independent image content. A standalone informative drawing may be a non-requirement image item; an informative/supporting figure already belonging to a clause is not independent merely because it is non-normative. Image inventory is a reading aid, not a to-do list of new SRCs.

## Supporting figure Map binding

Suppose source ref `12` contains a complete numbered clause and ref `52` is its supporting figure. After inspecting the original, keep the figure as owned content on that clause:

```json
{"node_id":"clause-2-6","numbered_body":{"source":12,"label":"2.6","continuations":[52]}}
```

This is a node entry in `reqpilot-map-edit/v1`; refs are illustrative. For an existing separately headed clause, `content:[12,52]` is the equivalent route. Preserve other body/continuation refs when replacing a list. Keep its existing clause SRC, then add source-bound `visual_readings` with `compile-annotations`; owning an image does not require another IMAGE SRC.

By contrast, `evidence_refs:[{"source":52}]` is non-owning navigation. Combining it with an image `NON_BUSINESS` disposition does not retain that image for clause display or later visual annotation, even if the reason says “supporting figure, annotate later.” Correct the Map ownership and only that conflicting disposition. A verified logo, page furniture or duplicate capture may legitimately remain excluded; inspect source meaning rather than guessing it from reason prose. Zero audit gaps means the capture was accounted for, not that every exclusion was semantically right.

When an empty `image-packet` has matching capture proof of reference-only/excluded images, `image_binding_note` explains the binding and gives source refs and repair guidance without changing the Map or blocking progress. Zero is also valid for a no-image document. If the current directory has no capture proof, use the complete Map directory or supply its actual matching `--source-ir`; the tool does not infer missing figures without image evidence.

```bash
python3 <plugin>/scripts/reqpilot.py image-packet --map-dir <map-dir> --source <original.pdf> --output-dir runtime-data/run/image-packets
```

With no `--node-id`, this inventories all Map nodes containing IMAGE/FIGURE anchors. Repeat `--node-id` for a bounded selection. `image-packets.json` contains each scope's original page/box/source hash, direct source context and `packet_sha256`. When `--source` is provided, the command verifies the original file hash and renders the relevant original pages. Each PNG has its own hash, original source SHA and page, so a vision host can bind actual image inputs. Read the original pixels, not just the navigation metadata.

The response also includes `source_image_renders` with exact per-anchor image
files, image/source hashes, original page/bbox and dimensions, including
table-owned pictures. It uses the shared export source loader: exact embedded
payloads retain their original pixel resolution; composite/overlaid source
regions use its evidence-preserving crop fallback. Read these files for fine
labels instead of relying only on the reduced whole-page preview. Missing
payloads are explicit `source_image_render_gaps`; no extra SRC is created.

Write an explicit `reqpilot-image-decisions/v1` envelope with `source_sha256`, `document_map_artifact_sha256` and `scopes`. Each scope has:

```json
{
  "node_id": "<real Map node ID>",
  "packet_sha256": "<exact image packet SHA>",
  "items": [
    {
      "item_key": "drawing-1",
      "display_label": "<Agent display label for this drawing>",
      "image_anchor_ids": ["<exact Map image anchor ID>"],
      "requirementness": "REQUIREMENT",
      "requirement_kinds": ["QUALITY"],
      "non_requirement_role": null,
      "confidence": 0.9,
      "decision_summary": "<why the visible drawing expresses a requirement>",
      "reason_codes": ["normative_drawing"],
      "visual_interpretation": "<optional Agent reading; never customer-verbatim text>"
    }
  ]
}
```

`SourceItemImageScopeCandidateV1.schema.json` is emitted by `schemas`. The example class is illustrative; classify the actual meaning, not the file medium. Every image anchor presented for independent-item decisions in a selected packet must occur exactly once. Multiple physical image anchors may belong to one complete independent graphical item when the source supports that grouping. An independent non-normative drawing uses NON_REQUIREMENT with its actual role. Supporting clause/table graphics follow the annotation route above; do not turn them into extra items or duplicate an already represented obligation.

```bash
python3 <plugin>/scripts/reqpilot.py compile-images --map-dir <map-dir> --baseline-dir <current-baseline-dir> --input <image-decisions.json> --output-dir runtime-data/run/baseline-with-images
```

Omit `--baseline-dir` only for a genuinely image-only initial scope. The output uses the existing `source-requirement-baseline.json` format, with `source_form: IMAGE` and exact `image_bindings`, plus the ordinary validation receipt. Text, table and image bindings are mutually exclusive within a SourceItem. `image-annotations.json` stores the separate Agent display/visual-reading authority and image identity history. Preserve it across revisions. The compiler can revise affected image scopes while retaining other source items and historical SRC numbers; labels and readings do not alter original pixels. Same-image annotations can be explicitly rebound after a text-only baseline revision, with the rebind recorded. A changed Map or image binding cannot silently inherit old readings.

`deliver` automatically loads the baseline directory's image annotations and merges source-bound review annotations. Pass the exact original PDF with `--source` for visual export. The original image appears on its SRC row; its Agent reading does not become the customer source prose.
