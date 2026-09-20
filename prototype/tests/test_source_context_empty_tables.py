"""Whole-document reads retain empty original grids for program export."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_item_agent_jobs as jobs


class SourceContextEmptyTableTests(unittest.TestCase):
    def test_full_document_includes_original_table_with_no_source_item(self):
        with TemporaryDirectory() as raw:
            source = Path(raw) / 'source.pdf'
            source.write_bytes(b'%PDF-1.4 synthetic source-context IO fixture')
            records = [SimpleNamespace(source_record_id='a', logical_table_scope_id='populated'),
                SimpleNamespace(source_record_id='b', logical_table_scope_id='')]
            bundle = {'baseline': SimpleNamespace(records=records), 'document_map': SimpleNamespace(
                logical_tables=[SimpleNamespace(scope_id='populated'), SimpleNamespace(scope_id='empty')])}
            current = {'document': {}, 'source_sha256': 'a' * 64, 'bundle': {}, 'handoff_attempts': []}
            grids = {'populated': {'original_cells': ['item', 'value']},
                'empty': {'original_cells': ['Material', 'Grade', '/', '/']}}
            with patch.object(jobs, '_current_reference_context', return_value=current), \
                    patch.object(jobs, '_load_effective_reference', return_value=bundle), \
                    patch.object(jobs, 'resolve_source_record_path', return_value=source), \
                    patch.object(jobs, '_verified_projection_operations', return_value=[]), \
                    patch.object(jobs, 'materialize_native_structure_table_preview',
                        side_effect=lambda **args: grids[args['scope_id']]):
                self.assertEqual(jobs._source_context('project', 'document', ['a', 'b'])[3], grids)
                self.assertEqual(jobs._source_context('project', 'document', ['a'])[3],
                    {'populated': grids['populated']})
                with self.assertRaises(ValueError):
                    jobs._source_context('project', 'document', ['unknown'])


if __name__ == '__main__':
    unittest.main()
