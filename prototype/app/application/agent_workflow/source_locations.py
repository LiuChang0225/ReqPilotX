"""Original-format locations; rendered pages are never substitute originals."""
from __future__ import annotations

from app.domain.extraction.source_ir import NativeLocator


def original_format(locator=None):
    if locator is None:
        return 'pdf'
    return NativeLocator.model_validate(locator).format.value


def native_location_fields(value):
    get = value.get if isinstance(value, dict) else lambda key, default=None: getattr(value, key, default)
    locator = get('native_locator')
    if locator is None:
        return {}
    return {'native_locator': NativeLocator.model_validate(locator).model_dump(mode='json', exclude_none=True), 'source_sequence': get('source_sequence')}


def validate_original_location(*, page, bbox, source_bounds, native_locator=None, source_sequence=None, subject='source', require_geometry=True):
    locator = NativeLocator.model_validate(native_locator) if native_locator is not None else None
    if original_format(locator) == 'pdf':
        if page is None or page < 1 or require_geometry and (bbox is None or source_bounds is None):
            raise ValueError(f'{subject} requires measured PDF page and geometry')
        if locator is not None and (locator.page_number != page or bbox is not None and locator.bbox is not None and tuple(locator.bbox) != tuple(bbox)):
            raise ValueError(f'{subject} PDF locator conflicts with original geometry')
    else:
        if original_format(locator) not in {'docx', 'xlsx'}:
            raise ValueError(f'{subject} original format is not supported for source items')
        if page is not None or bbox is not None or source_bounds is not None:
            raise ValueError(f'{subject} native Office location cannot claim PDF page or geometry')
        if type(source_sequence) is not int or source_sequence < 0:
            raise ValueError(f'{subject} native location requires its captured source sequence')
    return locator


def source_position(value, *, offset=0, row_offset=None):
    """Keep the established PDF key; Office uses captured order plus local offset."""
    locator = getattr(value, 'native_locator', None)
    if original_format(locator) == 'pdf':
        return (value.page, float(value.bbox[1]), float(value.bbox[0]), offset)
    sequence = getattr(value, 'source_sequence', None)
    if sequence is None:
        raise ValueError('Native source order is missing its captured sequence')
    locator = NativeLocator.model_validate(locator)
    local_row = getattr(value, 'row_start', 0) if row_offset is None else row_offset
    if locator.format.value == 'docx':
        return (locator.body_element_index, (locator.row_index or 0) + local_row, locator.column_index or 0,
                locator.paragraph_index or 0, locator.object_index if locator.object_index is not None else -1, offset)
    from openpyxl.utils.cell import range_boundaries
    column, row, _, _ = range_boundaries(locator.cell_range)
    return (sequence, row - 1 + local_row, column - 1, 0, locator.object_index if locator.object_index is not None else -1, offset)


def source_location_ref(value):
    locator = getattr(value, 'native_locator', None)
    if original_format(locator) == 'pdf':
        return {'format': 'pdf', 'page_number': value.page, 'bbox': list(value.bbox)}
    return NativeLocator.model_validate(locator).model_dump(mode='json', exclude_none=True)


def source_location_label(locator):
    value = NativeLocator.model_validate(locator)
    if value.format.value == 'pdf':
        return f'P{value.page_number}'
    if value.format.value == 'xlsx':
        return f'{value.sheet}!{value.cell_range}'
    if value.table_index is not None:
        label = f'表 {value.table_index + 1}'
        if value.row_index is not None and value.column_index is not None:
            label += f' · R{value.row_index + 1}C{value.column_index + 1}'
        if value.paragraph_index is not None:
            label += f' · 段落 {value.paragraph_index + 1}'
    else:
        label = f'段落 {value.paragraph_index + 1}' if value.paragraph_index is not None else f'正文对象 {value.body_element_index + 1}'
    if value.object_index is not None:
        label += f' · 对象 {value.object_index + 1}'
    return label


def validate_native_table_cells(fragment):
    """Local indices must describe the same native cells as their source table."""
    if original_format(fragment.native_locator) == 'pdf':
        return
    table = NativeLocator.model_validate(fragment.native_locator)
    for cell in fragment.cells:
        loc = NativeLocator.model_validate(cell.native_locator)
        if loc.format != table.format or cell.source_sequence != fragment.source_sequence:
            raise ValueError('Native table cell format/sequence differs from its source fragment')
        if table.format.value == 'docx':
            if (loc.body_element_index, loc.table_index, loc.row_index, loc.column_index) != (table.body_element_index, table.table_index, cell.row, cell.column):
                raise ValueError('DOCX cell locator differs from its native table/row/column')
        else:
            from openpyxl.utils.cell import range_boundaries
            left, top, right, bottom = range_boundaries(table.cell_range)
            col, row, end_col, end_row = range_boundaries(loc.cell_range)
            if (loc.workbook, loc.sheet) != (table.workbook, table.sheet) or (col, row, end_col, end_row) != (left + cell.column, top + cell.row, left + cell.column, top + cell.row):
                raise ValueError('XLSX cell locator differs from its native sheet/range/offset')
            if col + cell.column_span - 1 > right or row + cell.row_span - 1 > bottom:
                raise ValueError('XLSX merged cell escapes its actual source region')
