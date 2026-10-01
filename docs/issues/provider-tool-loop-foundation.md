# Provider tool loop foundation (2026-10-01)

The native Web source-document and source-item review tasks can select
`REQPILOT_AGENT_RUNTIME=provider-tool-loop` with explicit
`REQPILOT_AGENT_PROVIDER=azure` or `aliyun`. The runtime uses the existing Azure
OpenAI or Bailian connection profile and its authentication/client factory.
`REQPILOT_AGENT_MODEL_ID` is optional: the profile chat deployment/model is the
default. A task-specific model is accepted only when it names an already
configured chat deployment/model. `REQPILOT_AGENT_CUSTOMER_DATA_ENABLED=1` remains
required before a source task can start. The local job queue and application
publication checks remain authoritative.

## Native tool loop and recovery

This route does not require a Codex CLI, Codex login, Claude Code, or a developer
checkout. A private per-task `run-state.json` stores conversation state;
`calls/*.json` stores durable tool invocation outcomes; `turn-state-*.json` is an
immutable completion snapshot with a SHA-256 receipt. A resumed task reuses the
application thread ID and never repeats a tool mutation whose previous outcome
is unknown. The existing registered ReqPilotX tools, skill bundle identity,
source/version binding, feedback and publication guard are reused. The provider
chooses tools; the application validates and persists business artifacts.

If host publication fails after a completed turn, finalization can verify the
local completion snapshot and same-version close/deliver/publish calls without
requesting another model turn. It rejects an ambiguous tool outcome, failed
turn, changed source binding, or altered snapshot. Tool receipts accumulate
across interrupted turns in the same pinned task.

## Windowed table and direct-text stages

`ProviderSourceRequirementWindowExecutor` and `ProviderSourceItemTextExecutor`
reuse the existing source-bound business compiler, boundary/classification
windows, explicit validation retry, and cumulative durable checkpoints. Their
read-only stage transport calls the configured provider SDK directly and offers
original PDF pages/pixels, native text, and locked stage files to the model.
Full scope files remain available through tools; output scope never restricts
which source page the model may inspect. The model cannot publish business
objects through this transport. SDK retries are disabled; each structured phase owns a fresh configured SDK
client and closes only that owned HTTP transport. Cached application clients
remain separate, preventing a completed phase from poisoning later calls.
A regression uses real OpenAI/Azure SDK lifecycle with HTTP-only mocks.
Host/window retries
remain explicit and task time/cancellation limits are checked between steps.
Errors expose stable failure codes rather than provider prompts or credentials.

Provider, configured model, source bytes, project/document, semantic context,
execution context and the exact compatibility kit are bound to resumed work.
Changing the provider profile cannot silently change an already pinned model.
The legacy `codex_version` receipt field contains the explicitly prefixed opaque
identity `provider-structured-stage:<provider>:...`; it does not assert a Codex
installation. `provider-default` means no portable reasoning-effort parameter
was sent to the service, not a claim that a Codex effort setting was applied.

The compatibility kit was explicitly released as **0.16.0**, with locked mixed
text-ownership schema variants and the three matching runtime-profile versions.
Old kit/checkpoint identities fail closed. This release is independent of the
current extraction-loop skill bundle. The schema exporter and lock checker
verify the current candidate; updating either is an explicit release operation.

An installed host must supply the released kit at an absolute
`REQPILOT_STRUCTURED_AGENT_KIT_ROOT`. In explicitly configured development mode,
`REQPILOT_DEVELOPMENT_ROOT/agent-kit` is the fallback. A bare server wheel does
not currently contain this separately versioned kit; absence is a readiness
blocker, not permission to silently use another checkout or download a kit.

## Joint publication and tested boundaries

The durable mixed-source worker completes sibling tables competing for the same
node's table-owned text, preserves their private candidates/window checkpoints,
then validates exact direct-text ownership. One persisted product successor and
same-version Matrix/Excel contain the whole accepted group. An interruption
before text publication keeps completed tables; retry does not request them
again. Conflicting ownership or changed source/checkpoint/adapter identity
stops before publication. This structured extension requires a product
predecessor; cold start remains the source-document tool-loop route.

Cloud offline evidence uses synthetic documents and configured-SDK mocks:

- Existing provider tool loop: 11 tests
- Provider window adapters: 11 tests, plus existing Codex window regression
- Actual provider executors through durable joint publication, private object
  persistence and same-version Excel: 3 tests, covering both Azure and Bailian,
  text interruption/retry, stable predecessor identity, model/source drift
- Final source-requirement aggregate: 69 tests
- Compatibility kit: 16 tests; schema exporter and exact lock checks pass

These results do not establish deployment-specific tool/vision support, live
Azure authentication, Bailian service responses, semantic quality, customer
extraction, actual installed Pair/plugin host behavior, Mac UI acceptance, or
Windows installation. No live model calls, customer data or credential
transmission were used. The older structure handoff route still uses a
Codex-specific adapter and must not be counted as a provider-backed route.
