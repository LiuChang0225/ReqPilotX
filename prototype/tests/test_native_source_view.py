
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import hashlib
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routers.source_capture import build
from app.application.agent_workflow.office_source_evidence import build_native_office_source_view, read_native_office_source_image
from .test_office_source_pipeline import fixture


class NativeSourceViewTests(unittest.TestCase):
    def test_read_only_http_view_and_object_resolve_registered_original(self):
        from app.api.errors import application_error_handler
        from app.application.errors import ApplicationError
        app=FastAPI();app.add_exception_handler(ApplicationError,application_error_handler);app.include_router(build())
        for suffix in ['docx','xlsx']:
            with self.subTest(format=suffix),TemporaryDirectory() as tmp:
                source,snapshot,payload=fixture(Path(tmp),suffix)
                document={'id':'document','project_id':'project','source_sha256':snapshot.source_sha256}
                with mock.patch('app.application.source_capture.require_current_source_document',return_value=document) as resolve, mock.patch('app.application.source_runtime.resolve_source_record_path',return_value=source), TestClient(app) as client:
                    response=client.get('/api/projects/project/sor-documents/document/source-capture/native-view',params={'source_sha256':snapshot.source_sha256})
                    self.assertEqual(response.status_code,200,response.text);value=response.json()
                    self.assertEqual(value['source_format'],suffix)
                    resolve.assert_called_with(project_id='project',sor_document_id='document',expected_source_sha256=snapshot.source_sha256)
                    self.assertEqual([n['source_node_id'] for n in value['nodes']],[n.source_node_id for n in snapshot.nodes])
                    self.assertTrue(all('page_number' not in n['native_locator'] and 'bbox' not in n['native_locator'] for n in value['nodes']))
                    picture=next(n for n in value['nodes'] if n['image'])
                    image=client.get(picture['image']['url']);self.assertEqual(image.status_code,200,image.text if image.status_code!=200 else '')
                    self.assertEqual(image.content,payload);self.assertEqual(image.headers['x-source-sha256'],snapshot.source_sha256)
                    self.assertEqual(image.headers['etag'],'"'+hashlib.sha256(payload).hexdigest()+'"')
                    changed=client.get(picture['image']['url'].replace(picture['image']['content_sha256'],'0'*64))
                    self.assertEqual(changed.status_code,422)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),snapshot.source_sha256)

    def test_image_reader_rejects_wrong_source_and_wrong_object_even_with_existing_asset(self):
        with TemporaryDirectory() as tmp:
            source,snapshot,payload=fixture(Path(tmp),'docx');view=build_native_office_source_view(source,snapshot.source_sha256)
            picture=next(n for n in view['nodes'] if n['image'])
            with self.assertRaisesRegex(ValueError,'SHA mismatch'):
                build_native_office_source_view(source,'0'*64)
            with self.assertRaisesRegex(ValueError,'object or content SHA'):
                read_native_office_source_image(source,snapshot.source_sha256,'node-invented',picture['image']['content_sha256'])
            raw=read_native_office_source_image(source,snapshot.source_sha256,picture['source_node_id'],picture['image']['content_sha256'])
            self.assertEqual(raw['bytes'],payload);self.assertEqual((raw['width'],raw['height']),(120,60))
