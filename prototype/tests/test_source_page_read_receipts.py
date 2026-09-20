
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib,json
from pathlib import Path
import tempfile
import unittest
import pymupdf
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.application.agent_workflow.source_document_loop_jobs import _source_page_return_receipts
from app.adapters.agents.codex_app_server.native_loop_driver import _tool_result
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

REPO=Path(__file__).resolve().parents[2]

class SourcePageReadReceiptTests(unittest.TestCase):
    def test_rich_dynamic_crop_metadata_remains_a_crop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();workspace=root/'workspace';workspace.mkdir();calls=root/'runtime/calls';calls.mkdir(parents=True)
            pdf=pymupdf.open();page=pdf.new_page();page.insert_text((40,60),'Synthetic source');pdf.save(workspace/'source.pdf');pdf.close()
            sha=hashlib.sha256((workspace/'source.pdf').read_bytes()).hexdigest()
            host=NativeLoopToolHost(workspace=workspace,plugin_root=REPO/'plugins/reqpilot',project_root=REPO)
            args={'path':'source.pdf','page':1,'bbox':[20,20,100,100],'scale':3}
            response=_tool_result(host.invoke('reqpilot_read',args))
            call={'status':'RETURNED','arguments':args,'identity':{'name':'reqpilot_read','thread_id':'t','turn_id':'u','call_id':'c','arguments_sha256':canonical_json_sha256(args)},'response':response}
            path=calls/'call.json';path.write_text(json.dumps(call))
            receipts=_source_page_return_receipts(root,sha,'t')
            self.assertEqual(receipts[0]['view_kind'],'CROP');self.assertEqual(receipts[0]['scale'],3)
            metadata=json.loads(response['contentItems'][0]['text']);metadata['bbox']=[0,0,1,1]
            call['response']['contentItems'][0]['text']=json.dumps(metadata);path.write_text(json.dumps(call))
            with self.assertRaisesRegex(ValueError,'metadata'):_source_page_return_receipts(root,sha,'t')

    def test_native_page_open_uses_manifest_identity_not_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();workspace=root/'workspace';workspace.mkdir();(root/'runtime').mkdir()
            pdf=pymupdf.open();page=pdf.new_page();page.insert_text((40,60),'Synthetic source');pdf.save(workspace/'source.pdf');pdf.close()
            sha=hashlib.sha256((workspace/'source.pdf').read_bytes()).hexdigest()
            host=NativeLoopToolHost(workspace=workspace,plugin_root=REPO/'plugins/reqpilot',project_root=REPO)
            for op,args in [('capture',{'source':'source.pdf','output_dir':'capture'}),('reading-workspace',{'source':'source.pdf','source_ir':'capture/source_ir.json','output_dir':'reading'})]:
                result=host.invoke('reqpilot_run',{'operation':op,'arguments':args});self.assertTrue(result['success'],result)
            image=workspace/'reading/renders/high/P0001.png';digest=hashlib.sha256(image.read_bytes()).hexdigest()
            event={'type':'native_read_completed','name':'imageView','thread_id':'t','turn_id':'u','item_id':'i','path':str(image),'image_sha256':digest}
            events=root/'runtime/events.jsonl';events.write_text(json.dumps(event)+'\n')
            receipts=_source_page_return_receipts(root,sha,'t')
            self.assertEqual(len(receipts),1);self.assertEqual(receipts[0]['evidence_kind'],'NATIVE_IMAGE_OPENED');self.assertEqual(receipts[0]['page'],1)
            (workspace/'P0001.png').write_bytes(image.read_bytes());event['path']=str(workspace/'P0001.png');events.write_text(json.dumps(event)+'\n')
            self.assertEqual(_source_page_return_receipts(root,sha,'t'),[])

if __name__=='__main__':unittest.main()
