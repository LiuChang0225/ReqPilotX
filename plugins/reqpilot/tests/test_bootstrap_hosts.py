"""Host discovery fallback is independent from a Codex model runtime."""
from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reqpilot
from cold_run import execution_command, workbench_network_configuration


class BootstrapHostTests(unittest.TestCase):
    def test_local_workbench_network_is_explicit_and_does_not_enable_other_hosts(self):
        self.assertEqual(workbench_network_configuration(None), [])
        flags=workbench_network_configuration('http://127.0.0.1:18080/workspace?projectId=p&documentId=d')
        self.assertIn('sandbox_workspace_write.network_access=true',flags)
        self.assertIn('features.network_proxy.enabled=true',flags)
        self.assertIn('features.network_proxy.domains={"127.0.0.1"="allow"}',flags)
        for url in ('https://example.com/workspace?projectId=p&documentId=d','http://127.0.0.1:18080/workspace','http://user:pass@localhost/workspace?projectId=p&documentId=d'):
            with self.assertRaises(ValueError):workbench_network_configuration(url)

    def test_cold_command_pins_model_effort_and_does_not_enable_delegation_by_default(self):
        command=execution_command('codex',Path('/test/workspace'),Path('/test/output'),[],model='gpt-5.6-luna',effort='max')
        self.assertEqual(command[command.index('--model')+1],'gpt-5.6-luna')
        self.assertIn('model_reasoning_effort="max"',command)
        self.assertNotIn('--enable',command)
        with self.assertRaisesRegex(ValueError,'explicit model'):
            execution_command('codex',Path('/w'),Path('/o'),[],model=None,effort='high')

    def test_bootstrap_copies_skills_when_symlinks_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plugin = root / "plugin"
            (plugin / "runtime/prototype").mkdir(parents=True)
            (plugin / "skills/reqpilot/references").mkdir(parents=True)
            (plugin / "AGENTS.template.md").write_text("Read .reqpilot/skills/reqpilot/SKILL.md")
            (plugin / "build-receipt.json").write_text("{}")
            (plugin / "skills/reqpilot/SKILL.md").write_text("Use .reqpilot/scripts/reqpilot.py from the workspace root.")
            (plugin / "skills/reqpilot/references/contract.md").write_text("Source remains original.")
            workspace = root / "workspace"
            with patch.object(reqpilot, "PLUGIN_ROOT", plugin), patch.object(Path, "symlink_to", side_effect=OSError("Host does not permit symlinks")):
                result = reqpilot.bootstrap(Namespace(workspace=workspace, source=[], project_root=None))
            self.assertEqual(result["status"], "PREPARED")
            self.assertFalse((workspace / ".agents/skills/reqpilot").is_symlink())
            self.assertEqual((workspace / ".agents/skills/reqpilot/references/contract.md").read_bytes(), (workspace / ".reqpilot/skills/reqpilot/references/contract.md").read_bytes())
            manifest = json.loads((workspace / "runtime-data/input-manifest.json").read_text())
            self.assertEqual(manifest["skill_discovery_modes"]["reqpilot"], "VERIFIED_COPY")


if __name__ == "__main__":
    unittest.main()
