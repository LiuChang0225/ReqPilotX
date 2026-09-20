"""Local, disposable evidence-review surface for target-state experiments.

It reads an immutable, Agent-authored bundle. It neither extracts requirements nor
calls an LLM. Review events are append-only and bound to the source/result version.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
from threading import RLock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

STATIC = Path(__file__).parent / "static"
CLASSES = {
    "F",
    "Q",
    "C",
    "M",
    "CONTEXT",
    "DEFINITION",
    "REFERENCE",
    "NOTE",
    "METADATA",
    "REVISION_HISTORY",
    "EXAMPLE",
    "UNRESOLVED",
}
MAX_BODY = 32_768


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


class ReviewStore:
    def __init__(
        self,
        run_dir: Path,
        review_dir: Path | None = None,
        reviewer_kind: str = "engineer",
    ):
        self.run_dir = run_dir.resolve(strict=True)
        bundle_path = self.run_dir / "review-bundle.json"
        self.bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        if self.bundle.get("schema") != "reqpilot-target-review/v1":
            raise ValueError(
                "Unsupported review bundle; not a production extraction contract"
            )
        self.bundle_bytes = canonical(self.bundle)
        self.source_revision = self.bundle["revision"]
        proof = dict(self.bundle)
        proof.pop("revision")
        if hashlib.sha256(canonical(proof)).hexdigest() != self.source_revision:
            raise ValueError("Review bundle digest does not match its revision")
        if (
            hashlib.sha256((self.run_dir / "source.pdf").read_bytes()).hexdigest()
            != self.bundle["source"]["sha256"]
        ):
            raise ValueError("Source PDF digest does not match the review bundle")
        self.targets = {}
        for item in self.bundle["items"]:
            self.targets[item["id"]] = ("source", item)
            for derived in item.get("derived", []):
                if derived["id"] in self.targets:
                    raise ValueError("Duplicate review target identity")
                self.targets[derived["id"]] = ("derived", derived)
        expected = len(self.bundle["items"]) + sum(
            len(it.get("derived", [])) for it in self.bundle["items"]
        )
        if len(self.targets) != expected:
            raise ValueError("Duplicate source item identity")
        self._initialize_feedback(review_dir, reviewer_kind)

    def _initialize_feedback(self, review_dir, reviewer_kind):
        """Shared append-only feedback storage for legacy and native candidates."""
        self.review_dir = (review_dir or self.run_dir / "feedback").resolve()
        self.review_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.review_dir / "reviews.jsonl"
        self.lock = RLock()
        self.csrf_token = secrets.token_urlsafe(32)
        if reviewer_kind not in {"engineer", "qa"}:
            raise ValueError("Invalid reviewer kind")
        self.reviewer_kind = reviewer_kind
        self.events = []
        self.write_failed = False
        if self.path.exists():
            # A damaged/truncated log is never silently reset or partially accepted.
            for number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                event = json.loads(line)
                if (
                    event.get("revision") != number
                    or event.get("sourceRevision") != self.source_revision
                ):
                    raise ValueError(
                        "Review event log version/order mismatch; preserve it for recovery"
                    )
                if (
                    event.get("sourceSha256") != self.bundle["source"]["sha256"]
                    or event.get("targetId") not in self.targets
                ):
                    raise ValueError(
                        "Review log references a different source or unknown target"
                    )
                self.events.append(event)

    def state(self, include_token: bool = True):
        with self.lock:
            undone = {e["undoOf"] for e in self.events if e["action"] == "undo"}
            targets = {}
            for event in self.events:
                if event["action"] == "undo" or event["id"] in undone:
                    continue
                target = targets.setdefault(
                    event["targetId"],
                    {
                        "status": "unreviewed",
                        "patch": {},
                        "notes": [],
                        "lastEventId": None,
                    },
                )
                target["lastEventId"] = event["id"]
                target["actor"] = event["actor"]
                if event["action"] == "confirm":
                    target["status"] = "reviewed"
                elif event["action"] == "correct":
                    target["status"] = "changed"
                    target["patch"].update(event["patch"])
                elif event["action"] == "comment":
                    target["status"] = "needs-review"
                if event["note"]:
                    target["notes"].append(
                        {
                            "eventId": event["id"],
                            "text": event["note"],
                            "at": event["at"],
                            "action": event["action"],
                        }
                    )
            result = {
                "revision": len(self.events),
                "sourceRevision": self.source_revision,
                "targets": targets,
                "reviewerKind": self.reviewer_kind,
                "eventCount": len(self.events),
            }
            if include_token:
                result["writeToken"] = self.csrf_token
            return result

    def append(self, payload):
        with self.lock:
            if self.write_failed:
                raise ReviewError(
                    "上次磁盘写入结果不确定，已停止后续写入；请重启核对服务检查现有日志，不要反复提交。",
                    503,
                )
            if not isinstance(payload, dict) or set(payload) - {
                "mutationId",
                "expectedRevision",
                "sourceRevision",
                "targetId",
                "action",
                "note",
                "patch",
                "undoOf",
            }:
                raise ReviewError("反馈格式不支持；请刷新页面后重试。")
            mutation_id = payload.get("mutationId")
            if not isinstance(mutation_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9-]{16,80}", mutation_id
            ):
                raise ReviewError("反馈请求缺少有效身份。")
            digest = hashlib.sha256(canonical(payload)).hexdigest()
            for event in self.events:
                if event["id"] == mutation_id:
                    if event["requestDigest"] != digest:
                        raise ReviewError("同一反馈请求标识不能用于不同内容。", 409)
                    return {
                        "receipt": event["id"],
                        "idempotent": True,
                        "state": self.state(),
                    }
            if payload.get("sourceRevision") != self.source_revision:
                raise ReviewError(
                    "分析结果版本已变化；草稿未写入，请重新载入后核对。", 409
                )
            if type(payload.get("expectedRevision")) is not int or payload[
                "expectedRevision"
            ] != len(self.events):
                raise ReviewError(
                    "其它窗口已修改反馈；请载入最新状态后再保存，当前草稿仍保留。", 409
                )
            target_id = payload.get("targetId")
            if not isinstance(target_id, str) or target_id not in self.targets:
                raise ReviewError("反馈目标不存在；没有写入其它条目。")
            action = payload.get("action")
            if action not in {"confirm", "comment", "correct", "undo"}:
                raise ReviewError("不支持此反馈动作。")
            note = payload.get("note", "")
            if not isinstance(note, str) or len(note) > 8_000:
                raise ReviewError("反馈说明须为文字，最多8,000字符。")
            note = note.strip()
            patch = payload.get("patch", {})
            if not isinstance(patch, dict) or set(patch) - {"class", "text"}:
                raise ReviewError("只能修改分类或派生需求，原文证据不可覆盖。")
            if "class" in patch and (
                not isinstance(patch["class"], str) or patch["class"] not in CLASSES
            ):
                raise ReviewError("分类无效。")
            if "text" in patch:
                if self.targets[target_id][0] != "derived":
                    raise ReviewError(
                        "客户原文不可修改；请用问题记录提出边界/转写修正。"
                    )
                if (
                    not isinstance(patch["text"], str)
                    or not patch["text"].strip()
                    or len(patch["text"]) > 12_000
                ):
                    raise ReviewError("派生内容不能为空且最多12,000字符。")
                patch = {**patch, "text": patch["text"].strip()}
            if action == "correct" and (not patch or not note):
                raise ReviewError("记录修改须包含改动和修改原因。")
            if action == "comment" and not note:
                raise ReviewError("请填写需要核对的问题。")
            if action != "correct" and patch:
                raise ReviewError("此动作不能夹带内容修改。")
            undo_of = payload.get("undoOf")
            if action == "undo":
                latest = self.state()["targets"].get(target_id, {}).get("lastEventId")
                if not latest or undo_of != latest:
                    raise ReviewError("只能撤销本条当前最近一次有效反馈。", 409)
            elif undo_of is not None:
                raise ReviewError("非撤销动作不能引用被撤销记录。")
            event = {
                "id": mutation_id,
                "revision": len(self.events) + 1,
                "sourceRevision": self.source_revision,
                "sourceSha256": self.bundle["source"]["sha256"],
                "targetId": target_id,
                "action": action,
                "patch": patch,
                "note": note,
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": {"kind": self.reviewer_kind},
                "requestDigest": digest,
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
                        raise OSError("No progress writing review log")
                    view = view[written:]
                os.fsync(fd)
            except OSError:
                # Some or all bytes may already be durable. Do not append a retry
                # in this process; a restart validates/replays the existing log.
                self.write_failed = True
                raise
            finally:
                os.close(fd)
            self.events.append(event)
            return {"receipt": event["id"], "idempotent": False, "state": self.state()}


def make_server(store: ReviewStore, port: int = 8770, frontend_dir: Path | None = None):
    # A CLI loaded as __main__ and an imported native adapter can otherwise hold
    # distinct copies of the exception class. Preserve domain HTTP errors in both.
    review_error_types = (ReviewError, getattr(store, "review_error_type", ReviewError))
    if frontend_dir:
        frontend_dir = frontend_dir.resolve(strict=True)
        index_html = (frontend_dir / "index.html").read_text(encoding="utf-8")
        inline_scripts = re.findall(r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", index_html, re.S | re.I)
        script_hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'" for script in inline_scripts)
        try:
            from . import react_contract
        except ImportError:
            import react_contract

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReqPilotLocalReview/1"

        def log_message(self, fmt, *args):
            # Do not echo document content, query strings, tokens, or feedback.
            if args and str(args[-1]).isdigit() and int(args[-1]) >= 400:
                print(f"review-http status={args[-1]}", flush=True)

        def _host_valid(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def _send(
            self,
            code,
            body,
            content_type="application/json; charset=utf-8",
            download=None,
        ):
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
                ("default-src 'self'; script-src 'self' " + script_hashes + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'" if frontend_dir else "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
            )
            if download:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{download}"'
                )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_valid():
                return self._send(403, {"error": "仅允许本机访问。"})
            path = urlsplit(self.path).path
            if frontend_dir:
                if path == react_contract.prefix(store) + "/extraction/source-crop":
                    try:
                        query = parse_qs(urlsplit(self.path).query)
                        if query["source_sha256"] != [store.bundle["source"]["sha256"]]:
                            raise ValueError("Source mismatch")
                        body = react_contract.source_crop(store, int(query["page"][0]),
                            [float(query[key][0]) for key in ("x0", "y0", "x1", "y1")])
                    except (ValueError, KeyError, IndexError):
                        return self._send(400, {"error": "原文图片范围无效。"})
                    return self._send(200, body, "image/png")
                try:
                    payload = react_contract.api_payload(store, path)
                except (ValueError, KeyError):
                    return self._send(400, {"error": "核对资源范围无效。"})
                if payload is not None:
                    return self._send(200, payload)
                if path == react_contract.prefix(store) + "/file":
                    return self._send(200, (store.run_dir / "source.pdf").read_bytes(), "application/pdf")
                if path in {"/", "/workspace", "/pdf-viewer", "/home"} or path.startswith("/assets/"):
                    relative = "index.html" if path in {"/", "/workspace", "/pdf-viewer", "/home"} else path.lstrip("/")
                    asset = (frontend_dir / relative).resolve()
                    if not asset.is_relative_to(frontend_dir) or not asset.is_file():
                        return self._send(404, {"error": "主应用资源不存在，请先构建前端。"})
                    mime = "text/javascript" if asset.suffix == ".js" else (mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                    return self._send(200, asset.read_bytes(), mime)
            if path == "/api/bundle":
                return self._send(200, store.bundle_bytes)
            if path == "/api/review-state":
                return self._send(200, store.state())
            if path == "/api/export":
                with store.lock:
                    return self._send(
                        200,
                        {
                            "source": store.bundle["source"],
                            "sourceRevision": store.source_revision,
                            "state": store.state(False),
                            "events": store.events,
                        },
                        download="reqpilot-review-feedback.json",
                    )
            static_paths = {
                "/": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
                "/review-core.js": "review-core.js",
            }
            if path in static_paths:
                file = STATIC / static_paths[path]
            elif path == "/source.pdf":
                file = store.run_dir / "source.pdf"
            elif re.fullmatch(r"/pages/p-\d{2,4}\.png", path):
                file = store.run_dir / path.removeprefix("/")
            else:
                return self._send(404, {"error": "资源不存在。"})
            if not file.is_file() or (
                file not in [STATIC / name for name in static_paths.values()]
                and not file.resolve().is_relative_to(store.run_dir)
            ):
                return self._send(404, {"error": "原始证据资源缺失，未使用替代内容。"})
            content_type = (
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            )
            if file.suffix == ".js":
                content_type = "text/javascript"
            return self._send(200, file.read_bytes(), content_type)

        def do_POST(self):
            if not self._host_valid():
                return self._send(403, {"error": "仅允许本机访问。"})
            is_export = bool(frontend_dir and urlsplit(self.path).path == "/api/candidate-review/export")
            if urlsplit(self.path).path != "/api/review" and not is_export:
                return self._send(404, {"error": "操作不存在。"})
            origin = self.headers.get("Origin")
            if (
                origin != f"http://{self.headers.get('Host')}"
                or not secrets.compare_digest(
                    self.headers.get("X-Review-Token", ""), store.csrf_token
                )
            ):
                return self._send(
                    403,
                    {
                        "error": "核对会话版本已变化或请求来源无效；请重新载入本机页面，未保存草稿仍保留。"
                    },
                )
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "只接受JSON反馈。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ReviewError("反馈请求为空或过长。", 413)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if is_export:
                    result = react_contract.export_xlsx(store, payload)
                    return self._send(200, result, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", download="reqpilotx-review.xlsx")
                result = store.append(payload)
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "反馈请求无法读取；原反馈未覆盖。"})
            except review_error_types as exc:
                return self._send(exc.status, {"error": str(exc)})
            except OSError:
                return self._send(
                    500,
                    {"error": "本地保存失败；草稿仍保留。请检查磁盘和服务日志后重试。"},
                )
            return self._send(200, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--review-dir",
        type=Path,
        help="Optional separate feedback directory (e.g. isolated UI QA)",
    )
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--frontend-dir", type=Path, help="Serve the existing React application build for candidate review")
    parser.add_argument("--native-source-items", action="store_true", help="Review a pinned native Document Map/SourceItem successor without replacing the current baseline")
    parser.add_argument(
        "--reviewer-kind", choices=["engineer", "qa"], default="engineer"
    )
    args = parser.parse_args()
    if args.native_source_items:
        if not args.frontend_dir:
            parser.error("Native SourceItems require the existing React workbench")
        try:
            from .native_candidate import NativeCandidateStore
        except ImportError:
            from native_candidate import NativeCandidateStore
        store = NativeCandidateStore(args.run_dir, args.review_dir, args.reviewer_kind)
    else:
        store = ReviewStore(args.run_dir, args.review_dir, args.reviewer_kind)
    server = make_server(store, args.port, args.frontend_dir)
    print(f"Local review: http://127.0.0.1:{server.server_port}/", flush=True)
    print(
        f"Source items: {len(store.bundle['items'])}; acceptance: pending human review; mode: {args.reviewer_kind}",
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
