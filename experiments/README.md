# ReqPilotX experiments

Experiments are isolated engineering tools, not production workflow contracts
or proof that a capability has passed product acceptance.

- `codex-app-server/` owns the synthetic canary schemas and their historical
  notes. Runtime adapters consume these schemas through the explicit
  development resource resolver.
- `prototype/target_review/` remains an active local evidence-review experiment
  with private feedback consumers. It is retained in place and classified
  `UNRESOLVED` until those consumers have a tested migration.
- `prototype/type_review/` remains the all-types decision experiment and is
  retained for the same reason.
- Thin modules under `prototype/spikes/codex_app_server/` are compatibility
  entry points to the installed server implementation. They remain until old
  commands and tests exit.

No experiment reads customer data unless an operator explicitly supplies an
authorized private path to that experiment's documented command.
