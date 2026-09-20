"""Reading projections over a SourceItem's unchanged native evidence."""
from __future__ import annotations

from pathlib import Path

from app.adapters.documents.pdf_text_reading_projection import project_pdf_source_contents


def source_item_reading_projections(document_map, record, source_path: str | Path) -> dict:
    """Project complete source blocks only; partial bindings keep their raw text."""
    return baseline_reading_projections(document_map, [record], source_path).get(record.source_record_id, {})


def baseline_reading_projections(document_map, records, source_path: str | Path) -> dict:
    """Read relevant PDF pages once when projecting an entire baseline."""
    contents = {c.content_id: c for node in document_map.nodes for c in node.source_content}
    selected, record_content_ids = {}, {}
    for record in records:
        for binding in record.text_bindings:
            content = contents.get(binding.content_id)
            if (content is not None and binding.start_offset == 0 and binding.end_offset == len(content.text)
                    and binding.text == content.text and binding.text_sha256 == content.text_sha256):
                selected[content.content_id] = content.model_dump(mode="json")
                record_content_ids.setdefault(record.source_record_id, []).append(content.content_id)
    if not selected or Path(source_path).suffix.lower() != ".pdf":
        return {}
    projections = project_pdf_source_contents(source_path, expected_source_sha256=document_map.source_sha256, contents=selected.values())
    return {record_id: {key: projections[key] for key in keys if key in projections}
            for record_id, keys in record_content_ids.items() if any(key in projections for key in keys)}
