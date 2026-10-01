"""Run backend/plugin unittest suites with disposable runtime configuration.

Usage: prototype/.venv/bin/python scripts/test_python.py [backend|plugin|all]
Pass --pattern 'test_example.py' for a focused run. External integration tests
have their own explicit entrypoints; this command always disables their opt-ins.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("backend", "plugin", "all"), default="all", nargs="?")
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("REQPILOT_", "REQPILOTX_", "OPENAI_", "AZURE_", "DASHSCOPE_", "ALIYUN_", "ALIBABA_CLOUD_"))
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    with tempfile.TemporaryDirectory(prefix="reqpilotx-offline-tests-") as temporary:
        env.update({
            "REQPILOT_TEST_MODE": "1",
            "REQPILOT_ENV": "test",
            "REQPILOT_TEST_STORAGE_DIR": temporary,
            "REQPILOT_STORAGE_DIR": temporary,
            "REQPILOT_OBJECT_STORE": "local",
            "REQPILOT_OBJECT_ROOT": temporary,
            "REQPILOT_SCRATCH_ROOT": str(Path(temporary) / "scratch"),
            "REQPILOT_AGENT_RUNTIME": "none",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "DO_NOT_TRACK": "1",
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT / "packages/engine/src"),
                    str(ROOT / "apps/server/src"),
                    str(ROOT / "prototype"),
                )
            ),
        })
        suites = ("backend", "plugin") if args.suite == "all" else (args.suite,)
        results = []
        for suite in suites:
            # A socket audit hook fails real network attempts instead of waiting
            # on unavailable providers. ASGI TestClient and MCP stdio need none.
            bootstrap = (
                "import runpy,sys; "
                "exec('def deny_network(event, args):\\n"
                " if event == \"socket.connect\":\\n"
                "  raise RuntimeError(\"Network access is disabled in offline tests\")'); "
                "sys.addaudithook(deny_network); "
                "sys.argv[0]='unittest'; runpy.run_module('unittest',run_name='__main__')"
            )
            command = [sys.executable, "-c", bootstrap, "discover", "-v", "-p", args.pattern]
            if suite == "backend":
                command += ["-s", "prototype/tests", "-t", "prototype"]
            else:
                command += ["-s", "plugins/reqpilot/tests"]
            print(f"Running {suite} tests with isolated storage and model downloads disabled", flush=True)
            results.append(subprocess.run(command, cwd=ROOT, env=env, check=False).returncode)
        return int(any(results))


if __name__ == "__main__":
    raise SystemExit(main())
