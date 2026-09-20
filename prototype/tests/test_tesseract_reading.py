
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image,ImageDraw
from app.adapters.documents import tesseract_reading as reader
from app.domain.source_visual_text import VisualTextPageInput
import hashlib


class TesseractReadingTests(unittest.TestCase):
    def frame(self,image):
        stream=io.BytesIO();image.save(stream,format='PNG');data=stream.getvalue()
        return VisualTextPageInput(source_bytes=b'source',source_sha256=hashlib.sha256(b'source').hexdigest(),
            page_number=1,page_bbox=(0,0,image.width,image.height),page_image_bytes=data,
            page_image_sha256=hashlib.sha256(data).hexdigest(),page_image_width_px=image.width,
            page_image_height_px=image.height)

    def test_plain_text_success_is_not_silently_treated_as_empty_tsv(self):
        with patch.object(reader.shutil,'which',return_value='/test/tesseract'), patch.object(reader.subprocess,'run',
                return_value=SimpleNamespace(returncode=0,stdout=b'150 C\n',stderr=b'')):
            with self.assertRaisesRegex(ValueError,'INVALID_TSV'):
                reader.recognize_image(Image.new('RGB',(100,100),'white'))

    def test_binary_diagnostics_do_not_mask_the_ocr_failure(self):
        with patch.object(reader.shutil,'which',return_value='/test/tesseract'), patch.object(reader.subprocess,'run',
                return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'image cannot be read: \x89PNG')):
            with self.assertRaisesRegex(ValueError,'TESSERACT_FAILED.*image cannot be read'):
                reader.recognize_image(Image.new('RGB',(100,100),'white'))

    def test_invalid_recognized_bytes_are_never_replaced_with_plausible_text(self):
        with patch.object(reader.shutil,'which',return_value='/test/tesseract'), patch.object(reader.subprocess,'run',
                return_value=SimpleNamespace(returncode=0,stdout=b'\x89PNG',stderr=b'')):
            with self.assertRaisesRegex(ValueError,'INVALID_TSV.*not UTF-8'):
                reader.recognize_image(Image.new('RGB',(100,100),'white'))

    def test_numeric_alternative_does_not_overwrite_original_observation(self):
        image=Image.new('RGB',(200,100),'white')
        def word(text):return {'text':text,'confidence_raw':80,'block_num':1,'par_num':1,'line_num':1,
                              'word_num':1,'left':30,'top':30,'width':60,'height':20}
        with patch.object(reader,'recognize_image',side_effect=[[word('130C')],[word('150°C')]]),\
             patch.object(reader,'_engine_identity',return_value={'version':'test'}):
            snapshot,audit=reader.read_page(self.frame(image),profile='numbers')
        self.assertEqual(snapshot.observations[0].raw_text,'130C')
        self.assertEqual(audit['numeric_rechecks'][0]['candidate'],'150°C')
        self.assertFalse(audit['numeric_rechecks'][0]['applied'])

    def test_ruled_grid_preserves_real_header_merge(self):
        try:import cv2  # noqa: F401
        except ImportError:self.skipTest('OpenCV optional table dependency unavailable')
        image=Image.new('RGB',(224,224),'white');draw=ImageDraw.Draw(image)
        for y in (12,112,212):draw.line((12,y,212,y),fill='black',width=3)
        for x in (12,212):draw.line((x,12,x,212),fill='black',width=3)
        draw.line((112,112,112,212),fill='black',width=3)
        with patch.object(reader,'recognize_image',return_value=[]):
            _,table,_=reader.read_ruled_table(self.frame(image),image,{'bbox':[0,0,224,224],'header_row_count':1})
        self.assertEqual((table.row_count,table.column_count),(2,2))
        self.assertEqual([(c.row,c.column,c.row_span,c.column_span) for c in table.cells],
                         [(0,0,1,2),(1,0,1,1),(1,1,1,1)])


if __name__=='__main__':unittest.main()
