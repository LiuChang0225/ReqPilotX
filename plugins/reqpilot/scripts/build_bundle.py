#!/usr/bin/env python3
"""Build a portable plugin from the same, allowlisted ReqPilotX source modules.

This is a packaging operation, not a second implementation of the domain model.
No runtime/customer artifacts, databases, environment files or old answers enter
the bundle. The receipt records relative paths and hashes for reproducibility.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOTS = (
    "app.adapters.documents.tesseract_reading",
    "app.adapters.documents.pdf_reading_views",
    "app.adapters.documents.pdf_source_crop_renderer",
    "app.application.build_source_ir",
    "app.application.agent_workflow.source_requirement_baseline",
    "app.application.agent_workflow.source_item_delivery",
    "app.application.agent_workflow.source_item_workbench_metadata",
    "app.application.agent_workflow.source_item_decomposition",
    "app.application.agent_workflow.source_item_visual",
    "app.application.agent_workflow.source_item_analysis",
    "app.application.knowledge_evidence_capture",
    "app.application.agent_workflow.document_map_revision",
    "app.application.agent_workflow.delivery_snapshot_compatibility",
    "app.application.agent_workflow.artifact_kind_catalog",
    "app.application.agent_workflow.task_workspace",
)
SCRIPT_ROOTS = ("fast_source_capture.py",)
DEPENDENCIES = {
    "pydantic": "pydantic", "pypdf": "pypdf", "pymupdf": "pymupdf",
    "fitz": "pymupdf", "pdfplumber": "pdfplumber", "docx": "python-docx",
    "openpyxl": "openpyxl", "xlsxwriter": "xlsxwriter", "PIL": "Pillow",
    "jsonschema": "jsonschema", "pyxlsb": "pyxlsb",
}
TRANSITIVE_DEPENDENCIES = {"lxml": "python-docx"}
NAMESPACE_INITIALIZERS = frozenset({"app/__init__.py", "app/application/extraction/__init__.py"})
NAMESPACE_SOURCE = '"""Portable namespace; import concrete modules, without host startup side effects."""\n'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_repo(explicit: Path | None) -> Path:
    candidates = (explicit,) if explicit else (PLUGIN_ROOT, *PLUGIN_ROOT.parents)
    for path in candidates:
        if path and (path / "prototype/app/application/agent_workflow/source_requirement_baseline.py").is_file():
            return path.resolve()
    raise ValueError("Build requires the ReqPilotX source checkout via --repo; a built plugin needs no checkout at runtime")


def module_file(prototype: Path, module: str) -> Path | None:
    base = prototype.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def python_closure(prototype: Path, roots: tuple[str, ...], script_paths: list[Path]) -> tuple[set[Path], set[str]]:
    pending = list(script_paths)
    for module in roots:
        path = module_file(prototype, module)
        if path is None:
            raise ValueError(f"Required runtime module is missing: {module}")
        pending.append(path)
    files: set[Path] = set()
    external: set[str] = set()
    while pending:
        path = pending.pop()
        if path in files:
            continue
        if path.is_symlink() or prototype not in path.resolve().parents:
            raise ValueError(f"Runtime source must be a regular file within prototype: {path.name}")
        files.add(path)
        relative = path.relative_to(prototype)
        if relative.parts[0] == "app":
            for parent in path.parents:
                if parent == prototype:
                    break
                init = parent / "__init__.py"
                if init.is_file() and init not in files:
                    pending.append(init)
        package_parts = list(relative.with_suffix("").parts[:-1])
        if path.name == "__init__.py":
            package_parts = list(relative.parts[:-1])
        if str(relative) in NAMESPACE_INITIALIZERS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package_parts[:len(package_parts) - node.level + 1]
                    base = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                modules = [base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")]
            for module in modules:
                if not module:
                    continue
                if module == "app" or module.startswith("app."):
                    target = module_file(prototype, module)
                    if target and target not in files:
                        pending.append(target)
                elif not module.startswith("__future__"):
                    external.add(module.split(".")[0])
    return files, external


def build(repo: Path, output: Path, *, extraction_only: bool = False) -> dict:
    if output.exists():
        raise ValueError("Bundle output must not exist; each build gets a new immutable directory")
    if output == PLUGIN_ROOT or output in PLUGIN_ROOT.parents or PLUGIN_ROOT in output.parents:
        raise ValueError("Build output must be outside plugin source")
    prototype = repo / "prototype"
    scripts = [prototype / "scripts" / name for name in SCRIPT_ROOTS]
    for script in scripts:
        if not script.is_file():
            raise ValueError(f"Required tool is missing: {script.name}")
    selected_roots = tuple(module for module in MODULE_ROOTS if not (extraction_only and module.endswith(("source_item_delivery", "source_item_decomposition", "source_item_visual", "source_item_analysis"))))
    files, external = python_closure(prototype, selected_roots, scripts)
    # Fail if bundling accidentally reaches infrastructure/credentials/network hosts.
    banned = [path for path in files if any(part in {"persistence", "infrastructure"} for part in path.relative_to(prototype).parts)
              or path.relative_to(prototype).parts[:3] == ('app', 'adapters', 'agents')]
    if banned:
        raise ValueError("Portable closure reached host infrastructure: " + ", ".join(str(path.relative_to(prototype)) for path in sorted(banned)))
    shutil.copytree(PLUGIN_ROOT, output, ignore=shutil.ignore_patterns("runtime", "runtime-data", "tests", "__pycache__", "*.pyc", "*.zip", ".pytest_cache"))
    runtime = output / "runtime/prototype"
    for path in sorted(files):
        destination = runtime / path.relative_to(prototype)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if str(path.relative_to(prototype)) in NAMESPACE_INITIALIZERS:
            destination.write_text(NAMESPACE_SOURCE, encoding="utf-8")
        else:
            shutil.copyfile(path, destination)
    # Requirements are derived from actual imports and the checkout's pins.
    wanted = {DEPENDENCIES[name] for name in external if name in DEPENDENCIES}
    source_requirements = (prototype / "requirements.txt").read_text(encoding="utf-8").splitlines()
    selected = [line for line in source_requirements if line.split("==")[0] in wanted]
    selected_names = {line.split("==")[0] for line in selected}
    missing_pins = wanted - selected_names
    if missing_pins:
        shutil.rmtree(output)
        raise ValueError("Dependencies need explicit pins before release: " + ", ".join(sorted(missing_pins)))
    (output / "requirements.txt").write_text("\n".join(sorted(selected, key=str.casefold)) + "\n", encoding="utf-8")
    unknown_external = sorted(external - set(DEPENDENCIES) - set(TRANSITIVE_DEPENDENCIES) - set(sys.stdlib_module_names) - {"scripts"})
    receipt = {
        "schema_version": "reqpilot-portable-build/v1",
        "plugin_name": "reqpilot",
        "source_repository": "ReqPilotX",
        "domain_implementation": "UNCHANGED_ALLOWLIST_COPY",
        "namespace_initializers": [{"path": path, "reason": "Suppress eager host/provider exports; every concrete implementation remains unchanged"} for path in sorted(NAMESPACE_INITIALIZERS)],
        "source_checkout_required_at_runtime": False,
        "delivery_included": not extraction_only,
        "decomposition_included": not extraction_only,
        "customer_data_included": False,
        "module_roots": list(selected_roots),
        "python_minimum": "3.11",
        "requirements": sorted(selected, key=str.casefold),
        "optional_local_ocr_requirements": (PLUGIN_ROOT / "requirements-ocr.txt").read_text().splitlines(),
        "optional_or_unknown_imports": unknown_external,
        "runtime_files": [{"path": str(path.relative_to(prototype)), "sha256": digest(runtime / path.relative_to(prototype)), "upstream_sha256": digest(path), "bytes": (runtime / path.relative_to(prototype)).stat().st_size} for path in sorted(files)],
    }
    receipt["plugin_files"] = [{"path": str(path.relative_to(output)), "sha256": digest(path), "bytes": path.stat().st_size} for path in sorted(output.rglob("*")) if path.is_file() and "runtime" not in path.relative_to(output).parts]
    receipt["bundle_sha256"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"status": "EXTRACTION_ONLY_BUILT" if extraction_only else "BUILT", "output": str(output), "runtime_file_count": len(files), "runtime_bytes": sum(path.stat().st_size for path in files), "optional_or_unknown_imports": unknown_external}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="ReqPilotX checkout; used only at build time")
    parser.add_argument("--output", type=Path, required=True, help="New bundle directory outside plugin source")
    parser.add_argument("--extraction-only", action="store_true", help="Explicit intermediate bundle without the delivery bridge; never a full release")
    args = parser.parse_args()
    try:
        print(json.dumps(build(find_repo(args.repo), args.output.expanduser().resolve(), extraction_only=args.extraction_only), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "BUILD_FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
