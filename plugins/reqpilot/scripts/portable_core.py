"""Single source of truth for the installable ReqPilotX engine boundary."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DISTRIBUTION = "reqpilotx-engine"
ENGINE_BASE_VERSION = "0.2.0"
ENGINE_SOURCE_RELATIVE = Path("packages/engine/src")
MODULE_ROOTS = (
    "reqpilotx.adapters.documents.tesseract_reading",
    "reqpilotx.adapters.documents.pdf_reading_views",
    "reqpilotx.adapters.documents.pdf_source_crop_renderer",
    "reqpilotx.application.build_source_ir",
    "reqpilotx.application.agent_workflow.source_requirement_baseline",
    "reqpilotx.application.agent_workflow.source_item_delivery",
    "reqpilotx.application.agent_workflow.source_item_workbench_metadata",
    "reqpilotx.application.agent_workflow.source_item_decomposition",
    "reqpilotx.application.agent_workflow.source_item_visual",
    "reqpilotx.application.agent_workflow.source_item_analysis",
    "reqpilotx.application.knowledge_evidence_capture",
    "reqpilotx.application.agent_workflow.document_map_revision",
    "reqpilotx.application.agent_workflow.delivery_snapshot_compatibility",
    "reqpilotx.application.agent_workflow.artifact_kind_catalog",
    "reqpilotx.application.agent_workflow.task_workspace",
    "reqpilotx.ports.agent_structure_handoff_executor",
)
SCRIPT_ROOTS = ("reqpilotx.tools.fast_source_capture",)
DEPENDENCIES = {
    "pydantic": "pydantic",
    "pypdf": "pypdf",
    "pymupdf": "pymupdf",
    "fitz": "pymupdf",
    "pdfplumber": "pdfplumber",
    "docx": "python-docx",
    "openpyxl": "openpyxl",
    "xlsxwriter": "xlsxwriter",
    "PIL": "Pillow",
    "jsonschema": "jsonschema",
    "pyxlsb": "pyxlsb",
}
TRANSITIVE_DEPENDENCIES = {"lxml": "python-docx"}
NAMESPACE_INITIALIZERS = frozenset()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def canonical_digest(value: object) -> str:
    return digest_bytes(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def find_repo(explicit: Path | None) -> Path:
    candidates = (explicit,) if explicit else (PLUGIN_ROOT, *PLUGIN_ROOT.parents)
    marker = ENGINE_SOURCE_RELATIVE / (
        "reqpilotx/application/agent_workflow/source_requirement_baseline.py"
    )
    for path in candidates:
        if path and (path / marker).is_file():
            return path.resolve()
    raise ValueError(
        "Build requires the ReqPilotX source checkout via --repo; built releases "
        "need no checkout at runtime"
    )


def module_file(source_root: Path, module: str) -> Path | None:
    base = source_root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def python_closure(
    source_root: Path,
    roots: tuple[str, ...],
    script_paths: list[Path],
) -> tuple[set[Path], set[str]]:
    pending = list(script_paths)
    for module in roots:
        path = module_file(source_root, module)
        if path is None:
            raise ValueError(f"Required runtime module is missing: {module}")
        pending.append(path)
    files: set[Path] = set()
    external: set[str] = set()
    while pending:
        path = pending.pop()
        if path in files:
            continue
        if path.is_symlink() or source_root not in path.resolve().parents:
            raise ValueError(
                f"Runtime source must be a regular file within the engine: {path.name}"
            )
        files.add(path)
        relative = path.relative_to(source_root)
        for parent in path.parents:
            if parent == source_root:
                break
            initializer = parent / "__init__.py"
            if initializer.is_file() and initializer not in files:
                pending.append(initializer)
        package_parts = list(relative.with_suffix("").parts[:-1])
        if path.name == "__init__.py":
            package_parts = list(relative.parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package_parts[: len(package_parts) - node.level + 1]
                    base = ".".join(
                        prefix + ([node.module] if node.module else [])
                    )
                else:
                    base = node.module or ""
                modules = [
                    base,
                    *(
                        f"{base}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    ),
                ]
            for module in modules:
                if not module:
                    continue
                if module == "reqpilotx" or module.startswith("reqpilotx."):
                    target = module_file(source_root, module)
                    if target and target not in files:
                        pending.append(target)
                elif module == "app" or module.startswith("reqpilotx."):
                    raise ValueError(
                        f"Engine source still imports legacy module {module}: {relative}"
                    )
                elif module == "reqpilotx_server" or module.startswith(
                    "reqpilotx_server."
                ):
                    raise ValueError(
                        f"Engine cannot import the server package: {relative}"
                    )
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
    namespace_initializer: bool = False


@dataclass(frozen=True)
class CoreSelection:
    repo: Path
    source_root: Path
    module_roots: tuple[str, ...]
    members: tuple[CoreMember, ...]
    requirements: tuple[str, ...]
    optional_or_unknown_imports: tuple[str, ...]
    external_imports: tuple[str, ...]


def selected_module_roots(*, extraction_only: bool = False) -> tuple[str, ...]:
    if not extraction_only:
        return MODULE_ROOTS
    excluded = (
        "source_item_delivery",
        "source_item_decomposition",
        "source_item_visual",
        "source_item_analysis",
    )
    return tuple(module for module in MODULE_ROOTS if not module.endswith(excluded))


def _engine_requirements(repo: Path) -> tuple[str, ...]:
    configuration = tomllib.loads(
        (repo / "packages/engine/pyproject.toml").read_text(encoding="utf-8")
    )
    values = configuration.get("project", {}).get("dependencies", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("Engine dependencies must be a static PEP 621 list")
    return tuple(values)


def select_core(repo: Path, *, extraction_only: bool = False) -> CoreSelection:
    repo = find_repo(repo)
    source_root = repo / ENGINE_SOURCE_RELATIVE
    roots = selected_module_roots(extraction_only=extraction_only)
    scripts = [module_file(source_root, module) for module in SCRIPT_ROOTS]
    if any(path is None for path in scripts):
        raise ValueError("Required engine command implementation is missing")
    files, external = python_closure(
        source_root,
        roots,
        [path for path in scripts if path is not None],
    )
    requirements = _engine_requirements(repo)
    by_distribution = {line.split("==", 1)[0]: line for line in requirements}
    wanted = {DEPENDENCIES[name] for name in external if name in DEPENDENCIES}
    missing_pins = wanted - set(by_distribution)
    if missing_pins:
        raise ValueError(
            "Dependencies need explicit engine pins before release: "
            + ", ".join(sorted(missing_pins))
        )
    unknown = tuple(
        sorted(
            external
            - set(DEPENDENCIES)
            - set(TRANSITIVE_DEPENDENCIES)
            - set(sys.stdlib_module_names)
        )
    )
    members = []
    for path in sorted(files):
        runtime_relative = path.relative_to(source_root).as_posix()
        source_relative = path.relative_to(repo).as_posix()
        content = path.read_bytes()
        members.append(
            CoreMember(
                source_path=path,
                source_relative=source_relative,
                runtime_relative=runtime_relative,
                wheel_relative=runtime_relative,
                content=content,
                upstream_sha256=digest(path),
                packaged_sha256=digest_bytes(content),
            )
        )
    selected_requirements = tuple(
        sorted((by_distribution[name] for name in wanted), key=str.casefold)
    )
    return CoreSelection(
        repo,
        source_root,
        roots,
        tuple(members),
        selected_requirements,
        unknown,
        tuple(sorted(external)),
    )


def build_core_manifest(
    selection: CoreSelection,
    *,
    version: str,
    source_commit: str,
    source_tree: str,
) -> dict:
    manifest = {
        "schema_version": "reqpilotx-engine-core/v2",
        "distribution": ENGINE_DISTRIBUTION,
        "version": version,
        "source_repository": "ReqPilotX",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "python_minimum": "3.11",
        "module_roots": list(selection.module_roots),
        "requirements": list(selection.requirements),
        "optional_or_unknown_imports": list(selection.optional_or_unknown_imports),
        "namespace_initializers": [],
        "runtime_layout": "reqpilotx-package/v1",
        "runtime_files": [
            {
                "source_path": member.source_relative,
                "runtime_path": member.runtime_relative,
                "wheel_path": member.wheel_relative,
                "sha256": member.packaged_sha256,
                "upstream_sha256": member.upstream_sha256,
                "bytes": len(member.content),
                "namespace_initializer": False,
            }
            for member in selection.members
        ],
        "banned_host_members_found": 0,
        "customer_data_included": False,
    }
    manifest["core_identity"] = canonical_digest(manifest)
    return manifest
