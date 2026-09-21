"""Single source of truth for the portable ReqPilotX engine boundary.

Both the installable engine release and the portable plugin consume this
selection.  The selection describes existing source files; it does not create a
second business implementation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DISTRIBUTION = "reqpilotx-engine"
ENGINE_BASE_VERSION = "0.1.0"
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
BANNED_PARTS = frozenset({"persistence", "infrastructure"})


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def canonical_digest(value: object) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def find_repo(explicit: Path | None) -> Path:
    candidates = (explicit,) if explicit else (PLUGIN_ROOT, *PLUGIN_ROOT.parents)
    for path in candidates:
        if path and (path / "prototype/app/application/agent_workflow/source_requirement_baseline.py").is_file():
            return path.resolve()
    raise ValueError("Build requires the ReqPilotX source checkout via --repo; built releases need no checkout at runtime")


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


@dataclass(frozen=True)
class CoreMember:
    source_path: Path
    source_relative: str
    runtime_relative: str
    wheel_relative: str
    content: bytes
    upstream_sha256: str
    packaged_sha256: str
    namespace_initializer: bool


@dataclass(frozen=True)
class CoreSelection:
    repo: Path
    prototype: Path
    module_roots: tuple[str, ...]
    members: tuple[CoreMember, ...]
    requirements: tuple[str, ...]
    optional_or_unknown_imports: tuple[str, ...]
    external_imports: tuple[str, ...]


def selected_module_roots(*, extraction_only: bool = False) -> tuple[str, ...]:
    if not extraction_only:
        return MODULE_ROOTS
    excluded = ("source_item_delivery", "source_item_decomposition", "source_item_visual", "source_item_analysis")
    return tuple(module for module in MODULE_ROOTS if not module.endswith(excluded))


def select_core(repo: Path, *, extraction_only: bool = False) -> CoreSelection:
    repo = find_repo(repo)
    prototype = repo / "prototype"
    scripts = [prototype / "scripts" / name for name in SCRIPT_ROOTS]
    for script in scripts:
        if not script.is_file():
            raise ValueError(f"Required tool is missing: {script.name}")
    roots = selected_module_roots(extraction_only=extraction_only)
    files, external = python_closure(prototype, roots, scripts)
    banned = [
        path for path in files
        if any(part in BANNED_PARTS for part in path.relative_to(prototype).parts)
        or path.relative_to(prototype).parts[:3] == ("app", "adapters", "agents")
    ]
    if banned:
        raise ValueError("Portable closure reached host infrastructure: " + ", ".join(str(path.relative_to(prototype)) for path in sorted(banned)))
    wanted = {DEPENDENCIES[name] for name in external if name in DEPENDENCIES}
    source_requirements = (prototype / "requirements.txt").read_text(encoding="utf-8").splitlines()
    requirements = tuple(sorted((line for line in source_requirements if line.split("==")[0] in wanted), key=str.casefold))
    missing_pins = wanted - {line.split("==")[0] for line in requirements}
    if missing_pins:
        raise ValueError("Dependencies need explicit pins before release: " + ", ".join(sorted(missing_pins)))
    unknown = tuple(sorted(external - set(DEPENDENCIES) - set(TRANSITIVE_DEPENDENCIES) - set(sys.stdlib_module_names) - {"scripts"}))
    members = []
    for path in sorted(files):
        relative = str(path.relative_to(prototype))
        is_initializer = relative in NAMESPACE_INITIALIZERS
        content = NAMESPACE_SOURCE.encode("utf-8") if is_initializer else path.read_bytes()
        if relative.startswith("scripts/"):
            wheel_relative = "reqpilotx_engine_tools/" + Path(relative).name
        else:
            wheel_relative = relative
        members.append(CoreMember(
            source_path=path,
            source_relative=relative,
            runtime_relative=relative,
            wheel_relative=wheel_relative,
            content=content,
            upstream_sha256=digest(path),
            packaged_sha256=digest_bytes(content),
            namespace_initializer=is_initializer,
        ))
    return CoreSelection(repo, prototype, roots, tuple(members), requirements, unknown, tuple(sorted(external)))


def build_core_manifest(selection: CoreSelection, *, version: str, source_commit: str, source_tree: str) -> dict:
    manifest = {
        "schema_version": "reqpilotx-engine-core/v1",
        "distribution": ENGINE_DISTRIBUTION,
        "version": version,
        "source_repository": "ReqPilotX",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "python_minimum": "3.11",
        "module_roots": list(selection.module_roots),
        "requirements": list(selection.requirements),
        "optional_or_unknown_imports": list(selection.optional_or_unknown_imports),
        "namespace_initializers": [
            {"path": path, "reason": "Suppress eager host/provider exports; concrete implementations remain unchanged"}
            for path in sorted(NAMESPACE_INITIALIZERS)
        ],
        "runtime_files": [
            {
                "source_path": member.source_relative,
                "runtime_path": member.runtime_relative,
                "wheel_path": member.wheel_relative,
                "sha256": member.packaged_sha256,
                "upstream_sha256": member.upstream_sha256,
                "bytes": len(member.content),
                "namespace_initializer": member.namespace_initializer,
            }
            for member in selection.members
        ],
        "banned_host_members_found": 0,
        "customer_data_included": False,
    }
    manifest["core_identity"] = canonical_digest(manifest)
    return manifest
