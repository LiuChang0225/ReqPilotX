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

Successful, application-saved provider tasks now have a local candidate for
source-bound terminal-feedback continuation. It retains the entire audit and
projects only a verified deterministic handoff plus the active tool chain into
new requests; unfinished/cancelled recovery keeps its existing path. Usage is
persisted before length/parse rejection, with private metadata-only request
records. No quotas or additional model summaries are introduced. See the
[continuation contract](../architecture/provider-terminal-continuation.md) and
[offline validation](../validation/provider-terminal-continuation-20261008.md).

## Cancellation, deadline and SDK controls (2026-10-05 candidate)

Each native provider-loop request uses a task-local SDK view with
`max_retries=0` and the remaining task deadline as its request timeout. The
cached application client's retry settings and shared HTTP transport are left
unchanged and are never closed by this loop. Clients that cannot expose
`with_options` fail closed instead of silently retaining hidden SDK retries.

Controls are checked before requests, after provider responses, before each
host invocation (including after the activity/heartbeat callback), before
accepting a saved terminal response, and before sealing a completed turn.
Cancellation yields `INTERRUPTED`; an expired task deadline yields `FAILED`
with `PROVIDER_LOOP_TIMEOUT`. Provider call IDs already returned are retained
for safe continuation, but no additional model/tool request is started after
an observed control. A returned final message after cancellation does not
create completed proof usable by host-only publication recovery. An explicitly
continued cancelled turn can reuse its saved terminal response once controls
are clear. A timed-out `FAILED` turn requires a fresh provider completion; its
expired terminal prose is not accepted as completion of the new attempt.

Cancellation of an already-waiting synchronous SDK request is cooperative:
it is observed when the request returns or fails. The remaining-time SDK
transport timeout limits network operations; it is not a separate hard
wall-clock thread kill. The normal PostgreSQL lease/cancellation publication
fence remains authoritative if cancellation races with host finalization.

A prior `INVOKING` tool receipt or an unexpected host exception/invalid response
or failure to durably seal a returned host result has an uncertain outcome. The
turn remains `UNKNOWN` with
`PROVIDER_LOOP_TOOL_OUTCOME_UNKNOWN` and stops before another model or tool
request. The host must inspect/reconcile that outcome before continuation;
the loop does not offer it back to the model as an ordinary retryable failure.
Known host failure responses and schema-rejected arguments retain their normal
tool-response contract and remain available to the model for correction. If the
lease/cancellation heartbeat itself fails, the turn is sealed as redacted
`UNKNOWN` with `PROVIDER_LOOP_CONTROL_UNAVAILABLE`; it cannot supply completed
proof until the task can safely read its controls again.

The candidate is verified offline with both provider contracts and real
OpenAI/Azure SDK clients over HTTP-only MockTransport. This establishes control
status, bounded request options, no SDK retry and shared-client ownership;
actual provider transport, PostgreSQL end-to-end execution and employee host
journeys still require separate environment-specific evidence.

## Qwen3.8 Max thinking compatibility (2026-10-08 uncommitted candidate)

The native provider loop now explicitly configures thinking for Aliyun
`qwen3.8-max-0902` (and the documented `qwen3.8-max` alias). The exact configured
model is sent unchanged; this does not replace the snapshot with an alias.
`REQPILOT_AGENT_ALIYUN_ENABLE_THINKING=1` is the default and sends
`extra_body={"enable_thinking": true, "preserve_thinking": true}`. Setting it to
`0` sends both values as false for a non-thinking acceptance run. Other configured
Aliyun models retain their previous request parameters because these fields are
not universally supported. Azure receives neither flag nor `reasoning_content`.

Assistant `reasoning_content` is validated as text and kept separately from
`content` in the private, hash-bound conversation. Thinking-enabled requests
replay the complete saved history, including tool-call rounds and continued
tasks. Non-thinking requests omit reasoning from the outgoing history while
retaining the original saved messages. Old sessions that lack reasoning can
continue; previously discarded reasoning cannot be reconstructed. Public events
do not emit reasoning. Receipts record only the non-secret request policy.
The shared structured-stage normalizer also retains returned reasoning for
Aliyun and filters it out of Azure requests; its separate stage profile does
not inherit the native loop's thinking switch.

The [official deep-thinking guide](https://help.aliyun.com/zh/model-studio/deep-thinking)
lists this exact snapshot with thinking enabled by default. The
[official OpenAI-compatible API contract](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
documents default thinking preservation for Qwen3.8 Max, complete history in its
own reasoning field, and tolerance of missing historical reasoning. It also
documents `max_completion_tokens` as covering thinking plus the answer, with up
to 10 tokens of difference. These are vendor statements checked on 2026-10-08,
not live model measurements. The
[function-calling guide](https://help.aliyun.com/zh/model-studio/qwen-function-calling)
keeps returned call IDs associated with tool responses; this candidate retains
the existing durable ID/receipt contract.

Per the user's latest instruction, **no new request-count limit, cumulative
budget, output quota, `max_tokens` or `max_completion_tokens` parameter is added**.
The initially drafted budget/count/output configuration was removed before
delivery. Existing cancellation, task deadlines, SDK `max_retries=0`, and the
160-iteration loop guard remain. The guard counts loop iterations, including
tool handling, rather than a durable provider-request total. There is no new
monetary ceiling, cumulative billing system, no-progress detector or duplicate
model-request reconciliation layer. A returned `length` now yields `FAILED`
with `PROVIDER_LOOP_OUTPUT_TRUNCATED`, without appending a partial assistant
message, invoking any of its tools or producing completed publication proof.

The existing SDK zero-retry guarantee applies to one dispatch. The existing
worker may retry a raised provider transport error as a later job attempt; this
candidate does not change that recovery policy or prove no model re-dispatch
across attempts with an unknown provider outcome. Unknown host mutations still
use the existing durable tool fence. Do not equate the timeout regression with
task-wide exactly-once provider delivery.

This policy is local to the native source-document/review tool loop. Separate
windowed table/direct-text stages, source capture/OCR, visual transcription,
structure/semantic/refinement/atomization providers, AI Copilot screening and
analysis/retrieval/embeddings, and external Codex/Pair/plugin host model calls
are not governed by a new shared budget or by this native thinking setting.
The initial SaaS profile already disables the separate Kit-backed write routes.
No live provider, customer-source quality, production PostgreSQL, ECS/system
service, deployment, or employee browser acceptance is claimed by these offline
regressions.

Final candidate offline evidence: 188 Python unittests passed (0 failed, 0
skipped), including 9 new thinking-compatibility tests, native provider controls,
source-document/review jobs, runtime/settings, structured windows, plugin
workbench/delivery and execution contracts. Pair discovery/observation/host
contracts passed 14 tests (0 failed, 0 skipped). Three host tests initially
failed because the sandbox denied a temporary loopback listener; the approved
local-only rerun passed. Tests used the existing Python venv and Node, an empty
inherited environment, isolated test storage, synthetic source and SDK
MockTransport. No dependency install, build, real-model request, secret read,
production DB/service change, commit, push or deployment was performed.

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

- Baseline provider tool loop: 11 tests; this control candidate adds offline
  cancellation/deadline, resume, unknown-outcome and real-SDK retry regressions
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
