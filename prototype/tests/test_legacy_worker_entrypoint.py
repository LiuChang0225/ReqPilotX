from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]

# Execute the real legacy/canonical module entrypoints in a fresh process.
# Only database/runtime side-effect boundaries are substituted. Argparse and
# the production main() body remain real for help, bad arguments, and --once.
CHILD = r'''
import enum
import importlib
import json
from pathlib import Path
import runpy
import sys
import types

root, target, mode, *arguments = sys.argv[1:]
sys.path[:0] = [str(Path(root) / "prototype"),
               str(Path(root) / "apps/server/src"),
               str(Path(root) / "packages/engine/src")]

def deny_network(event, args):
    if event == "socket.connect":
        raise RuntimeError("Worker entrypoint test forbids network")
sys.addaudithook(deny_network)

trace = dict(build=0, start=0, once=0, forever=0, close=0, container_close=0,
             forwarded_main=0)

def module(name, **attributes):
    value = types.ModuleType(name)
    value.__dict__.update(attributes)
    sys.modules[name] = value
    return value

class ProcessRole(enum.Enum):
    WORKER = "worker"

class RuntimeSettings:
    @staticmethod
    def from_environment():
        return types.SimpleNamespace(process_role=ProcessRole.WORKER)

class Container:
    jobs = object()
    def close(self):
        trace["container_close"] += 1

def build_runtime_container(settings):
    trace["build"] += 1
    return Container()

class WorkerRuntime:
    def __init__(self, **values):
        trace["lease_seconds"] = values["lease_seconds"]
    def start(self):
        trace["start"] += 1
    def run_once(self):
        trace["once"] += 1
    def run_forever(self, **values):
        trace["forever"] += 1
        raise AssertionError("--once must not enter the polling loop")
    def close(self):
        trace["close"] += 1
    def request_drain(self):
        raise AssertionError("No signal is expected in this test")

module("reqpilotx_server.infrastructure.runtime_container",
       build_runtime_container=build_runtime_container)
module("reqpilotx_server.infrastructure.runtime_settings",
       ProcessRole=ProcessRole, RuntimeSettings=RuntimeSettings)
module("reqpilotx_server.services.ai_copilot",
       reload_runtime_ai_config_from_file=lambda: None)
module("reqpilotx_server.application.extraction.source_asset_objects",
       recover_stale_source_asset_publications=lambda: None)
module("reqpilotx_server.worker.handlers",
       build_job_handlers=lambda container: {"document.extract": object()})
module("reqpilotx_server.worker.runtime",
       WorkerRuntime=WorkerRuntime, JobHandler=object)
module("reqpilotx_server.composition.trust", configure_host_truststore=lambda: None)

sys.argv = [target, *arguments]
try:
    if mode == "import-only":
        legacy = importlib.import_module("app.worker.__main__")
        canonical = importlib.import_module("reqpilotx_server.worker.__main__")
        trace["same_module"] = legacy is canonical
    else:
        if mode == "return-code":
            canonical = importlib.import_module("reqpilotx_server.worker.__main__")
            def main():
                trace["forwarded_main"] += 1
                return 37
            canonical.main = main
        runpy.run_module(target, run_name="__main__", alter_sys=True)
finally:
    print("WORKER_TRACE=" + json.dumps(trace, sort_keys=True), file=sys.stderr)
'''


class LegacyWorkerEntrypointTests(unittest.TestCase):
    def invoke(self, target, *arguments, mode="normal"):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("REQPILOT", "OPENAI_", "AZURE_", "DASHSCOPE_",
                                   "ALIYUN_", "ALIBABA_CLOUD_", "PYTHONPATH", "PYTHONHOME"))
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", CHILD, str(ROOT), target, mode, *arguments],
            cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30,
        )
        traces = [line.removeprefix("WORKER_TRACE=")
                  for line in completed.stderr.splitlines()
                  if line.startswith("WORKER_TRACE=")]
        self.assertEqual(len(traces), 1, completed.stderr)
        return completed, json.loads(traces[0])

    def test_legacy_and_canonical_help_reach_real_parser(self):
        for target in ("app.worker", "reqpilotx_server.worker"):
            with self.subTest(target=target):
                result, trace = self.invoke(target, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Run the ReqPilotX durable worker.", result.stdout)
                self.assertIn("--once", result.stdout)
                self.assertEqual(trace["build"], 0)

    def test_legacy_and_canonical_invalid_arguments_preserve_failure(self):
        for target in ("app.worker", "reqpilotx_server.worker"):
            with self.subTest(target=target):
                result, trace = self.invoke(target, "--not-a-worker-option")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertEqual(trace["build"], 0)

    def test_legacy_and_canonical_once_run_and_close_exactly_once(self):
        for target in ("app.worker", "reqpilotx_server.worker"):
            with self.subTest(target=target):
                result, trace = self.invoke(target, "--once", "--lease-seconds", "17")
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in ("build", "start", "once", "close", "container_close"):
                    self.assertEqual(trace[name], 1, (name, trace))
                self.assertEqual(trace["forever"], 0)
                self.assertEqual(trace["lease_seconds"], 17)

    def test_legacy_propagates_main_return_code_once(self):
        result, trace = self.invoke("app.worker", mode="return-code")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(trace["forwarded_main"], 1)

    def test_import_only_keeps_same_module_without_running(self):
        result, trace = self.invoke("app.worker", mode="import-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(trace["same_module"])
        self.assertEqual(trace["build"], 0)
        self.assertEqual(trace["once"], 0)
        self.assertEqual(trace["forwarded_main"], 0)


if __name__ == "__main__":
    unittest.main()
