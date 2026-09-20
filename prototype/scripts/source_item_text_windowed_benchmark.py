#!/usr/bin/env python3
"""Evaluate Direct-text windowing latency without launching a customer job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SourceItemTextBenchmarkSampleV1(_StrictModel):
    sample_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    baseline_seconds: float = Field(gt=0)
    risk_class: Literal["LOW", "HIGH"]
    switch_threshold_seconds: float | None = Field(default=None, gt=0)
    observed_windowed_seconds: float | None = Field(default=None, gt=0)
    checkpoint_seconds: tuple[float, ...] = ()

    @model_validator(mode="after")
    def validate_checkpoints(self) -> SourceItemTextBenchmarkSampleV1:
        if any(value <= 0 for value in self.checkpoint_seconds) or tuple(
            sorted(self.checkpoint_seconds)
        ) != self.checkpoint_seconds:
            raise ValueError("checkpoint seconds must be positive and monotonic")
        if self.risk_class == "LOW" and self.switch_threshold_seconds is not None:
            raise ValueError("low-risk samples use the baseline 1.2x guardrail")
        return self


class SourceItemTextWindowedBenchmarkInputV1(_StrictModel):
    schema_version: Literal[
        "source-item-text-windowed-benchmark-input/v1"
    ] = "source-item-text-windowed-benchmark-input/v1"
    first_boundary_checkpoint_target_seconds: float = Field(default=120.0, gt=0)
    subsequent_checkpoint_gap_target_seconds: float = Field(default=90.0, gt=0)
    low_risk_regression_multiplier: float = Field(default=1.2, ge=1.0)
    samples: tuple[SourceItemTextBenchmarkSampleV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_samples(self) -> SourceItemTextWindowedBenchmarkInputV1:
        ids = tuple(item.sample_id for item in self.samples)
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark sample IDs must be unique")
        return self


def evaluate_source_item_text_windowed_benchmark(
    benchmark: SourceItemTextWindowedBenchmarkInputV1,
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    observed_count = 0
    all_passed = True
    for sample in benchmark.samples:
        maximum = (
            round(
                sample.baseline_seconds
                * benchmark.low_risk_regression_multiplier,
                3,
            )
            if sample.risk_class == "LOW"
            else sample.switch_threshold_seconds
        )
        if sample.observed_windowed_seconds is None:
            results.append(
                {
                    "sample_id": sample.sample_id,
                    "status": "NOT_RUN",
                    "maximum_seconds": maximum,
                    "failure_codes": [],
                }
            )
            continue
        observed_count += 1
        failures: list[str] = []
        if maximum is not None and sample.observed_windowed_seconds > maximum:
            failures.append("total_latency_gate_exceeded")
        if not sample.checkpoint_seconds:
            failures.append("checkpoint_observations_missing")
        else:
            if (
                sample.checkpoint_seconds[0]
                > benchmark.first_boundary_checkpoint_target_seconds
            ):
                failures.append("first_boundary_checkpoint_late")
            terminal = sample.observed_windowed_seconds
            timeline = sample.checkpoint_seconds + (terminal,)
            if any(
                later - earlier
                > benchmark.subsequent_checkpoint_gap_target_seconds
                for earlier, later in zip(timeline, timeline[1:])
            ):
                failures.append("checkpoint_gap_exceeded")
        passed = not failures
        all_passed = all_passed and passed
        results.append(
            {
                "sample_id": sample.sample_id,
                "status": "PASS" if passed else "FAIL",
                "maximum_seconds": maximum,
                "observed_windowed_seconds": sample.observed_windowed_seconds,
                "failure_codes": failures,
            }
        )
    status = "NOT_RUN" if observed_count == 0 else ("PASS" if all_passed else "FAIL")
    return {
        "schema_version": "source-item-text-windowed-benchmark-result/v1",
        "status": status,
        "observed_sample_count": observed_count,
        "sample_count": len(benchmark.samples),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-observations", action="store_true")
    args = parser.parse_args()
    benchmark = SourceItemTextWindowedBenchmarkInputV1.model_validate_json(
        args.input.read_bytes()
    )
    result = evaluate_source_item_text_windowed_benchmark(benchmark)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.write_text(encoded, encoding="utf-8")
    if result["status"] == "FAIL":
        return 1
    if args.require_observations and result["status"] == "NOT_RUN":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
