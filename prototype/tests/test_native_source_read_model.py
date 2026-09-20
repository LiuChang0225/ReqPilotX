from io import BytesIO
from types import SimpleNamespace
from datetime import datetime, UTC
import hashlib,json,unittest
from unittest.mock import patch
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import native_source_graph as graphs
from .test_native_source_graph import graph_fixture, advance_map

class NativeReadModelTests(unittest.TestCase):
    def test_current_head_and_bytes_are_checked_but_full_validation_is_reused(self):
        value=graph_fixture()
        value['matrix_snapshot']=graphs.validate_native_source_graph(value)['snapshot']
        value['artifact_sha256']=graphs.core.canonical_json_sha256({k:v for k,v in value.items() if k!='artifact_sha256'})
        raw=json.dumps(value).encode()
        pointer={'object_key':'artifacts/source-item-agents/p/d/working/graph.json','object_sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
        job=SimpleNamespace(job_id='job',updated_at=datetime.now(UTC),result={'native_source_graph_artifact':pointer},payload={})
        store=SimpleNamespace(open=lambda key:BytesIO(raw))
        jobs=SimpleNamespace(get_latest_succeeded=lambda **kwargs:job)
        container=SimpleNamespace(jobs=jobs,object_store=store)
        graphs._SOURCE_READ_BUNDLES.clear()
        with patch.object(graphs,'validate_native_source_graph',wraps=graphs.validate_native_source_graph) as validate:
            first=graphs.load_native_source_read_bundle('p','d',value['source_sha256'],container=container)
            first['manifest']['authority']='caller mutation'
            second=graphs.load_native_source_read_bundle('p','d',value['source_sha256'],container=container)
            self.assertEqual(second['manifest']['authority'],'NATIVE_AGENT_SOURCE_GRAPH')
            self.assertNotIn('snapshot',second)
            self.assertEqual(validate.call_count,1)
            raw += b' '
            with self.assertRaisesRegex(ValueError,'integrity'):
                graphs.load_native_source_read_bundle('p','d',value['source_sha256'],container=container)
        newer=advance_map(graph_fixture(),'revised-doc')
        newer['matrix_snapshot']=graphs.validate_native_source_graph(newer)['snapshot']
        newer['artifact_sha256']=graphs.core.canonical_json_sha256({k:v for k,v in newer.items() if k!='artifact_sha256'})
        raw=json.dumps(newer).encode()
        pointer.update(object_sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
        current=graphs.load_native_source_read_bundle('p','d',value['source_sha256'],container=container)
        self.assertEqual(current['document_map'].artifact_sha256,newer['document_map']['artifact_sha256'])
