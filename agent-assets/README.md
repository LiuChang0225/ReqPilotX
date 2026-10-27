# ReqPilotX Agent assets

This directory owns the maintained sources and immutable vendor snapshots used
by Agent discovery and capability builds. It does not contain customer files,
model traces, run workspaces, or accepted answers.

- `current/project-skills/` is the source for the repository-local project skill.
- `current/plugin-skills/` is the source for the portable business skills and
  references shipped in new ReqPilotX plugin bundles.
- `vendor/` keeps versioned third-party bytes together with upstream version,
  license and provenance records. A vendor upgrade creates a new release
  directory; it never rewrites an older Agent Kit identity.
- `compatibility/runtime-skills/` keeps explicit-only canary skills that are not
  the current workflow.
- `catalog.json` classifies each group, names its consumers and records the
  canonical source-tree identity.

Source-checkout discovery paths such as `.agents/skills` and
`plugins/reqpilot/skills` are deterministic projections. Edit the canonical
source, then rebuild and verify projections:

```bash
python3 agent-assets/scripts/asset_registry.py --repo . sync
python3 agent-assets/scripts/asset_registry.py --repo . check
```

Release builds verify that their Agent asset inputs are committed, clean and
from the same commit as the engine wheel. A built bundle can be checked without
the source checkout:

```bash
python3 agent-assets/scripts/asset_registry.py verify-bundle --bundle /absolute/path/to/reqpilot
```

Verification compares the complete `skills/` and `capabilities/pdf/` inventories
with declared assets, rejecting extra files, linked roots/parents and special
files. The evaluation host installer uses the same inventory guard, including
for retained pre-R5 bundles; ordinary host installation markers remain allowed
outside asset roots. Its verifier is trusted tool code, never loaded from the
bundle being inspected.

Release inputs are compared with the exact commit's member set, regular-file
modes and blob bytes. Git-ignored files are not exempt, and hidden working-tree
changes cannot be accepted merely because `git status` is clean. Rejection does
not delete files or stage them automatically.

The compatibility Agent Kit remains at `agent-kit/` because it has live runtime
and replay consumers. Its locked contents are not silently upgraded from the
current skills.

The release regression fixture at
`prototype/tests/fixtures/agent-assets/approved-release.json` pins the exact
member paths, SHA-256 hashes and lengths from its named reviewed source commit.
Repository and frozen-bundle tests compare those members as well as verifying
the catalog and projections. An intentional asset change updates the canonical
source, catalog, projections and this reviewed fixture together; a passing build
or a changed file count does not authorize a new member. Extra-file, tamper and
symlink rejection still runs against both bundle verification consumers.
