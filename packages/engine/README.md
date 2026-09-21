# ReqPilotX engine release

This project is the build configuration for the transitional, installable
ReqPilotX portable engine. `plugins/reqpilot/scripts/build_engine.py` stages the
existing allowlisted source closure here, writes its immutable identity manifest,
and builds a wheel and sdist. The staged tree is temporary and must never become
a second maintained source copy.

Build prerequisite: a prepared stable **uv >= 0.10.2** on `PATH`; CI pins
**uv 0.12.13**. The builder checks this before creating output and reports an
unsupported/missing version explicitly. It never upgrades or installs uv.

Engine releases require committed inputs: the `prototype/app` source tree,
selected command roots, dependency pins, builder/selector and package metadata.
Staged, unstaged or untracked changes in those inputs are refused. Selected
bytes are additionally checked against the recorded commit, and package metadata
is staged from those Git blobs. Unrelated documentation/UI changes and ignored
build output do not block a release. No dirty-release bypass is provided; commit
an isolated candidate before building it. The no-wheel plugin development build
continues to support working-tree source and is not a formal engine release.

The installed package intentionally retains the internal `app` namespace during
R1. It must be used in an isolated portable environment and must not be installed
beside the full ReqPilotX server package. R3 owns the eventual move to the public
`reqpilotx` namespace.
