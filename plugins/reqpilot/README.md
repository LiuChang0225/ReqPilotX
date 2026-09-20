# ReqPilotX portable plugin

`reqpilot` packages the ReqPilotX workflow for a new Codex session: original file → real Document Map → stable SourceItems → explicitly authorized decomposition → cited review and missing inputs → Excel Matrix draft. For a complete document request, the Agent autonomously reads, compiles, checks source evidence, repairs supported defects and delivers the best current draft with explicit remaining gaps. Structure and items are saved progressively in the workbench; human feedback is optional intervention, and only an explicitly requested structure checkpoint waits for approval. No internal evidence means skipped compliance analysis, not a blocked extraction task. It carries no customer examples or historical candidates. The optional XLSB capture converter has platform-specific external dependencies listed below.

The display name is **ReqPilotX**. The plugin ID `reqpilot`, CLI filename and five
skill names stay compatible with existing installations; they do not identify
or authorize access to the separate ReqPilot project.

The native PDF reading capability is pinned separately under `capabilities/pdf`, with unchanged official skill files and provenance. Updated native tasks verify the matching runtime, enable read-only Python/Poppler/image inspection, and keep shared compilers as the artifact-writing interface. PDF Map decision v2 adds exact source ranges, local table-fragment row coordinates and non-owning title references; existing v1 artifacts retain their replay behavior. This does not claim OCR-to-SourceIR enrichment or all-document semantic correctness.

The plugin has five discoverable skills and one tool entry, `scripts/reqpilot.py`. Text, table and image SourceItems use the same source ledger. A whole graphical requirement receives SRC/REQ and can be analyzed; original pixels and Agent visual readings remain separate. A built bundle contains a small, allowlisted copy of the existing application's Python modules, with hashes and dependency pins in `build-receipt.json`. Business implementations are copied unchanged. Broad package initializers are replaced by documented namespace shims so importing capture does not initialize model providers, databases or unrelated workflows.

Build from the ReqPilotX checkout into private/ignored output:

```bash
python3 plugins/reqpilot/scripts/build_bundle.py --repo . --output prototype/runtime-data/reqpilot-release/reqpilot
```

Install the built `requirements.txt` in a Python 3.11+ virtual environment, then run its `scripts/reqpilot.py doctor`. Move/copy the **whole built directory** to another machine; no source checkout is required for the portable path. The source plugin directory alone is not a complete release bundle.

Prepare a new workspace with project-level discovery:

```bash
python3 <built-plugin>/scripts/reqpilot.py bootstrap --workspace <new-empty-directory> --source <raw-document.pdf>
```

Bootstrap uses the Python standard library to prepare files; it does not create a virtual environment or install dependencies. Install the bundled requirements before running business commands.

Bootstrap prepares an environment once. For each new document task, use `workspace init --working-root <private-root> --source <original> --goal "<actual request>"` (optionally `--project-key` / `--document-key` and the real `--workbench-url`). It creates a physical project/document/task folder and returns `TASK.md` and `task_root`. `workspace run <operation> --workspace <task_root> --input <semantic-decisions.json>` selects saved source/Map/baseline paths, assigns a fresh output directory, validates the complete existing compiler bundle and records its commit. `workspace status` / `recover` resume source-only or Map-only work as well. See the concise [task workspace guide](skills/reqpilot/references/task-workspaces.md) for confirmation, custom bindings, candidate handling and existing review operations. New tasks default to no Map approval gate; use `--require-map-confirmation` only for an explicit user requirement. Existing task policies stay unchanged. Local commit and workbench synchronization are reported separately; this is not a second extraction algorithm.

This writes a project `AGENTS.md`, `.reqpilot/` runtime/skills, relative skill links and an ignored input directory. Run the generated `request.md` in a fresh session. It does not copy developer history or prior results. The generated request authorizes bounded multi-agent work and explicit useful decomposition; adjust the request to the user's actual scope before a real customer run when that scope differs.

Codex can discover the installed plugin or bootstrapped project skills natively.
For another coding agent, open the bootstrapped workspace and give it `request.md`,
or explicitly ask it to read `.reqpilot/skills/reqpilot/SKILL.md` and invoke
`.reqpilot/scripts/reqpilot.py` with the workspace Python. The shared capability
is ordinary Markdown skills plus a Python CLI; other hosts do not need to
understand Codex's plugin manifest. If a host cannot delegate, it can run the same
workflow sequentially and must disclose the missing independent Agent check.

The PDF/DOCX/XLSX business CLI uses pinned Python dependencies and platform-specific OS locking behind one interface; it needs no Codex service or Office desktop application. Bootstrap uses relative symlinks where supported
and copies the skill folders when symlink privileges are unavailable. The Codex
cold-session harness is a separate host adapter. Actual host execution has been
verified on macOS Codex; Windows lock/symlink branches and other coding agents are
supported interfaces, not a claim that those hosts have been end-to-end tested.

For a strict cold test, use `python3 <built-plugin>/scripts/cold_run.py --workspace <new-workspace> --model gpt-5.6-luna --reasoning-effort max --timeout-seconds 900` (or `gpt-5.6-sol` with `high`). It first asks the local CLI to render its actual prompt, disables ambient skills and automatic parent AGENTS injection, explicitly supplies this workspace's AGENTS, verifies the packaged skill inventory, and starts a new session. Model and effort are required; delegation defaults off and needs explicit authorization plus `--allow-delegation`. `--prepare-only` checks discovery without a model. It changes no global configuration or authentication directory. Public commands, failures, configuration and output hashes remain in private `runtime-data/cold-session/`; timeout is separate from completion and semantic success.

For native plugin-manager installation, use the Codex plugin-creator workflow to create the default personal marketplace, copy the **built** plugin to `~/plugins/reqpilot`, then run `codex plugin add reqpilot@personal --json`. The personal marketplace's `./plugins/reqpilot` source resolves relative to the home directory. An installed copy lives in the cache path returned by the CLI; run that copy's `scripts/reqpilot.py doctor` and inspect a new session's actual skill inventory. Do not overwrite an existing local plugin or marketplace entry; use plugin-creator's cachebuster/reinstall workflow for updates. Project bootstrap also makes the skills discoverable without changing global settings. Manifest validity, manager installation, actual discovery, tool readiness and source-driven semantic verification are separate checks.

Read `skills/reqpilot/references/tools.md` for exact input contracts. The tool adapters create only private artifacts and drafts. Formal publication, customer acceptance and production activation are not hidden inside the portable commands.

An optional typed task API now supports the same business request objects through `reqpilot.py task` and a local stdio MCP adapter. It reuses the fixed task CLI, existing workspaces and compilers; it does not create a second task service. MCP uses the separately pinned `requirements-mcp.txt` and is not automatically enabled by the manifest. See [typed task tools and setup](skills/reqpilot/references/task-api.md) for actual actions, retry/revision semantics and the initial capability limits. SDK interoperability and deterministic CLI/MCP equivalence do not establish model-level speed or quality gains.

After PDF capture, `reading-workspace --source <original.pdf> --source-ir <source_ir.json> --output-dir <new-dir>` reuses the proven page Markdown, high-render and full table-view helpers with source/image indexes. `reading-packet` remains optional exact-data inspection. The updated native host supports page/region zoom, annotation overlays and HTML text reading; `worked-example` exposes the complete synthetic example through the same CLI/native entry. Older pinned packages keep their existing tool surface. See [capture and reading](skills/reqpilot/references/tools-capture.md); generated views and tool-returned images do not establish Agent semantic acceptance or official PDF skill parity.

For an existing result use the [revision loop](skills/reqpilot/references/revision-loop.md):
`review-open` imports the authorized source/Map/baseline and current review state;
`review-feedback` records actual user feedback or explicit Agent self-review;
`review-packet` exposes current source-bound context; `review-revise` calls the
shared classification/visual compilers and carries compatible derived/analysis
state; `review-close` checks the exact export and rejects stale feedback before
advancing a new immutable local draft. Existing-SRC image readings are attached
without making duplicate image requirements. The same commands run from Codex,
another coding agent's shell, or an application host adapter.
Matrix summaries count completed analysis separately from technical conclusions;
an analyzed item awaiting evidence or clarification remains visible as such.

`cold_run.py --workspace <new-workspace> --review-dir runtime-data/review --model gpt-5.6-luna --reasoning-effort max` runs a
new non-resumed Codex session with an explicitly supplied predecessor and
feedback. This is a fresh-context **revision** test, distinct from the source-only
extraction cold test. Receipts record both the pre-run and post-run review hashes.

Current limits are explicit: native capture has no OCR/model fallback, but the Agent can read and classify Map-bound images as whole image SourceItems; PDF source bindings require measured original page/geometry; DOCX/XLSX use their original native paragraph/table/cell/object locators and keep unknown pages/geometry empty; complex table splits that cut merged cells are rejected; text-range decomposition is not a visual character-transcription validator. A running Agent supplies semantics and independent review. A valid schema, deterministic round trip or single successful cold case does not establish generalization across all documents. Exact execution evidence belongs in private release/test output, not in customer-bearing fixtures committed to the plugin.


Original DOCX and XLSX now have a tested synthetic program path through Map, SRC,
explicit decomposition, pending analysis and Excel. The compiler recaptures the
original file to verify the supplied text, table cells, native locators and image
assets. It never substitutes a converted PDF SHA or invents Office page numbers.
Excel is exported by the internal deterministic program. This is a macOS Python
CLI test with Codex absent from PATH, not a claim that every Office document,
other coding agent or operating system has been tested. Native application UI
navigation and additional complex Office shapes need their own integration checks.

XLSB is an optional capture/conversion adapter, not the DOCX/XLSX native full
pipeline. Its current implementation requires macOS (`Darwin`),
`/opt/homebrew/bin/soffice` at LibreOffice 26.2.4.2 and `/usr/bin/sandbox-exec`.
`doctor.input_formats.XLSB` reports these external file/platform checks and says
that binary version validation was not run; overall Python readiness does not
certify XLSB conversion. Windows/Linux need a separately implemented equivalent
converter. PDF/DOCX/XLSX do not require that converter.
