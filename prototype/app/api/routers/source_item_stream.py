"""Source-bound public event stream; task persistence remains framework neutral."""
import asyncio
import json
import time
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.adapters.agents.codex_app_server.public_messages import read_public_messages
from app.application.agent_workflow import source_item_loop_jobs as loop
from app.application.errors import ApplicationError


def read_batch(project_id, document_id, task_id, source_sha256, after, snapshot=False):
    _, root, meta = loop._existing(project_id, document_id, task_id)
    if meta["source_sha256"] != source_sha256:
        raise ApplicationError(status_code=409, detail="任务不属于当前来源版本。")
    response = SimpleNamespace(headers={})
    batch = loop.get_source_item_loop_events(project_id, document_id, task_id, response,
                                             after=after, limit=100, include_message_deltas=True)
    batch.update(task_id=task_id, source_sha256=source_sha256)
    if snapshot:
        batch["messages"] = read_public_messages(root / "runtime/messages.json")
    task = loop.get_source_item_loop(project_id, document_id, task_id, response)
    batch["job_status"] = task["job_status"]
    return batch


def frame(kind, value, cursor=None):
    prefix = f"id: {cursor}\n" if cursor is not None else ""
    return f"{prefix}event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


async def stream_source_item_events(project_id: str, document_id: str, task_id: str,
                                   request: Request, source_sha256: str, after: int = 0):
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except (TypeError, ValueError):
        raise ApplicationError(status_code=422, detail="消息游标无效。")
    initial = await run_in_threadpool(read_batch, project_id, document_id, task_id,
                                      source_sha256, cursor, True)

    async def stream():
        nonlocal cursor
        batch = initial
        heartbeat = time.monotonic()
        while not await request.is_disconnected():
            next_cursor = batch["next_cursor"]
            if next_cursor != cursor or "messages" in batch:
                yield frame("activity", batch, next_cursor)
                heartbeat = time.monotonic()
            if next_cursor == cursor and batch["job_status"] not in loop.ACTIVE:
                yield frame("complete", {"task_id": task_id, "source_sha256": source_sha256}, cursor)
                return
            cursor = next_cursor
            if time.monotonic() - heartbeat >= 10:
                yield ": connected\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(0.05 if len(batch["events"]) == 100 else 0.25)
            try:
                batch = await run_in_threadpool(read_batch, project_id, document_id, task_id,
                                                source_sha256, cursor)
            except ApplicationError:
                yield frame("unavailable", {"message": "消息连接已暂停，请重新同步任务。"})
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
