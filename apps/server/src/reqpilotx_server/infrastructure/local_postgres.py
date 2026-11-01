"""Private PostgreSQL cluster owned by one local desktop launcher.

The data directory is persistent.  A launcher never adopts or stops a process
which it did not start, even if that process uses the expected data directory.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path


class LocalPostgresError(RuntimeError):
    pass


def _binary(directory: Path, name: str) -> Path:
    return directory / (name + (".exe" if sys.platform == "win32" else ""))


def resolve_postgres_bin(*, packaged_root: Path | None = None) -> Path:
    configured = os.environ.get("REQPILOT_DESKTOP_POSTGRES_BIN", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve(strict=True)
    elif packaged_root is not None:
        root = Path(packaged_root) / "postgres" / "bin"
    else:
        located = shutil.which("initdb")
        if not located:
            raise LocalPostgresError("Local PostgreSQL binaries are unavailable.")
        root = Path(located).resolve().parent
    if not root.is_dir() or any(
        not _binary(root, name).is_file()
        for name in ("initdb", "pg_ctl", "postgres", "psql", "createdb")
    ):
        raise LocalPostgresError("A complete local PostgreSQL bin directory is required.")
    return root


def _private_file(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class ManagedPostgres:
    def __init__(self, storage_root: Path, bin_dir: Path) -> None:
        self.root = Path(storage_root) / "managed-postgres"
        self.data = self.root / "data"
        self.bin_dir = Path(bin_dir)
        self.port = 0
        self.pid = 0
        self._started = False
        self.database_url = ""

    def _db_env(self, password: str, port: int) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "PGHOST": "127.0.0.1", "PGPORT": str(port),
            "PGUSER": "reqpilotx", "PGPASSWORD": password,
            "PGDATABASE": "postgres", "PGCONNECT_TIMEOUT": "2",
        })
        return env

    def _postmaster_identity(self) -> tuple[int, str]:
        try:
            lines = (self.data / "postmaster.pid").read_text(encoding="ascii").splitlines()
            return int(lines[0]), lines[2]
        except (OSError, ValueError, IndexError) as exc:
            raise LocalPostgresError("PostgreSQL process identity is unavailable.") from exc

    def _write_receipt(self) -> None:
        pid, started_at = self._postmaster_identity()
        receipt = {"pid": pid, "started_at": started_at, "port": self.port}
        target = self.root / "runtime.json"
        temporary = self.root / f".runtime.{uuid.uuid4().hex}.tmp"
        _private_file(temporary, json.dumps(receipt))
        os.replace(temporary, target)

    def _verify_data_connection(self, password: str, port: int) -> dict[str, str]:
        env = self._db_env(password, port)
        reported = self._command(
            "psql", "-X", "-A", "-t", "-c", "SHOW data_directory", env=env,
        ).strip()
        if Path(reported).resolve() != self.data.resolve():
            raise LocalPostgresError("PostgreSQL data directory identity mismatch.")
        return env

    def _recover_orphan(self, password: str) -> None:
        receipt_path = self.root / "runtime.json"
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise LocalPostgresError("Live PostgreSQL has no trusted desktop receipt.")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            pid, started_at = self._postmaster_identity()
            port = int(receipt["port"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LocalPostgresError("Live PostgreSQL receipt is invalid.") from exc
        if (
            receipt.get("pid") != pid
            or receipt.get("started_at") != started_at
            or not 1 <= port <= 65535
        ):
            raise LocalPostgresError("Live PostgreSQL identity differs from desktop receipt.")
        self._verify_data_connection(password, port)
        # Workers from an abruptly killed launcher have an eight-second owner
        # heartbeat deadline.  Let them exit before stopping their database.
        heartbeat = self.root.parent / "desktop-worker-heartbeat.json"
        deadline = time.monotonic() + 12
        while heartbeat.exists():
            try:
                age = time.time() - heartbeat.stat().st_mtime
            except OSError:
                break
            if age > 9:
                break
            if time.monotonic() >= deadline or age < -2:
                raise LocalPostgresError("Previous desktop workers have not drained.")
            time.sleep(0.25)
        self._command("pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "-t", "20", "stop")
        receipt_path.unlink()

    def _command(self, name: str, *arguments: str, env: dict[str, str] | None = None) -> str:
        process_env = dict(os.environ if env is None else env)
        if sys.platform != "win32":
            process_env.setdefault("LC_ALL", "C")
        result = subprocess.run(
            [str(_binary(self.bin_dir, name)), *arguments],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=35,
            check=False,
        )
        if result.returncode:
            # PostgreSQL stderr can include paths or connection parameters.
            raise LocalPostgresError(
                f"Local PostgreSQL {name} failed (exit {result.returncode})."
            )
        return result.stdout

    def _prepare_root(self) -> str:
        if self.root.is_symlink() or self.data.is_symlink():
            raise LocalPostgresError("Managed PostgreSQL directory cannot be a symlink.")
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
        marker = self.root / "owner.json"
        if not marker.exists():
            if any(self.root.iterdir()):
                raise LocalPostgresError("Unknown PostgreSQL data directory was not adopted.")
            _private_file(marker, json.dumps({"application": "reqpilotx-desktop", "version": 1}))
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LocalPostgresError("Managed PostgreSQL ownership marker is invalid.") from exc
        if owner != {"application": "reqpilotx-desktop", "version": 1}:
            raise LocalPostgresError("Managed PostgreSQL ownership marker does not match.")
        password_path = self.root / "password"
        if password_path.is_symlink():
            raise LocalPostgresError("Managed PostgreSQL password file cannot be a symlink.")
        if not password_path.exists():
            _private_file(password_path, secrets.token_hex(32) + "\n")
        password = password_path.read_text(encoding="ascii").strip()
        if len(password) != 64 or any(ch not in "0123456789abcdef" for ch in password):
            raise LocalPostgresError("Managed PostgreSQL credential file is invalid.")
        if not (self.data / "PG_VERSION").exists():
            if self.data.exists() and any(self.data.iterdir()):
                raise LocalPostgresError("Incomplete PostgreSQL initialization needs repair.")
            self._command(
                "initdb", "-D", str(self.data), "--username=reqpilotx",
                "--auth-local=trust", "--auth-host=scram-sha-256",
                "--encoding=UTF8", "--locale=C", f"--pwfile={password_path}",
            )
        return password

    def start(self) -> str:
        if self._started:
            return self.database_url
        password = self._prepare_root()
        status = subprocess.run(
            [str(_binary(self.bin_dir, "pg_ctl")), "-D", str(self.data), "status"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
        if status.returncode == 0:
            self._recover_orphan(password)
        elif status.returncode != 3:
            raise LocalPostgresError("Managed PostgreSQL status could not be verified.")
        self.port = _free_loopback_port()
        options = f"-h 127.0.0.1 -p {self.port}"
        if sys.platform != "win32":
            options += " -k ''"
        try:
            self._command(
                "pg_ctl", "-D", str(self.data), "-l", str(self.root / "postgres.log"),
                "-o", options, "-w", "-t", "20", "start",
            )
            self.pid, _ = self._postmaster_identity()
            self._started = True
            db_env = self._verify_data_connection(password, self.port)
            exists = self._command(
                "psql", "-X", "-A", "-t", "-c",
                "SELECT 1 FROM pg_database WHERE datname='reqpilotx'", env=db_env,
            ).strip()
            if exists != "1":
                self._command("createdb", "reqpilotx", env=db_env)
            self.database_url = (
                f"postgresql://reqpilotx:{password}@127.0.0.1:{self.port}/reqpilotx"
            )
            self._write_receipt()
            return self.database_url
        except Exception:
            self.stop()
            raise

    def alive(self) -> bool:
        if not self._started:
            return False
        try:
            actual_pid, _ = self._postmaster_identity()
        except LocalPostgresError:
            return False
        if actual_pid != self.pid:
            return False
        result = subprocess.run(
            [str(_binary(self.bin_dir, "pg_ctl")), "-D", str(self.data), "status"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
        return result.returncode == 0

    def stop(self) -> None:
        if not self._started:
            return
        if self.alive():
            self._command("pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "-t", "20", "stop")
        elif (self.data / "postmaster.pid").exists():
            raise LocalPostgresError("PostgreSQL ownership changed; refusing to stop it.")
        (self.root / "runtime.json").unlink(missing_ok=True)
        self._started = False
        self.database_url = ""
