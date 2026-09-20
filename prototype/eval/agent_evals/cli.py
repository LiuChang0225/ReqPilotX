"""One local entry point for registry, execution and source-bound evaluation."""

from argparse import ArgumentParser
import json
from pathlib import Path

from .contracts import MODELS, registry


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("validate")
    p = commands.add_parser("prepare-micro-inputs")
    p.add_argument("--out", type=Path, required=True)
    commands.add_parser(
        "seed-stage-internal", help="Operator preparation subprocess; no model call"
    )
    p = commands.add_parser("grade-stage")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--oracle", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--case", required=True)
    p.add_argument("--bindings", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--python", type=Path, required=True)
    p.add_argument("--model", choices=list(MODELS), required=True)
    p.add_argument("--transport", choices=["CLI", "MCP"], default="MCP")
    p.add_argument("--workbench-url")
    p.add_argument("--codex-binary", default="codex")
    p.add_argument(
        "--host-binary", help="Native agy or node binary for the selected model"
    )
    p.add_argument("--pair-repo", type=Path)
    p.add_argument("--observation-seconds", type=int, default=1200)
    p = commands.add_parser("run")
    p.add_argument("--run-dir", type=Path, required=True)
    p = commands.add_parser("import")
    p.add_argument("--case", required=True)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--kind", choices=["MODEL_RUN", "DETERMINISTIC_REPLAY"], default="MODEL_RUN"
    )
    p.add_argument("--metadata", type=Path)
    for name in ("grade", "review-template"):
        p = commands.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        if name == "grade":
            p.add_argument("--reviews", type=Path)
    p = commands.add_parser("report")
    p.add_argument("--input", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare-micro-inputs":
            from .micro_cases import generate

            result = generate(args.out)
        elif args.action == "seed-stage-internal":
            import sys
            from .stage_fixtures import seed_internal

            result = seed_internal(json.load(sys.stdin))
        elif args.action == "grade-stage":
            from .stage_fixtures import grade_stage

            result = grade_stage(args.run_dir, args.oracle, args.out)
        elif args.action == "validate":
            cases, rubrics = registry()
            result = {
                "status": "VALID",
                "cases": len(cases["cases"]),
                "rubrics": len(rubrics["rubrics"]),
            }
        elif args.action == "prepare":
            from .runner import prepare

            if args.observation_seconds < 1:
                raise ValueError(
                    "Observation interval must be positive; it is not a timeout"
                )
            result = prepare(
                args.case,
                args.bindings,
                args.bundle,
                args.out,
                args.python,
                args.model,
                args.transport,
                args.workbench_url,
                args.codex_binary,
                args.observation_seconds,
                args.host_binary,
                args.pair_repo,
            )
        elif args.action == "run":
            from .runner import run

            result = run(args.run_dir)
        else:
            from .reporting import grade, imported, review_template, table

            if args.action == "import":
                result = imported(
                    args.case,
                    args.result,
                    args.events,
                    args.workspace,
                    args.out,
                    args.kind,
                    args.metadata,
                )
            elif args.action == "grade":
                report = grade(args.run_dir, args.out, args.reviews)
                result = {
                    k: report[k] for k in ("run_id", "verdict", "agent_acceptance")
                }
                result["output"] = str(args.out)
            elif args.action == "review-template":
                result = review_template(args.run_dir, args.out)
            else:
                result = table(args.input, args.out)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 2
