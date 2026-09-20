"""Knowledge originals through the real router and verified local source store."""
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.errors import application_error_handler
from app.api.routers import knowledge as router
from app.application import knowledge, source_runtime
from app.application.errors import ApplicationError
from app.application.storage.runtime_files import RuntimeObjectFiles
from app.adapters.storage.local_object_store import LocalObjectStore
from app.infrastructure.runtime_settings import RuntimeSettings, DatabaseBackend


class KnowledgeSourceFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.payload=b'%PDF-1.4\nSynthetic registered knowledge original.\n%%EOF\n'
        self.sha=hashlib.sha256(self.payload).hexdigest()
        self.key=f'knowledge-documents/p/{self.sha}/source.pdf'
        self.store=LocalObjectStore(self.root/'objects')
        self.store.put(key=self.key,source=BytesIO(self.payload),expected_sha256=self.sha,media_type='application/pdf')
        self.files=RuntimeObjectFiles(self.store,self.root/'cache')
        settings=RuntimeSettings.from_environment(prototype_dir=Path(__file__).resolve().parents[1])
        self.binding=patch.object(source_runtime,'_BINDING',source_runtime._SourceRuntimeBinding(settings=settings,object_store=self.store,files=self.files));self.binding.start()
        self.document={'id':'k','project_id':'p','file_name':'产品依据.pdf','source_sha256':self.sha,'source_size':len(self.payload),
            'storage_key':self.key,'detected_format':'pdf','retrieval_sync_status':'PENDING'}
        self.database=sqlite3.connect(':memory:',check_same_thread=False);self.database.row_factory=sqlite3.Row
        self.database.execute('CREATE TABLE stored_objects (storage_key TEXT, sha256 TEXT, media_type TEXT)')
        self.database.execute('INSERT INTO stored_objects VALUES (?,?,?)',(self.key,self.sha,'application/pdf'))
        @contextmanager
        def connection():yield self.database
        self.patches=[patch('app.application.projects._require_project',return_value={'id':'p'}),
            patch.object(knowledge.db,'get_knowledge_document',side_effect=lambda _:self.document),
            patch.object(knowledge.db,'get_conn',side_effect=connection),
            patch.object(knowledge,'_enqueue_durable_knowledge_sync'),patch.object(knowledge,'_sync_knowledge_document_targets')]
        self.mocks=[item.start() for item in self.patches]
        app=FastAPI();app.add_exception_handler(ApplicationError,application_error_handler);app.include_router(router.build())
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):item.stop()
        self.binding.stop();self.database.close();self.temp.cleanup()

    def get(self,**params):return self.client.get('/api/projects/p/knowledge-documents/k/file',params=params)

    def test_pdf_original_inline_preserves_filename_sha_and_supports_ranges_without_jobs(self):
        response=self.get(expected_source_sha256=self.sha)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.content,self.payload)
        self.assertEqual(response.headers['content-type'],'application/pdf')
        self.assertEqual(response.headers['content-disposition'],"inline; filename*=utf-8''"+quote('产品依据.pdf',safe=''))
        self.assertEqual(response.headers['x-reqpilot-source-sha256'],self.sha)
        self.assertEqual(response.headers['etag'],'"'+self.sha+'"')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertNotIn(str(self.root),str(response.headers))
        ranged=self.client.get('/api/projects/p/knowledge-documents/k/file',headers={'Range':'bytes=0-4'})
        self.assertEqual(ranged.status_code,206);self.assertEqual(ranged.content,self.payload[:5])
        self.mocks[-1].assert_not_called();self.mocks[-2].assert_not_called()

    def test_other_registered_media_downloads_without_exposing_original_path(self):
        self.document['file_name']='C:\\private\\证据.docx'
        media='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        self.database.execute('UPDATE stored_objects SET media_type=?',(media,))
        response=self.get()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['content-type'],media)
        self.assertEqual(response.headers['content-disposition'],"attachment; filename*=utf-8''"+quote('证据.docx',safe=''))
        self.assertNotIn('private',response.headers['content-disposition'])

    def test_project_scope_and_source_pin_fail_before_opening_storage(self):
        with patch.object(self.store,'open',wraps=self.store.open) as opened:
            self.document['project_id']='another-project'
            self.assertEqual(self.get().status_code,404)
            self.document['project_id']='p'
            self.assertEqual(self.get(expected_source_sha256='0'*64).status_code,409)
            self.assertEqual(self.get(expected_source_sha256='not-a-sha').status_code,422)
            opened.assert_not_called()
        self.document=None
        self.assertEqual(self.get().status_code,404)

    def test_missing_and_corrupt_originals_fail_without_path_details(self):
        object_path=self.store.root/self.key
        object_path.write_bytes(b'corrupted')
        response=self.get()
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['detail']['code'],'KNOWLEDGE_SOURCE_IDENTITY_MISMATCH')
        self.assertNotIn(str(self.root),response.text)
        object_path.unlink()
        response=self.get();self.assertEqual(response.status_code,404)
        self.assertNotIn(str(self.root),response.text)

    def test_cache_revalidation_restores_the_registered_bytes(self):
        self.assertEqual(self.get().content,self.payload)
        cached=self.files.resolve_record(self.document);cached.write_bytes(b'bad cached data')
        self.assertEqual(self.get().content,self.payload)

    def test_missing_identity_metadata_mismatch_and_unavailable_store_are_explicit(self):
        self.document['source_sha256']=''
        self.assertEqual(self.get().json()['detail']['code'],'KNOWLEDGE_SOURCE_IDENTITY_MISSING')
        self.document['source_sha256']=self.sha
        self.database.execute('UPDATE stored_objects SET sha256=?',('b'*64,))
        self.assertEqual(self.get().status_code,409)
        self.database.execute('UPDATE stored_objects SET sha256=?',(self.sha,))
        with patch.object(self.files,'resolve_record',side_effect=OSError('/private/sensitive/path unavailable')):
            response=self.get()
        self.assertEqual(response.status_code,503)
        self.assertNotIn('/private',response.text)

    def test_legacy_test_path_also_checks_actual_sha(self):
        path=self.root/'legacy.txt';path.write_bytes(self.payload)
        self.document.pop('storage_key');self.document.update(file_path=str(path),file_name='legacy.txt')
        source_runtime._BINDING.settings=replace(source_runtime._BINDING.settings,database_backend=DatabaseBackend.SQLITE_TEST)
        self.assertEqual(self.get().status_code,200)
        path.write_bytes(b'different source')
        self.assertEqual(self.get().status_code,409)
