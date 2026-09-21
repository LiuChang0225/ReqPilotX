# ReqPilotX engine release

This project owns the authoritative, installable ReqPilotX business engine.
Runtime-neutral domain, application, port and portable document/export code lives
once under `src/reqpilotx`. `plugins/reqpilot/scripts/build_engine.py` selects the
reviewed closure from that package, writes its immutable identity manifest, and
builds a wheel and sdist without copying implementation from the server tree.

Build prerequisite: a prepared stable **uv >= 0.10.2** on `PATH`; CI pins
**uv 0.12.13**. The builder checks this before creating output and reports an
unsupported/missing version explicitly. It never upgrades or installs uv.

Engine releases require committed inputs: `packages/engine`, dependency pins,
the builder/selector and package metadata.
Staged, unstaged or untracked changes in those inputs are refused. Selected
bytes are additionally checked against the recorded commit, and package metadata
is staged from those Git blobs. Unrelated documentation/UI changes and ignored
build output do not block a release. No dirty-release bypass is provided; commit
an isolated candidate before building it. The no-wheel plugin development build
continues to support working-tree source and is not a formal engine release.

The installed package uses the public `reqpilotx` namespace and can be installed
beside `reqpilotx-server`; the engine has no import dependency on the server,
database, worker, API or concrete Agent host. Legacy `prototype/app` modules are
development compatibility aliases and are not included in the engine wheel.
