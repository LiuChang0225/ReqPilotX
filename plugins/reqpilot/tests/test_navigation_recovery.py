"""Real rejected reads lead to pinned, callable navigation without task writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_task_service import ROOT, ENTRY, make_fixture, TaskService
from task_bridge import TaskBridge
from task_service import REQUESTS


class NavigationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);source,_,mapping=make_fixture(self.root/'fixture')
        self.service=TaskService(self.root,ENTRY,ROOT)
        self.bridge=TaskBridge(self.root,ENTRY,project_root=ROOT)
        opened=self.service.invoke('open',{'mode':'create','source':str(source),'goal':'Read the synthetic source.','creation_key':'navigation'})
        self.task=opened['task_id']
        state=self.service.invoke('prepare',{'task_id':self.task,'expected_revision':opened['revision']})
        decisions={k:v for k,v in mapping.items() if k!='source_sha256'};decisions['schema_version']='reqpilot-map-edit/v1'
        self.state=self.service.invoke('map',{'task_id':self.task,'expected_revision':state['revision'],'decisions':decisions})
        self.index=Path(self.state['task_root'])/'workspace-index.json';self.before=self.index.read_bytes()

    def call(self, tool_action, **arguments):
        return self.bridge.invoke(tool_action,{'task_id':self.task,**arguments})

    def follow(self, link):
        action={'source_read':'read','task_describe':'describe'}[link['tool']]
        REQUESTS[action].model_validate(link['request'])
        result=self.bridge.invoke(action,link['request'])
        self.assertIn(result['status'],{'READ','DESCRIBED'},result)
        self.assertEqual(self.index.read_bytes(),self.before)
        return result

    def test_scope_and_missing_source_recover_to_actual_table_cells(self):
        resumed=self.call('open',mode='resume')
        scopes=self.follow(resumed['scope_read'])
        table=next(s for s in scopes['scopes'] if s['scope_kind']=='TABLE')
        for part in ['table_reads.cells','cells','table_reads','table_cells','original_image','TABLE_CELLS','table:999','content:999']:
            with self.subTest(part=part):
                bad=self.call('read',view='scope',scope_id=table['scope_id'],part=part)
                self.assertEqual(bad['error_code'],'UNKNOWN_SCOPE_PART',bad)
                index=self.follow(bad['next_action'])
                fragment=next(e for e in index['entries'] if e['kind']=='TABLE_FRAGMENT')
                cells=self.follow(fragment['read'])
                self.assertEqual(cells['fragment_id'],fragment['fragment_id'])
                self.assertTrue(cells['cells'])
        for arguments in [{'view':'scope','part':'template'}, {'view':'scope','scope_id':'text:missing'}, {'view':'items'}, {'view':'part','part':'template'}]:
            bad=self.call('read',**arguments)
            self.assertEqual(bad['status'],'FAILED',bad)
            self.follow(bad['next_action'])
        stale=self.call('read',view='scope',scope_id=table['scope_id'],part='cells',expected_map_sha256='0'*64)
        self.assertEqual(stale['status'],'FAILED')
        self.assertNotIn('next_action',stale)
        self.assertIn('Map changed',stale['message'])
        self.assertEqual(self.index.read_bytes(),self.before)

    def test_schema_guess_recovers_to_real_definitions_without_changing_version(self):
        for part in ['#/$defs/ItemInput','#/$defs/TextSelection','#/$defs/TextSpanSelection','invented-part']:
            bad=self.call('describe',action='items',part=part,max_chars=24000)
            self.assertEqual(bad['error_code'],'UNKNOWN_SCHEMA_PART',bad)
            listing=self.follow(bad['next_action'])
            definitions=listing['data']['definitions']
            self.assertEqual({d['name'] for d in definitions},set(REQUESTS['items'].model_json_schema()['$defs']))
            self.assertNotIn('ItemInput',{d['name'] for d in definitions})
            selected=next(d for d in definitions if d['name']=='TextItemInput')
            shape=self.follow(selected['read'])
            self.assertTrue(shape['data']['fields'])
            stale={**bad['next_action']['request'],'expected_revision':'stale'}
            rejected=self.bridge.invoke('describe',stale)
            self.assertEqual(rejected['status'],'FAILED');self.assertNotIn('next_action',rejected)
        conflict=self.call('describe',action='items',part='#/$defs/ItemInput',expected_schema_sha256='0'*64)
        self.assertEqual(conflict['status'],'FAILED');self.assertNotIn('next_action',conflict)
        self.assertIn('DESCRIPTION_SCHEMA_CHANGED',conflict['message'])
        self.assertEqual(self.index.read_bytes(),self.before)
