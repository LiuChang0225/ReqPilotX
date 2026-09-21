# Native Office and empty templates

Command details for this topic; [tool index](tools.md) provides the common entry.

`compile-map --source original.docx|original.xlsx --source-ir ...` uses the same
map-decision contract. The program replays capture from the original bytes and
copies `native_locator` and `source_sequence`. It emits `source_format` on Office
Maps, empty `pages` and null PDF geometry. `image-packet --source <original>`
extracts actual embedded image payloads and preserves their native object
locations; no synthetic page render is created. `deliver` uses that same original
file and verifies each object/asset before embedding it in Excel.

A genuinely empty template may have `records: []`, complete `context_row_groups`
and the unchanged native table grid. Non-header context must be blank. If the
source uses a literal placeholder, add `exact_placeholder_values: ["<exact value>"]`
and `empty_template_reason: "<source-grounded reason>"` to that context group.
Every nonblank cell must match a declared value exactly. There is no default
normalization of slash, zero or N/A. This is an explicit reviewable Agent semantic
decision, not program proof that no obligation exists. Meaningful text requires
its SRC; it cannot be hidden as context. A zero-record revision must preserve the
previous header boundaries and all source cells. Template declarations are saved
with the context and carried into later source-scope and Map replay.
