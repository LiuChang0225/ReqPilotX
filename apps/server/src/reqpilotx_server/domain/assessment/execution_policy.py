from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_SHADOW_RUN_MODES = {"screening_llm_shadow", "llm_shadow"}
_ALLOWED_FINAL_MODES = {
    "heuristic": {"heuristic", "screening_only"},
    "llm_shadow": {"heuristic", "screening_only"},
    "llm_primary": {"llm_primary", "screening_only"},
}


@dataclass(frozen=True)
class AssessmentExecutionVerdict:
    serviceable: bool
    failure_reason: str
    analysis_label: str
    ai_decision: str
    assessment_mode: str
    shadow_degraded: bool
    shadow_warning_reasons: tuple[str, ...]


def evaluate_assessment_execution(
    *,
    assessment: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    configured_mode: str,
) -> AssessmentExecutionVerdict:
    normalized_mode = str(configured_mode or "").strip().lower()
    assessment_mode = str(assessment.get("assessment_mode") or "").strip().lower()
    analysis_label = str(assessment.get("analysis_label") or "").strip().upper()
    ai_decision = str(assessment.get("ai_decision") or "").strip().upper()

    shadow_warning_reasons = _shadow_warning_reasons(runs)
    mode_failure_reasons = _configured_mode_failure_reasons(
        configured_mode=normalized_mode,
        assessment_mode=assessment_mode,
        runs=runs,
    )
    primary_failure_reasons = (
        _primary_failure_reasons(runs) if normalized_mode == "llm_primary" else ()
    )
    fatal_failed_run_reasons = _fatal_failed_run_reasons(runs, configured_mode=normalized_mode)
    explicit_failure = assessment_mode in {"screening_failed", "analysis_failed"}
    result_shape_failure_reasons = _result_shape_failure_reasons(
        analysis_label=analysis_label,
        ai_decision=ai_decision,
        assessment_mode=assessment_mode,
    )
    failure_reasons = _unique_reasons(
        mode_failure_reasons,
        primary_failure_reasons,
        fatal_failed_run_reasons,
        result_shape_failure_reasons,
        ((f"assessment declared {assessment_mode}",) if explicit_failure else ()),
    )
    serviceable = not failure_reasons
    shadow_degraded = bool(
        serviceable and shadow_warning_reasons and normalized_mode == "llm_shadow"
    )
    if not serviceable and not failure_reasons:
        failure_reasons = (
            str(assessment.get("analysis_basis") or "incomplete assessment result").strip(),
        )
    return AssessmentExecutionVerdict(
        serviceable=serviceable,
        failure_reason="; ".join(reason for reason in failure_reasons if reason),
        analysis_label=analysis_label,
        ai_decision=ai_decision,
        assessment_mode=assessment_mode,
        shadow_degraded=shadow_degraded,
        shadow_warning_reasons=tuple(shadow_warning_reasons),
    )


def _configured_mode_failure_reasons(
    *,
    configured_mode: str,
    assessment_mode: str,
    runs: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    allowed_final_modes = _ALLOWED_FINAL_MODES.get(configured_mode)
    if allowed_final_modes is None:
        return (f"unsupported configured assessment mode: {configured_mode or 'missing'}",)

    reasons: list[str] = []
    if assessment_mode not in allowed_final_modes:
        _append_unique(
            reasons,
            f"configured {configured_mode} cannot serve {assessment_mode or 'missing'} result",
        )
    if configured_mode == "llm_primary" and assessment_mode == "llm_primary":
        has_primary_assessment_run = any(
            str(run.get("mode") or "").strip().lower() == "llm_primary"
            for run in runs
        )
        if not has_primary_assessment_run:
            _append_unique(reasons, "llm_primary serving result has no primary assessment run")
    return tuple(reasons)


def _result_shape_failure_reasons(
    *,
    analysis_label: str,
    ai_decision: str,
    assessment_mode: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if analysis_label not in {"INFO", "REQUIREMENT"}:
        _append_unique(reasons, f"invalid analysis label: {analysis_label or 'missing'}")
    if analysis_label == "INFO" and ai_decision:
        _append_unique(reasons, "INFO result cannot carry a serving decision")
    if assessment_mode == "screening_only" and ai_decision:
        _append_unique(reasons, "screening_only result cannot carry a serving decision")
    if analysis_label == "REQUIREMENT" and not ai_decision and assessment_mode != "screening_only":
        _append_unique(reasons, "requirement result is missing a serving decision")
    return tuple(reasons)


def _shadow_warning_reasons(runs: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    reasons: list[str] = []
    for run in runs:
        mode = str(run.get("mode") or "").strip().lower()
        if mode not in _SHADOW_RUN_MODES:
            continue
        validation_status = str(run.get("validation_status") or "").strip().lower()
        fallback_reason = str(run.get("fallback_reason") or "").strip()
        if validation_status == "accepted" and not fallback_reason:
            continue
        _append_unique(reasons, fallback_reason or f"{mode} validation_status={validation_status or 'missing'}")
    return tuple(reasons)


def _primary_failure_reasons(runs: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    reasons: list[str] = []
    for run in runs:
        mode = str(run.get("mode") or "").strip().lower()
        if mode not in {"screening_llm_primary", "llm_primary"}:
            continue
        validation_status = str(run.get("validation_status") or "").strip().lower()
        fallback_reason = str(run.get("fallback_reason") or "").strip()
        if mode == "screening_llm_primary" and validation_status == "accepted" and not fallback_reason:
            continue
        if mode == "llm_primary":
            if validation_status == "accepted" and not fallback_reason:
                continue
            if (
                validation_status == "hard_rule_override"
                and "legacy_evidence_selector" not in fallback_reason
                and "llm_primary_failed" not in fallback_reason
            ):
                continue
        _append_unique(
            reasons,
            fallback_reason or f"{mode} validation_status={validation_status or 'missing'}",
        )
    return tuple(reasons)


def _fatal_failed_run_reasons(
    runs: Sequence[Mapping[str, Any]],
    *,
    configured_mode: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for run in runs:
        validation_status = str(run.get("validation_status") or "").strip().lower()
        if validation_status != "failed":
            continue
        mode = str(run.get("mode") or "").strip().lower()
        if configured_mode == "llm_shadow" and mode in _SHADOW_RUN_MODES:
            continue
        fallback_reason = str(run.get("fallback_reason") or "provider execution failed").strip()
        _append_unique(reasons, fallback_reason)
    return tuple(reasons)


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _unique_reasons(*groups: Sequence[str]) -> tuple[str, ...]:
    reasons: list[str] = []
    for group in groups:
        for reason in group:
            _append_unique(reasons, str(reason or "").strip())
    return tuple(reasons)
