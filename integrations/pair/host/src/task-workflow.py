"""Read the bound task's derived workflow state through its fixed TaskService."""
import json
import sys
from pathlib import Path


def main() -> int:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    workspace = Path(sys.argv[2]).resolve(strict=True)
    scripts = workspace / '.reqpilot/scripts'
    sys.path.insert(0, str(scripts))
    import reqpilot
    reqpilot.load_runtime()
    from task_service import TaskService
    service = TaskService(workspace, scripts / 'reqpilot.py')
    print(json.dumps(service.invoke('inspect', {'task_id': payload['task_id']}), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
