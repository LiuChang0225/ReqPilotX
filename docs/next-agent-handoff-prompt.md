# Next Agent Handoff Prompt

更新时间：2026-04-01

把下面整段提示词直接交给下一个窗口的 agent。

```text
You are taking over the ReqPilotX repository on Windows.

Workspace root:
c:\Users\uib03712\OneDrive - Vitesco Technologies\00_WORK SPACE\CE PMT\VibeCoding\ReqPilotX

Your job is not to re-plan the project from scratch. Start from the current documented reality, preserve the local-ops-first direction, and resolve the Aliyun retrieval blocker with a concrete implementation direction.

First, read these files in this order before editing anything:
1. AGENTS.md
2. docs/项目.md
3. docs/开发执行清单.md
4. docs/active-context.md
5. prototype/docs/extraction-active-context.md
6. If you touch Azure or deployment work, also read docs/Azure环境接手清单.md

Project reality you should assume:
- ReqPilotX is a FastAPI + static HTML/JS monolith rooted in prototype/.
- Current near-term focus is local server operations on port 8000; Azure App Service single-instance pilot remains a future recovery path after formal release approval.
- Current live AI baseline is llm_shadow + azure_sdk + rbac.
- Stable Azure auth path on this machine is Azure CLI default subscription plus AzureCliCredential.
- Current Azure resources already validated for data-plane use are:
  - subscription: 0adac5c3-3e8b-4670-9c15-cc95ea432b0f
  - tenant: 67416604-6509-4014-9859-45e709f53d3f
  - Azure OpenAI: oaicspq001reqpilotbdpsgcq
  - chat deployment: gpt-5.4
  - embedding deployment: text-embedding-3-large
  - Azure AI Search: cscspq001reqpilotbdpsgcq
  - index: reqpilot-knowledge
- Azure smoke scripts were just hardened so they no longer default to live prototype/storage:
  - prototype/scripts/azure_screening_smoke.py
  - prototype/scripts/azure_rag_smoke.py
  - both now default to prototype/storage/smoke/azure_screening or azure_rag
  - both refuse live prototype/storage unless --allow-live-storage is passed
- sample10 real Azure benchmark is green, but evidence relevance still needs human review.
- Current Azure deployment is intentionally paused; existing blocker evidence is management-plane RBAC plus formal release gating, not Azure OpenAI/Search data-plane access.
- App Service remains constrained to 1 instance until SQLite plus in-process jobs are replaced.

Do not spend this turn rewriting architecture or expanding scope. Do not start frontend rewrite, parser replacement, llm_primary rollout, full DB migration, full Blob migration, or Document Intelligence cutover.

Primary next task:
Continue AI-07 from the new blocker state. Do not resume the removed qdrant / dashvector / local retrieval paths.

Project reality you must assume for Aliyun work:
- The repo currently keeps the Azure profile as the only runnable knowledge-backed path.
- Aliyun retrieval backends qdrant, dashvector, and local were intentionally removed from the active runtime.
- The removal happened because:
  - this workspace does not ship a Qdrant deployment artifact
  - the current developer machine does not have a verified running Qdrant instance
  - there is no verified DashVector resource baseline in the repo docs/env
  - local heuristic retrieval was explicitly rejected on quality grounds and must not be reintroduced
- The Aliyun profile now reports retrieval_backend=disabled and is intentionally not runnable until a supported remote retrieval backend is implemented.

What AI-07 means here now:
- Inspect prototype/app/services/ai_copilot.py, prototype/app/main.py, prototype/app/runtime_readiness.py, prototype/tests/, and the current handoff docs.
- Choose one supported remote retrieval/index backend for Aliyun that is actually deployable and operable in this repo context.
- Do not restore qdrant, dashvector, or local as a stopgap.
- Either:
  - implement the new backend end-to-end with config, sync, retrieval, readiness, tests, and smoke guidance, or
  - produce a precise blocker document explaining why no acceptable Aliyun retrieval backend can be landed yet.
- Keep the existing Azure path green while changing Aliyun.

Validation expectations for your turn:
- Run the smallest relevant pytest target first, then expand if needed.
- If you touch Aliyun runtime behavior, update or add tests under prototype/tests/.
- If you choose a new remote backend, add the minimal operator-facing setup and smoke steps.
- Do not assume a running local server is authoritative; check with prototype/status.sh or start from prototype/ explicitly.

Working constraints:
- Treat docs/项目.md + docs/开发执行清单.md + docs/active-context.md as the source of truth.
- Port 8000 is the authoritative live port when the app is running; 8001 is only a disposable preview port.
- Do not point smoke runs at live prototype/storage unless explicitly and intentionally required.
- Azure CLI credential is the trusted auth path; tenant mismatch from other cached credentials is known noise.
- azd provision/preview is still blocked until the active Entra object has Microsoft.Resources/subscriptions/resourcegroups/write and Microsoft.Resources/deployments/whatIf/action.

Documentation requirements after your change:
- Update docs/开发执行清单.md with task status and concrete progress.
- Update docs/active-context.md.
- Update prototype/docs/extraction-active-context.md.
- Keep the notes operational and brief.

Success criteria for this turn:
- You land a viable Aliyun remote retrieval direction with tests and doc sync, or
- You produce a precise blocker with evidence after attempting that implementation.
```