# Whole front matter and a runnable reference

Command details for this topic; [tool index](tools.md) provides the common entry.

Map node kinds also include COVER, TABLE_OF_CONTENTS and DOCUMENT_CONTROL for explicitly identified whole navigation/control blocks. These are not per-entry chapters. A generic block label without title_source_ids is marked AGENT_DISPLAY_LABEL; provide actual content/table source handles. Keep independent body obligations in separate source-backed scopes. A request with structural_role accepts whole text through structure_only_spans and whole table rows through FRONT_MATTER context_row_groups, with exact coverage and zero SRC. Ordinary body scopes cannot use that disposition.

See [worked examples](worked-examples.md) and run `python scripts/worked_example.py --output-dir /absolute/new/directory` in the bundle. Workbench discovery, opening and native/portable handoff are described in the general [workbench entry](tools-host.md#workbench-entry-and-current-limits); they apply at task entry, independently of front matter.
