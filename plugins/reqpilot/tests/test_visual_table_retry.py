"""Failed local table attempts must not block a later successful retry."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'packages/engine/src'), str(ROOT / 'prototype')]

from PIL import Image

from reqpilotx.adapters.documents import tesseract_reading
from reqpilotx.application.project_visual_source import project_visual_source
from reqpilotx.domain.extraction.source_ir import (
    SourceIRSnapshot,
    stable_document_id,
    stable_source_ir_id,
)
from reqpilotx.domain.source_visual_text import (
    VisualLayoutTable,
    VisualLayoutTableCell,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_snapshot_id,
)

REGION = {'bbox': [10, 10, 110, 110], 'header_row_count': 1}


def page_frame():
    stream = io.BytesIO()
    Image.new('RGB', (120, 120), 'white').save(stream, format='PNG')
    image = stream.getvalue()
    source = b'%PDF-1.4 synthetic visual table retry fixture'
    return VisualTextPageInput(
        source_bytes=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        page_number=1,
        page_bbox=(0.0, 0.0, 120.0, 120.0),
        page_image_bytes=image,
        page_image_sha256=hashlib.sha256(image).hexdigest(),
        page_image_width_px=120,
        page_image_height_px=120,
    )


def layout(text):
    return VisualLayoutTable(
        row_count=1,
        column_count=1,
        header_row_count=1,
        cells=(VisualLayoutTableCell(row=0, column=0, raw_text=text,
                                     bbox=(12.0, 12.0, 108.0, 108.0)),),
    )


def table_result(text):
    return ((10.0, 10.0, 110.0, 110.0), layout(text),
            {'cells': [{'row': 0, 'column': 0, 'raw_text': text}]})


def base_snapshot(frame):
    document_id = stable_document_id(frame.source_sha256)
    adapter_id, adapter_version = 'visual-table-retry-fixture', '1'
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id, source_sha256=frame.source_sha256,
            adapter_id=adapter_id, adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=frame.source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status='COMPLETE',
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class VisualTableRetryTests(unittest.TestCase):
    def test_total_table_failure_does_not_emit_an_empty_snapshot(self):
        frame = page_frame()
        with mock.patch.object(tesseract_reading, 'read_ruled_table',
                               side_effect=ImportError('cv2 unavailable')):
            with self.assertRaisesRegex(ValueError, 'TABLE_PROFILE_NO_OBSERVATIONS'):
                tesseract_reading.read_page(frame, profile='tables', table_regions=[REGION])

    def test_exact_replay_is_equal_and_changed_outcome_is_distinct(self):
        frame = page_frame()
        with mock.patch.object(tesseract_reading, 'read_ruled_table',
                               return_value=table_result('Part')):
            first, _ = tesseract_reading.read_page(frame, profile='tables', table_regions=[REGION])
            replay, _ = tesseract_reading.read_page(frame, profile='tables', table_regions=[REGION])
        self.assertEqual(first, replay)
        with mock.patch.object(tesseract_reading, 'read_ruled_table',
                               return_value=table_result('Part reviewed')):
            changed, _ = tesseract_reading.read_page(frame, profile='tables', table_regions=[REGION])
        self.assertNotEqual(first.visual_text_snapshot_id, changed.visual_text_snapshot_id)
        updated = project_visual_source(project_visual_source(base_snapshot(frame), (first,)), (changed,))
        self.assertEqual(
            [snapshot.visual_text_snapshot_id for snapshot in updated.visual_text_snapshots],
            [first.visual_text_snapshot_id, changed.visual_text_snapshot_id],
        )

    def test_partial_then_complete_retry_appends_without_rewriting(self):
        frame = page_frame()
        second = {'bbox': [10, 10, 110, 110], 'header_row_count': 0}
        responses = [table_result('Part'), ImportError('region unresolved')]
        def read_table(*args, **kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        with mock.patch.object(tesseract_reading, 'read_ruled_table', side_effect=read_table):
            partial, audit = tesseract_reading.read_page(
                frame, profile='tables', table_regions=[REGION, second])
        self.assertEqual(len(partial.observations), 1)
        self.assertEqual(len(audit['diagnostics']), 1)
        with mock.patch.object(tesseract_reading, 'read_ruled_table',
                               return_value=table_result('Part')):
            complete, _ = tesseract_reading.read_page(
                frame, profile='tables', table_regions=[REGION, second])
        self.assertNotEqual(partial.visual_text_snapshot_id, complete.visual_text_snapshot_id)
        updated = project_visual_source(
            project_visual_source(base_snapshot(frame), (partial,)), (complete,))
        self.assertEqual(
            {snapshot.visual_text_snapshot_id for snapshot in updated.visual_text_snapshots},
            {partial.visual_text_snapshot_id, complete.visual_text_snapshot_id},
        )

    def test_legacy_empty_attempt_recovers_on_the_same_region(self):
        frame = page_frame()
        legacy_version = 'tess1-' + hashlib.sha256(json.dumps({
            'implementation_sha256': '0' * 64,
            'profile': 'tables',
            'table_regions': [REGION],
            'engine': tesseract_reading._engine_identity(None),
        }, sort_keys=True).encode()).hexdigest()[:32]
        common = dict(
            source_sha256=frame.source_sha256,
            page_image_sha256=frame.page_image_sha256,
            provider_id='local-tesseract',
            provider_version=legacy_version,
            page_number=frame.page_number,
        )
        shape = dict(
            page_bbox=frame.page_bbox,
            page_image_width_px=frame.page_image_width_px,
            page_image_height_px=frame.page_image_height_px,
        )
        legacy = VisualTextSnapshot(
            **common,
            **shape,
            evaluated_content_types=('TEXT',),
            visual_text_snapshot_id=stable_visual_text_snapshot_id(**common, **shape),
        )
        with mock.patch.object(tesseract_reading, 'read_ruled_table',
                               return_value=table_result('Part')):
            recovered, _ = tesseract_reading.read_page(frame, profile='tables', table_regions=[REGION])
        self.assertNotEqual(legacy.visual_text_snapshot_id, recovered.visual_text_snapshot_id)
        updated = project_visual_source(base_snapshot(frame), (legacy, recovered))
        self.assertEqual(
            {snapshot.visual_text_snapshot_id for snapshot in updated.visual_text_snapshots},
            {legacy.visual_text_snapshot_id, recovered.visual_text_snapshot_id},
        )


if __name__ == '__main__':
    unittest.main()
