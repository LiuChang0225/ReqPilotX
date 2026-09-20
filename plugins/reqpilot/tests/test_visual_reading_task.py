"""Real task lifecycle for local OCR and source-reviewed transcription."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import shutil
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
ENTRY=ROOT/'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0]=[str(ENTRY.parent),str(ROOT/'prototype')]
from task_service import TaskService,PrepareRequest,VisualRevisionRequest
from task_bridge import TaskBridge


class VisualReadingTaskTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('tesseract'),'local OCR binary required')
    def test_ocr_and_correction_commit_revision_and_replay_without_changing_old_capture(self):
        import pymupdf
        with TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.pdf'
            doc=pymupdf.open()
            for number in (1,2):
                page=doc.new_page();page.insert_text((40,80),f'{number}. Reading example',fontsize=16)
                page.insert_text((40,120),'Temperature shall be 150 C.',fontsize=14)
            doc.save(source);doc.close();original=source.read_bytes()
            # Exercise the actual fixed-package gate and CLI, not just the service.
            service=TaskBridge(root,ENTRY,project_root=ROOT)
            state=service.invoke('open',{'mode':'create','source':str(source),'goal':'Read the original.','creation_key':'visual-reading'})
            task=state['task_id'];state=service.invoke('prepare',{'task_id':task,'expected_revision':state['revision']})
            original_capture=Path(state['selected']['READING']['path']);before=original_capture.read_bytes()
            request={'task_id':task,'expected_revision':state['revision'],'command_id':'ocr-two-pages',
                     'mode':'ocr','pages':[1,2]}
            updated=service.invoke('prepare',request)
            self.assertEqual(updated['operation_state'],'COMMITTED',updated)
            public_read=service.invoke('read',updated['source_authoring_read']['request'])
            self.assertEqual(public_read['status'],'READ',public_read)
            self.assertTrue(public_read['nodes'])
            replay=service.invoke('prepare',request)
            self.assertTrue(replay['replayed']);self.assertEqual(replay['operation_id'],updated['operation_id'])
            reading=Path(updated['selected']['READING']['path']);ir=json.loads(reading.read_text())
            ref=next(i for i,n in enumerate(ir['nodes']) if n.get('visual_text_ref') and '150' in n['raw_text'])
            correction={'task_id':task,'expected_revision':updated['revision'],'command_id':'reviewed-reading',
                        'changes':[{'ref':ref,'text':'Temperature shall be 150 C.','reason':'Compared the original line image.'}]}
            revised=service.invoke('visual_revise',correction)
            self.assertEqual(revised['operation_state'],'COMMITTED',revised)
            corrected_view=service.invoke('read',revised['source_authoring_read']['request'])
            self.assertEqual(corrected_view['status'],'READ',corrected_view)
            current=json.loads(Path(revised['selected']['READING']['path']).read_text())
            self.assertEqual(current['nodes'][:len(ir['nodes'])],ir['nodes'])
            snapshots={s['visual_text_snapshot_id']:s for s in current['visual_text_snapshots']}
            self.assertTrue(all(snapshots[s['visual_text_snapshot_id']]==s for s in ir['visual_text_snapshots']))
            self.assertTrue(service.invoke('visual_revise',correction)['replayed'])
            self.assertEqual(original_capture.read_bytes(),before);self.assertEqual(source.read_bytes(),original)

    def test_scope_and_correction_selectors_are_explicit(self):
        for changes in ({'mode':'ocr','pages':[]},{'mode':'ocr','pages':[True]},
                        {'mode':'capture','pages':[1]},{'mode':'pages','pages':[0]},
                        {'mode':'pages','profile':'lines'}, {'mode':'ocr','pages':[1],'profile':'tables'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PrepareRequest.model_validate({'task_id':'t','expected_revision':'r',**changes})
        with self.assertRaises(ValueError):
            VisualRevisionRequest.model_validate({'task_id':'t','expected_revision':'r',
                'changes':[{'ref':1,'refs':[1],'text':'x','reason':'review'}]})

    def test_lossless_array_transport_keeps_member_validation(self):
        valid=PrepareRequest.model_validate({'task_id':'t','expected_revision':'r','mode':'pages','pages':'[1,2]','profile':'page'})
        self.assertEqual(valid.pages,[1,2])
        PrepareRequest.model_validate({'task_id':'t','expected_revision':'r','mode':'capture','profile':'page'})
        corrected=VisualRevisionRequest.model_validate({'task_id':'t','expected_revision':'r',
            'changes':json.dumps([{'refs':'[1,2]','text':'Source words.','reason':'Read original pixels.'}])})
        self.assertEqual(corrected.changes[0].refs,[1,2])
        for value in ('[true]', '["1"]', '[[1]]', '[1,1]', '"[1]"', '[NaN]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PrepareRequest.model_validate({'task_id':'t','expected_revision':'r','mode':'ocr','pages':value})
        with self.assertRaises(ValueError):
            VisualRevisionRequest.model_validate({'task_id':'t','expected_revision':'r',
                'changes':'[{"ref":1,"ref":2,"text":"x","reason":"review"}]'})

    def test_page_request_scope_is_validated_and_reported_through_bridge(self):
        import pymupdf
        with TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.pdf';doc=pymupdf.open()
            for _ in range(2):doc.new_page().insert_text((40,80),'Example')
            doc.save(source);doc.close()
            bridge=TaskBridge(root,ENTRY,project_root=ROOT)
            state=bridge.invoke('open',{'mode':'create','source':str(source),'goal':'Read source','creation_key':'pages'})
            state=bridge.invoke('prepare',{'task_id':state['task_id'],'expected_revision':state['revision'],'profile':'page'})
            req={'task_id':state['task_id'],'expected_revision':state['revision'],'mode':'pages','pages':'[2]','profile':'page'}
            invalid=bridge.invoke('prepare',{**req,'pages':'[3]'})
            self.assertIn('INVALID_PAGE',json.dumps(invalid))
            self.assertEqual(bridge.invoke('inspect',{'task_id':state['task_id']})['revision'],state['revision'])
            prepared=bridge.invoke('prepare',req)
            self.assertEqual(prepared['operation_state'],'COMMITTED',prepared)
            self.assertEqual(prepared['page_preparation']['requested_pages'],[2])
            self.assertEqual(prepared['page_preparation']['prepared_pages'],[1,2])
            page=bridge.invoke('read',{'task_id':state['task_id'],'view':'page','page':2})
            self.assertEqual(page['status'],'READ',page)

    def test_task_catalog_cli_and_models_advertise_the_same_actions(self):
        import subprocess
        from task_service import REQUESTS
        import reqpilot
        actions=json.loads((ENTRY.parent/'task-api-capabilities.json').read_text())['actions']
        self.assertEqual(set(actions),set(REQUESTS))
        self.assertEqual(reqpilot.catalog(None)['tools'][0]['command'],'task '+ '/'.join(actions))
        help_text=subprocess.check_output([sys.executable,str(ENTRY),'task','--help'],text=True)
        for action in actions:self.assertIn(action,help_text)
        bridge=TaskBridge(ROOT,ENTRY,project_root=ROOT)
        description=bridge.invoke('describe',{'action':'visual_revise','max_chars':50000})
        self.assertEqual(description['read_budget']['applied_max_chars'],24000)


if __name__=='__main__':unittest.main()
