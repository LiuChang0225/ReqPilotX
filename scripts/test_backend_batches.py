"""Run every discovered backend test module in resumable, isolated batches.

PG/provider acceptance has separate explicit entrypoints. This runner preserves
the offline suite's network denial and configuration stripping, and records each
module's source hash, exit code, counts and log rather than inferring completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CODE_ROOTS = ("apps/server", "packages/engine", "prototype", "plugins/reqpilot/scripts", "agent-assets/scripts", "scripts")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_fingerprint() -> str:
    inventory = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    records = []
    for name in sorted(set(inventory)):
        if not name or not any(name == base or name.startswith(base + "/") for base in CODE_ROOTS):
            continue
        if name.startswith("prototype/tests/") or "/__pycache__/" in name:
            continue
        path = ROOT / name
        if path.is_file() and not path.is_symlink() and path.suffix in {".py", ".sql", ".toml", ".json"}:
            records.append((name, file_hash(path)))
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


def prepare(directory: Path, size: int) -> dict:
    modules = discovered_modules()
    manifest = {"schema": 1, "code_sha256": code_fingerprint(), "batch_size": size,
                "modules": modules, "batches": [modules[start:start + size] for start in range(0, len(modules), size)]}
    destination = directory / "manifest.json"
    if destination.exists():
        raise ValueError("Manifest already exists; retain it or choose a new validation directory")
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def discovered_modules() -> list[dict]:
    """Mirror unittest's recursion into importable packages, not just root files."""
    test_root = ROOT / "prototype/tests"
    modules = []
    for path in sorted(test_root.rglob("test*.py")):
        parent = path.parent
        importable = True
        while parent != test_root:
            if not (parent / "__init__.py").is_file():
                importable = False
                break
            parent = parent.parent
        if importable:
            module = ".".join(path.relative_to(ROOT / "prototype").with_suffix("").parts)
            modules.append({"module": module, "path": str(path.relative_to(ROOT)), "sha256": file_hash(path)})
    return modules


def run_batch(directory: Path, manifest: dict, index: int) -> dict:
    modules = manifest["batches"][index]
    if code_fingerprint() != manifest["code_sha256"]:
        raise ValueError("Backend/source fingerprint changed; do not mix evidence across candidates")
    for item in modules:
        if file_hash(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("Test module changed: " + item["path"])
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("REQPILOT_", "REQPILOTX_", "OPENAI_", "AZURE_", "DASHSCOPE_", "ALIYUN_", "ALIBABA_CLOUD_"))
           and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
    runtime_root = directory / "runtime"
    runtime_root.mkdir(exist_ok=True)
    started = time.time()
    log_path = directory / f"batch-{index:03d}.log"
    with tempfile.TemporaryDirectory(prefix=f"batch-{index:03d}-", dir=runtime_root) as temporary:
        env.update({"REQPILOT_TEST_MODE": "1", "REQPILOT_ENV": "test", "REQPILOT_TEST_STORAGE_DIR": temporary,
                    "REQPILOT_STORAGE_DIR": temporary, "REQPILOT_OBJECT_STORE": "local", "REQPILOT_OBJECT_ROOT": temporary,
                    "REQPILOT_SCRATCH_ROOT": str(Path(temporary) / "scratch"), "REQPILOT_AGENT_RUNTIME": "none",
                    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "DO_NOT_TRACK": "1",
                    "PYTHONPATH": os.pathsep.join(str(ROOT / path) for path in ("packages/engine/src", "apps/server/src", "prototype"))})
        bootstrap = (
            "import runpy,sys; "
            "exec('def deny_network(event,args):\\n if event == \"socket.connect\":\\n  raise RuntimeError(\"Network access is disabled in offline tests\")'); "
            "sys.addaudithook(deny_network); sys.argv[0]='unittest'; runpy.run_module('unittest',run_name='__main__')"
        )
        with log_path.open("w") as stream:
            completed = subprocess.run([sys.executable, "-c", bootstrap, "-v", *(item["module"] for item in modules)],
                                       cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    output = log_path.read_text()
    count = re.search(r"^Ran (\d+) tests? in ([0-9.]+)s$", output, re.MULTILINE)
    skipped = re.findall(r"\bskipped=(\d+)", output)
    result = {"index": index, "exit_code": completed.returncode, "terminal_summary": count is not None,
              "tests": int(count.group(1)) if count else None, "skipped": int(skipped[-1]) if skipped else 0,
              "elapsed_seconds": round(time.time() - started, 3), "modules": modules, "log": log_path.name,
              "code_sha256": manifest["code_sha256"], "source_unchanged": code_fingerprint() == manifest["code_sha256"]}
    (directory / f"batch-{index:03d}.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def reconcile(directory: Path, manifest: dict) -> dict:
    results = []
    missing = []
    invalid = []
    for index, expected in enumerate(manifest["batches"]):
        path = directory / f"batch-{index:03d}.json"
        if not path.exists():
            missing.append(index)
            continue
        result = json.loads(path.read_text())
        if (result["modules"] != expected or result["code_sha256"] != manifest["code_sha256"]
                or not result["source_unchanged"] or not result["terminal_summary"] or result["exit_code"] != 0):
            invalid.append(index)
        results.append(result)
    current_modules = {item["module"] for item in discovered_modules()}
    declared_modules = {item["module"] for item in manifest["modules"]}
    covered_modules = {item["module"] for result in results if result["index"] not in invalid for item in result["modules"]}
    current_hashes_match = all(file_hash(ROOT / item["path"]) == item["sha256"] for item in manifest["modules"])
    success = (not missing and not invalid and covered_modules == declared_modules == current_modules
               and current_hashes_match and code_fingerprint() == manifest["code_sha256"])
    summary = {"status": "PASS" if success else "INCOMPLETE", "module_count": len(declared_modules),
               "covered_module_count": len(covered_modules), "missing_batches": missing, "invalid_batches": invalid,
               "tests": sum(result["tests"] or 0 for result in results if result["index"] not in invalid),
               "skipped": sum(result["skipped"] for result in results if result["index"] not in invalid),
               "module_hashes_match": current_hashes_match, "code_sha256": manifest["code_sha256"]}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 32:
        parser.error("batch-size must be 1..32")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = prepare(directory, args.batch_size) if args.prepare else json.loads((directory / "manifest.json").read_text())
    if args.batch is not None:
        result = run_batch(directory, manifest, args.batch)
    elif args.reconcile:
        result = reconcile(directory, manifest)
    else:
        result = {"module_count": len(manifest["modules"]), "batch_count": len(manifest["batches"]), "code_sha256": manifest["code_sha256"]}
    print(json.dumps(result))
    return 0 if result.get("status", "PASS") == "PASS" and result.get("exit_code", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
