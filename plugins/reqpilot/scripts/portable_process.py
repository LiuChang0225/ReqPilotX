"""Process isolation and timeout cleanup for the portable task transport."""
from __future__ import annotations

import os
import signal
import subprocess


def isolation_kwargs(*, windows: bool | None = None) -> dict:
    windows = os.name == "nt" if windows is None else windows
    if windows:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def stop_timed_out_process(process, *, force: bool, interrupt: bool = False,
                           windows: bool | None = None) -> None:
    """Stop the fixed CLI and, on Windows escalation, its child process tree."""
    if process.poll() is not None:
        return
    windows = os.name == "nt" if windows is None else windows
    if windows:
        if force:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            try:
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", 1))
            except (OSError, ValueError):
                # A detached host may lack a console; the caller escalates to
                # taskkill /T after its bounded grace period.
                pass
    else:
        try:
            target = signal.SIGKILL if force else signal.SIGINT if interrupt else signal.SIGTERM
            os.killpg(process.pid, target)
        except ProcessLookupError:
            pass
