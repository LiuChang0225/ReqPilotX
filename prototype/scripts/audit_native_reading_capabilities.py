"""Observe portable/native reading parity without running a model or customer task.

Reports available surfaces, not semantic quality. Temporary probes contain only
synthetic text. Missing capabilities are findings, not startup approval gates.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost


def audit(plugin: Path) -> dict:
    plugin = plugin.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='reqpilot-reading-audit-') as temporary:
        workspace = Path(temporary)
        for name, content in [('sample.md', '# Synthetic page\nOriginal text'),
                              ('sample.html', '<table><tr><td>Synthetic cell</td></tr></table>'),
                              ('sample.json', '{"cells":[{"text":"Synthetic cell"}]}')]:
            (workspace / name).write_text(content)
        host = NativeLoopToolHost(workspace=workspace, plugin_root=plugin,
                                  review_dir=workspace / 'review')
        specs = {spec.name: dict(spec.input_schema) for spec in host.specs()}

        def outcome(name, arguments):
            result = host.invoke(name, arguments)
            return {'success': result['success'],
                    'response': result['content_items'][0]['text'] if not result['success'] else None}

        reads = {name: outcome('reqpilot_read', {'path': name})
                 for name in ('sample.md', 'sample.html', 'sample.json')}
        script = 'scripts/worked_example.py'
        example = {'packaged_script': (plugin / script).is_file(),
                   'script_read_via_skill': outcome('reqpilot_skill', {'path': script}),
                   'native_example_operations': [op for op in sorted(host.operations) if 'example' in op]}
        annotation_help = subprocess.run([sys.executable, str(plugin / 'scripts/reqpilot.py'),
            'compile-annotations', '--help'], capture_output=True, text=True, timeout=20)
        manifest = plugin / '.codex-plugin/plugin.json'
        read_fields = list(specs['reqpilot_read']['properties'])
        return {
            'schema_version': 'reqpilotx-reading-capability-audit/v1',
            'observed_at': datetime.now(UTC).isoformat(),
            'plugin_path': str(plugin),
            'plugin_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'plugin_version': json.loads(manifest.read_text()).get('version'),
            'host_module_sha256': hashlib.sha256(Path(sys.modules[NativeLoopToolHost.__module__].__file__).read_bytes()).hexdigest(),
            'scope': 'Current repository native tool host with selected plugin and the source-task review directory shape. No App Server or model turn; only synthetic reader probes and CLI help. Does not prove what a running task loaded.',
            'packaged_skills': sorted(path.parent.name for path in (plugin / 'skills').glob('*/SKILL.md')),
            'native_operations': sorted(host.operations),
            'native_read_parameters': read_fields,
            'pdf_crop_parameter': 'bbox' in read_fields or 'clip' in read_fields,
            'pdf_scale_parameter': 'scale' in read_fields or 'dpi' in read_fields,
            'synthetic_read_probes': reads,
            'worked_example': example,
            'compile_annotations': {'cli_help_available': annotation_help.returncode == 0,
                                    'native_operation_exposed': 'compile-annotations' in host.operations},
            'semantic_verification': 'NOT_RUN', 'customer_task_mutated': False,
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.plugin)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Do not silently replace an earlier audit observation.
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
