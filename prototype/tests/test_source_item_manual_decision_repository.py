from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from .test_source_item_manual_decisions import context
from app.adapters.persistence import source_item_manual_decisions as repo
from app.application.agent_workflow import source_item_manual_decision_api as api
from app.application.errors import ApplicationError


class Sink:
    def __init__(self):self.headers={}


class ManualRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.path=Path(self.temp.name)/'manual.sqlite';self.context=context()
        with self.connection() as conn:
            conn.execute('CREATE TABLE sor_documents(id TEXT,project_id TEXT,source_sha256 TEXT)')
            conn.execute('INSERT INTO sor_documents VALUES(?,?,?)',('d','p','c'*64))
        self.dbpatch=patch.object(repo.db,'get_conn',self.connection);self.dbpatch.start()
        self.ctxpatch=patch.object(api,'resolve_current_manual_context',side_effect=lambda *args:self.context);self.ctxpatch.start()
        self.readpatch=patch.object(api,'_resolve_current_manual_read',side_effect=lambda *args:(self.context,'f'*64));self.readpatch.start()

    def tearDown(self):
        self.readpatch.stop();self.ctxpatch.stop();self.dbpatch.stop();self.temp.cleanup()

    @contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path);conn.row_factory=sqlite3.Row
        try:
            yield conn;conn.commit()
        except Exception:
            conn.rollback();raise
        finally:conn.close()

    def payload(self,revision=0,command='one',reason='工程师的工作意见'):
        return api.EngineerResponseRequest(command_id=command,expected_source_sha256='c'*64,
            expected_baseline_sha256=self.context['baseline_sha256'],expected_response_revision=revision,
            decisions=[{'unit_id':'u','expected_unit_context_sha256':self.context['units']['u']['unit_context_sha256'],
                        'decision':'NON_COMPLIANT','reason':reason,'conditions':'当前产品版本','evidence_requests':'请补试验报告'}])

    def test_api_write_reopen_read_and_idempotency_keep_user_origin(self):
        payload=self.payload();first=api.post_engineer_responses('p','d',payload,Sink())
        self.assertEqual(first['records']['u']['origin'],'USER_INPUT')
        self.assertEqual(first['records']['u']['decision'],'NON_COMPLIANT')
        self.assertEqual(first['human_acceptance'],'NOT_CLAIMED')
        reopened=api.get_engineer_responses('p','d',Sink())
        self.assertEqual(reopened['response_sha256'],first['response_sha256'])
        again=api.post_engineer_responses('p','d',payload,Sink())
        self.assertEqual(again['response_revision'],1)
        with self.assertRaises(ApplicationError):api.post_engineer_responses('p','d',self.payload(command='two'),Sink())

    def test_publication_uses_latest_user_edit_then_stales_without_reviving(self):
        api.post_engineer_responses('p','d',self.payload(),Sink())
        api.post_engineer_responses('p','d',self.payload(1,'two','用户在Agent运行中更新的意见'),Sink())
        with self.connection() as conn:
            result=repo.rebind_manual_decisions_in_transaction(conn,project_id='p',document_id='d',
                previous_context=context(),current_context=context(baseline='f'),publication_id='published-1')
        self.assertEqual(result['response_revision'],3)
        self.context=context(baseline='f')
        self.assertEqual(api.get_engineer_responses('p','d',Sink())['records']['u']['reason'],'用户在Agent运行中更新的意见')
        with self.connection() as conn:
            repo.rebind_manual_decisions_in_transaction(conn,project_id='p',document_id='d',
                previous_context=self.context,current_context=context('d','e'),publication_id='published-2')
            repo.rebind_manual_decisions_in_transaction(conn,project_id='p',document_id='d',
                previous_context=context('d','e'),current_context=context(),publication_id='published-3')
        self.context=context()
        self.assertEqual(api.get_engineer_responses('p','d',Sink())['records']['u']['state'],'STALE')

    def test_same_baseline_but_changed_decomposition_context_rejects_old_form(self):
        payload=self.payload();self.context=context('d')
        with self.assertRaises(ApplicationError):api.post_engineer_responses('p','d',payload,Sink())
        self.assertIsNone(repo.current_manual_decisions('p','d'))

    def test_unknown_historical_context_marks_existing_user_opinion_stale(self):
        api.post_engineer_responses('p','d',self.payload(),Sink())
        with self.connection() as conn:
            repo.rebind_manual_decisions_in_transaction(conn,project_id='p',document_id='d',previous_context=None,
                current_context=context(),publication_id='migration-unknown')
        self.assertEqual(api.get_engineer_responses('p','d',Sink())['records']['u']['state'],'STALE')


if __name__=='__main__':unittest.main()
