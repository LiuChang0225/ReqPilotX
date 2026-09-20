from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Iterable


_WORKER_COMMAND = re.compile(r"(?:^|\s)-m\s+app\.worker(?:\s|$)")
_PORT_MARKER = re.compile(r"(?:^|\s)REQPILOT_LOCAL_RUNTIME_PORT=(\d+)(?:\s|$)")
_SLOT_MARKER = re.compile(r"(?:^|\s)REQPILOT_LOCAL_WORKER_SLOT=(\d+)(?:\s|$)")
_INSTANCE_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_LOCAL_RUNTIME_INSTANCE_ID=([A-Za-z0-9_.:-]+)(?:\s|$)"
)
_REVISION_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_RUNTIME_SOURCE_REVISION=([0-9a-f]{64})(?:\s|$)"
)
_WORKER_COUNT_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_LOCAL_WORKER_COUNT=([12])(?:\s|$)"
)
_CLAIM_PROFILE_ARGUMENT = re.compile(
    r"(?:^|\s)--claim-profile(?:=|\s+)([A-Za-z0-9_.:-]+)(?=\s|$)"
)
_ACCEPTED_KIND_ARGUMENT = re.compile(
    r"(?:^|\s)--accepted-kind(?:=|\s+)([A-Za-z0-9_.:-]+)(?=\s|$)"
)
_PID_FILE = re.compile(r"worker_(\d+)(?:_(\d+))?\.pid$")


@dataclass(frozen=True)
class WorkerProcess:
    pid: int
    command: str
    cwd: Path
    marked_port: int | None
    marked_slot: int | None
    instance_id: str
    source_revision: str
    worker_count: int | None
    claim_profile: str
    accepted_kinds: tuple[str, ...]


@dataclass(frozen=True)
class WorkerPoolStatus:
    compatible: bool
    slot_pids: dict[int, int]
    missing_slots: tuple[int, ...]
    extra_pids: tuple[int, ...]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )


def _process_cwd(pid: int) -> Path | None:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve(strict=True)
    except (OSError, RuntimeError):
        pass

    result = _run("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn")
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if line == "fcwd":
            for candidate in lines[index + 1 :]:
                if candidate.startswith("f"):
                    break
                if candidate.startswith("n") and len(candidate) > 1:
                    try:
                        return Path(candidate[1:]).resolve(strict=True)
                    except (OSError, RuntimeError):
                        return None
    return None


def _process_environment_command(pid: int) -> str:
    result = _run("ps", "eww", "-p", str(pid), "-o", "command=")
    return result.stdout if result.returncode == 0 else ""


def _marked_port(command: str) -> int | None:
    match = _PORT_MARKER.search(command)
    return int(match.group(1)) if match else None


def _marked_slot(command: str) -> int | None:
    match = _SLOT_MARKER.search(command)
    return int(match.group(1)) if match else None


def _marked_value(pattern: re.Pattern[str], command: str) -> str:
    match = pattern.search(command)
    return str(match.group(1)) if match else ""


def _worker_lane(command: str) -> tuple[str, tuple[str, ...]]:
    profile_match = _CLAIM_PROFILE_ARGUMENT.search(command)
    profile = profile_match.group(1) if profile_match else "general"
    accepted_kinds = tuple(sorted(_ACCEPTED_KIND_ARGUMENT.findall(command)))
    return profile, accepted_kinds


def _lane_matches_slot(worker: WorkerProcess, slot: int) -> bool:
    if slot == 1:
        return worker.claim_profile == "general" and not worker.accepted_kinds
    return (
        slot == 2
        and worker.claim_profile == "local-extraction-no-llm"
        and worker.accepted_kinds == ("document.extract",)
    )


def _is_python_process(pid: int) -> bool:
    result = _run("ps", "-p", str(pid), "-o", "comm=")
    if result.returncode != 0:
        return False
    executable = Path(result.stdout.strip()).name.casefold()
    return executable.startswith("python")


def observe_worker(
    pid: int, *, root: Path, command: str | None = None
) -> WorkerProcess | None:
    if pid <= 0:
        return None
    if command is None:
        result = _run("ps", "-ww", "-p", str(pid), "-o", "command=")
        if result.returncode != 0:
            return None
        command = result.stdout.strip()
    if not _WORKER_COMMAND.search(command) or not _is_python_process(pid):
        return None
    cwd = _process_cwd(pid)
    if cwd is None or cwd != root.resolve():
        return None
    environment_command = _process_environment_command(pid)
    claim_profile, accepted_kinds = _worker_lane(command)
    return WorkerProcess(
        pid=pid,
        command=command,
        cwd=cwd,
        marked_port=_marked_port(environment_command),
        marked_slot=_marked_slot(environment_command),
        instance_id=_marked_value(_INSTANCE_MARKER, environment_command),
        source_revision=_marked_value(_REVISION_MARKER, environment_command),
        worker_count=(
            int(value)
            if (value := _marked_value(_WORKER_COUNT_MARKER, environment_command))
            else None
        ),
        claim_profile=claim_profile,
        accepted_kinds=accepted_kinds,
    )


def list_repo_workers(root: Path) -> list[WorkerProcess]:
    result = _run("ps", "-Aww", "-o", "pid=,command=")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "cannot list local processes")
    workers: list[WorkerProcess] = []
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        if not _WORKER_COMMAND.search(command):
            continue
        worker = observe_worker(int(fields[0]), root=root, command=command)
        if worker is not None:
            workers.append(worker)
    return workers


def tracked_worker_ports(root: Path) -> dict[int, set[int]]:
    tracked: dict[int, set[int]] = {}
    for path in (root / "tmp").glob("worker_*.pid"):
        match = _PID_FILE.fullmatch(path.name)
        if match is None:
            continue
        try:
            value = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not value.isdigit() or int(value) <= 0:
            continue
        tracked.setdefault(int(value), set()).add(int(match.group(1)))
    return tracked


def pool_status(
    root: Path,
    port: int,
    *,
    count: int,
    instance_id: str,
    source_revision: str,
) -> WorkerPoolStatus:
    if not 1 <= count <= 2:
        raise ValueError("count must be between 1 and 2")
    target, _protected = workers_for_port(root, port)
    slot_pids: dict[int, int] = {}
    extras: list[int] = []
    for worker in target:
        slot = worker.marked_slot
        if (
            slot is None
            or slot < 1
            or slot > count
            or slot in slot_pids
            or worker.instance_id != instance_id
            or worker.source_revision != source_revision
            or worker.worker_count != count
            or not _lane_matches_slot(worker, slot)
        ):
            extras.append(worker.pid)
            continue
        pid_path = root / "tmp" / f"worker_{port}_{slot}.pid"
        try:
            tracked_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            extras.append(worker.pid)
            continue
        if tracked_pid != worker.pid:
            extras.append(worker.pid)
            continue
        slot_pids[slot] = worker.pid
    missing = tuple(slot for slot in range(1, count + 1) if slot not in slot_pids)
    return WorkerPoolStatus(
        compatible=not extras,
        slot_pids=slot_pids,
        missing_slots=missing,
        extra_pids=tuple(sorted(extras)),
    )


def worker_belongs_to_port(
    worker: WorkerProcess,
    *,
    port: int,
    tracked_ports: dict[int, set[int]],
) -> bool:
    if worker.marked_port is not None:
        return worker.marked_port == port
    ports = tracked_ports.get(worker.pid, set())
    if ports:
        return ports == {port}
    # Legacy workers had no port marker. If no other port tracks one, it is an
    # unsafe orphan in this repository and must not share the durable queue.
    return True


def workers_for_port(
    root: Path, port: int
) -> tuple[list[WorkerProcess], list[WorkerProcess]]:
    tracked_ports = tracked_worker_ports(root)
    target: list[WorkerProcess] = []
    protected: list[WorkerProcess] = []
    for worker in list_repo_workers(root):
        if worker_belongs_to_port(worker, port=port, tracked_ports=tracked_ports):
            target.append(worker)
        else:
            protected.append(worker)
    return target, protected


def _still_target(root: Path, port: int, pid: int) -> bool:
    worker = observe_worker(pid, root=root)
    if worker is None:
        return False
    return worker_belongs_to_port(
        worker,
        port=port,
        tracked_ports=tracked_worker_ports(root),
    )


def stop_workers(
    root: Path,
    port: int,
    *,
    keep_pid: int | None = None,
    keep_pids: Iterable[int] = (),
    timeout_seconds: float = 10.0,
) -> list[int]:
    target, _protected = workers_for_port(root, port)
    preserved = {int(pid) for pid in keep_pids if int(pid) > 0}
    if keep_pid is not None:
        preserved.add(keep_pid)
    stopping = [worker for worker in target if worker.pid not in preserved]
    for worker in stopping:
        print(f"[INFO] stopping local durable worker for port {port}: {worker.pid}")
        try:
            os.kill(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError(f"cannot stop worker {worker.pid}: {exc}") from exc

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    remaining = {worker.pid for worker in stopping}
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _still_target(root, port, pid)}
        if remaining:
            time.sleep(0.1)

    for pid in sorted(remaining):
        if not _still_target(root, port, pid):
            continue
        print(f"[WARN] force stopping local durable worker for port {port}: {pid}")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError(f"cannot force stop worker {pid}: {exc}") from exc
    return [worker.pid for worker in stopping]


def drain_workers(root: Path, port: int) -> list[int]:
    """Ask owned workers to finish their current handler and stop claiming.

    No timeout and no SIGKILL are used here. The app.worker SIGTERM handler
    turns this into a cooperative drain; forceful termination remains an
    explicit normal-stop operation.
    """

    target, _protected = workers_for_port(root, port)
    for worker in target:
        print(f"[INFO] draining local durable worker for port {port}: {worker.pid}")
        try:
            os.kill(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError(f"cannot drain worker {worker.pid}: {exc}") from exc
    return [worker.pid for worker in target]


def wait_for_workers(root: Path, port: int, *, timeout_seconds: float = 0.0) -> bool:
    started = time.monotonic()
    next_report = started
    while True:
        target, _protected = workers_for_port(root, port)
        if not target:
            return True
        now = time.monotonic()
        if timeout_seconds > 0 and now - started >= timeout_seconds:
            return False
        if now >= next_report:
            pids = ",".join(str(worker.pid) for worker in target)
            print(f"[INFO] waiting for drained workers to finish: {pids}")
            next_report = now + 5.0
        time.sleep(0.2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely inspect or stop ReqPilotX local durable workers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("matches", "cleanup", "drain", "wait", "pool-status", "status"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--root", type=Path, required=True)
        subparser.add_argument("--port", type=int, required=True)
        if name == "matches":
            subparser.add_argument("--pid", type=int, required=True)
        if name == "cleanup":
            subparser.add_argument("--keep-pid", type=int, action="append", default=[])
            subparser.add_argument("--timeout-seconds", type=float, default=10.0)
        if name == "wait":
            subparser.add_argument("--timeout-seconds", type=float, default=0.0)
        if name == "pool-status":
            subparser.add_argument("--count", type=int, required=True)
            subparser.add_argument("--instance-id", required=True)
            subparser.add_argument("--source-revision", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve(strict=True)
    if args.port <= 0 or args.port > 65535:
        raise SystemExit("port must be between 1 and 65535")
    if args.command == "matches":
        worker = observe_worker(args.pid, root=root)
        if worker is None:
            return 1
        return (
            0
            if worker_belongs_to_port(
                worker,
                port=args.port,
                tracked_ports=tracked_worker_ports(root),
            )
            else 1
        )
    if args.command == "cleanup":
        stop_workers(
            root,
            args.port,
            keep_pids=args.keep_pid,
            timeout_seconds=args.timeout_seconds,
        )
        return 0
    if args.command == "drain":
        drain_workers(root, args.port)
        return 0
    if args.command == "wait":
        return (
            0
            if wait_for_workers(
                root,
                args.port,
                timeout_seconds=args.timeout_seconds,
            )
            else 3
        )
    if args.command == "pool-status":
        status = pool_status(
            root,
            args.port,
            count=args.count,
            instance_id=args.instance_id,
            source_revision=args.source_revision,
        )
        slots = ",".join(
            f"{slot}:{pid}" for slot, pid in sorted(status.slot_pids.items())
        ) or "none"
        missing = ",".join(str(slot) for slot in status.missing_slots) or "none"
        extras = ",".join(str(pid) for pid in status.extra_pids) or "none"
        print(f"WORKER_POOL_COMPATIBLE: {'yes' if status.compatible else 'no'}")
        print(f"WORKER_POOL_SLOTS: {slots}")
        print(f"WORKER_POOL_MISSING_SLOTS: {missing}")
        print(f"WORKER_POOL_EXTRA_PIDS: {extras}")
        return 0 if status.compatible else 1
    target, protected = workers_for_port(root, args.port)
    target_pids = ",".join(str(worker.pid) for worker in target) or "none"
    protected_pids = ",".join(str(worker.pid) for worker in protected) or "none"
    print(f"WORKER_PROCESSES: {len(target)} ({target_pids})")
    slots = ",".join(
        f"{worker.marked_slot or 'legacy'}:{worker.pid}" for worker in target
    ) or "none"
    print(f"WORKER_SLOTS: {slots}")
    print(f"WORKERS_PROTECTED_FOR_OTHER_PORTS: {protected_pids}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
