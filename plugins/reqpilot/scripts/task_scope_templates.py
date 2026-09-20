"""Task-bound blank authoring templates; no automatic semantic grouping."""


def scope_template(kind, request):
    text=kind=='TEXT'
    common={'template_only':True,'submit_unchanged':False,'action':'items',
        'scope_id':request.scope_id if text else request.logical_table_scope_id,
        'source_selection':{'content_refs':[{'content_ref':i,'is_title':c.is_title,'characters':len(c.text)}
            for i,c in enumerate(request.source_content)]},
        'instructions':['Set retain_map_structure=true to retain declared Map titles/front matter; empty body arrays do not cover ordinary content.',
            'Keep stable item_key for text and record_key for tables. Use scope-local numeric refs or unique exact quotes; no copied hashes are needed.',
            'Use task_describe(action=items, task_id=current task) for the same current field contract.']}
    judgment={'label':'<source-faithful label>','source_form':'<choose from the actual source>',
        'requirementness':'<REQUIREMENT or NON_REQUIREMENT>','requirement_kinds':[],
        'confidence':'<number 0..1>','decision_summary':'<source-based boundary/classification judgment>',
        'reason_codes':['<concise reason code>']}
    if text:
        common.update(decisions={'retain_map_structure':True,'text_decisions':[{'scope_id':request.scope_id,'items':[]}]},
            prototypes={'item':{'item_key':'<stable local key>',**judgment,'text_spans':[]},
                'text_selection':{'content_ref':'<index in this scope>','quote':'<optional exact unique substring; omit to select full content>'},
                'structure_only':{'text_span':{'content_ref':'<index>'},'reason_codes':['<reason for retaining structure only>']}},
            semantic_reminder='Original numbered body units normally separate. Retain governing body text as source items that applicable clauses can reference; structure_only spans are for headings and declared front matter.')
        if request.structural_role:
            common['prototypes'].pop('item')
            common.update(structural_role=request.structural_role,allowed_disposition='STRUCTURE_ONLY',
                semantic_reminder='This Map scope is already declared whole front matter: retain all its content as structure, with zero SRC. If it actually contains an independent obligation, correct that Map boundary first; do not put it in this structural scope.')
    else:
        common['source_selection']['fragments']=[{'fragment_ref':i,'rows':[0,f.row_count],'columns':f.column_count,
            'coordinates':'0-based half-open local rows, not original source row_range'} for i,f in enumerate(request.fragments)]
        common.update(logical_table_role=request.logical_table_role,logical_table_title=request.logical_table_title,
            decisions={'retain_map_structure':True,'table_decisions':[{'logical_table_scope_id':request.logical_table_scope_id,'records':[],'context_row_groups':[]}]},
            prototypes={'record':{'record_key':'<stable local key>',**judgment,'row_groups':[]},
                'row_group':{'fragment_ref':'<index in this table scope>','row_start':'<inclusive local row>','row_end':'<exclusive local row>'},
                'context_row_group':{'role':'<HEADER or another supported context role>','row_group':{},'reason_codes':['<source relation>']}},
            semantic_reminder='A complete assembly BOM normally stays one source item with its component rows and quantities. Distinct independent configurations may separate. A test row retains method, shared values and all criteria, including cross-page continuation. Row numbers alone do not decide grouping.')
    return common
