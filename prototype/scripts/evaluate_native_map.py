#!/usr/bin/env python3
"""Isolated native Map evaluation with explicit environment and timing boundaries.

Uses the production goal builder, tool host and SDK driver. It does not exercise
HTTP upload/queue/DB/UI or give a semantic pass based on the model's final text.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'prototype'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def validate_case(case):
    required = {'case_id', 'source_path', 'source_sha256', 'goal', 'split', 'reviewed_in_development',
                'observation_seconds', 'hard_timeout_seconds', 'model', 'reasoning_effort'}
    if set(case) != required:
        raise ValueError('Case manifest fields must be: ' + ', '.join(sorted(required)))
    if case['split'] not in {'development_regression', 'held_out'}:
        raise ValueError('Unknown evaluation split')
    if type(case['reviewed_in_development']) is not bool:
        raise ValueError('reviewed_in_development must be explicit')
    if case['split'] == 'held_out' and case['reviewed_in_development']:
        raise ValueError('A case used to tune methods cannot be called held_out')
    observations, timeout = case['observation_seconds'], case['hard_timeout_seconds']
    if not isinstance(observations, list) or not observations or any(type(x) is not int or x <= 0 for x in observations):
        raise ValueError('Observation thresholds must be positive seconds')
    if type(timeout) is not int or timeout <= max(observations):
        raise ValueError('Hard timeout must be later than observation thresholds')
    if not isinstance(case['goal'], str) or not case['goal'].strip():
        raise ValueError('Case needs a source-level goal, without supplied semantic answers')


def stage_observations(stages, thresholds, duration):
    """Report observations, not semantic completion or an inferred model limit."""
    return [{'seconds': t, 'run_had_ended': duration <= t,
             'saved_stages': sum(s['since_model_start_seconds'] <= t for s in stages),
             'content_stage_saved': any(s['has_source_content'] and s['since_model_start_seconds'] <= t for s in stages)}
            for t in thresholds]


def verify_bundle_files(bundle, build):
    mismatches = []
    for key, prefix in [('runtime_files', bundle/'runtime/prototype'), ('plugin_files', bundle)]:
        for item in build[key]:
            path = (prefix/item['path']).resolve()
            if not path.is_relative_to(bundle.resolve()) or not path.is_file() or sha(path) != item['sha256']:
                mismatches.append(key + ':' + item['path'])
    if mismatches:
        raise ValueError('Evaluation bundle differs from its build receipt: ' + ', '.join(mismatches))
    return {'runtime_files':len(build['runtime_files']), 'plugin_files':len(build['plugin_files']), 'mismatches':[]}


def snapshot_host_sources(root):
    paths = [Path(__file__).resolve(),
        *[REPO/'prototype/app/application/agent_workflow'/name for name in
          ['native_loop_tools.py','source_document_loop_jobs.py','source_document_stages.py','native_task_brief.py']],
        *[REPO/'prototype/app/adapters/agents/codex_app_server'/name for name in
          ['native_loop_driver.py','native_loop_pdf.py','runtime.py','lifecycle.py',
           'agent_native_read_only_runtime.py','canonical_stage_transport.py','primary_runtime_pdf.py','protocol_lock.json']]]
    result = []
    for path in paths:
        relative=path.relative_to(REPO);destination=root/'host-source-snapshot'/relative
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,destination)
        result.append({'path':str(relative),'sha256':sha(destination)})
    return result


def verified_artifact_handoff(workspace, source_relative, source_sha256, stage_index):
    """Derive usable file references from host pointers, never model-written URLs."""
    workspace=Path(workspace).resolve(strict=True)
    def checked(relative, expected):
        raw=workspace/relative;path=raw.resolve(strict=True)
        if raw.is_symlink() or not path.is_relative_to(workspace) or sha(path)!=expected:
            raise ValueError('Artifact link does not resolve to its pinned workspace file')
        return path
    source=checked(source_relative,source_sha256)
    files=[{'kind':'SOURCE','path':str(source),'sha256':source_sha256}]
    for stage in stage_index.values():
        if stage['source_sha256']!=source_sha256:
            raise ValueError('Stage link belongs to a different source')
        path=checked(stage['path'],stage['file_sha256'])
        files.append({'kind':stage['stage'],'path':str(path),'sha256':stage['file_sha256']})
        review=path.parent/'map-review.json'
        if stage['stage']=='MAP' and review.is_file():
            value=json.loads(review.read_text())
            if review.is_symlink() or value.get('map_sha256')!=stage['artifact_sha256'] or value.get('source_sha256')!=source_sha256:
                raise ValueError('Review link does not match the pinned Map')
            files.append({'kind':'MAP_REVIEW','path':str(review),'sha256':sha(review)})
    return {'schema_version':'reqpilot-evaluation-handoff/v1','producer':'HOST_DERIVED_FILE_REFERENCES',
            'source_sha256':source_sha256,'files':files,'model_message_rewritten':False}


def run(args):
    from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
    from app.application.agent_workflow.source_document_loop_jobs import SourceDocumentToolHost, source_document_goal
    from app.application.agent_workflow.source_document_stages import analysis_policy
    from app.adapters.agents.codex_app_server.native_loop_driver import LocalCodexAppServerNativeLoopDriver
    from app.adapters.agents.codex_app_server.native_loop_pdf import native_loop_skill_paths

    case = json.loads(args.case.read_text()); validate_case(case)
    source = Path(case['source_path']).expanduser().resolve(strict=True)
    if sha(source) != case['source_sha256']: raise ValueError('Case source bytes changed')
    if source.suffix.lower() != '.pdf': raise ValueError('This bounded evaluator currently covers PDF Map work')
    bundle = args.bundle.resolve(strict=True)
    build = json.loads((bundle/'build-receipt.json').read_text())
    bundle_files = verify_bundle_files(bundle, build)
    root = args.output.resolve()
    if root.exists(): raise ValueError('Use a new output directory; do not overwrite or silently rerun a case')
    # The private rubric is retained outside the model-readable workspace.
    rubric = args.rubric.resolve(strict=True) if args.rubric else None
    workspace = root/'workspace'; (workspace/'source').mkdir(parents=True)
    host_sources = snapshot_host_sources(root)
    target = workspace/'source'/source.name; shutil.copyfile(source, target)
    policy = analysis_policy({}, 'SKIP')
    task_input = {'source': {'path': str(target.relative_to(workspace)), 'sha256': case['source_sha256']},
        'knowledge_catalog': 'knowledge/knowledge-catalog.json', 'analysis_policy': policy,
        'require_map_confirmation': True, 'prior_answers_provided': False, 'review_directory': 'review',
        'authority': 'ISOLATED_EVALUATION_SOURCE_NOT_AN_APPLICATION_UPLOAD'}
    save(workspace/'knowledge/knowledge-catalog.json', {'documents': [], 'evidence_catalog': {}})
    save(workspace/'task-input.json', task_input)
    setup = NativeLoopToolHost(workspace=workspace, plugin_root=bundle)
    setup_calls = []
    setup_started = time.monotonic()
    if args.preparation == 'reading_ready':
        for operation, arguments in [
            ('capture', {'source': task_input['source']['path'], 'output_dir': 'capture'}),
            ('reading-workspace', {'source': task_input['source']['path'], 'source_ir': 'capture/source_ir.json', 'output_dir': 'reading'}),
        ]:
            t = time.monotonic(); result = setup.invoke('reqpilot_run', {'operation': operation, 'arguments': arguments})
            setup_calls.append({'operation': operation, 'elapsed_seconds': time.monotonic()-t, 'result': result})
            if not result['success']:
                save(root/'setup.json', setup_calls); raise ValueError('Reading preparation failed before model execution')
    save(root/'setup.json', {'actor': 'EVALUATION_SETUP_NOT_MODEL', 'preparation': args.preparation,
                            'elapsed_seconds': time.monotonic()-setup_started, 'calls': setup_calls})
    meta = {'task_id': 'eval-' + case['case_id'], 'source_path': task_input['source']['path'],
            'source_sha256': case['source_sha256'], 'workflow_version': 1, 'analysis_policy': policy,
            'plugin_bundle_sha256': build['bundle_sha256'], 'request': {'require_map_confirmation': True}}
    host = SourceDocumentToolHost(task_root=root, meta=meta, workspace=workspace, plugin_root=bundle)
    host.set_task_context(goal=case['goal'],origin='USER_REQUEST',mode='INITIAL_SOURCE',timeout_seconds=case['hard_timeout_seconds'])
    brief = host.task_brief()
    goal = source_document_goal(case['goal'], origin='USER_REQUEST', phase='INITIAL_SOURCE', reading_v1=host.reading_v1,brief=brief)
    save(root/'task-brief.json',{'task_brief':brief,'sent_goal':goal})
    code_files = ['native_loop_tools.py', 'source_document_loop_jobs.py']
    environment = {'schema_version': 'reqpilot-native-map-evaluation/v1', 'case': case,
        'preparation': args.preparation, 'case_sha256': sha(args.case), 'rubric_sha256': sha(rubric) if rubric else None,
        'bundle_sha256': build['bundle_sha256'], 'codex_binary_sha256': sha(args.codex_bin),
        'host_code': {n: sha(REPO/'prototype/app/application/agent_workflow'/n) for n in code_files},
        'driver_sha256': sha(REPO/'prototype/app/adapters/agents/codex_app_server/native_loop_driver.py'),
        'protocol_lock_sha256': sha(REPO/'prototype/app/adapters/agents/codex_app_server/protocol_lock.json'),
        'tool_schema_sha256': hashlib.sha256(json.dumps([s.app_server_payload() for s in host.specs()], sort_keys=True).encode()).hexdigest(),
        'goal': goal, 'goal_sha256': hashlib.sha256(goal.encode()).hexdigest(),
        'task_brief': brief,
        'scope': 'SDK_AND_TOOL_HOST_ONLY', 'application_queue_db_ui_tested': False,
        'bundle_files_verified': bundle_files, 'host_source_snapshot': host_sources,
        'model_cache_control': 'NOT_CONTROLLED; use actual receipt token usage',
        'interventions': [], 'rubric_in_model_workspace': False,
        'submitted_utc': datetime.now(timezone.utc).isoformat()}
    save(root/'environment.json', environment)
    driver = LocalCodexAppServerNativeLoopDriver(codex_bin=args.codex_bin, codex_home=args.codex_home,
        repository_root=REPO, model_id=case['model'], reasoning_effort=case['reasoning_effort'])
    started = time.monotonic(); model_started = None; stages = []; seen = set()

    def event_sink(event):
        nonlocal model_started
        if event['type'] == 'turn/started' and model_started is None: model_started = time.monotonic()
        pointer_path = root/'stage-results.json'
        if event['type'] != 'tool_completed' or model_started is None or not pointer_path.is_file(): return
        for pointer in json.loads(pointer_path.read_text()).values():
            key = pointer['artifact_sha256']
            if key in seen: continue
            seen.add(key); artifact = json.loads((workspace/pointer['path']).read_text())
            content = bool(artifact.get('logical_tables')) or any(
                any(not c.get('is_title') and c.get('text','').strip() for c in n.get('source_content',[]))
                or any(a.get('asset_id') for a in n.get('source_anchors',[])) for n in artifact.get('nodes',[]))
            stages.append({**pointer, 'since_model_start_seconds': time.monotonic()-model_started,
                           'since_invocation_seconds': time.monotonic()-started, 'has_source_content': content})
            save(root/'stage-observations.json', stages)
            print(json.dumps({'event':'STAGE_SAVED','stage':pointer['stage'],'count':pointer['count'],
                              'elapsed_seconds':stages[-1]['since_model_start_seconds']},ensure_ascii=False),flush=True)

    failure = None; payload = None; terminal = None
    try:
        result = driver.run(session_dir=root/'runtime', goal=goal, skill_paths=native_loop_skill_paths(bundle, include_pdf=True),
            skill_bundle_root=bundle, tool_host=host, binding={'task_id':meta['task_id'], 'source_sha256':case['source_sha256'],
            'bundle_sha256':build['bundle_sha256'], 'mode':'RAW_SOURCE'}, timeout_seconds=case['hard_timeout_seconds'], event_sink=event_sink)
        payload = {'status':result.status,'final_message':result.final_message,'receipt':result.receipt}
        save(root/'result.json',payload)
    except Exception as exc:
        failure = {'type':type(exc).__name__,'error':str(exc)}; save(root/'failure.json',failure)
        state_path = root/'runtime/run-state.json'
        if state_path.is_file() and json.loads(state_path.read_text()).get('thread_id'):
            try: terminal = driver.inspect_session(session_dir=root/'runtime')
            except Exception as check: terminal = {'inspection_error':str(check)}
            save(root/'terminal-verification.json',terminal)
    duration = time.monotonic()-(model_started or started)
    summary = {'status':payload['status'] if payload else 'TRANSPORT_FAILED', 'failure':failure,
        'model_started':model_started is not None, 'stages':stages,
        'observation_points':stage_observations(stages,case['observation_seconds'],duration),
        'duration_from_model_start_seconds':duration, 'duration_from_invocation_seconds':time.monotonic()-started,
        'preparation':args.preparation, 'semantic_status':'PENDING_SOURCE_REVIEW' if model_started is not None else 'NOT_EVALUATED_NO_MODEL_RUN', 'human_acceptance':'NOT_CLAIMED',
        'host_files_changed_on_disk': [item['path'] for item in host_sources if sha(REPO/item['path']) != item['sha256']],
        'source_unchanged':sha(target)==case['source_sha256'], 'terminal_observation':terminal}
    stage_path=root/'stage-results.json'
    try:
        handoff=verified_artifact_handoff(workspace,task_input['source']['path'],case['source_sha256'],
            json.loads(stage_path.read_text()) if stage_path.is_file() else {})
        save(root/'verified-handoff.json',handoff)
        summary['verified_artifacts']=handoff['files']
    except (ValueError,OSError,KeyError) as exc:
        summary['handoff_error']=str(exc)
    save(root/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return 1 if failure else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--rubric',type=Path)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--preparation',choices=['source_only','reading_ready'],required=True)
    parser.add_argument('--codex-bin',type=Path,required=True)
    parser.add_argument('--codex-home',type=Path,required=True)
    return run(parser.parse_args())


if __name__=='__main__':raise SystemExit(main())
