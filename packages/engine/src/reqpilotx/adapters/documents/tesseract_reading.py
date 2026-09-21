"""Bounded local OCR and ruled-table reading over an exact source page image.

No automatic numeric substitution or semantic classification. Callers retain
raw OCR, source images and unresolved table diagnostics. OpenCV is needed only
when explicit table regions are requested.
"""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from reqpilotx.adapters.documents.visual_text_provider_support import (
    pixel_polygon_to_page_bbox, validate_visual_text_page_input,
)
from reqpilotx.domain.source_visual_text import (
    VisualLayoutTable, VisualLayoutTableCell, VisualTextObservation, VisualTextSnapshot,
    stable_visual_text_observation_id, stable_visual_text_snapshot_id,
)


def _engine_identity(tessdata_dir):
    binary=shutil.which('tesseract')
    identity={'binary_sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest() if binary else None,
              'version':None,'language_files':{}}
    if binary:
        version=subprocess.run([binary,'--version'],capture_output=True,text=True,timeout=15)
        identity['version']=(version.stdout or version.stderr).splitlines()[0]
    directory=Path(tessdata_dir) if tessdata_dir else None
    if directory is None and binary:
        output=subprocess.run([binary,'--list-langs'],capture_output=True,text=True,timeout=15)
        match=re.search(r'"([^\"]+)"',output.stdout.splitlines()[0] if output.stdout else '')
        if match:directory=Path(match.group(1))
    for name in ('eng','chi_sim'):
        path=directory/(name+'.traineddata') if directory else None
        identity['language_files'][name]=hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    return identity


def _join(words):
    text = ''
    cjk = re.compile(r'[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]')
    for word in words:
        if text and not (cjk.match(text[-1]) or cjk.match(word[0])):
            text += ' '
        text += word
    return text


def recognize_image(image, *, lang='eng+chi_sim', psm=3, tessdata_dir=None, timeout=90, numeric=False):
    """Call the system OCR binary; no Python OCR/model package is required."""
    binary = shutil.which('tesseract')
    if binary is None:
        raise ValueError('TESSERACT_UNAVAILABLE: local tesseract binary is required')
    if lang not in {'eng+chi_sim','chi_sim+eng','eng','chi_sim'} or psm not in {3,6,7,11,13}:
        raise ValueError('unsupported_tesseract_profile')
    with tempfile.TemporaryDirectory(prefix='reqpilot-ocr-') as temp:
        path = Path(temp)/'input.png'
        image.save(path)
        # Leptonica on macOS can fail to reopen files through /tmp's symlink.
        # MCP's minimal environment commonly selects /tmp rather than TMPDIR.
        path = path.resolve(strict=True)
        command = [binary,str(path),'stdout','-l',lang,'--oem','1','--psm',str(psm)]
        if tessdata_dir is not None:
            data_dir = Path(tessdata_dir).resolve(strict=True)
            if any(not (data_dir/(name+'.traineddata')).is_file() for name in lang.split('+')):
                raise ValueError('TESSERACT_LANGUAGE_DATA_MISSING')
            command += ['--tessdata-dir',str(data_dir)]
        # Custom traineddata directories may not contain Tesseract's configs/tsv.
        # Set the output flag explicitly; otherwise the binary can exit 0 while
        # returning plain text, which must never be mistaken for empty OCR.
        command += ['-c','tessedit_create_tsv=1']
        if numeric:
            command += ['-c','load_system_dawg=0','-c','load_freq_dawg=0']
        completed = subprocess.run(command,capture_output=True,timeout=timeout)
    if completed.returncode:
        # A failed image reader may print binary image bytes in its diagnostics.
        # Replacement decoding is only for errors, never recognized source text.
        raise ValueError('TESSERACT_FAILED: '+completed.stderr.decode('utf-8',errors='replace').strip()[-1000:])
    try:
        recognized=completed.stdout.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('TESSERACT_INVALID_TSV: recognized output is not UTF-8') from exc
    words = []
    rows = csv.DictReader(io.StringIO(recognized),delimiter='\t')
    if not {'text','conf','left','top','width','height','block_num','par_num','line_num','word_num'}.issubset(rows.fieldnames or ()):
        raise ValueError('TESSERACT_INVALID_TSV: successful process did not produce structured OCR')
    for row in rows:
        if not row.get('text','').strip() or float(row['conf']) < 0:
            continue
        words.append({'text':row['text'], 'confidence_raw':float(row['conf']),
                      **{k:int(row[k]) for k in ('block_num','par_num','line_num','word_num','left','top','width','height')}})
    return words


def _line_groups(words):
    groups = {}
    for word in words:
        groups.setdefault(tuple(word[k] for k in ('block_num','par_num','line_num')),[]).append(word)
    return list(groups.values())


def _bbox(words):
    return (min(w['left'] for w in words),min(w['top'] for w in words),
            max(w['left']+w['width'] for w in words),max(w['top']+w['height'] for w in words))


def _page_box(frame, box):
    x0,y0,x1,y1=box
    return pixel_polygon_to_page_bbox(coordinates=(x0,y0,x1,y0,x1,y1,x0,y1),
        image_width_px=frame.page_image_width_px,image_height_px=frame.page_image_height_px,
        page_bbox=frame.page_bbox)


def _pixel_box(frame, box):
    px0,py0,px1,py1=frame.page_bbox
    return tuple(round(v) for v in (
        (box[0]-px0)/(px1-px0)*frame.page_image_width_px,(box[1]-py0)/(py1-py0)*frame.page_image_height_px,
        (box[2]-px0)/(px1-px0)*frame.page_image_width_px,(box[3]-py0)/(py1-py0)*frame.page_image_height_px))


def read_ruled_table(frame, image, region, *, tessdata_dir=None):
    import cv2
    import numpy as np
    from PIL import ImageOps
    box = tuple(region['bbox'])
    px0,py0,px1,py1=frame.page_bbox
    if not (px0 <= box[0] < box[2] <= px1 and py0 <= box[1] < box[3] <= py1):
        raise ValueError('table_region_outside_page')
    x0,y0,x1,y1=_pixel_box(frame,box)
    crop=image.crop((x0,y0,x1,y1))
    gray=cv2.cvtColor(np.array(crop),cv2.COLOR_RGB2GRAY)
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(20,crop.width//10)),np.uint8))
    vertical=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(20,crop.height//5),1),np.uint8))
    def peaks(mask):
        groups=[]
        for index in np.where(mask)[0]:
            if not groups or index-groups[-1][-1]>5:groups.append([int(index)])
            else:groups[-1].append(int(index))
        return [int(round(np.mean(g))) for g in groups]
    xs=peaks(np.sum(vertical>0,axis=0)>crop.height*.35)
    ys=peaks(np.sum(horizontal>0,axis=1)>crop.width*.4)
    rows,cols=len(ys)-1,len(xs)-1
    if rows < 1 or cols < 1 or rows*cols > 400:
        raise ValueError('TABLE_GRID_UNRESOLVED: select one ruled table region')
    parent=list(range(rows*cols))
    def root(v):
        while parent[v]!=v:
            parent[v]=parent[parent[v]];v=parent[v]
        return v
    def join(a,b):parent[root(b)]=root(a)
    for row in range(rows):
        for col in range(cols):
            if col+1<cols:
                edge=vertical[ys[row]+5:ys[row+1]-5,max(0,xs[col+1]-3):xs[col+1]+4]
                if edge.size and np.mean(np.any(edge>0,axis=1))<.6:
                    join(row*cols+col,row*cols+col+1)
            if row+1<rows:
                edge=horizontal[max(0,ys[row+1]-3):ys[row+1]+4,xs[col]+5:xs[col+1]-5]
                if edge.size and np.mean(np.any(edge>0,axis=0))<.6:
                    join(row*cols+col,(row+1)*cols+col)
    components={}
    for row in range(rows):
        for col in range(cols):components.setdefault(root(row*cols+col),[]).append((row,col))
    cells=[];raw_cells=[]
    for slots in sorted(components.values(),key=lambda s:min(s)):
        row,col=min(r for r,c in slots),min(c for r,c in slots)
        re,ce=max(r for r,c in slots)+1,max(c for r,c in slots)+1
        if set(slots)!={(r,c) for r in range(row,re) for c in range(col,ce)}:
            raise ValueError('TABLE_GRID_NON_RECTANGULAR: source review required')
        bounds=(x0+xs[col],y0+ys[row],x0+xs[ce],y0+ys[re])
        content=image.crop((bounds[0]+4,bounds[1]+4,bounds[2]-4,bounds[3]-4))
        words=recognize_image(ImageOps.expand(content,border=12,fill='white'),
                              lang='chi_sim+eng',psm=6,tessdata_dir=tessdata_dir)
        text='\n'.join(_join([w['text'] for w in line]) for line in _line_groups(words))
        cells.append(VisualLayoutTableCell(row=row,column=col,row_span=re-row,column_span=ce-col,
                                          raw_text=text,bbox=_page_box(frame,bounds)))
        raw_cells.append({'row':row,'column':col,'source_pixel_bbox':bounds,'words':words})
    table_box=_page_box(frame,(x0+xs[0],y0+ys[0],x0+xs[-1],y0+ys[-1]))
    layout=VisualLayoutTable(row_count=rows,column_count=cols,cells=tuple(cells),
                            header_row_count=region.get('header_row_count',0))
    return table_box,layout,{'source_region_bbox':box,'x_lines_px':[x0+x for x in xs],
                             'y_lines_px':[y0+y for y in ys],'cells':raw_cells}


def read_page(frame, *, profile='page', tessdata_dir=None, table_regions=()):
    """Return one existing VisualTextSnapshot plus raw audit data and diagnostics."""
    from PIL import Image,ImageOps
    validate_visual_text_page_input(frame)
    if profile not in {'page','lines','numbers','tables'}:
        raise ValueError('unknown_local_reading_profile')
    image=Image.open(io.BytesIO(frame.page_image_bytes)).convert('RGB')
    if image.size!=(frame.page_image_width_px,frame.page_image_height_px):
        raise ValueError('page_image_dimensions_mismatch')
    version_data={'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'profile':profile,'table_regions':list(table_regions),'engine':_engine_identity(tessdata_dir)}
    import json
    version='tess1-'+hashlib.sha256(json.dumps(version_data,sort_keys=True).encode()).hexdigest()[:32]
    common=dict(source_sha256=frame.source_sha256,page_image_sha256=frame.page_image_sha256,
                provider_id='local-tesseract',provider_version=version,page_number=frame.page_number)
    observations=[];audit={'config':version_data,'page_words':[],'line_rechecks':[],
                           'numeric_rechecks':[],'tables':[],'diagnostics':[]}
    def add(box,text,layout=None):
        index=len(observations)
        observations.append(VisualTextObservation(**common,observation_index=index,bbox=box,raw_text=text,
            observation_id=stable_visual_text_observation_id(**common,observation_index=index,bbox=box),
            confidence=None,content_type='TABLE' if layout else 'TEXT',layout_table=layout))
    if profile!='tables':
        words=recognize_image(image,tessdata_dir=tessdata_dir)
        audit['page_words']=words
        for line in _line_groups(words):
            box=_bbox(line);text=_join([w['text'] for w in line])
            observation_index=len(observations)
            if profile=='lines':
                padded=(max(0,box[0]-8),max(0,box[1]-8),min(image.width,box[2]+8),min(image.height,box[3]+8))
                crop=image.crop(padded);crop=crop.resize((crop.width*2,crop.height*2),Image.Resampling.LANCZOS)
                revised=recognize_image(crop,lang='chi_sim+eng',psm=7,tessdata_dir=tessdata_dir)
                candidate=_join([w['text'] for w in revised])
                audit['line_rechecks'].append({'source_pixel_bbox':padded,'scale':2,'original':text,'candidate':candidate,'words':revised})
                # Both alternatives remain explicit; the baseline is retained as
                # the reading observation until the caller reviews a correction.
            add(_page_box(frame,box),text)
            if profile=='numbers':
                for word in line:
                    old=word['text']
                    if re.search(r'[\u3400-\u9fff]',old) or not re.search(r'[0-9]',old) or word['height']<8:
                        continue
                    x,y,w,h=(word[k] for k in ('left','top','width','height'))
                    pad=max(8,int(h*.4))
                    roi=(max(0,x-pad),max(0,y-6),min(image.width,x+w+pad),min(image.height,y+h+6))
                    crop=image.crop(roi);crop=crop.resize((crop.width*2,crop.height*2),Image.Resampling.LANCZOS)
                    alternative=recognize_image(crop,lang='eng',psm=7,tessdata_dir=tessdata_dir,numeric=True)
                    candidate=_join([w['text'] for w in alternative])
                    audit['numeric_rechecks'].append({'observation_index':observation_index,
                        'original':old,'candidate':candidate,'bbox':_page_box(frame,roi),
                        'source_pixel_bbox':roi,'words':alternative,'applied':False,
                        'disagrees':''.join(old.split())!=''.join(candidate.split())})
    for region in table_regions:
        try:
            box,layout,raw=read_ruled_table(frame,image,region,tessdata_dir=tessdata_dir)
            add(box,'',layout);audit['tables'].append(raw)
        except (ValueError,ImportError) as exc:
            audit['diagnostics'].append({'region':region,'message':str(exc)})
    shape=dict(page_bbox=frame.page_bbox,page_image_width_px=frame.page_image_width_px,
               page_image_height_px=frame.page_image_height_px)
    evaluated=tuple(t for t in ('TEXT','TABLE') if any(o.content_type==t for o in observations)) or ('TEXT',)
    snapshot=VisualTextSnapshot(**common,**shape,observations=tuple(observations),evaluated_content_types=evaluated,
        visual_text_snapshot_id=stable_visual_text_snapshot_id(**common,**shape))
    return snapshot,audit
