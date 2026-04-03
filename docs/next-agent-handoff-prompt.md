# Next Agent Handoff Prompt

更新时间：2026-04-02 07:36 CST

把下面整段提示词直接交给下一个窗口的 agent。

```text
You are taking over the ReqPilotX repository on Windows.

Workspace root:
c:\Users\uib03712\OneDrive - Vitesco Technologies\00_WORK SPACE\CE PMT\VibeCoding\ReqPilotX

Do not re-plan the project from scratch. Start from the current documented reality, preserve the local-ops-first direction, and continue from the current execution order unless the user explicitly redirects you.

First, read these files in this order before editing anything:
1. AGENTS.md
2. docs/项目.md
3. docs/开发执行清单.md
4. docs/active-context.md
5. prototype/docs/extraction-active-context.md
6. If you touch Azure or deployment work, also read docs/Azure环境接手清单.md
7. If you touch Aliyun local RAG work, also read docs/Aliyun本地Qdrant-PoC清单.md

Current repo reality you should assume:
- ReqPilotX is a FastAPI + static HTML/JS monolith rooted in prototype/.
- main is the authoritative branch for current runtime and doc truth; current remote main commit is c537a85 ("Stabilize Azure runtime and deployment scaffold").
- Current near-term focus is local server operations on port 8000.
- Azure App Service single-instance pilot remains a future recovery path after formal release approval; do not resume azd provision/up by default.
- Current live AI baseline is llm_shadow + azure_sdk + rbac.
- Azure remains the default profile even when Aliyun becomes runnable; do not auto-promote Aliyun unless the user explicitly asks or REQPILOT_AI_DEFAULT_PROFILE=aliyun is set.
- Stable Azure auth path on this machine is Azure CLI default subscription plus AzureCliCredential.
- App startup now best-effort injects truststore, and Windows cert handling has already been fixed for the recent Azure knowledge-sync TLS failure.
- Workspace/status payloads are now profile-aware and include ai_profiles, default_ai_profile_id, and runnable_ai_profile_count.
- The home page now includes an AI path setup card before knowledge upload:
  - explicit default-profile choice now matters for knowledge upload
  - Aliyun runtime config is entered there
  - Azure local auth should still prefer Azure CLI / RBAC
- Knowledge document upload now syncs only the default profile by default; Aliyun sync is lazy and happens when the user explicitly runs profile_id=aliyun.
- If the user explicitly chooses Aliyun as the default profile before upload, the app should block knowledge upload until Aliyun retrieval config is actually ready; do not silently fall back to Azure.
- Aliyun local Qdrant POC now exists behind REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant. Missing local Qdrant should only fail checks.aliyun and must not break Azure readiness when Azure is runnable.
- If the UI says there are no runnable AI paths, first suspect a stale 8000 process serving an old /workspace schema; compare /api/health and /workspace output before assuming a readiness bug.
- Azure smoke scripts no longer default to live prototype/storage:
  - prototype/scripts/azure_screening_smoke.py
  - prototype/scripts/azure_rag_smoke.py
  - both now default to prototype/storage/smoke/azure_screening or azure_rag
  - both refuse live prototype/storage unless --allow-live-storage is passed
- Aliyun local smoke now has its own isolated script:
  - prototype/scripts/aliyun_local_rag_smoke.py
  - defaults to prototype/storage/smoke/aliyun_local_rag
  - preflights local Qdrant before running
- sample10 real Azure benchmark is green, but evidence relevance still needs human review.
- App Service remains constrained to 1 instance until SQLite plus in-process jobs are replaced.

Default execution priority at handoff time:
1. OPS-02: finish production-foundation hardening around settings, readiness, observability, and AI job reliability.
2. OPS-03: keep smoke DB isolated from live app.db and stabilize local-ops workflow.
3. QA-01: expand manual review and threshold confidence around the current Azure benchmark baseline.
4. Only after the above, or if the user explicitly asks, resume AI-07 hardening for the Aliyun local Qdrant POC.

What "continue from current state" means in practice:
- Treat docs/项目.md + docs/开发执行清单.md + docs/active-context.md as the source of truth.
- Use docs/next-agent-handoff-prompt.md only as an entry prompt, not as a replacement for the execution order in docs/开发执行清单.md.
- Port 8000 is the authoritative live port when the app is running; 8001 is only a disposable preview port.
- Do not assume a running local server is authoritative; check with prototype/status.sh or restart from prototype/ when needed.
- Do not point smoke runs at live prototype/storage unless explicitly and intentionally required.

If the user explicitly resumes AI-07 / Aliyun retrieval work:
- Keep the Azure path green while changing Aliyun.
- Treat Aliyun as aliyun-local-poc, not as a replacement for the Azure mainline.
- Aliyun request-threading, provenance, and UI selection are already wired through runtime, DB, workspace payloads, and tests.
- Supported Aliyun knowledge-backed path in main is now:
  - REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant
  - DashScope for chat + embeddings
  - local Qdrant service for retrieval/index
- Do not let Aliyun steal default-profile behavior from Azure:
  - upload-time sync must stay default-profile-only
  - Azure should remain the default profile unless explicitly changed
  - Aliyun readiness failures should remain isolated under checks.aliyun
- Use prototype/scripts/aliyun_local_rag_smoke.py for smoke, not the Azure scripts.
- The remaining AI-07 question is no longer "restore any backend"; it is:
  - validate and harden the existing aliyun-local-poc on a real non-company-network setup, or
  - if the user asks for a production-grade Aliyun path, treat that as a new architecture decision beyond this POC

Validation expectations:
- Run the smallest relevant unittest/pytest target first, then expand if needed.
- If you touch runtime behavior, update or add tests under prototype/tests/.
- If you touch docs or handoff narrative, keep them aligned in the same turn.
- Keep smoke, live server checks, and context docs operational and brief.

Documentation requirements after meaningful changes:
- Update docs/开发执行清单.md when priorities or task status shift.
- Update docs/active-context.md.
- Update prototype/docs/extraction-active-context.md.
- Keep notes short, factual, and ready for a new Codex window.
```
