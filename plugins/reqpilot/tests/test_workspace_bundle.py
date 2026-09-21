"""The shared task brief must not pull a model host into the portable bundle."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'plugins/reqpilot/scripts'))
import build_bundle


class WorkspaceBundleTests(unittest.TestCase):
    def test_workspace_closure_keeps_native_agent_hosts_out(self):
        source_root = ROOT/'packages/engine/src'
        scripts = [build_bundle.module_file(source_root, name) for name in build_bundle.SCRIPT_ROOTS]
        files, external = build_bundle.python_closure(
            source_root,
            build_bundle.MODULE_ROOTS,
            [path for path in scripts if path is not None],
        )
        paths = {str(path.relative_to(source_root)) for path in files}
        self.assertIn('reqpilotx/application/agent_workflow/task_workspace.py', paths)
        self.assertIn('reqpilotx/application/agent_workflow/native_task_brief.py', paths)
        self.assertFalse([path for path in paths if path.startswith('reqpilotx_server/')])
        self.assertFalse({'openai', 'azure', 'haystack', 'qdrant_client', 'dashvector'} & external)


if __name__ == '__main__':
    unittest.main()
