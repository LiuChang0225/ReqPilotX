"""Deterministic continuation of a provider task whose product was actually saved.

This is an evidence index, not a generated summary. Source files, immutable
versions, feedback and the complete runtime audit remain authoritative.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from reqpilotx_server.adapters.agents.provider_tool_loop import ADAPTER_ID, _paired_history
from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import _digest, _write_json


def saved_predecessor(container, meta):
    """Pin only an acknowledged, successfully persisted provider product."""
    previous_id = meta.get("latest_job_id")
    if not previous_id:
        return {}
    previous = container.jobs.get(previous_id)
    result = previous.result or {}
    if (previous.status.value == "SUCCEEDED" and result.get("application_saved") is True
        and (result.get("runtime_receipt") or {}).get("adapter_id") == ADAPTER_ID):
        return {"continuation_predecessor_job_id": previous.job_id}
    return {}


def _reference(workspace, path, expected=None):
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(workspace.resolve()):
        raise ValueError("Continuation evidence leaves its task workspace")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if expected is not None and expected != digest:
        raise ValueError("Continuation evidence bytes changed")
    return {"path": str(resolved.relative_to(workspace.resolve())), "sha256": digest}


def _verify_active_handoff(container, root, meta, state, context):
    """Revalidate fixed evidence without rebasing the unfinished conversation."""
    handoff = context.get("handoff") or {}
    if (handoff.get("schema_version") != "reqpilotx-continuation-handoff/v1"
        or context.get("sha256") != _digest(handoff)
        or handoff.get("source_sha256") != meta["source_sha256"]
        or handoff.get("binding_sha256") != state.get("identity", {}).get("binding_sha256")):
        raise ValueError("Continuation active source/binding changed")
    workspace = root / ("workspace" if meta.get("job_kind") == "agent.source-item.extract-loop" else "review")

    def read(ref):
        path = workspace / ref["path"]
        _reference(workspace, path, ref["sha256"])
        return path

    for ref in (handoff["source"], handoff["original_source"], *handoff["source_ir"],
                *handoff["version_files"].values(), handoff["review"],
                *handoff["delivery"].values(), *handoff["audit"].values()):
        read(ref)
    previous = container.jobs.get(handoff["predecessor_job_id"])
    saved = previous.result or {}
    receipt = saved.get("runtime_receipt") or {}
    if (previous.status.value != "SUCCEEDED" or saved.get("application_saved") is not True
        or saved.get("baseline_sha256") != handoff["baseline_sha256"]
        or _digest(receipt) != handoff["completed_receipt_sha256"]
        or receipt.get("status") != "COMPLETED" or receipt.get("adapter_id") != ADAPTER_ID
        or receipt.get("local_state_persisted") is not True
        or receipt.get("identity") != state.get("identity")
        or any(previous.payload.get(key) != meta[key]
               for key in ("task_id", "project_id", "document_id", "source_sha256"))):
        raise ValueError("Continuation active predecessor is not the saved completed task")
    runtime = root / "runtime"
    snapshot_name = receipt["run_state_reference"]
    receipt_name = "receipt-" + receipt["attempt_id"] + ".json"
    for name in (snapshot_name, receipt_name):
        original = runtime / name
        if original.is_symlink() or not original.resolve(strict=True).is_relative_to(runtime.resolve()):
            raise ValueError("Continuation original completion proof leaves its runtime")
        if original.read_bytes() != read(handoff["audit"][name]).read_bytes():
            raise ValueError("Continuation original completion proof changed")
    snapshot = json.loads((runtime / snapshot_name).read_text(encoding="utf-8"))
    if (_digest(snapshot) != handoff["completed_run_state_sha256"]
        or receipt["run_state_sha256"] != handoff["completed_run_state_sha256"]
        or json.loads((runtime / receipt_name).read_text(encoding="utf-8")) != receipt
        or snapshot.get("status") != "COMPLETED" or snapshot.get("identity") != receipt["identity"]
        or snapshot.get("tool_receipts") != receipt["tools"]
        or _digest(snapshot.get("messages")) != handoff["completed_messages_sha256"]
        or not _paired_history(snapshot["messages"]) or snapshot.get("pending_images")):
        raise ValueError("Continuation active completion proof changed")
    calls = json.loads(read(handoff["audit"]["calls_index"]).read_text(encoding="utf-8"))["calls"]
    if len(calls) != len(receipt["tools"]):
        raise ValueError("Continuation active tool index changed")
    for ref, row in zip(calls, receipt["tools"]):
        copied = read(ref)
        name = hashlib.sha256((row["thread_id"] + ":" + row["turn_id"] + ":" + row["call_id"]).encode()).hexdigest() + ".json"
        original = runtime / "calls" / name
        if (ref["call_id"] != row["call_id"] or ref["turn_id"] != row["turn_id"]
            or original.is_symlink() or not original.resolve(strict=True).is_relative_to(runtime.resolve())
            or original.read_bytes() != copied.read_bytes()):
            raise ValueError("Continuation active original tool audit changed")
    if meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _read_private
        graph = _read_private(container, saved["native_source_graph_artifact"],
            project_id=meta["project_id"], document_id=meta["document_id"], namespace="working")
        if (graph.get("source_sha256") != meta["source_sha256"]
            or graph["baseline"]["artifact_sha256"] != handoff["baseline_sha256"]
            or graph["runtime_receipt"] != receipt):
            raise ValueError("Continuation active saved graph changed")
    return handoff


def build_continuation_handoff(container, root: Path, meta, job, *, verify_review):
    predecessor_id = job.payload.get("continuation_predecessor_job_id")
    runtime = root / "runtime"
    if not predecessor_id and not (runtime / "run-state.json").exists():
        return None
    state = json.loads((runtime / "run-state.json").read_text(encoding="utf-8"))
    if not predecessor_id or state.get("status") != "COMPLETED":
        context = state.get("continuation_context") or {}
        if not context and not predecessor_id:
            return None  # Markerless v1 / unfinished work keeps its old path.
        handoff = _verify_active_handoff(container, root, meta, state, context)
        if predecessor_id and handoff.get("predecessor_job_id") != predecessor_id:
            raise ValueError("Continuation has no matching active handoff")
        return handoff if predecessor_id else None  # No new terminal compaction.

    from reqpilotx_server.application.agent_workflow.native_loop_finalization import _completed_provider_result
    completed = _completed_provider_result(runtime)
    snapshot = json.loads((runtime / completed.receipt["run_state_reference"]).read_text(encoding="utf-8"))
    if (state.get("messages") != snapshot.get("messages")
        or state.get("messages_sha256") != _digest(state.get("messages"))
        or state.get("turn_id") != snapshot.get("turn_id")
        or state.get("tool_receipts") != snapshot.get("tool_receipts")):
        raise ValueError("Continuation current audit differs from its completed snapshot")
    previous = container.jobs.get(predecessor_id)
    saved = previous.result or {}
    if (previous.status.value != "SUCCEEDED" or saved.get("application_saved") is not True
        or saved.get("runtime_receipt") != completed.receipt
        or any(previous.payload.get(key) != meta[key]
               for key in ("task_id", "project_id", "document_id", "source_sha256"))
        or not _paired_history(state["messages"]) or state.get("pending_images")):
        raise ValueError("Continuation predecessor is not the saved completed source task")
    raw = meta.get("job_kind") == "agent.source-item.extract-loop"
    graph = None
    saved_review_sha = saved.get("review_sha256")
    if raw:
        from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _read_private
        graph = _read_private(container, saved["native_source_graph_artifact"],
            project_id=meta["project_id"], document_id=meta["document_id"], namespace="working")
        if (graph.get("source_sha256") != meta["source_sha256"]
            or graph["baseline"]["artifact_sha256"] != saved.get("baseline_sha256")
            or graph["runtime_receipt"] != completed.receipt):
            raise ValueError("Continuation saved graph changed")
        saved_review_sha = graph["host_verification"]["review"]["review_sha256"]
    workspace = root / "workspace" if raw else root / "review"
    review = workspace / "review" if raw else workspace
    verified = verify_review(review)
    head = json.loads((review / "workspace-manifest.json").read_text(encoding="utf-8"))
    if (verified.get("review_sha256") != head.get("review_sha256")
        or verified.get("source_sha256") != meta["source_sha256"]
        or not saved.get("baseline_sha256")
        or head["current"]["baseline_sha256"] != saved["baseline_sha256"]
        or job.payload.get("baseline_sha256") != saved["baseline_sha256"]
        or saved_review_sha not in
            {entry["review_sha256"] for entry in verified["version_chain"]}):
        raise ValueError("Continuation baseline/source/review ancestry mismatch")

    def reference(path: Path, expected=None):
        return _reference(workspace, path, expected)

    source = reference(review / head["source"]["path"], meta["source_sha256"])
    original_source = reference(workspace / meta["source_path"], meta["source_sha256"]) if raw else source
    current = review / head["current"]["version_path"]
    files = {name: reference(current / name, sha) for name, sha in sorted(head["current"]["files"].items())}
    baseline = json.loads((current / "source-requirement-baseline.json").read_text(encoding="utf-8"))
    if baseline.get("artifact_sha256") != saved["baseline_sha256"] or baseline.get("source_sha256") != meta["source_sha256"]:
        raise ValueError("Continuation baseline artifact changed")
    known = {record["source_record_id"] for record in baseline["records"]}
    feedback = [entry for entry in head["feedback"] if entry["status"] != "ADDRESSED"]
    targets = sorted({sid for entry in feedback for sid in entry["source_item_ids"]})
    if set(targets) - known:
        raise ValueError("Continuation feedback record is missing from the baseline")
    delivery = review / head["delivery"]["path"]
    delivery_receipt = json.loads((delivery / "delivery-receipt.json").read_text(encoding="utf-8"))
    if (delivery_receipt != saved.get("delivery_receipt")
        or delivery_receipt.get("baseline_artifact_sha256") != saved["baseline_sha256"]
        or delivery_receipt.get("source_sha256") != meta["source_sha256"]):
        raise ValueError("Continuation delivery version changed")
    delivery_files = {"receipt": reference(delivery / "delivery-receipt.json"),
                      "xlsx": reference(delivery / "matrix.xlsx", head["delivery"]["xlsx_sha256"])}
    source_ir = []
    if raw:
        events = [json.loads(line) for line in (root / "producer-events.jsonl").read_text().splitlines()]
        receipt = graph["map_compilation_receipt"]
        matches = []
        for event in events:
            if event.get("operation") != "compile-map":
                continue
            directory = workspace / event["arguments"]["output_dir"]
            path = directory / "map-compilation-receipt.json"
            reference(path)
            if json.loads(path.read_text()) == receipt:
                matches.append(directory)
        if not matches:
            raise ValueError("Continuation source has no retained SourceIR producer")
        inputs = receipt["source_inputs"]
        source_ir = [reference(matches[-1] / inputs["source_ir_path"], inputs["source_ir_sha256"])]
        for entry in graph["host_verification"].get("source_capture_chain", []):
            source_ir.append(reference(workspace / entry["source_ir_path"], entry["source_ir_sha256"]))
    else:
        # Review-only workspaces do not carry a SourceIR producer ledger. Do not
        # claim arbitrary files as source evidence merely because of their name.
        if any(path.name in {"source_ir.json", "source-ir.json", "map-source-ir.json"}
               for path in workspace.rglob("*.json")):
            raise ValueError("Continuation review SourceIR has no retained producer proof")

    # Exact immutable audit copies are readable with the existing reqpilot_read.
    # Original runtime snapshots/receipts/events/calls are never removed or edited.
    attempt = completed.receipt["attempt_id"]
    audit = workspace / "continuation-audit" / attempt
    if audit.is_symlink() or (audit.parent.exists() and audit.parent.is_symlink()):
        raise ValueError("Continuation audit directory cannot be a symbolic link")
    audit.mkdir(parents=True, exist_ok=True, mode=0o700)

    def audit_copy(source_path, target):
        if source_path.is_symlink() or not source_path.resolve(strict=True).is_relative_to(runtime.resolve()):
            raise ValueError("Continuation audit source leaves its runtime")
        data = source_path.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.parent.resolve().is_relative_to(audit.resolve()):
            raise ValueError("Continuation audit copy leaves its directory")
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise ValueError("Continuation audit copy changed")
        else:
            temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return reference(target)

    audit_refs = {name: audit_copy(runtime / name, audit / name) for name in
        (completed.receipt["run_state_reference"], "receipt-" + attempt + ".json")}
    calls = []
    for row in completed.receipt["tools"]:
        name = hashlib.sha256((row["thread_id"] + ":" + row["turn_id"] + ":" + row["call_id"]).encode()).hexdigest() + ".json"
        source_path = runtime / "calls" / name
        if source_path.is_symlink() or not source_path.resolve(strict=True).is_relative_to(runtime.resolve()):
            raise ValueError("Continuation original tool audit leaves its runtime")
        call = json.loads(source_path.read_text(encoding="utf-8"))
        identity = {key: row[key] for key in ("thread_id", "turn_id", "call_id", "name", "arguments_sha256")}
        if (call.get("status") != "RETURNED" or call.get("identity") != identity
            or _digest(call.get("arguments")) != row["arguments_sha256"]
            or _digest(call.get("response")) != row["response_sha256"]
            or call.get("response_sha256") != row["response_sha256"]):
            raise ValueError("Continuation original tool audit changed or is unpaired")
        copied = audit_copy(source_path, audit / "calls" / name)
        calls.append({"call_id": row["call_id"], "turn_id": row["turn_id"], **copied})
    _write_json(audit / "calls-index.json", {"calls": calls})
    audit_refs["calls_index"] = reference(audit / "calls-index.json")
    handoff = {"schema_version": "reqpilotx-continuation-handoff/v1",
        "predecessor_job_id": predecessor_id,
        "binding_sha256": completed.receipt["identity"]["binding_sha256"],
        "completed_run_state_sha256": completed.receipt["run_state_sha256"],
        "completed_receipt_sha256": _digest(completed.receipt),
        "completed_messages_sha256": state["messages_sha256"],
        "source_sha256": meta["source_sha256"], "source": source,
        "original_source": original_source, "source_ir": source_ir,
        "baseline_sha256": saved["baseline_sha256"], "baseline_revision": baseline["revision"],
        "version_path": str(current.relative_to(workspace)), "version_files": files,
        "record_count": len(known), "target_record_ids": targets,
        "feedback_ids": [entry["feedback_id"] for entry in feedback],
        "review": reference(review / "reviews" / (head["review_sha256"] + ".json")),
        "review_sha256": head["review_sha256"], "saved_review_sha256": saved_review_sha,
        "delivery": delivery_files, "audit": audit_refs,
        "read_next": "Read current review feedback and complete target records using reqpilot_read; "
                     "use reqpilot_context/review-packet for related records and source references. "
                     "Reread current review before publication; produce a same-source successor and delivery."}
    _write_json(audit / "handoff.json", handoff)
    return handoff
