from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow.native_loop_workspace import export_registered_knowledge
from app.application.knowledge_evidence_capture import capture_knowledge_facts
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from . import test_knowledge_facts as knowledge_fixture


class NativeKnowledgeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        knowledge_fixture.KnowledgeFactsTests.setUp(self)

    def catalog(self):
        captured = capture_knowledge_facts(self.record, self.path, project_id='project-a')
        value = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'project-a', 'knowledge_base_version': 1,
                 'documents': [captured['document']], 'evidence_catalog': captured['facts'], 'gaps': [], 'selection': 'ALL_PROJECT_KNOWLEDGE'}
        return {**value, 'catalog_sha256': canonical_json_sha256(value)}

    def test_application_registration_can_be_recaptured_from_copied_original(self):
        import json
        with tempfile.TemporaryDirectory() as directory, patch('app.db.list_knowledge_documents', return_value=[self.record]), \
             patch('app.application.source_runtime.resolve_source_record_path', return_value=self.path):
            root = Path(directory); catalog = self.catalog()
            export_registered_knowledge(catalog, root)
            registration = json.loads((root / 'evidence-registration.json').read_text())
            copied = registration['documents'][0]
            actual = capture_knowledge_facts(copied['record'], root / copied['source_path'], project_id='project-a')
            self.assertEqual(actual['facts'], catalog['evidence_catalog'])
            self.assertEqual(registration['declaration']['origin'], 'HOST_REGISTERED')
            self.assertEqual((root / copied['source_path']).read_bytes(), self.path.read_bytes())

    def test_changed_actual_document_cannot_be_frozen_as_old_registered_facts(self):
        catalog = self.catalog()
        self.path.write_text('A different test result')
        with tempfile.TemporaryDirectory() as directory, patch('app.db.list_knowledge_documents', return_value=[self.record]), \
             patch('app.application.source_runtime.resolve_source_record_path', return_value=self.path):
            with self.assertRaises(ValueError):
                export_registered_knowledge(catalog, Path(directory))
