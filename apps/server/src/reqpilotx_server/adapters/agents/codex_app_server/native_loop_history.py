"""Explicit adoption of SDK-reconstructed private history, without inventing turns."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

from .native_loop_driver import _session_lock, _write_json


def rebuild_sdk_history_copy(
    *,
    codex_bin: Path,
    original_rollout: Path,
    source_state_sqlite: Path,
    thread_id: str,
    output_dir: Path,
):
    """Invoke the pinned SDK's own legacy-to-paginated migration on a clone.

    Only transport-format metadata is downgraded on the clone. Adoption requires
    that the SDK restore an exactly equivalent paginated payload stream. No
    credentials are copied, no model turn is started, and source files are read-only.
    """
    from .lifecycle import (
        build_lifecycle_command,
        build_lifecycle_environment,
        verify_protocol_binary_identity,
        load_protocol_lock,
    )
    from .native_loop_driver import (
        NativeLoopRpcSession,
        NativeLoopEventLedger,
        _read_status_snapshot,
    )

    verify_protocol_binary_identity(codex_bin, lock=load_protocol_lock())
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    directories = {
        name: output_dir / name
        for name in ("home", "sqlite", "logs", "tmp", "workspace")
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    original_bytes = original_rollout.read_bytes()
    lines = original_bytes.splitlines(keepends=True)
    metadata = json.loads(lines[0])
    if (
        metadata.get("type") != "session_meta"
        or metadata.get("payload", {}).get("id") != thread_id
    ):
        raise ValueError("SDK migration input does not identify the selected thread")
    if metadata["payload"].get("history_mode") != "paginated":
        raise ValueError(
            "Only the observed paginated history projection defect is supported"
        )
    copied = directories["home"] / "sessions" / original_rollout.name
    copied.parent.mkdir(mode=0o700)
    metadata["payload"]["history_mode"] = "legacy"
    copied.write_bytes(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n"
        + b"".join(lines[1:])
    )
    copied.chmod(0o600)
    command = build_lifecycle_command(
        codex_bin, sqlite_home=directories["sqlite"], log_dir=directories["logs"]
    )
    command += ["--config", "project_doc_max_bytes=0"]
    environment = build_lifecycle_environment(
        codex_home=directories["home"],
        credential_home=Path.home(),
        tmp_dir=directories["tmp"],
    )

    def open_session():
        session = NativeLoopRpcSession(
            command=command,
            environment=environment,
            cwd=directories["workspace"],
            timeout_seconds=45,
            ledger=NativeLoopEventLedger(),
            experimental_api=True,
        )
        try:
            session.initialize()
        except BaseException:
            session.close(force=True)
            raise
        return session

    bootstrap = open_session()
    bootstrap.close(force=True)
    clone_db = directories["sqlite"] / "state_5.sqlite"
    with (
        _connection(f"file:{source_state_sqlite}?mode=ro", uri=True) as source,
        _connection(clone_db) as clone,
    ):
        for table in ("threads", "thread_dynamic_tools"):
            source_columns = source.execute(f"PRAGMA table_info({table})").fetchall()
            clone_columns = clone.execute(f"PRAGMA table_info({table})").fetchall()
            if not source_columns or source_columns != clone_columns:
                raise ValueError(
                    "SDK clone metadata schema differs from the real thread"
                )
            names = [column[1] for column in source_columns]
            key = "id" if table == "threads" else "thread_id"
            records = source.execute(
                f"SELECT * FROM {table} WHERE {key}=?", (thread_id,)
            ).fetchall()
            if table == "threads" and len(records) != 1:
                raise ValueError(
                    "SDK source metadata does not contain exactly the selected thread"
                )
            for record in records:
                values = list(record)
                if table == "threads":
                    values[names.index("history_mode")] = "legacy"
                    values[names.index("rollout_path")] = str(copied.resolve())
                # Startup may already discover the cloned rollout. Replace only
                # this clone's row with the original SDK metadata, never a turn.
                clone.execute(
                    f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in values)})",
                    values,
                )
    migration = subprocess.run(
        [
            str(codex_bin),
            "migrate-rollouts",
            "--apply",
            "--thread",
            thread_id,
            "--json",
            "--max-mib-per-second",
            "200",
            "--config",
            "sqlite_home=" + json.dumps(str(directories["sqlite"])),
            "--config",
            "log_dir=" + json.dumps(str(directories["logs"])),
        ],
        env=environment,
        cwd=directories["workspace"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if migration.returncode:
        raise ValueError("SDK isolated migrate-rollouts failed")
    migration_report = json.loads(migration.stdout)
    _write_json(output_dir / "sdk-migrate-rollouts-receipt.json", migration_report)
    session = open_session()
    try:
        snapshot = _read_status_snapshot(session, thread_id)
    finally:
        session.close(force=True)
    with _connection(f"file:{clone_db}?mode=ro", uri=True) as clone:
        generated_path, mode = clone.execute(
            "SELECT rollout_path,history_mode FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
    generated = Path(generated_path).resolve(strict=True)
    if mode != "paginated" or not generated.is_relative_to(output_dir.resolve()):
        raise ValueError("SDK did not complete its isolated history migration")
    if original_rollout.read_bytes() != original_bytes:
        raise ValueError("Original rollout changed while reconstructing its snapshot")
    proof = verify_sdk_history_reconstruction(original_rollout, generated, thread_id)
    proof.update(
        status="SDK_COPY_VERIFIED",
        rebuilt_rollout_path=str(generated),
        rebuilt_history_sqlite=str(directories["sqlite"] / "thread_history_1.sqlite"),
        status_snapshot=snapshot["thread"]["turns"],
        status_read=snapshot["status_read"],
        credentials_copied=False,
        original_sqlite_modified=False,
        clone_only_metadata_downgrade="history_mode=legacy",
        source_rollout_modified=False,
    )
    _write_json(output_dir / "reconstruction-receipt.json", proof)
    return proof


@contextmanager
def _connection(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def verify_sdk_history_reconstruction(original: Path, rebuilt: Path, thread_id: str):
    left, right = original.read_bytes().splitlines(), rebuilt.read_bytes().splitlines()
    if len(left) != len(right) or not left:
        raise ValueError("SDK history reconstruction changed the record count")
    changes = {"ordinal_reindexed": 0, "optional_reasoning_null_normalized": 0}

    def compare(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            b = dict(b)
            if (
                a.get("type") == "reasoning"
                and "content" not in a
                and b.get("content") is None
                and "content" in b
            ):
                b.pop("content")
                changes["optional_reasoning_null_normalized"] += 1
            return set(a) == set(b) and all(compare(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(compare(x, y) for x, y in zip(a, b))
        return type(a) is type(b) and a == b

    for index, (old, new) in enumerate(zip(left, right)):
        a, b = json.loads(old), json.loads(new)
        if index == 0 and (
            a.get("type") != "session_meta"
            or a.get("payload", {}).get("id") != thread_id
        ):
            raise ValueError("SDK history reconstruction has another thread identity")
        if b.get("ordinal") != index:
            raise ValueError("SDK history reconstruction ordinals are not contiguous")
        if a.get("ordinal") != b["ordinal"]:
            changes["ordinal_reindexed"] += 1
        a.pop("ordinal", None)
        b.pop("ordinal", None)
        if not compare(a, b):
            raise ValueError(
                f"SDK history reconstruction changed payload at record {index}"
            )
    return {
        "schema_version": "reqpilotx-sdk-history-equivalence/v1",
        "thread_id": thread_id,
        "original_rollout_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "rebuilt_rollout_sha256": hashlib.sha256(rebuilt.read_bytes()).hexdigest(),
        "record_count": len(left),
        "same_order_and_payloads": True,
        "sdk_normalizations": changes,
        "new_model_turn_started": False,
    }


def adopt_sdk_history_reconstruction(
    *, session_dir: Path, rebuilt_rollout: Path, rebuilt_sqlite: Path
):
    root = session_dir.resolve(strict=True)
    with _session_lock(root):
        return _adopt_sdk_history_reconstruction_locked(
            root, rebuilt_rollout, rebuilt_sqlite
        )


def _adopt_sdk_history_reconstruction_locked(root, rebuilt_rollout, rebuilt_sqlite):
    state = json.loads((root / "run-state.json").read_text())
    if (
        state.get("pid")
        or state.get("app_server_pid")
        or state.get("status") not in {"UNKNOWN", "FAILED", "INTERRUPTED", "COMPLETED"}
    ):
        raise ValueError("History repair requires a stopped task transport")
    thread = state["thread_id"]
    state_db, history_db = (
        root / "sqlite/state_5.sqlite",
        root / "sqlite/thread_history_1.sqlite",
    )
    with _connection(f"file:{state_db}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT rollout_path FROM threads WHERE id=?", (thread,)
        ).fetchone()
    if not row:
        raise ValueError("History repair cannot resolve the task thread")
    original = Path(row[0]).resolve(strict=True)
    proof = verify_sdk_history_reconstruction(original, rebuilt_rollout, thread)
    target = root / "history-reconciliations" / proof["rebuilt_rollout_sha256"]
    if target.exists():
        raise ValueError(
            "History repair already has a reconciliation directory; inspect before retry"
        )
    target.mkdir(parents=True, mode=0o700)
    for name, source in (
        ("state-before.sqlite", state_db),
        ("history-before.sqlite", history_db),
    ):
        with (
            _connection(f"file:{source}?mode=ro", uri=True) as src,
            _connection(target / name) as dst,
        ):
            src.backup(dst)
    _write_json(target / "runtime-state-before.json", deepcopy(state))
    rollout = target / rebuilt_rollout.name
    rollout.write_bytes(rebuilt_rollout.read_bytes())
    rollout.chmod(0o600)
    tables = (
        "thread_turns",
        "thread_items",
        "thread_history_projection_state",
        "thread_realtime_items",
    )
    with _connection(state_db) as db:
        db.execute("ATTACH DATABASE ? AS history", (str(history_db),))
        db.execute(
            "ATTACH DATABASE ? AS rebuilt",
            (str(rebuilt_sqlite.resolve(strict=True)),),
        )
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT rollout_path FROM threads WHERE id=?", (thread,)
        ).fetchone()
        if (
            current != row
            or hashlib.sha256(original.read_bytes()).hexdigest()
            != proof["original_rollout_sha256"]
        ):
            raise ValueError("Original history changed during reconstruction")
        turns = db.execute(
            "SELECT turn_id,status FROM rebuilt.thread_turns WHERE thread_id=?",
            (thread,),
        ).fetchall()
        if dict(turns).get(state["turn_id"]) not in {
            "completed",
            "interrupted",
            "failed",
        }:
            raise ValueError("SDK rebuilt history lacks the actual terminal turn")
        for table in tables:
            old_columns = db.execute(f"PRAGMA history.table_info({table})").fetchall()
            new_columns = db.execute(f"PRAGMA rebuilt.table_info({table})").fetchall()
            if old_columns != new_columns or not old_columns:
                raise ValueError("SDK reconstructed history schema differs")
            db.execute(f"DELETE FROM history.{table} WHERE thread_id=?", (thread,))
            db.execute(
                f"INSERT INTO history.{table} SELECT * FROM rebuilt.{table} WHERE thread_id=?",
                (thread,),
            )
        db.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (str(rollout), thread)
        )
        proof.update(
            status="ADOPTED_PENDING_COLD_API_VERIFICATION",
            original_rollout_path=str(original),
            active_rollout_path=str(rollout),
            backup_directory=str(target),
            task_turn_id=state["turn_id"],
            sdk_rebuilt_turns=[{"id": t, "status": s} for t, s in turns],
            original_rollout_retained=True,
            tools_contract_unchanged=True,
        )
        _write_json(target / "adoption-receipt.json", proof)
    return proof


def _thread_tools_digest(path, thread_id):
    with _connection(f"file:{path}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT * FROM thread_dynamic_tools WHERE thread_id=? ORDER BY position",
            (thread_id,),
        ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def recover_completed_native_result(session_dir: Path):
    """Project actual retained completion evidence after a transport/index failure.

    This creates a linked native recovery receipt, never edits the original
    attempt receipt, synthesizes a tool, or promotes an interrupted turn.
    """
    from .native_loop_driver import NativeLoopResult

    root = session_dir.resolve(strict=True)
    state = json.loads((root / "run-state.json").read_text())
    if (
        state.get("status") != "COMPLETED"
        or state.get("pid")
        or state.get("app_server_pid")
    ):
        raise ValueError("Host finalization requires the actual stopped completed turn")
    original = state.get("last_receipt") or {}
    if (original.get("thread_id"), original.get("turn_id")) != (
        state.get("thread_id"),
        state.get("turn_id"),
    ) or original.get("identity") != state.get("identity"):
        raise ValueError(
            "Retained native receipt belongs to a different turn or runtime identity"
        )
    attempt_file = root / ("receipt-" + str(original.get("attempt_id")) + ".json")
    if json.loads(attempt_file.read_text()) != original:
        raise ValueError("Original native attempt receipt changed")
    observation = state.get("terminal_observation") or {}
    if not (
        original.get("terminal_snapshot_verified") is True
        and original.get("status") == "COMPLETED"
    ):
        if (
            observation.get("verified_terminal") is not True
            or observation.get("observed_status") != "completed"
            or (observation.get("thread_id"), observation.get("turn_id"))
            != (state["thread_id"], state["turn_id"])
        ):
            raise ValueError(
                "Native completion has no authoritative SDK terminal verification"
            )
    events = []
    with (root / "events.jsonl").open() as stream:
        for line in stream:
            event = json.loads(line)
            if (
                event.get("attempt_id") == original["attempt_id"]
                and event.get("thread_id") == state["thread_id"]
                and event.get("turn_id") == state["turn_id"]
            ):
                events.append(event)
    terminal = [event for event in events if event.get("type") == "turn/completed"]
    if len(terminal) != 1 or terminal[0].get("status") != "completed":
        raise ValueError(
            "Original runtime did not observe the same turn actually complete"
        )
    messages = [
        event.get("text")
        for event in events
        if event.get("type") == "agent_message" and event.get("phase") == "final"
    ]
    final = messages[-1] if messages else original.get("final_message")
    receipt = {
        **original,
        "status": "COMPLETED",
        "terminal_snapshot_verified": True,
        "final_message": final,
    }
    receipt.pop("reason_code", None)
    receipt["host_terminal_recovery"] = {
        "original_attempt_receipt_sha256": hashlib.sha256(
            attempt_file.read_bytes()
        ).hexdigest(),
        "original_transport_status": original["status"],
        "original_reason_code": original.get("reason_code"),
        "turn_completed_event_sha256": hashlib.sha256(
            json.dumps(terminal[0], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "sdk_terminal_verified": True,
        "new_model_turn_started": False,
    }
    return NativeLoopResult(status="COMPLETED", final_message=final, receipt=receipt)


def observed_completed_native_turn(session_dir: Path):
    """Gate automatic repair on this attempt's real public terminal event."""
    try:
        state = json.loads((session_dir / "run-state.json").read_text())
        if state.get("pid") or state.get("app_server_pid"):
            return False
        receipt = state.get("last_receipt") or {}
        if (receipt.get("thread_id"), receipt.get("turn_id")) != (
            state.get("thread_id"),
            state.get("turn_id"),
        ):
            return False
        terminal = []
        with (session_dir / "events.jsonl").open() as stream:
            for line in stream:
                item = json.loads(line)
                if (
                    item.get("attempt_id") == receipt.get("attempt_id")
                    and item.get("thread_id") == state.get("thread_id")
                    and item.get("turn_id") == state.get("turn_id")
                    and item.get("type") == "turn/completed"
                ):
                    terminal.append(item.get("status"))
        return terminal == ["completed"]
    except (OSError, ValueError, KeyError):
        return False


def _rollback_reconciliation(root, adoption):
    """Restore only the backed-up target thread, never replace another DB/head."""
    backup = Path(adoption["backup_directory"])
    thread_id = adoption["thread_id"]
    with _connection(root / "sqlite/state_5.sqlite") as db:
        db.execute(
            "ATTACH DATABASE ? AS history",
            (str(root / "sqlite/thread_history_1.sqlite"),),
        )
        db.execute(
            "ATTACH DATABASE ? AS old_state", (str(backup / "state-before.sqlite"),)
        )
        db.execute(
            "ATTACH DATABASE ? AS old_history", (str(backup / "history-before.sqlite"),)
        )
        db.execute("BEGIN IMMEDIATE")
        for table in (
            "thread_turns",
            "thread_items",
            "thread_history_projection_state",
            "thread_realtime_items",
        ):
            db.execute(f"DELETE FROM history.{table} WHERE thread_id=?", (thread_id,))
            db.execute(
                f"INSERT INTO history.{table} SELECT * FROM old_history.{table} WHERE thread_id=?",
                (thread_id,),
            )
        db.execute("DELETE FROM thread_dynamic_tools WHERE thread_id=?", (thread_id,))
        db.execute(
            "INSERT INTO thread_dynamic_tools SELECT * FROM old_state.thread_dynamic_tools WHERE thread_id=?",
            (thread_id,),
        )
        columns = [
            row[1]
            for row in db.execute("PRAGMA table_info(threads)").fetchall()
            if row[1] != "id"
        ]
        for column in columns:
            db.execute(
                f"UPDATE threads SET {column}=(SELECT {column} FROM old_state.threads WHERE id=?) WHERE id=?",
                (thread_id, thread_id),
            )
    state = json.loads((backup / "runtime-state-before.json").read_text())
    state.update(
        status="UNKNOWN", reason_code="NATIVE_LOOP_HISTORY_RECONCILIATION_FAILED"
    )
    state.pop("terminal_observation", None)
    _write_json(root / "run-state.json", state)


def reconcile_native_loop_history(*, driver, session_dir: Path, apply: bool = False):
    """One executable, bounded recovery path for an observed ordinal defect.

    Must run with stopped transport. Valid normal histories are never rewritten.
    A repair failure cannot authorize publication or start a replacement thread.
    """
    root = session_dir.resolve(strict=True)
    with _session_lock(root):
        state = json.loads((root / "run-state.json").read_text())
        if (
            state.get("pid")
            or state.get("app_server_pid")
            or state.get("status")
            not in {"UNKNOWN", "FAILED", "INTERRUPTED", "COMPLETED"}
        ):
            raise ValueError("History reconciliation requires a stopped task transport")
        thread_id = state["thread_id"]
        state_db = root / "sqlite/state_5.sqlite"
        with _connection(f"file:{state_db}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT rollout_path FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
        if row is None:
            raise ValueError(
                "History reconciliation cannot resolve the original thread"
            )
        original = Path(row[0]).resolve(strict=True)
        anomalies, previous = [], -1
        with original.open() as stream:
            for index, line in enumerate(stream):
                event = json.loads(line)
                ordinal = event.get("ordinal")
                if type(ordinal) is not int or ordinal < 0:
                    raise ValueError("Unsupported SDK rollout ordinal shape")
                if ordinal != previous + 1:
                    if (
                        ordinal != previous
                        or event.get("type") != "event_msg"
                        or event.get("payload", {}).get("type")
                        != "thread_settings_applied"
                    ):
                        raise ValueError(
                            "Unsupported SDK rollout discontinuity; retain UNKNOWN"
                        )
                    anomalies.append(
                        {
                            "record_index": index,
                            "ordinal": ordinal,
                            "event_type": "thread_settings_applied",
                        }
                    )
                previous = ordinal
        if not anomalies:
            return {
                "status": "NO_REPAIR_NEEDED",
                "thread_id": thread_id,
                "new_model_turn_started": False,
            }
        if len(anomalies) > 32:
            raise ValueError(
                "SDK history reconciliation exceeds its bounded anomaly scope"
            )
        observed = {
            "schema_version": "reqpilotx-native-history-reconciliation/v1",
            "thread_id": thread_id,
            "turn_id": state["turn_id"],
            "anomalies": anomalies,
            "new_model_turn_started": False,
        }
        if not apply:
            return {
                **observed,
                "status": "REPAIR_AVAILABLE",
                "original_rollout_sha256": hashlib.sha256(
                    original.read_bytes()
                ).hexdigest(),
            }
        request_dir = root / "history-reconstruction-attempts" / uuid4().hex
        tools_before = _thread_tools_digest(state_db, thread_id)
        adoption = None
        try:
            rebuilt = rebuild_sdk_history_copy(
                codex_bin=driver.codex_bin,
                original_rollout=original,
                source_state_sqlite=state_db,
                thread_id=thread_id,
                output_dir=request_dir,
            )
            adoption = _adopt_sdk_history_reconstruction_locked(
                root,
                Path(rebuilt["rebuilt_rollout_path"]),
                Path(rebuilt["rebuilt_history_sqlite"]),
            )
            cold = driver._inspect_session_locked(root)
            current = json.loads((root / "run-state.json").read_text())
            if (
                not cold["verified_terminal"]
                or cold["thread_id"] != thread_id
                or cold["turn_id"] != state["turn_id"]
                or cold["turns"] != rebuilt["status_snapshot"]
                or current.get("identity") != state.get("identity")
                or _thread_tools_digest(state_db, thread_id) != tools_before
                or hashlib.sha256(original.read_bytes()).hexdigest()
                != rebuilt["original_rollout_sha256"]
            ):
                raise ValueError(
                    "Cold SDK verification differs from the reconstructed thread identity/statuses"
                )
            receipt = {
                **observed,
                "status": "COLD_API_VERIFIED",
                "adoption": adoption,
                "terminal_observation": cold,
                "original_payloads_preserved": True,
                "tools_contract_sha256": tools_before,
                "reconstruction_receipt": str(
                    request_dir / "reconstruction-receipt.json"
                ),
            }
            _write_json(request_dir / "reconciliation-receipt.json", receipt)
            current["history_reconciliation"] = {
                "status": "COLD_API_VERIFIED",
                "receipt_path": str(request_dir / "reconciliation-receipt.json"),
            }
            _write_json(root / "run-state.json", current)
            return receipt
        except Exception as exc:
            if adoption is not None:
                _rollback_reconciliation(root, adoption)
            else:
                unchanged = json.loads((root / "run-state.json").read_text())
                unchanged.update(
                    status="UNKNOWN",
                    reason_code="NATIVE_LOOP_HISTORY_RECONCILIATION_FAILED",
                )
                unchanged.pop("terminal_observation", None)
                _write_json(root / "run-state.json", unchanged)
            request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_json(
                request_dir / "reconciliation-failure.json",
                {
                    **observed,
                    "status": "UNKNOWN",
                    "error_type": type(exc).__name__,
                    "original_rollout_retained": True,
                    "adopted_indices_rolled_back": adoption is not None,
                },
            )
            raise


def main():
    import argparse
    from .native_loop_driver import LocalCodexAppServerNativeLoopDriver

    parser = argparse.ArgumentParser(
        description="Reconcile an observed stopped native-loop SDK ordinal defect without a model turn."
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    driver = LocalCodexAppServerNativeLoopDriver(
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
        repository_root=args.repository_root,
    )
    result = reconcile_native_loop_history(
        driver=driver, session_dir=args.session_dir, apply=args.apply
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
