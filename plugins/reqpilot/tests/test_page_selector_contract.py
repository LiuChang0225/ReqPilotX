"""Observed L2 read failure: explain page/pages without silently changing meaning."""
# ruff: noqa: E402
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'plugins/reqpilot/scripts'), str(ROOT / 'prototype')]
from task_service import ReadRequest, AnnotationsRequest


class PageSelectorTests(unittest.TestCase):
    def test_invalid_batch_page_read_exposes_valid_routes(self):
        for view in ('page', 'image'):
            with self.subTest(view=view), self.assertRaisesRegex(ValueError, 'view=source with pages'):
                ReadRequest(task_id='task', view=view, pages=[1, 2])
            with self.subTest(view=view), self.assertRaisesRegex(ValueError, 'SOURCE_READ_SELECTOR_CONFLICT'):
                ReadRequest(task_id='task', view=view, page=1, pages=[2])

    def test_existing_source_filter_and_located_single_page_remain_valid(self):
        source = ReadRequest(task_id='task', view='source', pages=[1, 2])
        self.assertEqual(source.pages, [1, 2])
        for view in ('page', 'image'):
            self.assertEqual(ReadRequest(task_id='task', view=view, page=2).page, 2)
            self.assertEqual(ReadRequest(task_id='task', view=view, ref=3).ref, 3)

    def test_json_encoded_containers_preserve_the_typed_request(self):
        self.assertEqual(ReadRequest(task_id='task', pages='[1,2]').pages, [1, 2])
        request = {'task_id': 'task', 'expected_revision': 'revision',
                   'self_review_finding': {'text': 'Original definition applies.', 'source_item_ids': ['SRC-0002']},
                   'changes': {'annotation_updates': [{'source_item_id': 'SRC-0002', 'context_source_ids': ['SRC-0001'], 'reason': 'Explicit original scope.'}]}}
        expected = AnnotationsRequest.model_validate(request).model_dump()
        request['self_review_finding'] = json.dumps(request['self_review_finding'])
        self.assertEqual(AnnotationsRequest.model_validate(request).model_dump(), expected)

    def test_container_tolerance_never_coerces_members_or_ambiguous_json(self):
        for pages in ('[true]', '["1"]', '{"pages":[1]}', '[NaN]', '"[1]"'):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                ReadRequest(task_id='task', pages=pages)
        request = {'task_id': 'task', 'expected_revision': 'revision',
                   'changes': {'annotation_updates': [{'source_item_id': 'SRC-0002', 'context_source_ids': [], 'reason': 'Scope excludes this item.'}]}}
        for finding in ('{"text":"a","text":"b"}', '{"unknown":"x"}', '"{}"', '[]', '{"text":NaN}'):
            with self.subTest(finding=finding), self.assertRaises(ValueError):
                AnnotationsRequest.model_validate({**request, 'self_review_finding': finding})


if __name__ == '__main__':
    unittest.main()
