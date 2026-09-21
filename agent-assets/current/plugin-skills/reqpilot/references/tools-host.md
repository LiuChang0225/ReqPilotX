# New session and optional host

## Workbench entry and current limits

ReqPilotX is the engineer's review surface. Use the actual selected project/document route; the CLI reads `projectId` and `documentId` from that URL, so the Agent need not copy internal IDs. The original must already be registered as that document in ReqPilotX. Use the existing project/upload interface when it is not; do not guess a target from the folder name.

The CLI is `../../scripts/reqpilot.py` relative to the main skill directory, or `.reqpilot/scripts/reqpilot.py` in a bootstrapped workspace. Use the configured Python executable, normally `.venv/bin/python`. Resolve the script path, not the virtualenv Python symlink; invoking its underlying system Python can lose the installed dependencies.

```text
<python> <plugin>/scripts/reqpilot.py workbench
<python> <plugin>/scripts/reqpilot.py workbench --source <original.pdf> --url "<actual project/document workbench URL>" --open
```

The first command only discovers the local UI (`REQPILOT_WORKBENCH_URL`, otherwise `http://127.0.0.1:8000/workspace`). The second verifies the source against the selected application document, creates a plugin draft connection and returns its exact workbench link. Explicit `--project-id`/`--document-id` are also accepted. Neither call starts a model or replaces the application's existing working baseline.

Once connected, successful CLI `compile-map`, `compile-items`, `compile-images`, `review-open` and `review-close` synchronize saved results to that connection. A failed network synchronization preserves the local artifact and reports `NOT_SYNCHRONIZED`. On a connection requiring structure confirmation, item compilation waits until that exact Map is confirmed. A matching cached confirmation allows local work during a network interruption; a new Map does not inherit a different Map's confirmation.

You can also synchronize an already saved scope or current review explicitly:

```text
<python> <plugin>/scripts/reqpilot.py workbench --map-dir <saved-map> --url "<actual document URL>" --open
<python> <plugin>/scripts/reqpilot.py workbench --baseline-dir <saved-items>
<python> <plugin>/scripts/reqpilot.py workbench --review-dir <current-review>
<python> <plugin>/scripts/reqpilot.py workbench-pull --review-dir <current-review>
```

`--map-dir`, `--baseline-dir`, `--review-dir` are alternatives. When an initial full review still needs structure confirmation, only its Map is synchronized; `pending_items_sync` reports the saved items still waiting. Confirm in the workbench, pull the result and synchronize again. New connections proceed autonomously by default; use `--require-map-confirmation` only when the user explicitly requested that checkpoint. Existing bindings retain their saved policy. Do not create extra per-item approvals.

The connection lives in `runtime-data/workbench-bindings/<source SHA>.json`; the adjacent `-inbox.json` retains real version-bound feedback and confirmation. `workbench-pull --source <original.pdf>` works before a local review exists. With `--review-dir`, compatible user feedback is imported once into the existing review. Map-only or changed-baseline feedback remains in the inbox for explicit source review; it is not silently rebound. Continue with `review-packet`/`review-revise`, deliver and `review-close`. Addressed feedback returns with the new workbench version as a plugin-reported change, without claiming human acceptance. Continue other authorized work; user review is optional unless specifically required.

A custom `--binding-file` is honored by both before/after hooks. With a task marker, binding paths resolve relative to that task root and persist in its context; entering reading or another child directory does not lose the task. Explicit `--workspace` takes precedence and must be valid. Source, task and application identities are checked together; the same SHA does not redirect another task. Without a task marker, the existing cwd-based behavior remains. `workbench-pull` does not wake an ended Codex conversation; a running Agent reads the inbox, or the user continues that task later. No second model job is created in the application.

For new independent tasks use [workspace init/status/run/recover](task-workspaces.md). The wrapper commits and checks its selected version before synchronization; candidates stay local. Actual host receipts are separate from immutable artifact receipts. Direct compiler commands remain available and accept `--workspace`/`--binding-file`; their explicit source/output paths keep their existing meaning.

The UI keeps the version being reviewed while new results arrive, retains feedback text and its original target, exposes a link from the project document list, and exports the pinned snapshot through the shared program. Opening a URL alone still does not prove that the correct source/stage is visible: check the actual page and version.

**First supported transport:** PDF Map, original items/classification, image interpretations, decomposition and declared analysis policy. Existing evidence-bearing assessments or engineer-opinion files are rejected explicitly until their proof transport is supported; do not omit them to force import. Native Office and automatic wake-up of a finished external conversation are not implemented by this bridge. A newer CLI with an older API may receive 404; update the matching ReqPilotX application instead of inventing parameters.

For an existing native task, use `reqpilot_context.task_brief` and navigation. Its own host publishes stages and records the requested confirmation; native `reqpilot_publish` remains its application-saving operation. Do not attach a second plugin connection or assume portable commands are native dynamic tools.

If the application is unavailable, preserve local drafts and state the missing interaction. The standalone bundle does not install a separate frontend. Use the host's configured application startup procedure; a development checkout's `prototype/start.sh` also manages dependencies/migrations/general workers and is not simply an opener.

## PDF tools and preparing a workspace

For PDF tasks, the updated native app attaches the unchanged official PDF reading skill from `capabilities/pdf` and verifies its bundled Python/Poppler dependencies. Native read-only shell/Python and `view_image` are available in the attested source/dependency roots; use these alongside the shared readers. This is source reading, not permission to edit originals or install dependencies. The actual Python executable and source workspace are supplied by the host; dynamic tool paths remain workspace-relative. Quote shell paths containing spaces, or use Python `Path` values. A file-not-found error is not evidence of a permission denial, and an unattempted check has no passing result.

Command details for this topic; [tool index](tools.md) provides the common entry.

```bash
python3 <plugin>/scripts/reqpilot.py bootstrap --workspace <new-empty-directory> --source <raw-customer-file>
cd <new-empty-directory>
codex exec --skip-git-repo-check --json - < request.md
```

Bootstrap copies only the plugin bundle, project AGENTS, discoverable skills and supplied raw files. It refuses to replace existing governance or skills. Customer data stays under ignored `runtime-data/`. It prepares a request but does not start a model or claim the test ran. Run a new `exec`, keep JSON events and final output in `runtime-data/`, and record the real outcome. Do not pass old candidates, expected counts or private feedback into a cold test. Cold means no prior task context; it does not require ephemeral storage. A real CLI 0.153.4 trial with `--ephemeral` failed to spawn a child with `no thread with id`, so verify actual child execution before using that optional flag for a multi-agent run.

For a strictly isolated cold test run `python3 <plugin>/scripts/cold_run.py --workspace <new-workspace> --model gpt-5.6-luna --reasoning-effort max --timeout-seconds 900` (Sol comparisons use `gpt-5.6-sol` / `high`). It verifies the actually exposed packaged skill inventory, disables ambient skill/ancestor AGENTS injection and explicitly supplies this workspace's AGENTS. Model and effort are mandatory for execution; delegation defaults off. `--prepare-only` checks discovery without a model. Receipts distinguish actual execution, timeout, artifacts and semantic review; no global config or authentication directory changes. This is a test launcher, not an extra step for an Agent already processing a document.

The existing ReqPilotX App Server/workbench adapter is optional and uses an explicit checkout: `reqpilot.py --project-root <repo> host ...`. It delegates to that checkout's `prototype/scripts/agent_workflow.py`; supported commands include `doctor`, `start` with exact project/document/source/command identities, `status` and a bounded `run-job --job-id`. This path uses that host's configured storage/authentication; it is separate from the self-contained offline tools and does not make an API-only server a prerequisite. Inspect the host CLI's current help before starting a job.

Local skill discovery and symlinked skill directories are described in [OpenAI's skill documentation](https://learn.chatgpt.com/docs/build-skills). The [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli) documents ephemeral runs, JSON events and output files. `--ignore-user-config` skips the user's configuration but retains account authentication; it does not itself promise exclusion of all user-level skill discovery. A cold test therefore also states the permitted workspace resources and audits its actual file/tool actions.
