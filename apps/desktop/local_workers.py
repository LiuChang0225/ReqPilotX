"""Launcher-owned durable worker processes for a local PostgreSQL runtime."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib import request as urllib_request


class LocalWorkerError(RuntimeError):
    pass


class LocalWorkerPool:
    def __init__(self, *, storage_root: Path, api_port: int, count: int, instance_id: str) -> None:
        if count not in (1, 2):
            raise LocalWorkerError("Local worker count must be one or two.")
        self.storage_root = Path(storage_root)
        self.api_port = int(api_port)
        self.count = count
        self.instance_id = instance_id
        self.processes: dict[int, subprocess.Popen[bytes]] = {}
        self._stopping = False
        self.heartbeat_path = self.storage_root / "desktop-worker-heartbeat.json"
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._restart_times: dict[int, list[float]] = {}

    def _write_heartbeat(self, *, stopping: bool = False) -> None:
        temporary = self.storage_root / f".worker-heartbeat.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "instance_id": self.instance_id,
                "updated_at": time.time(),
                "stopping": stopping,
            }, stream)
        os.replace(temporary, self.heartbeat_path)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(1.0):
            try:
                self._write_heartbeat()
            except OSError:
                # Children stop themselves when this heartbeat is stale.
                return

    def _environment(self, slot: int) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "REQPILOT_PROCESS_ROLE": "worker",
            "REQPILOT_LOCAL_WORKER_SLOT": str(slot),
            "REQPILOT_LOCAL_WORKER_COUNT": str(self.count),
            "REQPILOT_LOCAL_RUNTIME_INSTANCE_ID": self.instance_id,
            "REQPILOT_ENV": "local",
            "REQPILOT_DESKTOP_PARENT_INSTANCE_ID": self.instance_id,
            "REQPILOT_DESKTOP_HEARTBEAT_PATH": str(self.heartbeat_path),
        })
        if getattr(sys, "frozen", False):
            env["REQPILOT_FROZEN_PYTHON_CHILD"] = "1"
        return env

    def _spawn(self, slot: int) -> None:
        args = [sys.executable, "-m", "local_worker_entry"]
        if slot == 2:
            args.extend(("--accepted-kind", "document.extract", "--claim-profile", "local-extraction-no-llm"))
        flags = 0x08000000 if sys.platform == "win32" else 0
        self.processes[slot] = subprocess.Popen(
            args,
            cwd=str(self.storage_root),
            env=self._environment(slot),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def start(self, *, timeout_seconds: float = 25.0) -> None:
        self._write_heartbeat()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        for slot in range(1, self.count + 1):
            self._spawn(slot)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in self.processes.values()):
                raise LocalWorkerError("A durable worker exited before readiness.")
            try:
                with urllib_request.urlopen(
                    f"http://127.0.0.1:{self.api_port}/api/runtime/worker-fleet",
                    timeout=1,
                ) as response:
                    fleet = json.loads(response.read(64 * 1024))
            except Exception:
                fleet = {}
            workers = fleet.get("workers") if isinstance(fleet, dict) else None
            slots = {
                str(worker.get("slot")) for worker in (workers or ())
                if isinstance(worker, dict)
                and worker.get("fresh") is True
                and worker.get("runtime_instance_match") is True
                and worker.get("source_revision_match") is True
            }
            if fleet.get("state") == "READY" and slots.issuperset(
                str(slot) for slot in range(1, self.count + 1)
            ):
                return
            time.sleep(0.2)
        raise LocalWorkerError("Durable workers did not acknowledge readiness.")

    def check(self) -> bool:
        return not self._stopping and all(
            process.poll() is None for process in self.processes.values()
        )

    def restart_exited(self) -> tuple[int, ...]:
        if self._stopping:
            return ()
        restarted: list[int] = []
        now = time.monotonic()
        for slot, process in tuple(self.processes.items()):
            if process.poll() is None:
                continue
            recent = [when for when in self._restart_times.get(slot, ()) if now - when < 60]
            if len(recent) >= 3:
                raise LocalWorkerError("A durable worker repeatedly exited.")
            self._restart_times[slot] = [*recent, now]
            self._spawn(slot)
            restarted.append(slot)
        return tuple(restarted)

    def stop(self) -> None:
        self._stopping = True
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
        if self.processes:
            try:
                self._write_heartbeat(stopping=True)
            except OSError:
                pass
        for process in self.processes.values():
            if process.poll() is None and sys.platform != "win32":
                try:
                    process.send_signal(signal.SIGTERM)
                except OSError:
                    pass
        for process in self.processes.values():
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.processes.clear()
        try:
            payload = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
            if payload.get("instance_id") == self.instance_id:
                self.heartbeat_path.unlink()
        except (OSError, ValueError):
            pass
