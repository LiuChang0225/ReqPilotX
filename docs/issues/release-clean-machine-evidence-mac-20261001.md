# ReqPilotX v2 candidate: Mac verification, 2026-10-01

This is supporting evidence for [the release clean-machine record](release-clean-machine-evidence.md), not an employee release declaration. The canonical source is `ReqPilotX-Workspace/repo`; the older `New project` checkout was preserved.

## Source reconciliation

- Base Git commit: `a7d9d29e081b1be8c73e72e57bf67cbd44ec5903`.
- The original 95 dirty paths and the integrated 145-path candidate were unchanged before reconciliation. All 2362 original migration entries matched their hashes and exact symlink targets.
- Downloaded `reqpilotx-cloud-source-delta-20261001-v2.tar.gz`: 479840 bytes; SHA-256 `d92792a2e0cd08b72097f3784bd4599d49c06575da47e7e5d7edf1b3e206cf8c`.
- Every archive member, source payload, metadata digest and modified-file preimage was checked before applying. The package contains 64 modified, 15 added and one supplemental source entry, with no deletions.
- Backed up the original dirty state and overwritten source files. Applied 74 local fixes followed by 79 cloud replacements; the already matching branding asset required no replacement. Runtime data and previous capability/task stores were preserved in place.
- Final migrated-source and delta hashes matched before and after builds. Cloud candidate identity: `722d94edab82daf88885e046352556faf65354a05a8846f73bb06b7a4124c1a9`.
- This evidence note is an additional documentation file. After verification, the commit whitespace gate required removal of a trailing space in `host_launch.py` and an extra final blank line in `test_smoke_native_provider.py`; token content and behavior are unchanged. Original v2 bytes and both correction preimages remain preserved locally.

## Offline verification

The delivered `mac-verify-cloud-candidate-v2.sh` completed with Python 3.13.2 and existing dependencies. The runner uses isolated storage and rejects network connections.

- Sixteen focused Python suite runs passed. Suite counts can overlap and are not a unique-test total.
- Frontend: 83 files / 909 tests passed.
- Typecheck, Web build, shared panel build, CSS scope tests and Pair build passed.
- Pair contract suite: 18 passed.
- Agent Kit schemas/lock and current asset registry checks passed.

## Synthetic services

The delivered `mac-verify-services-v2.sh` completed using Homebrew PostgreSQL 16.14, fresh private databases and synthetic PDF fixtures. All receipts reported PASS and all owned processes stopped.

| Path | Verified on this Mac |
|---|---|
| External workbench | Real PostgreSQL/API opinions, fixed-version delivery/export, withdrawal, restart and backup/restore |
| Native provider route | Real durable queue/workers and Azure SDK against a loopback HTTP model mock; same-task continuation, opinions and fixed-version delivery |
| Portable plugin | Actual MCP/fixed CLI subprocesses through real API/PostgreSQL; feedback, pinned opinions/delivery reads, export and restart |
| Pair service adapter | Newly built non-editable engine/server wheels; actual managed service/proxy lifecycle, opinions/delivery and restart; no external Pair UI |

Engine/server wheels were built offline from this candidate. The private installed interpreter reused the already tested dependency site-packages. This establishes installed-distribution behavior, not a fresh-machine closed-dependency installation.

Private logs, databases, receipts, backups, wheels and fixture outputs remain outside Git. No credentials or customer documents were copied or transmitted, and no real model was called.

## Remaining acceptance

Actual Pair/plugin autonomous hosts, rendered employee UI/PDF, live Bailian/Azure, authorized customer-document quality, Windows 11 installation/runtime and colleague independent use remain unverified. The separately authorized 200 CNY cumulative synthetic-model budget has not been spent; the budget guard must be installed and verified before any live call.

## CI follow-up

The first development-branch CI run rejected seven historical machine paths and a synthetic credential-bearing URL fixture under the existing repository safety rules. The paths now use portable workspace/evaluation placeholders; the URL remains a userinfo rejection case with a loopback host. No safety rule was weakened. Original bytes remain in the private local reconciliation evidence. CI status must be read from the current PR head; the Mac receipts above do not establish a GitHub or Windows pass.

The broader GitHub Python discovery run also exposed a missing engine compatibility alias, two new tests lacking the standard runtime isolation preamble, and a saved-number test fixture missing the session identity now required by the response view. The aliases and fixtures were brought into the existing contracts; production session identity checks remain strict. Focused local contract tests verify these corrections; the full Linux discovery result is recorded by CI.
