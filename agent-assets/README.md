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

The compatibility Agent Kit remains at `agent-kit/` because it has live runtime
and replay consumers. Its locked contents are not silently upgraded from the
current skills.
