"""Expose the existing original-PDF region renderer as a typed task image read."""
import hashlib
import os
from pathlib import Path
import struct
import tempfile


def source_region_image(state, request):
    from app.adapters.documents.pdf_source_crop_renderer import (
        inspect_pdf_page_geometry, render_pdf_source_crop, PdfSourceCropRejected,
    )
    root=Path(state['task_root']).resolve(strict=True)
    source=root/state['context']['source']['path']
    sha=state['context']['source']['sha256']
    capture_sha=hashlib.sha256(Path(state['selected']['READING']['path']).read_bytes()).hexdigest()
    try:
        geometry=inspect_pdf_page_geometry(source,expected_source_sha256=sha)
        if type(request.page) is not int or not 1<=request.page<=len(geometry):
            raise ValueError('INVALID_PAGE: page is outside this original')
        page=geometry[request.page-1]
        bbox=request.bbox if request.bbox is not None else page.bounds
        scale=request.scale if request.scale is not None else 2.0
        raw=render_pdf_source_crop(source,page_number=request.page,bbox=tuple(bbox),
            expected_source_sha256=sha,scale=scale,annotations=False)
    except PdfSourceCropRejected as exc:
        raise ValueError(f'{exc.code.value}: {exc}') from exc
    digest=hashlib.sha256(raw).hexdigest()
    # Read projections are cached outside committed compiler bundles: neither
    # source/Map/Items selection nor task revision changes when inspecting pixels.
    directory=(root/'reading'/'region-previews').resolve()
    if not directory.is_relative_to(root):raise ValueError('Image cache leaves the task root')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(digest+'.png')
    with tempfile.NamedTemporaryFile(dir=directory,delete=False) as stream:
        temporary=Path(stream.name);stream.write(raw)
    try:
        try:os.link(temporary,path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes()!=raw:raise ValueError('Saved source region image changed')
    finally:temporary.unlink()
    width,height=struct.unpack('>II',raw[16:24])
    return {'schema_version':'reqpilot-task-reading-image/v1','path':str(path),'mime_type':'image/png',
        'sha256':digest,'source_sha256':sha,'capture_sha256':capture_sha,'page':request.page,
        'bbox':list(bbox),'page_bounds':list(page.bounds),'rotation_cw_degrees':page.rotation_cw_degrees,
        'scale':scale,'width':width,'height':height,'annotations_rendered':False,
        'origin':'ORIGINAL_PDF_REGION','semantic_authority':'NONE'}
