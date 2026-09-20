"""Compatibility entrypoint for the promoted Codex App Server probe."""

from app.adapters.agents.codex_app_server.probe import *  # noqa: F403
from app.adapters.agents.codex_app_server.probe import main


if __name__ == "__main__":
    raise SystemExit(main())
