"""Short selections over existing item evidence; no number inference or rewriting."""
from copy import deepcopy


def number_sources(document_map, baseline, record, tables):
    from task_change_reading import _facts
    facts, _ = _facts(record, document_map, baseline, tables)
    node = next(n for n in document_map.nodes if n.node_id == record.node_id)
    headings = [c for c in node.source_content if c.is_title]
    return facts, headings


def resolve_number_reference(document_map, baseline, record, tables, value, reference):
    kind = reference.get('kind')
    if kind not in {'ITEM_SOURCE', 'ITEM_HEADING'}:
        return reference
    facts, headings = number_sources(document_map, baseline, record, tables)
    if kind == 'ITEM_SOURCE':
        index = reference['segment_index']
        if not 0 <= index < len(facts):
            raise ValueError('NUMBER_REFERENCE_OUT_OF_RANGE: read this item source segments')
        fact = facts[index]; text = fact['text']; ref = deepcopy(fact['source_ref'])
        if ref['kind'] == 'TABLE_CELL':
            ref['fragment_id'] = ref.pop('table_id')
        start = ref['start_offset']
        ref = {k:v for k,v in ref.items() if k in {'kind','content_id','fragment_id','cell_id'}}
    else:
        index = reference['heading_ref']
        if not 0 <= index < len(headings):
            raise ValueError('NUMBER_REFERENCE_OUT_OF_RANGE: read this item numbering headings')
        heading = headings[index]; text = heading.text; start = 0
        ref = {'kind':'MAP_HEADING','content_id':heading.content_id}
    leading = len(text) - len(text.lstrip())
    if not value or not text[leading:].startswith(value):
        raise ValueError('NUMBER_NOT_LEADING: compact references require the exact leading number; internal occurrences use the existing absolute reference and explicit extent')
    ref.update(start_offset=start+leading, end_offset=start+leading+len(value))
    # Caller runs the shared compiler next: ownership, complete identifier,
    # heading eligibility, extents and exact source evidence are unchanged.
    return ref


def numbering_sources(document_map, baseline, record, tables):
    facts, headings = number_sources(document_map, baseline, record, tables)
    return {'source_segments':[{'segment_index':i,'text':f['text'],
                'source_ref':f['source_ref'],'page':f.get('page'),
                'source_number_ref':{'kind':'ITEM_SOURCE','segment_index':i}}
            for i,f in enumerate(facts)],
        'heading_sources':[{'heading_ref':i,'text':h.text,'page':h.page,
                'source_number_ref':{'kind':'ITEM_HEADING','heading_ref':i}}
            for i,h in enumerate(headings)],
        'selection_rule':'These are source locations, not detected numbers. Select an actual original identifier and its returned reference. Do not promote a parameter, quantity, stage name or inherited heading to a row number. For several original numbers use source_numbers; do not split a complete BOM just to number it. Internal text numbers retain the exact reference/source_extent contract.'}
