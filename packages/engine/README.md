# ReqPilotX engine release

This project is the build configuration for the transitional, installable
ReqPilotX portable engine. `plugins/reqpilot/scripts/build_engine.py` stages the
existing allowlisted source closure here, writes its immutable identity manifest,
and builds a wheel and sdist. The staged tree is temporary and must never become
a second maintained source copy.

The installed package intentionally retains the internal `app` namespace during
R1. It must be used in an isolated portable environment and must not be installed
beside the full ReqPilotX server package. R3 owns the eventual move to the public
`reqpilotx` namespace.
