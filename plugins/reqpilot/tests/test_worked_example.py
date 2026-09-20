from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json,sys,unittest
from zipfile import ZipFile
from xml.etree import ElementTree as ET
PLUGIN=Path(__file__).resolve().parents[1];REPO=PLUGIN.parents[1]
sys.path[:0]=[str(PLUGIN/'scripts'),str(REPO/'prototype')]
from worked_example import generate
from app.services.response_matrix import _result, group_assessment

class WorkedExampleTests(unittest.TestCase):
    def test_original_to_excel_retains_front_matter_without_src_and_skips_empty_analysis(self):
        with TemporaryDirectory() as temp, redirect_stdout(StringIO()):
            root=Path(temp)/'example';receipt=generate(REPO,root)
            self.assertEqual((receipt['source_items'],receipt['requirements'],receipt['front_matter_source_items']),(3,2,0))
            snapshot=json.loads((root/'delivery/matrix-snapshot.json').read_text())
            front=[n for n in snapshot['nodes'] if n['source_kind'] in {'COVER','TABLE_OF_CONTENTS'}]
            self.assertEqual(len(front),2);self.assertFalse(any(n.get('source_item') for n in front))
            targets=[n for n in snapshot['nodes'] if (n.get('source_item') or {}).get('requirementness')=='REQUIREMENT']
            self.assertEqual([_result(n)[0] for n in targets],['未执行符合性分析']*2)
            parent=next(n for n in snapshot['nodes'] if n['node_id']=='document')
            self.assertEqual(group_assessment(parent,{n['node_id']:n for n in snapshot['nodes']})[0],'未执行符合性分析')
            with ZipFile(root/'delivery/matrix.xlsx') as z:
                text='\n'.join(''.join(ET.fromstring(z.read(name)).itertext()) for name in z.namelist()
                    if name=='xl/sharedStrings.xml' or name.startswith('xl/worksheets/') and name.endswith('.xml'))
            for original in ['Document ID: DEMO-01','Revision: A','1.1 Opening response ........ 2','Housing','Seal','30 ms','40 ms','未执行符合性分析']:
                self.assertIn(original,text)
