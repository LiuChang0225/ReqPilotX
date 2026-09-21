# Portable tool guide

If ReqPilot task tools are provided, use the [task API](task-api.md) and its returned workbench link. For script-only work use [the workbench entry](tools-host.md#workbench-entry-and-current-limits) to discover or bind the actual review surface; this does not require creating another task when one already exists. `catalog` lists `workbench` for discovery or explicit source/artifact binding and `workbench-pull` for feedback. The no-artifact opener only opens a URL; explicit synchronization returns the bound source, stages and revision.

For existing-version feedback, Agent self-review, classification corrections,
existing-SRC image readings and revision/export advancement, read
[the revision loop](revision-loop.md). It uses the same artifacts described here.

Use Python 3.11 or later. `<plugin>` is the directory containing `.codex-plugin/plugin.json` and `scripts/reqpilot.py`. In a bootstrapped workspace it is `.reqpilot`. A released bundle includes `runtime/` and `requirements.txt`; the source checkout alone must first be built. The source checkout is not a runtime prerequisite.

Only when setting up a new script environment or repairing a diagnosed missing dependency:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r <plugin>/requirements.txt
.venv/bin/python <plugin>/scripts/reqpilot.py doctor
.venv/bin/python <plugin>/scripts/reqpilot.py catalog
```

On Windows use `.venv/Scripts/python.exe`. `doctor` checks imports and packaged file hashes only. It does not claim semantic success or call a model.

Read only the relevant topic file below. These files contain command/schema details; use [the case index](reading-guide.md) for semantic choices. Native skill reads return a whole file, so each topic has its own file. Existing section links remain valid here.

## Source capture

Read [Source capture](tools-capture.md).

## Source-bound Document Map decisions

Read [Source-bound Document Map decisions](tools-map.md).

After compilation, inspect [source-first completeness and expanded order](tools-map-audit.md).

## Source item decisions

Read [Source item decisions](tools-items.md).

## Image SourceItems

Read [Image SourceItems](tools-images.md).

## Explicit decomposition

Read [Explicit decomposition](tools-decomposition.md).

## Analysis and delivery

Read [Analysis and delivery](tools-analysis-delivery.md).

## New session and optional host

Read [New session and optional host](tools-host.md).

## Native Office and empty templates

Read [Native Office and empty templates](tools-office.md).

## Whole front matter and a runnable reference

Read [Whole front matter and a runnable reference](tools-front-matter.md).
