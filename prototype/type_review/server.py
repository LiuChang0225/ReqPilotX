"""Local owner-decision surface for the ReqPilotX all-types campaign."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
from threading import RLock
from urllib.parse import urlsplit


STATIC = Path(__file__).parent / "static"
MAX_BODY = 16_384
ACTIONS = {"accept", "adjust", "defer", "comment", "undo"}


class DecisionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class DecisionStore:
    def __init__(
        self,
        campaign_dir: Path,
        review_dir: Path | None = None,
        reviewer_kind: str = "owner",
    ) -> None:
        self.campaign_dir = campaign_dir.resolve(strict=True)
        self.artifact = json.loads(
            (self.campaign_dir / "type-review.json").read_text(encoding="utf-8")
        )
        if self.artifact.get("schema") != "reqpilotx-all-types-owner-review/v1":
            raise ValueError("Unsupported all-types review artifact")
        self.source_revision = self.artifact["revision"]
        proof = dict(self.artifact)
        proof.pop("revision")
        if hashlib.sha256(canonical(proof)).hexdigest() != self.source_revision:
            raise ValueError("All-types artifact digest does not match revision")
        self.targets: dict[str, dict] = {}
        for target in [*self.artifact["decisions"], *self.artifact["types"]]:
            target_id = target["id"]
            if target_id in self.targets:
                raise ValueError("Duplicate decision target")
            self.targets[target_id] = target
        if len(self.targets) != 23:
            raise ValueError("All-types artifact must contain 6 campaign and 17 type targets")
        self.previews: dict[str, Path] = {}
        for item in self.artifact["types"]:
            preview = (self.campaign_dir / item["preview"]).resolve(strict=True)
            if not preview.is_relative_to(self.campaign_dir) or preview.suffix.lower() != ".png":
                raise ValueError("Preview path leaves the campaign or is not a PNG")
            self.previews[item["id"]] = preview
        if reviewer_kind not in {"owner", "qa"}:
            raise ValueError("Invalid reviewer kind")
        self.reviewer_kind = reviewer_kind
        self.review_dir = (review_dir or self.campaign_dir / "owner-feedback").resolve()
        self.review_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.review_dir / "decisions.jsonl"
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = RLock()
        self.write_failed = False
        self.events: list[dict] = []
        if self.path.exists():
            for number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(),
                1,
            ):
                event = json.loads(line)
                if (
                    event.get("revision") != number
                    or event.get("sourceRevision") != self.source_revision
                    or event.get("targetId") not in self.targets
                ):
                    raise ValueError(
                        "Decision log version/order mismatch; preserve it for recovery"
                    )
                self.events.append(event)

    def state(self, include_token: bool = True) -> dict:
        with self.lock:
            undone = {event["undoOf"] for event in self.events if event["action"] == "undo"}
            targets: dict[str, dict] = {}
            for event in self.events:
                if event["action"] == "undo" or event["id"] in undone:
                    continue
                target = targets.setdefault(
                    event["targetId"],
                    {"status": "pending", "notes": [], "lastEventId": None},
                )
                target["lastEventId"] = event["id"]
                target["actor"] = event["actor"]
                target["status"] = {
                    "accept": "accepted",
                    "adjust": "adjustment-needed",
                    "defer": "deferred",
                    "comment": "commented",
                }[event["action"]]
                if event["note"]:
                    target["notes"].append(
                        {
                            "eventId": event["id"],
                            "action": event["action"],
                            "text": event["note"],
                            "at": event["at"],
                        }
                    )
            state = {
                "revision": len(self.events),
                "sourceRevision": self.source_revision,
                "targets": targets,
                "reviewerKind": self.reviewer_kind,
                "eventCount": len(self.events),
            }
            if include_token:
                state["writeToken"] = self.csrf_token
            return state

    def append(self, payload: object) -> dict:
        with self.lock:
            if self.write_failed:
                raise DecisionError(
                    "上次磁盘写入结果不确定，已停止后续写入；请重启服务核对日志。",
                    503,
                )
            if not isinstance(payload, dict) or set(payload) - {
                "mutationId",
                "expectedRevision",
                "sourceRevision",
                "targetId",
                "action",
                "note",
                "undoOf",
            }:
                raise DecisionError("决策格式不支持；请刷新后重试。")
            mutation_id = payload.get("mutationId")
            if not isinstance(mutation_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9-]{16,80}",
                mutation_id,
            ):
                raise DecisionError("决策请求缺少有效身份。")
            request_digest = hashlib.sha256(canonical(payload)).hexdigest()
            for event in self.events:
                if event["id"] == mutation_id:
                    if event["requestDigest"] != request_digest:
                        raise DecisionError("同一请求身份不能用于不同内容。", 409)
                    return {
                        "receipt": mutation_id,
                        "idempotent": True,
                        "state": self.state(),
                    }
            if payload.get("sourceRevision") != self.source_revision:
                raise DecisionError("代理候选版本已经变化；请重新载入后再决定。", 409)
            expected = payload.get("expectedRevision")
            if type(expected) is not int or expected != len(self.events):
                raise DecisionError("其它窗口已更新决定；请载入最新状态。", 409)
            target_id = payload.get("targetId")
            if not isinstance(target_id, str) or target_id not in self.targets:
                raise DecisionError("决策目标不存在。")
            action = payload.get("action")
            if action not in ACTIONS:
                raise DecisionError("不支持此决策动作。")
            note = payload.get("note", "")
            if not isinstance(note, str) or len(note) > 8_000:
                raise DecisionError("说明须为文字，最多8,000字符。")
            note = note.strip()
            if action in {"adjust", "defer", "comment"} and not note:
                raise DecisionError("需要调整、暂缓或记录问题时，请填写具体原因。")
            undo_of = payload.get("undoOf")
            if action == "undo":
                latest = self.state()["targets"].get(target_id, {}).get("lastEventId")
                if not latest or latest != undo_of:
                    raise DecisionError("只能撤销当前对象最近一次有效决定。", 409)
            elif undo_of is not None:
                raise DecisionError("非撤销动作不能引用被撤销记录。")
            event = {
                "id": mutation_id,
                "revision": len(self.events) + 1,
                "sourceRevision": self.source_revision,
                "targetId": target_id,
                "action": action,
                "note": note,
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": {"kind": self.reviewer_kind},
                "requestDigest": request_digest,
            }
            if action == "undo":
                event["undoOf"] = undo_of
            data = canonical(event) + b"\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("No progress writing decision log")
                    view = view[written:]
                os.fsync(fd)
            except OSError:
                self.write_failed = True
                raise
            finally:
                os.close(fd)
            self.events.append(event)
            return {"receipt": event["id"], "idempotent": False, "state": self.state()}


def make_server(store: DecisionStore, port: int = 8780) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReqPilotTypeReview/1"

        def log_message(self, fmt: str, *args: object) -> None:
            if args and str(args[-1]).isdigit() and int(str(args[-1])) >= 400:
                print(f"type-review-http status={args[-1]}", flush=True)

        def _host_valid(self) -> bool:
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def _send(
            self,
            code: int,
            body: object,
            content_type: str = "application/json; charset=utf-8",
            download: str | None = None,
        ) -> None:
            if not isinstance(body, bytes):
                body = canonical(body)
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._host_valid():
                return self._send(403, {"error": "仅允许本机访问。"})
            path = urlsplit(self.path).path
            if path == "/api/campaign":
                return self._send(200, canonical(store.artifact))
            if path == "/api/state":
                return self._send(200, store.state())
            if path == "/api/export":
                return self._send(
                    200,
                    {
                        "sourceRevision": store.source_revision,
                        "state": store.state(False),
                        "events": store.events,
                    },
                    download="reqpilotx-all-types-decisions.json",
                )
            if path == "/report.md":
                report = store.campaign_dir / "REPORT.md"
                return self._send(200, report.read_bytes(), "text/markdown; charset=utf-8")
            match = re.fullmatch(r"/preview/(T\d{2})", path)
            if match:
                preview = store.previews.get(match.group(1))
                if preview and preview.is_file():
                    return self._send(200, preview.read_bytes(), "image/png")
                return self._send(404, {"error": "该类型没有可用预览。"})
            static_paths = {
                "/": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
                "/review-core.js": "review-core.js",
            }
            filename = static_paths.get(path)
            if not filename:
                return self._send(404, {"error": "资源不存在。"})
            file = STATIC / filename
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if file.suffix == ".js":
                content_type = "text/javascript"
            return self._send(200, file.read_bytes(), content_type)

        def do_POST(self) -> None:
            if not self._host_valid():
                return self._send(403, {"error": "仅允许本机访问。"})
            if urlsplit(self.path).path != "/api/decision":
                return self._send(404, {"error": "操作不存在。"})
            origin = self.headers.get("Origin")
            if (
                origin != f"http://{self.headers.get('Host')}"
                or not secrets.compare_digest(
                    self.headers.get("X-Review-Token", ""),
                    store.csrf_token,
                )
            ):
                return self._send(403, {"error": "决策会话无效；请重新载入本机页面。"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "只接受JSON决策。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise DecisionError("决策请求为空或过长。", 413)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = store.append(payload)
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "决策请求无法读取；没有写入。"})
            except DecisionError as error:
                return self._send(error.status, {"error": str(error)})
            except OSError:
                return self._send(500, {"error": "本地保存失败；请检查磁盘后重试。"})
            return self._send(200, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--reviewer-kind", choices=["owner", "qa"], default="owner")
    args = parser.parse_args()
    store = DecisionStore(args.campaign_dir, args.review_dir, args.reviewer_kind)
    server = make_server(store, args.port)
    print(f"All-types review: http://127.0.0.1:{server.server_port}/", flush=True)
    print(
        f"Targets: {len(store.targets)}; source revision: {store.source_revision}; mode: {store.reviewer_kind}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
