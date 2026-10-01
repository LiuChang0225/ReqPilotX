"""Opt-in portable CLI/MCP -> real HTTP/PostgreSQL synthetic service smoke.

No live model, customer source, browser interaction or release-install claim.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.parse import parse_qs, urlsplit

import smoke_workbench as service

ROOT = service.ROOT
SCRIPTS = ROOT / 'plugins/reqpilot/scripts'


def run(args):
    output = args.output.expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    binaries = service.postgres_tools(args.postgres_bin)
    pg_port, api_port = service.unused_port(), service.unused_port()
    service.require(pg_port != api_port, 'Random ports collided; rerun')
    db_url = f'postgresql://reqpilot_smoke@127.0.0.1:{pg_port}/reqpilot_smoke'
    env = service.isolate(output, db_url)
    env = {key: value for key, value in env.items() if key.upper() not in {'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}}
    # Child Python processes get the same fail-closed loopback-only audit hook.
    audit = output / 'python-audit'
    audit.mkdir(mode=0o700)
    (audit / 'sitecustomize.py').write_text(
        'import sys\n'
        'def guard(event,args):\n'
        ' if event == "socket.connect":\n'
        '  a=args[1]\n'
        '  if not isinstance(a,tuple) or a[0] not in {"127.0.0.1","::1","localhost"}:\n'
        '   raise RuntimeError("Portable synthetic smoke permits loopback only")\n'
        'sys.addaudithook(guard)\n', encoding='utf-8')
    env['PYTHONPATH'] = str(audit) + os.pathsep + env['PYTHONPATH']
    os.environ.clear(); os.environ.update(env)
    sys.path[:0] = [str(SCRIPTS), str(ROOT/'plugins/reqpilot/tests'), str(ROOT/'packages/engine/src'), str(ROOT/'apps/server/src'), str(ROOT/'prototype')]
    sys.addaudithook(service.block_external_connections)
    import httpx
    from mcp import Client, StdioServerParameters
    from task_bridge import TaskBridge
    from portable_fixtures import make_fixture, item_decisions
    from revision_tools import published_workbook_snapshot_sha256
    from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

    base = f'http://127.0.0.1:{api_port}'
    http = httpx.Client(base_url=base, timeout=90, trust_env=False)
    receipt = {'schema_version':'reqpilotx-portable-service-smoke/v1','status':'RUNNING',
               'started_at':datetime.now(timezone.utc).isoformat(), 'output_directory':str(output),
               'model_dispatch':'NOT_PERFORMED','source_kind':'INVENTED_SYNTHETIC_PDF',
               'browser_interaction':'NOT_RUN','requests':[],'mcp_tools':[], 'fixed_cli':[]}
    server = None

    def request(method,path,expected=200,**kwargs):
        result = http.request(method,path,**kwargs)
        receipt['requests'].append({'method':method,'path':path,'status_code':result.status_code,
                                    'response_sha256':service.sha(result.content)})
        service.require(result.status_code == expected, f'{method} {path}: {result.status_code} {result.text[:1200]}')
        return result

    def ready():
        deadline = time.monotonic()+45
        while time.monotonic()<deadline:
            service.require(server is not None and server.poll() is None,'API exited; see api.log')
            try:
                if http.get('/api/projects').status_code == 200:return
            except httpx.TransportError:pass
            time.sleep(.2)
        raise RuntimeError('API readiness deadline; see api.log')

    try:
        receipt['postgres'] = service.start_postgres(output,binaries,pg_port,env)
        server = service.start_server(output,api_port,env); ready()
        source,_,mapping = make_fixture(output/'synthetic-input')
        source_sha = service.sha(source.read_bytes())
        project = request('POST','/api/projects',json={'name':'Portable service synthetic','customer_name':'Invented'}).json()
        documents = f'/api/projects/{project["id"]}/sor-documents'
        with source.open('rb') as stream:
            document = request('POST',documents,files={'file':(source.name,stream,'application/pdf')}).json()
        document_path = documents+'/'+document['id']
        working = output/'portable-working';working.mkdir(mode=0o700)
        bridge = TaskBridge(working,SCRIPTS/'reqpilot.py',project_root=ROOT,source_roots=[output/'synthetic-input'])
        params = StdioServerParameters(command=sys.executable,args=[str(SCRIPTS/'mcp_server.py'),
            '--project-root',str(ROOT),'--working-root',str(working),'--source-root',str(output/'synthetic-input')],env=env)

        async def exercise():
            async with Client(params) as client:
                tools = {tool.name:tool for tool in (await client.list_tools()).tools}
                service.require(tools['workbench_delivery_read'].annotations.read_only_hint is True,'Delivery tool must remain readonly')
                service.require(not any('engineer_response' in name or 'accept' in name for name in tools),'MCP advertises a human-authoring tool')
                async def call(name,payload):
                    result = await client.call_tool(name,payload)
                    service.require(not result.is_error, f'{name}: {result}')
                    value=result.structured_content
                    receipt['mcp_tools'].append({'name':name,'status':value.get('status'),'revision':value.get('revision')})
                    return value
                state = await call('task_open',{'mode':'create','source':str(source),'goal':'Synthetic portable service lifecycle',
                    'creation_key':'portable-service','require_map_confirmation':False,
                    'workbench_url':base+f'/workspace?projectId={project["id"]}&documentId={document["id"]}'})
                task_id = state['task_id']
                state = await call('source_prepare',{'command_id':'capture-once'})
                replay = await call('source_prepare',{'command_id':'capture-once'})
                service.require(replay['replayed'] and replay['revision']==state['revision'],'Capture retry did not replay')
                page = await call('source_read',{'view':'page','page':1,'max_chars':2048})
                state['revision']=page['revision']
                inspected = bridge.invoke('inspect',{'task_id':task_id})
                service.require(inspected['revision']==state['revision'],'Fixed CLI and MCP observed different tasks')
                receipt['fixed_cli'].append({'action':'inspect','revision':inspected['revision']})
                edit = {key:value for key,value in mapping.items() if key!='source_sha256'}
                edit['schema_version']='reqpilot-map-edit/v1'
                state = await call('map_apply',{'command_id':'map-once','decisions':edit})
                decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
                for choice in decisions['table_decisions']:
                    for record in choice['records']:
                        for group in record['row_groups']:
                            group.pop('fragment_id');group['fragment_ref']=0
                    for context in choice['context_row_groups']:
                        context['row_group'].pop('fragment_id');context['row_group']['fragment_ref']=0
                for key in ('schema_version','source_sha256','document_map_artifact_sha256'):decisions.pop(key,None)
                state = await call('items_apply',{'command_id':'items-once','decisions':decisions})
                service.require(state['result'].get('workbench_sync',{}).get('status')=='WORKBENCH_SYNCHRONIZED',
                                f'Items not actually synchronized: {state}')
                state = await call('review_prepare',{'command_id':'review-once'})
                state = await call('task_deliver',{'command_id':'delivery-once'})
                service.require(state['local_delivery_ready'],'Portable delivery not registered')
                link = state['workbench_link']; query = parse_qs(urlsplit(link['url']).query)
                session = query['pluginSessionId'][0]
                remote = document_path+'/agent-structure/delivery/plugin-workbench/'+session
                current = request('GET',remote).json()
                baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
                first_source = baseline['records'][0]['source_record_id']
                feedback_payload={'command_id':'portable-http-feedback','expected_source_sha256':source_sha,
                    'expected_revision_id':current['revision_id'],'source_item_ids':[first_source],
                    'text':'Retain this exact synthetic original item and its conditions.'}
                request('POST',remote+'/feedback',json=feedback_payload)
                pulled = await call('workbench_sync',{'direction':'pull'})
                repeated = await call('workbench_sync',{'direction':'pull'})
                service.require(len(pulled['sync']['imported_feedback_ids'])==1
                                and repeated['sync']['imported_feedback_ids']==[], 'Feedback import was not exact/idempotent')
                responses = request('GET',remote+'/engineer-responses').json()
                authored = request('POST',remote+'/engineer-responses',json={
                    'command_id':'portable-synthetic-human-opinions','expected_source_sha256':source_sha,
                    'expected_revision_id':responses['revision_id'],'expected_version':responses['version'],
                    'expected_response_revision':responses['response_revision'],'expected_baseline_sha256':responses['baseline_sha256'],
                    'decisions':[{'unit_id':uid,'expected_unit_context_sha256':unit['unit_context_sha256'],
                                  'decision':'COMPLIANT','reason':'Invented synthetic HTTP human opinion'}
                                 for uid,unit in responses['units'].items()]}).json()
                read = await call('workbench_delivery_read',{'view':'responses','result_revision':authored['revision_id']})
                service.require(read['delivery']==authored and not read['local_artifacts_changed'],'MCP response read changed or misbound opinions')
                rejected = await client.call_tool('workbench_delivery_read',{
                    'view':'responses','decision':'COMPLIANT','accept_snapshot':True})
                service.require(rejected.is_error,'Readonly MCP endpoint accepted human-authoring input')
                preview=request('GET',remote+'/versions/preview').json()
                formal=request('POST',remote+'/versions',json={
                    'command_id':'portable-synthetic-formal','expected_source_sha256':source_sha,
                    'expected_revision_id':preview['revision_id'],'expected_version':preview['version'],
                    'expected_response_revision':preview['response_revision'],'expected_delivery_sha256':preview['delivery_sha256'],
                    'mode':'FORMAL','accept_snapshot':True}).json()
                frozen=await call('workbench_delivery_read',{'view':'version','delivery_revision':1})
                service.require(frozen['delivery']['snapshot']==preview['snapshot'],'MCP frozen version snapshot mismatch')
                excel=request('GET',remote+'/versions/1/export').content
                workbook=output/'portable-formal-delivery.xlsx';workbook.write_bytes(excel)
                service.require(service.sha(excel)==formal['export_sha256'],'Frozen Excel hash mismatch')
                service.require(published_workbook_snapshot_sha256(workbook)==canonical_json_sha256(preview['snapshot']),
                                'Frozen Excel embedded snapshot mismatch')
                before_local=bridge.invoke('inspect',{'task_id':task_id})['revision']
                receipt.update(task_id=task_id,session_id=session,source_sha256=source_sha,
                    result_revision_id=authored['revision_id'],source_items=len(baseline['records']),
                    snapshot_sha256=canonical_json_sha256(preview['snapshot']),formal_export_sha256=formal['export_sha256'])
            # A new actual stdio server resumes only the explicit persisted task.
            async with Client(params) as client:
                resumed=await client.call_tool('task_open',{'mode':'resume','task_id':task_id})
                service.require(not resumed.is_error and resumed.structured_content['revision']==before_local,'MCP restart changed local task')
                reread=await client.call_tool('workbench_delivery_read',{'view':'version','delivery_revision':1})
                service.require(not reread.is_error and reread.structured_content['delivery']==frozen['delivery'],
                                'MCP restart changed frozen remote delivery')
            return remote,frozen['delivery'],excel

        remote,frozen,excel=asyncio.run(exercise())
        old_pid=server.pid;service.stop_server(server);server=service.start_server(output,api_port,env);ready()
        service.require(request('GET',remote+'/versions/1').json()==frozen
                        and request('GET',remote+'/versions/1/export').content==excel,'API restart changed plugin formal delivery')
        receipt.update(status='PASS',actual_stdio=True,fixed_cli_subprocess=True,feedback_pull_idempotent=True,
                       human_opinion_transport='READ_ONLY',human_authoring_input_rejected=True,
                       mcp_restart_preserved_task=True,api_restart_preserved_delivery=True,
                       api_restart={'old_pid':old_pid,'new_pid':server.pid},worker_started=False,
                       limits=['Deterministic synthetic judgments; no autonomous/live model acceptance',
                               'Explicit development checkout plugin, not a clean published portable release',
                               'Actual browser/employee host acceptance not run'])
    except Exception as exc:
        receipt.update(status='FAIL',error=f'{type(exc).__name__}: {exc}')
        receipt['failure_traceback'] = ''.join(traceback.format_exception(exc))
        traceback.print_exception(exc)
    finally:
        http.close()
        if server is not None:
            try:service.stop_server(server)
            except Exception as exc:receipt.update(status='FAIL',api_shutdown_error=str(exc))
        try:service.stop_postgres(output,env)
        except Exception as exc:receipt.update(status='FAIL',postgres_shutdown_error=str(exc))
        receipt['process_cleanup'] = {'api_stopped':server is None or server.poll() is not None,
                                      'postgres_stopped':not (output/'postgres-data/postmaster.pid').exists(),
                                      'mcp_contexts_closed':True}
        if not all(receipt['process_cleanup'].values()):
            receipt.update(status='FAIL',cleanup_error='An owned service did not stop')
        receipt['finished_at']=datetime.now(timezone.utc).isoformat()
        service.save(output/'receipt.json',receipt)
        print(json.dumps({key:receipt.get(key) for key in ('status','output_directory','task_id','source_items','error')},indent=2))
    return 0 if receipt['status']=='PASS' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--postgres-bin',type=Path,required=True)
    raise SystemExit(run(parser.parse_args()))
