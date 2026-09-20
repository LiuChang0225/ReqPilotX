from __future__ import annotations
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest
from unittest.mock import patch
import pymupdf
from PIL import Image, ImageDraw
from app.services.exporter import _build_export_source_image_context, _source_pdf_crop_payload, _matrix_node_figure_refs, _load_matrix_figure_payload, export_compliance_matrix

class OriginalPdfImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'synthetic.pdf'
        self.image=Image.new('RGB',(800,400),'white');ImageDraw.Draw(self.image).text((100,100),'SOURCE DIMENSION 175.039',fill='red')
        stream=BytesIO();self.image.save(stream,format='PNG');self.png=stream.getvalue();self.bbox=[40,40,120,80]
    def document(self, overlay=False):
        with pymupdf.open() as document:
            page=document.new_page();page.insert_image(pymupdf.Rect(self.bbox),stream=self.png)
            if overlay:page.draw_rect(pymupdf.Rect(70,50,90,65),color=(0,0,1),fill=(0,0,1))
            document.save(self.path)
        sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        return _build_export_source_image_context(source_file_path=self.path,source_sha256=sha)
    def payload(self,ctx,bbox=None):
        return _source_pdf_crop_payload({'location':{'format':'pdf','page_number':1,'bbox':bbox or self.bbox}},source_image_context=ctx,require_declared_rendering=False)
    def test_exact_complete_placement_keeps_native_pixels(self):
        payload=self.payload(self.document());self.assertEqual(payload[1:],(800,400))
        with Image.open(payload[0]) as image:self.assertEqual(image.convert('RGB').tobytes(),self.image.tobytes())
    def test_overlay_is_rendered_with_source_instead_of_extracted_away(self):
        payload=self.payload(self.document(overlay=True));self.assertEqual(payload[1:],(160,80))
        with Image.open(payload[0]) as image:
            r,g,b=image.convert('RGB').getpixel((80,40));self.assertGreater(b,r);self.assertGreater(b,g)
    def test_partial_crop_does_not_expand_to_the_full_embedded_image(self):
        payload=self.payload(self.document(),[40,40,80,80]);self.assertEqual(payload[1:],(80,80))
    def test_original_hash_mismatch_cannot_return_native_pixels(self):
        context=self.document();context.source_sha256='0'*64;self.assertIsNone(self.payload(context))
    def test_exact_image_anchor_shape_retains_coordinates_as_a_figure(self):
        values=_matrix_node_figure_refs({'figure_refs':[{'precision':'EXACT_IMAGE','page':1,'bbox':self.bbox,'asset_id':'shared','source_sha256':'a'*64}]})
        self.assertEqual(len(values),1);self.assertEqual(values[0]['kind'],'image')
        self.assertEqual(values[0]['location'],{'format':'pdf','page_number':1,'bbox':self.bbox})

    def sparse_reference(self, context):
        return {'precision':'SOURCE_IR_BLOCK','relation':'AGENT_CONFIRMED_IMAGE_CONTEXT','page':1,
                'bbox':self.bbox,'asset_id':'original-image','anchor_id':'image-placement','source_sha256':context.source_sha256}

    def test_frozen_sparse_source_ir_reference_requires_the_actual_image_placement(self):
        context=self.document();reference=self.sparse_reference(context)
        normalized=_matrix_node_figure_refs({'figure_refs':[reference]})[0]
        payload=_load_matrix_figure_payload(normalized,source_image_context=context)
        self.assertEqual(payload[1:],(800,400))
        with Image.open(payload[0]) as image:self.assertEqual(image.convert('RGB').tobytes(),self.image.tobytes())
        reference['bbox']=[40,90,120,110]
        text_crop=_matrix_node_figure_refs({'figure_refs':[reference]})[0]
        self.assertIsNone(_load_matrix_figure_payload(text_crop,source_image_context=context))

    def image_snapshot(self, context):
        from .test_response_matrix import response_snapshot
        data=response_snapshot();data['source_item_baseline']['source_sha256']=context.source_sha256
        next(n for n in data['nodes'] if n['node_id']=='context')['figure_refs']=[self.sparse_reference(context)]
        return data

    def test_program_receipt_proves_the_original_payload_is_in_the_workbook(self):
        context=self.document();receipt={}
        _,path=export_compliance_matrix('Example','',Path(self.temp.name)/'export',matrix_snapshot=self.image_snapshot(context),
            source_file_path=self.path,source_sha256=context.source_sha256,image_export_receipt=receipt)
        self.assertTrue(Path(path).is_file())
        self.assertEqual((receipt['expected_reference_count'],receipt['embedded_media_count'],receipt['inserted_placement_count']),(1,1,1))
        self.assertEqual(receipt['gaps'],[])
        self.assertEqual(receipt['references'][0]['owner'],'context')
        self.assertTrue(receipt['references'][0]['media_parts'])
        self.assertTrue(receipt['references'][0]['figure_placements'])

    def test_one_missing_image_rejects_partial_export_with_the_named_anchor(self):
        context=self.document();data=self.image_snapshot(context)
        node=next(n for n in data['nodes'] if n['node_id']=='context')
        node['figure_refs'].append({**self.sparse_reference(context),'anchor_id':'missing-image','bbox':[40,90,120,110]})
        output=Path(self.temp.name)/'partial'
        with self.assertRaisesRegex(ValueError,'context/missing-image: IMAGE_PAYLOAD_UNAVAILABLE'):
            export_compliance_matrix('Example','',output,matrix_snapshot=data,source_file_path=self.path,source_sha256=context.source_sha256)
        self.assertEqual(list(output.glob('*.xlsx')),[])

    def test_silent_image_writer_omission_fails_the_actual_package_guard(self):
        context=self.document();output=Path(self.temp.name)/'omitted'
        with patch('xlsxwriter.worksheet.Worksheet.insert_image',return_value=0), self.assertRaisesRegex(ValueError,'IMAGE_NOT_EMBEDDED'):
            export_compliance_matrix('Example','',output,matrix_snapshot=self.image_snapshot(context),source_file_path=self.path,source_sha256=context.source_sha256)
        self.assertEqual(list(output.glob('*.xlsx')),[])

    def test_lost_projection_cannot_hide_an_original_source_image_binding(self):
        context=self.document();data=self.image_snapshot(context)
        node=next(n for n in data['nodes'] if n['node_id']=='context')
        node['source_item']['image_bindings']=[self.sparse_reference(context)];node['figure_refs']=[]
        with self.assertRaisesRegex(ValueError,'context/image-placement: MISSING_IMAGE_PROJECTION'):
            export_compliance_matrix('Example','',Path(self.temp.name)/'lost',matrix_snapshot=data,source_file_path=self.path,source_sha256=context.source_sha256)
