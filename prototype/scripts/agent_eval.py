#!/usr/bin/env python3
"""Run the ReqPilotX local Agent Eval suite; private artifacts stay outside code."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.agent_evals.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
