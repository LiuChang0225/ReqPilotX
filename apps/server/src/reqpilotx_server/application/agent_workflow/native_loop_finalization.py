"""Retry host publication of an already completed native turn, without a model.

The original failed durable job remains history. A new same-kind host job reuses
its exact business predecessor, frozen epoch and actual publish/close/deliver
records, then passes the normal validation, lease and PostgreSQL publication CAS.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import (
    NativeLoopResult,
)
from reqpilotx_server.adapters.agents.provider_tool_loop import (
    ADAPTER_ID as PROVIDER_ADAPTER_ID,
    STATE_SCHEMA as PROVIDER_STATE_SCHEMA,
    TRANSPORT_KIND as PROVIDER_TRANSPORT_KIND,
)

from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import (
    ACTIVE,
    ENTRYPOINT,
    LoopControlRequestV1,
    _existing,
    _locked,
    _read,
    _write_json,
    get_source_item_loop,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink


def _response_payload(call):
    texts = [
        item["text"]
        for item in call["response"].get("contentItems", [])
        if item.get("type") == "inputText"
    ]
    if len(texts) != 1:
        raise ValueError("The publication tool has no unique structured receipt")
    return json.loads(texts[0])


def _inside(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Publication recovery artifact leaves the original task")
    return resolved


def _completed_provider_result(runtime_root: Path) -> NativeLoopResult:
    """Reopen a completed local provider turn using only immutable on-disk proof."""
    state_path = runtime_root / "run-state.json"
    if state_path.is_symlink():
        raise ValueError("Provider loop state is a symbolic link")
    state = _read(state_path)
    receipt = state.get("last_receipt") or {}
    attempt_id = receipt.get("attempt_id")
    reference = receipt.get("run_state_reference")
    if (state.get("schema_version") != PROVIDER_STATE_SCHEMA
        or state.get("status") != "COMPLETED"
        or receipt.get("adapter_id") != PROVIDER_ADAPTER_ID
        or receipt.get("transport_kind") != PROVIDER_TRANSPORT_KIND
        or receipt.get("status") != "COMPLETED"
        or receipt.get("local_state_persisted") is not True
        or not isinstance(attempt_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
        or reference != "turn-state-" + attempt_id + ".json"
        or not isinstance(receipt.get("run_state_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["run_state_sha256"])):
        raise ValueError("No completed provider turn can be finalized")
    snapshot_path = runtime_root / reference
    receipt_path = runtime_root / ("receipt-" + attempt_id + ".json")
    if snapshot_path.is_symlink() or receipt_path.is_symlink():
        raise ValueError("Provider loop completion proof is a symbolic link")
    snapshot = _read(snapshot_path)
    if (
        canonical_json_sha256(snapshot) != receipt["run_state_sha256"]
        or _read(receipt_path) != receipt
        or snapshot.get("schema_version") != PROVIDER_STATE_SCHEMA
        or snapshot.get("status") != "COMPLETED"
        or snapshot.get("identity") != state.get("identity")
        or receipt.get("identity") != state.get("identity")
        or snapshot.get("thread_id") != state.get("thread_id")
        or snapshot.get("thread_id") != receipt.get("thread_id")
        or snapshot.get("turn_id") != receipt.get("turn_id")
        or snapshot.get("tool_receipts") != receipt.get("tools")
        or canonical_json_sha256(snapshot.get("messages")) != snapshot.get("messages_sha256")
        or snapshot.get("final_message") != receipt.get("final_message")
    ):
        raise ValueError("Provider loop completion state changed")
    terminal = snapshot.get("terminal_model_message") or {}
    if (terminal.get("text") != receipt.get("final_message")
        or not isinstance(snapshot.get("messages"), list)
        or not snapshot["messages"]
        or terminal.get("message_sha256") != canonical_json_sha256(snapshot["messages"][-1])):
        raise ValueError("Provider loop terminal model message changed")
    return NativeLoopResult("COMPLETED", receipt.get("final_message"), receipt)


def verified_finalization_inputs(root, meta, original_job):
    from reqpilotx_server.adapters.agents.codex_app_server.native_loop_history import (
        recover_completed_native_result,
    )

    runtime_root = root / "runtime"
    state_path = runtime_root / "run-state.json"
    if state_path.is_symlink():
        raise ValueError("Native loop state is a symbolic link")
    state = _read(state_path) if state_path.exists() else {}
    provider_runtime = state.get("schema_version") == PROVIDER_STATE_SCHEMA
    result = (
        _completed_provider_result(runtime_root)
        if provider_runtime
        else recover_completed_native_result(runtime_root)
    )
    if (
        original_job.payload.get("task_id") != meta["task_id"]
        or original_job.payload.get("source_sha256") != meta["source_sha256"]
        or original_job.payload.get("capability_epoch") != meta.get("capability_epoch")
    ):
        raise ValueError("Completed turn task/source/frozen capability changed")
    if provider_runtime:
        binding = {
            "task_id": meta["task_id"],
            "project_id": meta["project_id"],
            "document_id": meta["document_id"],
            "source_sha256": meta["source_sha256"],
        }
        if meta.get("job_kind") == "agent.source-item.extract-loop":
            binding["mode"] = "RAW_SOURCE"
        else:
            binding["document_map_sha256"] = meta["document_map_sha256"]
        if (
            result.receipt["identity"].get("binding_sha256")
            != canonical_json_sha256(binding)
            or result.receipt["identity"].get("capability_epoch_sha256")
            != canonical_json_sha256(meta.get("capability_epoch") or {})
        ):
            raise ValueError("Provider completion belongs to another source/version binding")
    request_file = root / ("publish-request-" + original_job.job_id + ".json")
    request = _read(request_file)
    if request.get("job_id") != original_job.job_id:
        raise ValueError("Publication request belongs to another durable job")
    raw = meta.get("job_kind") == "agent.source-item.extract-loop"
    workspace = root / "workspace" if raw else root / "review"
    review = workspace / "review" if raw else workspace
    head = _read(review / "workspace-manifest.json")
    if head["review_sha256"] != request["review_sha256"]:
        raise ValueError(
            "Review/feedback changed after the completed turn requested publication"
        )
    calls = []
    for receipt in result.receipt["tools"]:
        if not receipt.get("success"):
            continue
        if provider_runtime:
            path = root / "runtime/calls" / (
                hashlib.sha256((receipt["thread_id"] + ":" + receipt["turn_id"]
                                + ":" + receipt["call_id"]).encode()).hexdigest() + ".json"
            )
        else:
            path = root / "runtime/calls" / (
                canonical_json_sha256([receipt["thread_id"], receipt["call_id"]]) + ".json"
            )
        if path.is_symlink():
            raise ValueError("Completed tool receipt is a symbolic link")
        call = _read(path)
        identity = call.get("identity", {})
        if (
            call.get("status") != "RETURNED"
            or identity.get("turn_id") != (
                receipt["turn_id"] if provider_runtime else result.receipt["turn_id"]
            )
            or identity.get("thread_id") != result.receipt["thread_id"]
            or identity.get("call_id") != receipt["call_id"]
            or identity.get("name") != receipt["name"]
            or canonical_json_sha256(call.get("arguments"))
            != receipt["arguments_sha256"]
            or canonical_json_sha256(call.get("response")) != receipt["response_sha256"]
            or call["response"].get("success") is not True
            or (provider_runtime and call.get("response_sha256") != receipt["response_sha256"])
        ):
            raise ValueError("Original completed-turn tool receipt changed")
        calls.append(call)
    publishes = [
        call
        for call in calls
        if call["identity"]["name"] == "reqpilot_publish"
        and call["arguments"].get("review_sha256") == request["review_sha256"]
        and _response_payload(call).get("status") == "PUBLICATION_REQUEST_RECORDED"
    ]
    closes = [
        call
        for call in calls
        if call["identity"]["name"] == "reqpilot_run"
        and call["arguments"].get("operation") == "review-close"
        and _response_payload(call).get("status") == "REVIEW_ADVANCED"
        and _response_payload(call).get("review_sha256") == request["review_sha256"]
    ]
    if not publishes or not closes:
        raise ValueError(
            "Completed turn lacks its actual successful publish and close receipts"
        )
    close = closes[-1]
    close_input = _read(
        _inside(workspace / close["arguments"]["arguments"]["input"], workspace)
    )
    delivery = _inside(review / close_input["delivery_path"], workspace)
    candidate = _inside(review / close_input["candidate_path"], workspace)
    closed_version = _inside(review / head["current"]["version_path"], workspace)
    if (
        _response_payload(close).get("current_version_path")
        != head["current"]["version_path"]
        or _response_payload(close).get("baseline_sha256")
        != head["current"]["baseline_sha256"]
        or delivery != _inside(review / head["delivery"]["path"], workspace)
    ):
        raise ValueError(
            "Completed close does not identify the actual current candidate/delivery"
        )
    deliveries = [
        call
        for call in calls
        if call["identity"]["name"] == "reqpilot_run"
        and call["arguments"].get("operation") == "deliver"
        and _response_payload(call).get("status") == "DRAFT_EXPORTED"
        and _inside(workspace / call["arguments"]["arguments"]["output_dir"], workspace)
        == delivery
        and _inside(
            workspace / call["arguments"]["arguments"]["baseline_dir"], workspace
        )
        == candidate
    ]
    if not deliveries:
        raise ValueError(
            "Completed turn lacks the same-version successful delivery tool receipt"
        )
    for name, sha in head["current"]["files"].items():
        if (
            hashlib.sha256(
                _inside(closed_version / name, closed_version).read_bytes()
            ).hexdigest()
            != sha
        ):
            raise ValueError("The completed candidate's immutable file SHA changed")
    delivery_receipt = _read(delivery / "delivery-receipt.json")
    xlsx_sha = hashlib.sha256((delivery / "matrix.xlsx").read_bytes()).hexdigest()
    if (
        xlsx_sha != head["delivery"]["xlsx_sha256"]
        or xlsx_sha != delivery_receipt.get("xlsx_sha256")
        or delivery_receipt.get("baseline_artifact_sha256")
        != head["current"]["baseline_sha256"]
        or delivery_receipt.get("source_sha256") != meta["source_sha256"]
    ):
        raise ValueError(
            "Completed delivery bytes do not bind its source/baseline receipt"
        )
    if any(
        entry["status"] == "PENDING"
        for entry in _read(root / "controls.json")["steering"]
    ):
        raise ValueError(
            "New feedback requires an Agent continuation, not host-only finalization"
        )
    proof = {
        "original_job_id": original_job.job_id,
        "original_turn_id": result.receipt["turn_id"],
        "thread_id": result.receipt["thread_id"],
        "review_sha256": request["review_sha256"],
        "runtime_receipt_sha256": canonical_json_sha256(result.receipt),
        "publication_request_sha256": canonical_json_sha256(request),
        "successful_publish_call_id": publishes[-1]["identity"]["call_id"],
        "successful_close_call_id": close["identity"]["call_id"],
        "successful_deliver_call_id": deliveries[-1]["identity"]["call_id"],
        "model_turn_started": False,
    }
    return result, request, proof


def finalize_source_item_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopControlRequestV1,
    response: ResponseSink,
):
    container, root, _ = _existing(project_id, document_id, task_id)
    with _locked(root):
        meta = _read(root / "task.json")
        commands = meta.setdefault("finalize_commands", {})
        if payload.command_id in commands:
            return get_source_item_loop(project_id, document_id, task_id, response)
        latest = container.jobs.get(meta["latest_job_id"])
        if latest.status.value in ACTIVE or (latest.result or {}).get(
            "application_saved"
        ):
            raise ApplicationError(
                status_code=409, detail="原任务仍在执行或该版本已经保存。"
            )
        source_job_id = (latest.payload.get("finalization") or {}).get(
            "original_job_id", latest.job_id
        )
        original = container.jobs.get(source_job_id)
        try:
            _, _, proof = verified_finalization_inputs(root, meta, original)
        except (KeyError, OSError, ValueError) as exc:
            raise ApplicationError(
                status_code=409,
                detail="尚无完整可核验的已完成轮次与同版发布回执，请先恢复原任务。",
            ) from exc
        job = container.jobs.enqueue(
            kind=original.kind,
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key=canonical_json_sha256(
                {
                    "task_id": task_id,
                    "command_id": payload.command_id,
                    "finalization": proof,
                }
            ),
            payload={
                **original.payload,
                "command_id": payload.command_id,
                "application_entrypoint": ENTRYPOINT,
                "finalization": proof,
            },
            priority=24,
            max_attempts=1,
        )
        commands[payload.command_id] = job.job_id
        meta["latest_job_id"] = job.job_id
        _write_json(root / "task.json", meta)
        return get_source_item_loop(project_id, document_id, task_id, response)


def run_native_loop_finalization(container, root, meta, job, context):
    original = container.jobs.get(job.payload["finalization"]["original_job_id"])
    result, request, proof = verified_finalization_inputs(root, meta, original)
    if proof != job.payload["finalization"]:
        raise ValueError("Completed turn finalization inputs changed after enqueue")
    context.heartbeat(
        progress={
            "stage": "VERIFYING_COMPLETED_NATIVE_PUBLICATION",
            "thread_id": proof["thread_id"],
            "turn_id": proof["original_turn_id"],
            "message": "核验原已完成轮次并保存同一版本，不再调用模型。",
        }
    )
    with _locked(root):
        if meta.get("job_kind") == "agent.source-item.extract-loop":
            from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
                _publish_initial_graph,
            )

            saved = _publish_initial_graph(
                container, root, meta, job, context, result.receipt, request
            )
        else:
            from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import (
                _publish_loop,
            )

            saved = _publish_loop(
                container, root, meta, job, context, result.receipt, request
            )
    saved["host_finalization"] = proof
    return saved
