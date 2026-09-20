"""Image annotations preserve the source-authoritative export projection."""

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
import unittest

from .test_document_map_revision import fixture
from app.application.agent_workflow.source_item_delivery import source_item_image_export_refs, merge_source_item_related_images
from app.application.agent_workflow.source_item_visual import _binding


class ImageExportBindingTests(unittest.TestCase):
    def setUp(self):
        _, self.document_map, baseline, _, _ = fixture()
        self.record = next(record for record in baseline.records if record.image_bindings)
        self.node = next(node for node in self.document_map.nodes if node.node_id == self.record.node_id)
        anchor = next(anchor for anchor in self.node.source_anchors if anchor.anchor_kind in {'IMAGE', 'FIGURE'})
        self.related = {**_binding(self.document_map, anchor).model_dump(mode='json'),
                        'caption': 'Agent image caption', 'relation': 'AGENT_CONFIRMED_IMAGE_CONTEXT'}
        self.rich = source_item_image_export_refs(self.record)

    def merge(self, related):
        return merge_source_item_related_images(self.document_map, self.node, self.rich, [related], self.record.source_record_id)

    def test_sparse_annotation_adds_caption_without_erasing_kind_or_location(self):
        original = deepcopy((self.rich, self.related))
        merged = self.merge(self.related)[0]
        self.assertEqual(merged['kind'], 'image')
        self.assertEqual(merged['location'], self.rich[0]['location'])
        self.assertEqual(merged['caption'], self.related['caption'])
        for field in ('source_sha256', 'anchor_id', 'source_handle', 'asset_id', 'page', 'bbox', 'source_bounds', 'precision'):
            self.assertEqual(merged[field], self.rich[0][field])
        self.assertEqual((self.rich, self.related), original)

    def test_source_or_geometry_conflict_is_rejected_before_projection(self):
        conflicts = {'source_sha256': 'f'*64, 'source_handle': 'other-source', 'asset_id': 'other-asset',
                     'page': 99, 'bbox': [0,0,1,1], 'source_bounds': [0,0,1,1], 'precision': 'OTHER',
                     'kind': 'text', 'location': {'format':'pdf','page_number':99,'bbox':[0,0,1,1]}}
        for field, value in conflicts.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'conflicts with original source'):
                self.merge({**self.related, field: value})
