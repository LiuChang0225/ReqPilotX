#!/usr/bin/env python3
"""Revalidate a stopped original-document task after a Codex binary update.

Uses live application state plus task/session locks. Does not start a model,
change source/confirmation, clear historical failures, or replace the skill pack.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO/'prototype'))


def main():
    from app.application.agent_workflow.source_item_loop_jobs import _locked
    from app.application.agent_workflow.source_document_loop_jobs import SCHEMA
    from app.adapters.agents.codex_app_server.native_loop_driver import LocalCodexAppServerNativeLoopDriver,_digest,_write_json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-root',type=Path,required=True)
    parser.add_argument('--api-url',default='http://127.0.0.1:8000')
    parser.add_argument('--codex-bin',type=Path,required=True)
    parser.add_argument('--codex-home',type=Path,required=True)
    parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    root=args.task_root.resolve(strict=True)
    allowed=(REPO/'prototype/runtime-data/objects/runtime/native-loop-tasks').resolve()
    if not root.is_relative_to(allowed):raise ValueError('Task must belong to this ReqPilotX runtime store')
    parsed=urlsplit(args.api_url)
    if parsed.scheme not in {'http','https'} or parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        raise ValueError('Use the local ReqPilotX application base URL')
    with _locked(root):
        meta=json.loads((root/'task.json').read_text())
        if meta.get('schema_version')!=SCHEMA:raise ValueError('This maintenance entry handles original-document tasks only')
        url=args.api_url.rstrip('/')+'/api/projects/'+quote(meta['project_id'],safe='')+'/sor-documents/'+quote(meta['document_id'],safe='')+'/agent-structure/delivery/extraction-loop/'+quote(meta['task_id'],safe='')
        with urlopen(url,timeout=15) as response:public=json.load(response)
        if public.get('task_id')!=meta['task_id'] or public.get('source_sha256')!=meta['source_sha256']:
            raise ValueError('Live application task/source differs from local metadata')
        if public['job_status'] in {'PENDING','RUNNING','CANCEL_REQUESTED'}:raise ValueError('Task has queued or active work')
        state=json.loads((root/'runtime/run-state.json').read_text())
        if meta.get('capability_epoch') and meta['capability_epoch']!=state.get('capability_epoch'):
            raise ValueError('Complete or resolve the staged capability change first')
        workspace=(root/'workspace').resolve(strict=True)
        source=(workspace/meta['source_path']).resolve(strict=True)
        if not source.is_relative_to(workspace) or hashlib.sha256(source.read_bytes()).hexdigest()!=meta['source_sha256']:
            raise ValueError('Task original no longer matches its source pin')
        files=[root/'task.json',source]
        for name in ['map-confirmation.json','stage-results.json']:
            if (root/name).exists():files.append(root/name)
        if (root/'stage-results.json').exists():
            for stage in json.loads((root/'stage-results.json').read_text()).values():
                path=(workspace/stage['path']).resolve(strict=True)
                if not path.is_relative_to(workspace):raise ValueError('Stage path escaped workspace')
                files.append(path)
        fingerprints=lambda:{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        before=fingerprints()
        driver=LocalCodexAppServerNativeLoopDriver(codex_bin=args.codex_bin,codex_home=args.codex_home,repository_root=REPO,
            model_id=state['identity']['model_id'],reasoning_effort=state['identity']['reasoning_effort'])
        result=driver.revalidate_runtime_identity(session_dir=root/'runtime',expected_identity_sha256=_digest(state['identity']),reason=args.reason)
        if before!=fingerprints():raise ValueError('Business files changed during runtime maintenance; inspect before continuing')
        record={'runtime':result,'task_id':meta['task_id'],'business_files':before,'business_files_unchanged':True,'customer_model_resumed':False}
        path=root/'runtime-maintenance'/(_digest(record)+'.json')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        _write_json(path,record)
    print(json.dumps({'status':result['status'],'task_id':meta['task_id'],'thread_id':result['thread_id'],
                      'business_files_unchanged':True,'customer_model_resumed':False,'receipt':str(path)},ensure_ascii=False))


if __name__=='__main__':main()
