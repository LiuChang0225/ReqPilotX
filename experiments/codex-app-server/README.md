# Codex App Server capability probe

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

This isolated B1-C0 probe checks only:

- App Server stdio initialization;
- ChatGPT-managed account mode and plan type;
- image-capable model availability;
- the expected hardening overrides.

It does **not** start a model turn, read/copy `auth.json`, inspect email, open a
PDF, or call Azure/Alibaba Cloud.

Run from `prototype/`:

```bash
PYTHONPATH=. .venv/bin/python -m spikes.codex_app_server.probe
```

The current default probe uses the already logged-in local Codex session only
for this no-data check. Live B1 lifecycle/PDF runners require a stable
repository-external `REQPILOT_CODEX_HOME` and a fresh ChatGPT browser/device-code
login in that environment. Never copy the default Codex auth file.

Successful output is text-free JSON using
`reqpilotx-codex-capability-probe/v1`. It includes only auth mode, plan type,
model ids/modalities, and hardening facts. It never includes email or tokens.

## B1-C1 lifecycle canary

`lifecycle.py` is a separate fail-closed runner for the first synthetic model
turn. It does not accept document bytes, paths, user prompts, Skills, images, or
tool definitions. It requires:

- a repository-external, non-symlinked `REQPILOT_CODEX_HOME` owned by the
  current user with mode `0700`;
- a fresh ChatGPT login in that home, stored in the OS keyring rather than a
  copied `auth.json`;
- the exact binary and generated App Server schemas pinned in
  `protocol_lock.json`.

Login once from outside the repository:

```bash
export REQPILOT_CODEX_HOME="/absolute/private/path/to/reqpilotx-codex-home"
chmod 700 "$REQPILOT_CODEX_HOME"
CODEX_HOME="$REQPILOT_CODEX_HOME" codex login \
  -c 'cli_auth_credentials_store="keyring"'
```

Then run the fixed normal-turn canary from `prototype/`:

```bash
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" \
  PYTHONPATH=. .venv/bin/python -m spikes.codex_app_server.lifecycle
```

The remaining lifecycle scenarios use the same locked environment:

```bash
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" PYTHONPATH=. \
  .venv/bin/python -m spikes.codex_app_server.lifecycle --mode interrupt-resume
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" PYTHONPATH=. \
  .venv/bin/python -m spikes.codex_app_server.lifecycle --mode crash-restart
```

On macOS, the runner preserves the real OS `HOME` only so Keychain can resolve
the dedicated login; all Codex config/history still comes from the separate
`CODEX_HOME`. It creates private `TMPDIR`, `sqlite_home`, log, and sterile
working directories for the run. It disables shell/exec, apps,
plugins, hooks, multi-agent, browser/computer use, image tools, MCP, analytics,
and OTEL exporters; verifies the effective config before inference; rejects any
server request or tool/file/network item; validates a fixed JSON marker; deletes
the synthetic thread; and emits only event counts and SHA-256 receipts.

The current implementation and opt-in live checks cover protocol lock,
environment/config preflight, a normal structured turn, exact interrupt,
graceful process restart plus resume, forced crash with a non-replayed
`UNKNOWN_AFTER_CRASH` turn, thread deletion, a new-process active/archived
absence check, CODEX_HOME session marker scan, and run-scoped temp-tree removal.
These are synthetic lifecycle proofs, not customer-data or production-runtime
qualification and not a claim of SQLite/WAL forensic erasure.

## B1-C2 PDF Skills canary

The product Runtime Skills live under
`agent-assets/compatibility/runtime-skills/` and are explicit-only:

- `reqpilotx-pdf-understanding`
- `reqpilotx-structure-tree-generation`

Run the two-turn synthetic canary from `prototype/`:

```bash
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" PYTHONPATH=. \
  .venv/bin/python -m spikes.codex_app_server.pdf_skill_canary
```

The runner generates a private two-page technical PDF at runtime. Page 1 has
native headings and a 3x3 table; page 2 is one raster image with no native text
and visual-only headings, requirement, diagram relations, and canary code. None
of those page-2 golden values is placed in the understanding turn's pre-inference
text or native facts. The fresh Structure turn intentionally receives only the
host-validated understanding proposal and coverage. Existing ReqPilotX preflight,
PDF SourceIR, Structure resolver, page geometry, and annotation-free renderer
produce the facts and page image before either turn. The canary then:

1. disables every ambient Skill by exact path and proves that only the two
   product Skills remain enabled;
2. explicitly invokes the PDF Skill with bounded facts plus one `localImage`;
3. validates exact native/visual evidence channels and refs, rejects opposite
   direction semantics, and creates a host-only coverage receipt;
4. invokes the Structure Skill in a fresh thread without the image;
5. validates native candidate refs and exact non-dangling parentage while
   keeping visual nodes advisory;
6. deletes both threads and removes the entire run-scoped temp tree.

Passing proves explicit Skill injection, local-image grounding, structured
output, coverage separation, and Proposal validation. It deliberately leaves
page 2 `IMAGE_REQUIRED`, the document `PARTIAL`, and `apply_eligible=false`.
Agent-selected PDF tool calls are not part of this gate; they begin with the B2
local stdio MCP.

## B2 read-only MCP canary

`read_only_mcp.py` is a pure-standard-library stdio MCP server over one private,
run-scoped descriptor. It exposes exactly six tools:

- `workflow.get_document_facts`
- `pdf.preflight`
- `document.get_page_ledger`
- `document.get_source_chunks`
- `document.render_scoped_page`
- `document.validate_coverage`

Every tool accepts opaque handles rather than paths or business IDs, rechecks
the exact source bytes, has a small call budget, and is declared read-only,
non-destructive, and closed-world. The host has no business, job/event, cache,
apply, publish, lock, export, SQL, shell, or arbitrary-file write capability.

Run the Agent-selected tool canary with the same dedicated login:

```bash
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" PYTHONPATH=. \
  .venv/bin/python -m spikes.codex_app_server.read_only_mcp_canary
```

The B2 fixture adds visible prompt-injection text to the raster-only page. The
model may observe it as source content, but it cannot add a tool or capability.
The runner verifies the exact MCP event sequence, permits at most one rejected
coverage proposal followed by one corrected validation, compares the validated
proposal digest with the final output, checks all three write-spy categories are
zero, deletes the thread, scans the dedicated home for synthetic markers, and
removes the run-scoped tree. Passing still leaves page 2 `IMAGE_REQUIRED`, the
document `PARTIAL`, and `apply_eligible=false`.

## B3 immutable shadow Structure Proposal

The B3 stage narrows the MCP allowlist to two tools:

- `structure.get_current`
- `structure.validate_proposal`

Run it with the same dedicated login:

```bash
REQPILOT_CODEX_HOME="$REQPILOT_CODEX_HOME" PYTHONPATH=. \
  .venv/bin/python -m spikes.codex_app_server.shadow_structure_canary
```

The model receives only an opaque run handle and the explicit Structure Skill.
The current tool returns the validated understanding/coverage, candidate
catalog, exact base Structure SHA/revision, and a short expiry. The validator
checks the complete proposal before the model can return it. The host then wraps
the validated payload in `shadow-structure-proposal/v1` and writes a private
`0600`, `O_EXCL`, run-scoped artifact. The artifact is immutable for the run,
has `apply_eligible=false / serving_impact=NONE`, and is deleted with the run
temp tree. It is not a Structure revision or durable business artifact.

The focused C0-B3 suite currently contains 46 tests: 38 C0-B2 tests plus 5
shadow envelope/artifact tests and 3 B3 runner contract tests.
