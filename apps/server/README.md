# ReqPilotX server

This package owns the API, durable worker, configuration, persistence, object
storage and concrete Agent-host adapters. It depends on `reqpilotx-engine` and
never carries a second copy of the engine implementation.

Installed entrypoints are `reqpilotx-api` and `reqpilotx-worker`. The legacy
`prototype/start.sh` and `prototype/app` imports remain development compatibility
routes while R4 migrates the outer application packaging.
