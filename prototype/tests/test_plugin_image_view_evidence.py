
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from summarize_plugin_image_views import summarize


class PluginImageViewEvidenceTests(unittest.TestCase):
    def test_exec_images_are_counted_without_retaining_reasoning_or_image_bytes(self):
        rows=[{'type':'session_meta','payload':{'id':'t'}},
            {'type':'response_item','payload':{'type':'reasoning','text':'DO_NOT_RETAIN'}},
            {'type':'response_item','payload':{'type':'custom_tool_call','name':'exec','call_id':'c','input':'const r = await tools.view_image({path:"/test/page.png"}); image(r.image_url);'}},
            {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'c','output':[{'type':'input_text','text':'done'},{'type':'input_image','image_url':'DO_NOT_RETAIN_PIXELS'}]}}]
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'session.jsonl';path.write_text('\n'.join(json.dumps(row) for row in rows))
            result=summarize(path,'t')
            self.assertEqual(result['returned_image_blocks'],1)
            self.assertEqual(result['calls'][0]['requests'],[{'path':'/test/page.png'}])
            self.assertNotIn('DO_NOT_RETAIN',json.dumps(result))
            with self.assertRaisesRegex(ValueError,'requested thread'):summarize(path,'another')


if __name__=='__main__':unittest.main()
