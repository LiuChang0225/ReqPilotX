"""Logical table display labels are explicit metadata, never source titles."""

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
from unittest.mock import patch
import unittest

from .test_document_map_revision import fixture
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import compile_document_map_revision, rebind_baseline_for_document_map_revision
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
from app.application.agent_workflow.native_source_graph import native_source_graph_table_preview


class DocumentMapTableDisplayTests(unittest.TestCase):
    def setUp(self):
        self.ref,self.previous,self.baseline,self.validation,self.tables=fixture()
        self.original=(deepcopy(self.ref),self.previous.model_dump(mode='json'),self.baseline.model_dump(mode='json'),deepcopy(self.tables))

    def renamed(self):
        ref=deepcopy(self.ref);ref['logical_tables'][0]['agent_display_label']='合成模块试验项目'
        candidate=core.compile_document_map_artifact(ref)
        mapped=compile_document_map_revision(self.previous,candidate)
        current=core.DocumentMapArtifactV1.model_validate(mapped['document_map'])
        rebound=rebind_baseline_for_document_map_revision(self.previous,current,self.baseline,self.validation,self.tables)
        return mapped,current,rebound,core.SourceRequirementBaselineArtifactV1.model_validate(rebound['baseline'])

    def test_missing_label_stays_absent_in_map_request_native_reference_and_snapshot(self):
        table=self.previous.logical_tables[0]
        self.assertNotIn('agent_display_label',table.model_dump(mode='json'))
        self.assertNotIn('agent_display_label',self.previous.model_dump(mode='json')['logical_tables'][0])
        self.assertEqual(core.document_map_table_presentation(table),{})
        request=core.build_source_requirement_scope_request(self.previous,self.tables[table.scope_id])
        self.assertNotIn('logical_table_presentation',request.model_dump(mode='json'))
        snapshot=build_delivery_snapshot(self.previous,self.baseline,self.tables)
        self.assertNotIn('title_presentation',snapshot['logical_tables'][0]['projection'])
        self.assertEqual(snapshot['logical_tables'][0]['title'],table.title)

    def test_label_round_trips_native_map_and_keeps_originals_identity_and_context(self):
        mapped,current,rebound,baseline=self.renamed();table=current.logical_tables[0]
        self.assertEqual(table.title,self.previous.logical_tables[0].title)
        self.assertEqual(table.agent_display_label,'合成模块试验项目')
        self.assertEqual(core.compile_document_map_artifact(mapped['native_reference'],revision=current.revision),current)
        self.assertEqual(mapped['receipt']['table_changes'][0]['successor']['agent_display_label'],'合成模块试验项目')
        self.assertEqual(rebound['receipt']['context_changed_source_item_ids'],[])
        self.assertEqual(rebound['table_previews'],self.tables)
        for old,new in zip(self.baseline.records,baseline.records):
            self.assertEqual(old.model_dump(exclude={'document_map_artifact_id','document_map_artifact_sha256'}),
                new.model_dump(exclude={'document_map_artifact_id','document_map_artifact_sha256'}))
        request=core.build_source_requirement_scope_request(current,self.tables[table.scope_id])
        self.assertEqual(request.logical_table_title,'合成模块试验项目')
        self.assertEqual(request.logical_table_presentation,{'display_title':'合成模块试验项目','authority':'AGENT_DISPLAY_LABEL','map_title':table.title})
        snapshot=build_delivery_snapshot(current,baseline,rebound['table_previews'])
        logical=snapshot['logical_tables'][0]
        self.assertEqual(logical['title'],'合成模块试验项目')
        self.assertEqual(logical['projection']['title_presentation'],request.logical_table_presentation)
        with patch('app.application.agent_workflow.native_source_graph.validate_native_source_graph',return_value={'document_map':current,'table_previews':self.tables}):
            preview=native_source_graph_table_preview({},table.scope_id)
        self.assertEqual(preview['title'],table.title)
        self.assertEqual(preview['presentation'],request.logical_table_presentation)
        self.assertEqual(self.original,(self.ref,self.previous.model_dump(mode='json'),self.baseline.model_dump(mode='json'),self.tables))

    def test_blank_display_label_or_changed_source_title_is_not_silently_accepted(self):
        ref=deepcopy(self.ref);ref['logical_tables'][0]['agent_display_label']='  '
        with self.assertRaises(ValueError):core.compile_document_map_artifact(ref)
        ref['logical_tables'][0].update(agent_display_label='Display only',title='Invented customer caption')
        with self.assertRaisesRegex(ValueError,'TABLE_TITLE_NOT_SOURCE_GROUNDED'):
            compile_document_map_revision(self.previous,core.compile_document_map_artifact(ref))


if __name__=='__main__':unittest.main()
