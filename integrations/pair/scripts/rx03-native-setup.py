"""Prepare one real RX03 task/review before the scripted DSH tool turn."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--mapping', type=Path, required=True)
    parser.add_argument('--scenario', type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.repo / 'plugins/reqpilot/scripts'),
                    str(args.repo / 'plugins/reqpilot/tests'),
                    str(args.repo / 'packages/engine/src'), str(args.repo / 'prototype')]
    from portable_fixtures import item_decisions
    from task_service import TaskService
    from revision_tools import bundle

    context = json.loads((args.task_root / 'task-context.json').read_text())
    task_id = context['work_id']
    service = TaskService(args.workspace, args.bundle / 'scripts/reqpilot.py')
    mapping = json.loads(args.mapping.read_text())

    def state():
        return service.invoke('inspect', {'task_id': task_id})

    def write(action, **extra):
        current = state()
        value = service.invoke(action, {'task_id': task_id, 'expected_revision': current['revision'], **extra})
        if value.get('operation_state') != 'COMMITTED':
            raise SystemExit(json.dumps({'status': 'SETUP_FAILED', 'action': action, 'result': value}))
        return value

    def edit():
        value = {key: deepcopy(item) for key, item in mapping.items() if key != 'source_sha256'}
        value['schema_version'] = 'reqpilot-map-edit/v1'
        return value

    write('prepare', mode='capture')
    wrong = edit()
    for node in wrong['nodes']:
        if node['node_id'] == 's4':
            node['parent_id'] = 's2'
    mapped = write('map', command_id='rx03-native-initial-map', decisions=wrong)
    decisions = item_decisions(Path(mapped['selected']['MAP']['output_dir']))
    for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'):
        decisions.pop(key, None)
    write('items', command_id='rx03-native-items', decisions=decisions)
    reviewed = write('review', command_id='rx03-native-review', analysis_policy='preserve')
    current = state()
    review_root = Path(current['task_root']) / 'review'
    head = json.loads((review_root / 'workspace-manifest.json').read_text())
    baseline = bundle(Path(head['current']['version_path']) if Path(head['current']['version_path']).is_absolute()
                      else review_root / head['current']['version_path'])[1]
    source_item = baseline.records[0].source_display_id
    scenario = {
        'schema': 'reqpilot-rx03-native-scenario-input/v1',
        'task_id': task_id, 'task_root': str(args.task_root), 'workspace': str(args.workspace),
        'source_sha256': context['source']['sha256'], 'revision': current['revision'],
        'review_sha256': head['review_sha256'], 'source_item_id': source_item,
        'map_decisions': edit(),
        'self_review_finding': {
            'text': 'Restore the actual Construction owner after the review packet was read.',
            'source_item_ids': [source_item],
            'rationale': 'Synthetic scripted native review correction for the bounded Map path.'},
        'commands': {'review_apply': 'rx03-native-review-apply', 'deliver': 'rx03-native-deliver'},
    }
    args.scenario.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'READY', 'task_id': task_id, 'revision': current['revision'],
                      'review_sha256': head['review_sha256'], 'scenario': str(args.scenario)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
