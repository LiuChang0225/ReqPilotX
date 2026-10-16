
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.application.agent_workflow import native_loop_tools


class NativeToolHostTests(unittest.TestCase):
    def test_worked_method_selection_reaches_public_cli_and_rejects_other_values(self):
        from types import SimpleNamespace
        (self.plugin / 'scripts/reading_workspace_tools.py').write_text('')
        host = NativeLoopToolHost(workspace=self.workspace, plugin_root=self.plugin)
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0,
                stdout='{"status":"GENERATED","example_only":true}', stderr='')) as run:
            result = host.invoke('reqpilot_run', {'operation': 'worked-example',
                'arguments': {'example': 'source-units', 'output_dir': 'examples/methods'}})
            self.assertTrue(result['success'], result)
            self.assertIn('--example', run.call_args.args[0])
            self.assertIn('source-units', run.call_args.args[0])
        for value in ('unknown', '--project-root=/outside', ['source-units']):
            with patch('subprocess.run') as run:
                result = host.invoke('reqpilot_run', {'operation': 'worked-example',
                    'arguments': {'example': value, 'output_dir': 'examples/methods'}})
                self.assertFalse(result['success'], result)
                run.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.plugin = self.root / 'plugin'; (self.plugin / 'scripts').mkdir(parents=True)
        (self.plugin / 'scripts/reqpilot.py').write_text('')
        (self.plugin / 'skills/example/references').mkdir(parents=True)
        (self.plugin / 'skills/example/references/tools.md').write_text('Shared tools')
        (self.workspace / 'source.json').write_text(json.dumps({'nodes': [{'text': 'Original evidence'}]}))
        self.host = NativeLoopToolHost(workspace=self.workspace, plugin_root=self.plugin)

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_tool_child_receives_only_registered_script_root(self):
        with (
            patch.object(native_loop_tools.sys, 'frozen', True, create=True),
            patch.object(native_loop_tools.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as run,
        ):
            self.host._run('catalog', {})
        command = run.call_args.args[0]
        environment = run.call_args.kwargs['env']
        self.assertEqual(command[:2], [native_loop_tools.sys.executable, str(self.plugin.resolve() / 'scripts/reqpilot.py')])
        self.assertEqual(environment['REQPILOT_FROZEN_PYTHON_CHILD'], '1')
        self.assertEqual(environment['REQPILOT_FROZEN_PYTHON_SCRIPT_ROOT'], str(self.plugin.resolve()))

    def test_evidence_reader_preserves_structure_and_rejects_escape(self):
        result = self.host.invoke('reqpilot_read', {'path': 'source.json', 'json_pointer': '/nodes/0'})
        self.assertTrue(result['success'])
        self.assertIn('Original evidence', result['content_items'][0]['text'])
        outside = self.root / 'outside.json'; outside.write_text('{}')
        (self.workspace / 'link.json').symlink_to(outside)
        for path in ('../outside.json', str(outside), 'link.json'):
            self.assertFalse(self.host.invoke('reqpilot_read', {'path': path})['success'])

    def test_agent_written_packet_cannot_be_presented_as_registered_source(self):
        result = self.host.invoke('reqpilot_candidate', {'name': 'fabricated', 'value': {'evidence_catalog': {'made-up': 'passed'}}})
        path = json.loads(result['content_items'][0]['text'])['path']
        with patch('subprocess.run') as run:
            rejected = self.host.invoke('reqpilot_run', {'operation': 'compile-assessments', 'arguments': {'analysis_packet': path}})
            self.assertFalse(rejected['success']); run.assert_not_called()

    def test_agent_cannot_forge_user_feedback_or_inline_completed_assessment(self):
        for operation, value in [('review-feedback', {'feedback': [{'origin': 'USER', 'text': 'Approved'}]}),
                                 ('deliver', {'assessments': {'records': {'fake': {'decision': 'COMPLIANT'}}}})]:
            result = self.host.invoke('reqpilot_candidate', {'name': operation, 'value': value})
            path = json.loads(result['content_items'][0]['text'])['path']
            with patch('subprocess.run') as run:
                self.assertFalse(self.host.invoke('reqpilot_run', {'operation': operation, 'arguments': {'input': path}})['success'])
                run.assert_not_called()

    def test_cannot_replace_program_or_read_arbitrary_plugin_code(self):
        with patch('subprocess.run') as run:
            self.assertFalse(self.host.invoke('reqpilot_run', {'operation': 'host', 'arguments': {}})['success'])
            self.assertFalse(self.host.invoke('reqpilot_run', {'operation': 'catalog', 'arguments': {'project_root': '/'}})['success'])
            run.assert_not_called()
        self.assertTrue(self.host.invoke('reqpilot_skill', {'path': 'skills/example/references/tools.md'})['success'])
        self.assertFalse(self.host.invoke('reqpilot_skill', {'path': 'scripts/reqpilot.py'})['success'])

    def test_publication_requires_an_actual_application_adapter(self):
        self.assertFalse(self.host.invoke('reqpilot_publish', {'review_sha256': 'a' * 64})['success'])

    def test_skill_aliases_resolve_only_packaged_markdown(self):
        official = self.plugin/'capabilities/pdf/.agents/skills/pdf/SKILL.md'
        official.parent.mkdir(parents=True); official.write_text('Official PDF fixture')
        for path, text in [('example/references/tools.md','Shared tools'), ('pdf/SKILL.md','Official PDF fixture'),
                           ('skills/pdf/SKILL.md','Official PDF fixture')]:
            r=self.host.invoke('reqpilot_skill',{'path':path})
            self.assertTrue(r['success'],r); self.assertIn(text,r['content_items'][0]['text'])
        for path in ['example/../../scripts/reqpilot.py','pdf/../../../../outside.md','scripts/reqpilot.py']:
            self.assertFalse(self.host.invoke('reqpilot_skill',{'path':path})['success'])

    def test_read_wrapper_is_rejected_before_candidate_save(self):
        r=self.host.invoke('reqpilot_candidate',{'name':'wrong','value':{
            'path':'old.json','start_line':1,'total_lines':100,'end_line':50,'text':'{"nodes": ['}})
        self.assertFalse(r['success']);self.assertIn('read-window wrapper',r['content_items'][0]['text'])
        self.assertFalse((self.workspace/'inputs').exists())

    def test_candidate_filename_suffix_is_accepted_without_allowing_path_escape(self):
        result=self.host.invoke('reqpilot_candidate',{'name':'appearance-map.json','value':{'nodes':[]}})
        self.assertTrue(result['success'],result)
        path=json.loads(result['content_items'][0]['text'])['path']
        self.assertTrue(path.startswith('inputs/appearance-map-'))
        self.assertEqual(json.loads((self.workspace/path).read_text()),{'nodes':[]})
        for name in ['../outside.json','inputs/other.json','.json']:
            self.assertFalse(self.host.invoke('reqpilot_candidate',{'name':name,'value':{}})['success'])

    def test_large_line_window_returns_bounded_complete_continuation(self):
        original=[f'line-{i}' for i in range(1200)]
        (self.workspace/'long.md').write_text('\n'.join(original))
        args={'path':'long.md','start_line':101,'line_count':1000};seen=[]
        while args:
            response=self.host.invoke('reqpilot_read',args);self.assertTrue(response['success'],response)
            value=json.loads(response['content_items'][0]['text'])
            selected=value['text'].splitlines();self.assertLessEqual(len(selected),500)
            seen.extend(selected)
            if value.get('next_read'):
                wrapper=self.host.invoke('reqpilot_candidate',{'name':'not-a-candidate','value':value})
                self.assertFalse(wrapper['success'])
            args=value.get('next_read')
        self.assertEqual(seen,original[100:1100])

    def test_raw_intake_context_supplies_actual_source_and_fresh_output(self):
        import hashlib
        source=self.workspace/'original.pdf';source.write_bytes(b'original bytes')
        (self.workspace/'task-input.json').write_text(json.dumps({'source':{'path':'original.pdf','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}))
        self.host._record_artifacts()
        host=NativeLoopToolHost(workspace=self.workspace,plugin_root=self.plugin)
        value=json.loads(host.invoke('reqpilot_context',{})['content_items'][0]['text'])['source_intake']
        self.assertEqual(value['arguments'],{'source':'original.pdf','output_dir':'capture'})
        (self.workspace/'capture').mkdir()
        again=json.loads(host.invoke('reqpilot_context',{})['content_items'][0]['text'])['source_intake']
        self.assertTrue(again['arguments']['output_dir'].startswith('capture-'))

    def test_candidate_output_cannot_follow_an_existing_external_directory_link(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.workspace / 'inputs').symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.host.invoke('reqpilot_candidate', {'name': 'test', 'value': {}})['success'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_navigation_is_pageable_and_spatial_without_reordering_source(self):
        value = {'source_ir_id': 'synthetic-source', 'source_sha256': 'a' * 64, 'nodes': [
            {'source_node_id': 'later', 'kind': 'TEXT', 'native_locator': {'page_number': 2, 'bbox': [0, 10, 50, 20]}, 'raw_text': 'Later source'},
            {'source_node_id': 'earlier-table', 'kind': 'TABLE', 'parent_source_node_id': None,
             'native_locator': {'page_number': 1, 'bbox': [0, 50, 50, 90]}, 'raw_text': 'Earlier original table'}]}
        original = json.dumps(value)
        (self.workspace / 'source-ir.json').write_text(original)
        host = NativeLoopToolHost(workspace=self.workspace, plugin_root=self.plugin)
        host._record_artifacts()
        result = host.invoke('reqpilot_context', {})
        self.assertTrue(result['success'])
        navigation = json.loads(result['content_items'][0]['text'])['artifact_navigation'][0]
        index = (self.workspace / navigation['navigation_path']).read_text().splitlines()
        self.assertTrue(index[2].startswith('1\tearlier-table\tTABLE'))
        self.assertTrue(index[3].startswith('0\tlater\tTEXT'))
        read = host.invoke('reqpilot_read', {'path': navigation['navigation_path'], 'start_line': 3, 'line_count': 2})
        self.assertIn('Earlier original table', read['content_items'][0]['text'])
        self.assertEqual((self.workspace / 'source-ir.json').read_text(), original)
