"""Read only public tool-call metadata from one explicitly identified CLI session."""
import argparse
import json
from pathlib import Path
import re


def public_tool_records(session_path):
    with Path(session_path).open(encoding='utf-8') as stream:
        for line in stream:
            if 'session_meta' in line or 'tool_call' in line or 'function_call' in line:
                yield json.loads(line)


def summarize(session_path, thread_id):
    calls, replies, actual_thread = [], {}, None
    for event in public_tool_records(session_path):
        item = event.get('payload', {})
        if event.get('type') == 'session_meta':
            actual_thread = item.get('id')
            if actual_thread != thread_id:
                raise ValueError('This session record does not belong to the requested thread')
        kind, name = item.get('type'), item.get('name', '')
        if kind == 'custom_tool_call' and name.rsplit('.',1)[-1] == 'exec' and re.search(r'tools\.view_image\s*\(',item.get('input','')):
            requests = []
            for expression in re.findall(r'tools\.view_image\s*\((\{.*?\})\)',item['input']):
                try: request = json.loads(expression)
                except ValueError:
                    match = re.search(r'''["']?path["']?\s*:\s*(["'])(.*?)\1''',expression)
                    request = {'path':match.group(2)} if match else {'arguments':'DYNAMIC_EXPRESSION_NOT_EVALUATED'}
                requests.append({key:value for key,value in request.items() if key in {'path','detail','arguments'}})
            calls.append({'call_id':item['call_id'],'timestamp':event.get('timestamp'),'via':'exec','requests':requests})
        elif kind == 'function_call' and name.rsplit('.',1)[-1] == 'view_image':
            args = json.loads(item.get('arguments','{}'))
            calls.append({'call_id':item['call_id'],'timestamp':event.get('timestamp'),'via':'direct',
                'requests':[{key:value for key,value in args.items() if key in {'path','detail'}}]})
        elif kind in {'custom_tool_call_output','function_call_output'}:
            output = item.get('output')
            replies[item.get('call_id')] = sum(isinstance(block,dict) and block.get('type') in {'input_image','image'} for block in output) if isinstance(output,list) else 0
    if actual_thread != thread_id:
        raise ValueError('This session record does not belong to the requested thread')
    for call in calls:
        call['tool_reply_present'] = call['call_id'] in replies
        call['returned_image_blocks'] = replies.get(call['call_id'],0)
    return {'schema_version':'reqpilot-plugin-image-views/v1','thread_id':thread_id,'calls':calls,
        'returned_image_blocks':sum(call['returned_image_blocks'] for call in calls),
        'limits':'Actual view-image tool calls and image replies, not a judgment about image meaning or full-document visual coverage. CLI command-item events alone may omit calls inside exec. Dynamic arguments are not executed or guessed.',
        'reasoning_content_retained':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-file',type=Path,required=True)
    parser.add_argument('--thread-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=summarize(args.session_file,args.thread_id)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    args.output.chmod(0o600)
    print(json.dumps({'thread_id':args.thread_id,'returned_image_blocks':result['returned_image_blocks']}))
