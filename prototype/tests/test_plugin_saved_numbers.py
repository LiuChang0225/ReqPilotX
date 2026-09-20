"""Published plugin views consume the same immutable numbering as their Matrix."""
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import hashlib
import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from app.application.errors import ApplicationError
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow import plugin_workbench as api
from app.application.agent_workflow import plugin_workbench_reader as reader
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.application.agent_workflow.source_item_decomposition import compile_source_item_decomposition
from .test_source_item_numbering import compiled, originals


def write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PluginSavedNumberTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.mapping,self.baseline = compiled(['1.1 Original scope definition.','1.2 Response-time definition.'])
        self.head = {'project_id':'p','document_id':'d','source_sha256':self.baseline.source_sha256,
            'versions':[],'feedback':[],'feedback_resolutions':{}}
        self.patch = patch.object(api,'_load',return_value=(self.root,self.head));self.patch.start();self.addCleanup(self.patch.stop)

    def save(self, revision, snapshot, decomposition=None):
        directory = self.root / 'versions' / revision; directory.mkdir(parents=True)
        # Build the receipt through the same shared validator as ordinary Map/Items.
        receipt = core._compile_validation_receipt_from_artifact(document_map=self.mapping,artifact=self.baseline)
        artifacts = {'document-map.json':self.mapping.model_dump(mode='json'), 'source-requirement-baseline.json':self.baseline.model_dump(mode='json'),
            'validation-receipt.json':receipt.model_dump(mode='json'),'table-previews.json':{},'matrix-snapshot.json':snapshot}
        if decomposition: artifacts['source-item-decomposition.json'] = decomposition
        files = {name:write(directory/name,value) for name,value in artifacts.items()}
        version = {'revision_id':revision,'source_sha256':self.baseline.source_sha256,'baseline_sha256':self.baseline.artifact_sha256,
            'snapshot_sha256':snapshot['delivery_sha256'],'files':files}
        write(directory/'version.json',version)
        self.head['versions'].append({'revision_id':revision,'number':len(self.head['versions'])+1})
        self.head['revision_id'] = revision
        return directory,version

    def read(self, revision):
        return reader.get_plugin_reader('p','d','plugin-saved',revision,SimpleNamespace(headers={}))

    def test_old_empty_numbers_and_new_proved_numbers_follow_the_selected_snapshot(self):
        with patch('app.application.agent_workflow.source_item_numbering.observed_source_item_number',return_value=None):
            old = build_delivery_snapshot(self.mapping,self.baseline,{})
        new = build_delivery_snapshot(self.mapping,self.baseline,{})
        old_dir,_ = self.save('a'*64,old); self.save('b'*64,new)
        before = {p.name:p.read_bytes() for p in old_dir.iterdir()}
        for revision,snapshot,expected in [('a'*64,old,['','']),('b'*64,new,['1.1','1.2'])]:
            with self.subTest(revision=revision):
                view = self.read(revision)
                self.assertEqual([r['source_number'] for r in view['outline']['records']],expected)
                self.assertEqual(api.get_plugin_snapshot('p','d','plugin-saved',SimpleNamespace(headers={}),revision),snapshot)
                workbook = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
                try:
                    values = [row[2] or '' for row in workbook['ComplianceMatrix'].values if row and str(row[0]).startswith('SRC-')]
                    self.assertEqual(values,expected)
                finally: workbook.close()
                with patch.object(api,'_source',return_value={}),patch.object(api,'resolve_source_record_path',return_value=None):
                    detail = reader.get_plugin_record('p','d','plugin-saved',self.baseline.records[0].source_record_id,revision,SimpleNamespace(headers={}))
                self.assertEqual(detail['record']['source_number'],expected[0])
                self.assertEqual(detail['record']['source_numbers'],view['outline']['records'][0]['source_numbers'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in old_dir.iterdir()})

    def test_derived_numbers_are_also_read_from_the_saved_version(self):
        text = '1.1 Open.\nClose.'
        self.mapping,self.baseline = compiled([text],requirement=True)
        sid = self.baseline.records[0].source_record_id;mid = text.index('\n')
        split = compile_source_item_decomposition(self.baseline,[{'source_item_id':sid,'reason':'Two independent actions.',
            'children':[{'text':text[4:mid],'source_ranges':[{'segment_index':0,'start_offset':4,'end_offset':mid}]},
                {'text':text[mid+1:],'source_ranges':[{'segment_index':0,'start_offset':mid+1,'end_offset':len(text)}]}],
            'context_ranges':[{'source_range':{'segment_index':0,'start_offset':0,'end_offset':4},'disposition':'CONTEXT'}]}])
        with patch('app.application.agent_workflow.source_item_numbering.observed_source_item_number',return_value=None):
            old = build_delivery_snapshot(self.mapping,self.baseline,{},decomposition=split)
        new = build_delivery_snapshot(self.mapping,self.baseline,{},decomposition=split)
        self.save('c'*64,old,split);self.save('d'*64,new,split)
        for revision,snapshot in [('c'*64,old),('d'*64,new)]:
            expected = {n['node_id']:n for n in snapshot['nodes'] if n.get('decomposition')}
            actual = self.read(revision)['reasoning']['decomposition']['records'][sid]
            for child in actual:
                node = expected[child['singular_requirement_id']]
                self.assertEqual(child['source_number'],node['source_number'])
                self.assertEqual(child['source_number_projection'],node['decomposition']['source_number_projection'])

    def test_missing_snapshot_does_not_borrow_a_newer_versions_numbers(self):
        snapshot = build_delivery_snapshot(self.mapping,self.baseline,{})
        directory,version = self.save('a'*64,snapshot);self.save('b'*64,snapshot)
        version['files'].pop('matrix-snapshot.json');write(directory/'version.json',version)
        with self.assertRaises(ApplicationError) as caught:self.read('a'*64)
        self.assertEqual(caught.exception.status_code,404)
        self.assertEqual(self.read('b'*64)['outline']['records'][0]['source_number'],'1.1')

    def test_same_map_different_baseline_or_broken_snapshot_seal_is_rejected(self):
        snapshot = build_delivery_snapshot(self.mapping,self.baseline,{})
        for index,kind in enumerate(('baseline','map','source','snapshot_seal','source_identity')):
            value = deepcopy(snapshot)
            if kind in {'baseline','map','source'}:
                key = {'baseline':'artifact_sha256','map':'document_map_artifact_sha256','source':'source_sha256'}[kind]
                value['source_item_baseline'][key] = 'f'*64
            elif kind == 'source_identity':
                originals(value)[0]['source_item']['source_item_id'] = 'source-record-'+'f'*32
            value['delivery_sha256'] = core.canonical_json_sha256({k:v for k,v in value.items() if k!='delivery_sha256'})
            if kind == 'snapshot_seal':value['delivery_sha256'] = 'e'*64
            revision = str(index+1)*64;self.save(revision,value)
            with self.subTest(kind=kind),self.assertRaises(ApplicationError) as caught:self.read(revision)
            self.assertEqual(caught.exception.status_code,409)

    def test_changed_snapshot_bytes_keep_existing_artifact_integrity_error(self):
        snapshot = build_delivery_snapshot(self.mapping,self.baseline,{})
        directory,_ = self.save('a'*64,snapshot)
        (directory/'matrix-snapshot.json').write_text('{}')
        with self.assertRaises(ApplicationError) as caught:self.read('a'*64)
        self.assertEqual(caught.exception.status_code,409)


if __name__=='__main__':unittest.main()
