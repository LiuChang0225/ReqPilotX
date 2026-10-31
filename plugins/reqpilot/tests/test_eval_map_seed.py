"""Operator Map inputs retain source integrity and permit real new item writes."""
from pathlib import Path
from argparse import Namespace
import json
from test_task_change_contracts import ChangeFixture, ROOT
import artifact_tools
from test_portable import item_decisions
from task_service import TaskService
from evals.agent_evals.contracts import file_sha
from evals.agent_evals.stage_fixtures import seed_map_bundle


class MapSeedTests(ChangeFixture):
    def test_seed_is_explicit_preparation_not_model_output_and_can_compile_items(self):
        # This test permits Map editing; the older v1 fixture intentionally does
        # not. Compile the same invented source decisions through the v2 path.
        decisions=json.loads((self.mapping/'map-decisions.json').read_text())
        decisions['schema_version']='reqpilot-map-decisions/v2'
        input_path=self.root/'seed-map-v2.json';input_path.write_text(json.dumps(decisions))
        self.mapping=self.root/'map-v2'
        artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',
            input=input_path,output_dir=self.mapping))
        service=TaskService(self.root,ROOT/'plugins/reqpilot/scripts/reqpilot.py',ROOT)
        state=service.invoke('open',{'mode':'create','source':str(self.source),'goal':'Compile from prepared Map.','creation_key':'map-stage'})
        state=service.invoke('prepare',{'task_id':state['task_id'],'expected_revision':state['revision']})
        recipe={'source_sha256':self.baseline.source_sha256,'map_bundle':str(self.mapping),
            'map_bundle_files':{str(p.relative_to(self.mapping)):file_sha(p) for p in self.mapping.rglob('*') if p.is_file()}}
        seeded=seed_map_bundle(Path(state['task_root']),recipe)
        self.assertFalse(seeded['model_produced']);self.assertFalse(seeded['items_provided'])
        state=service.invoke('inspect',{'task_id':state['task_id']})
        self.assertEqual(set(state['selected']),{'READING','MAP','SOURCE_FINDINGS'})
        selected_capture=Path(state['selected']['READING']['path'])
        self.assertEqual(selected_capture.read_bytes(),(self.mapping/'map-source-ir.json').read_bytes())
        self.assertEqual(seeded['reading_basis'],'EXACT_PREPARED_MAP_CAPTURE')
        # Reading pages must retain the same capture; a legitimate Map repair
        # must remain possible before the first Items exist.
        state=service.invoke('prepare',{'task_id':state['task_id'],'expected_revision':state['revision'],'mode':'pages'})
        mapping=json.loads((self.mapping/'document-map.json').read_text())
        node=next(n for n in mapping['nodes'] if n['node_id']=='s1')
        state=service.invoke('map',{'task_id':state['task_id'],'expected_revision':state['revision'],
            'decisions':{'nodes':[{'node_id':node['node_id'],'label':node['label']}]}})
        self.assertEqual(state['operation_state'],'COMMITTED',state)
        choices=item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('schema_version','source_sha256','document_map_artifact_sha256'):choices.pop(key,None)
        result=service.invoke('items',{'task_id':state['task_id'],'expected_revision':state['revision'],'decisions':choices})
        self.assertEqual(result['operation_state'],'COMMITTED',result)
        history=json.loads((Path(state['task_root'])/'workspace-index.json').read_text())['committed']
        self.assertEqual([h['operation'] for h in history],['capture','eval-map-seed','reading-workspace','compile-map','compile-items'])
        (self.mapping/'source-findings.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'Prepared Map bundle changed'):
            seed_map_bundle(Path(state['task_root']),recipe)
