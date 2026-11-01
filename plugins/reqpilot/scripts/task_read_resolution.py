"""Resolve mechanical source addresses without weakening explicit version pins."""
from __future__ import annotations

import json
from pathlib import Path
import re

from task_reading import _source, _next, ReadNavigationError


def source_handle(capture_sha, ref):
    return f'src1:{capture_sha}:{ref}'


def _known_captures(state):
    """Count actual registered capture identities, including page-workspace copies."""
    root=Path(state['task_root']); captures=set()
    for entry in state['index']['committed']:
        for artifact in entry.get('artifacts',[]):
            if artifact['kind']=='SOURCE_IR':
                captures.add(artifact['file_sha256'])
            elif artifact['kind']=='READING_WORKSPACE':
                path=(root/artifact['path']).resolve(strict=True)
                if not path.is_relative_to(root.resolve()):raise ValueError('INVALID_EVIDENCE: reading manifest leaves task')
                captures.add(json.loads(path.read_bytes())['source_ir_sha256'])
    return captures


def resolve_source_request(state, request):
    """Return a pinned request and an explicit binding receipt; no writes."""
    if request.view not in {'source','part','page','image'}:return request,None
    reading=state['selected'].get('READING')
    if not reading:raise ValueError('CAPTURE_REQUIRED: call source_prepare to capture the original first')
    snapshot,capture=_source(reading['path'],state['context']['source']['sha256'])
    if request.capture_sha256 is not None and request.capture_sha256!=capture:
        raise ValueError('CAPTURE_VERSION_CONFLICT: reload references from the selected capture')
    ref=request.ref; method='EXPLICIT_CAPTURE' if request.capture_sha256 else 'CURRENT_PAGE_OR_SOURCE'
    if isinstance(ref,str):
        match=re.fullmatch(r'src1:([a-f0-9]{64}):(0|[1-9][0-9]*)',ref)
        if not match:raise ValueError('INVALID_SOURCE_HANDLE: use a handle returned by source_read')
        if match[1]!=capture:raise ValueError('CAPTURE_VERSION_CONFLICT: source handle belongs to another capture')
        ref=int(match[2]);method='CAPTURE_BOUND_HANDLE'
    if ref is not None:
        if type(ref) is not int or not 0<=ref<len(snapshot.nodes):
            raise ValueError('INVALID_SOURCE_REF: ref is outside this capture')
        if request.capture_sha256 is None and method!='CAPTURE_BOUND_HANDLE':
            if _known_captures(state)!={capture}:
                raise ValueError('CAPTURE_VERSION_REQUIRED: multiple or unregistered captures; use the returned handle or capture_sha256')
            method='ONLY_REGISTERED_CAPTURE'
    if request.view=='part' and ref is None:
        raise ReadNavigationError('INVALID_SOURCE_REF','part requires a returned ref or handle; follow the source index read',
            _next('source_authoring',{'capture_sha256':capture}))
    page=request.page
    if request.view in {'page','image'}:
        candidates=[]
        if ref is not None:candidates.append(snapshot.nodes[ref])
        if request.image_anchor_id:
            # These are capture occurrence IDs. Post-Map anchors keep their
            # existing image_scope_image/item_image view instead of fuzzy lookup.
            matches=[node for node in snapshot.nodes if node.asset_ref and
                request.image_anchor_id in {node.source_node_id,node.asset_ref}]
            if len(matches)!=1:raise ValueError('IMAGE_LOCATION_REQUIRED: use an exact capture occurrence/ref or its returned image view')
            candidates.extend(matches)
        located={node.native_locator.page_number for node in candidates}
        if len(located)>1 or (page is not None and located and located!={page}):
            raise ValueError('SOURCE_LOCATION_CONFLICT: explicit page and source reference disagree')
        if page is None and located:page=next(iter(located))
        if page is None:raise ValueError('SOURCE_LOCATION_REQUIRED: page/image needs page or a located source ref')
        if type(page) is not int or page<1:raise ValueError('INVALID_PAGE: use a positive original page number')
    pinned=request.model_copy(update={'ref':ref,'page':page,'capture_sha256':capture})
    return pinned,{'capture_sha256':capture,'ref':ref,'page':page,'binding':method}


def attach_source_handles(value, capture_sha):
    if isinstance(value,list):return [attach_source_handles(v,capture_sha) for v in value]
    if not isinstance(value,dict):return value
    result={k:attach_source_handles(v,capture_sha) for k,v in value.items()}
    # Keep integer refs for Map authoring; the opaque handle is for continued reads.
    if type(value.get('ref')) is int and value.get('source_node_id'):
        result['handle']=source_handle(capture_sha,value['ref'])
    if result.get('tool')=='source_read' and result.get('request',{}).get('view')=='part':
        args=result['request']
        if type(args.get('ref')) is int:
            args['ref']=source_handle(args.get('capture_sha256',capture_sha),args['ref'])
            args.pop('capture_sha256',None)
    return result
