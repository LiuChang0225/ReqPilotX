#!/usr/bin/env python3
"""Portable ReqPilot tool entry. No model calls or source semantics are hidden here."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    path.chmod(0o600)


def runtime_root(project_root: Path | None = None) -> Path:
    bundled = PLUGIN_ROOT / "runtime/prototype"
    if bundled.is_dir():
        return bundled
    if project_root is not None:
        candidate = project_root.expanduser().resolve() / "prototype"
        if (candidate / "app/application/agent_workflow/source_requirement_baseline.py").is_file():
            return candidate
    raise ValueError("Portable runtime is absent. Build the bundle first; --project-root is an explicit development-only alternative")


def load_runtime(project_root: Path | None = None) -> Path:
    root = runtime_root(project_root)
    sys.path.insert(0, str(root))
    return root


def doctor(args: argparse.Namespace) -> dict:
    try:
        root = runtime_root(args.project_root)
    except ValueError as exc:
        return {"status": "NOT_READY", "runtime": "MISSING", "message": str(exc), "semantic_validation": "NOT_RUN"}
    deps = {name: importlib.util.find_spec(name) is not None for name in ("pydantic", "pypdf", "pymupdf", "openpyxl", "xlsxwriter", "docx", "PIL")}
    receipt_path = PLUGIN_ROOT / "build-receipt.json"
    integrity = []
    dependency_versions = {}
    if receipt_path.is_file():
        receipt = read_json(receipt_path)
        for requirement in receipt.get("requirements", []):
            name, _, version = requirement.partition("==")
            try:
                installed = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            dependency_versions[name] = {"required": version, "installed": installed, "satisfied": installed == version}
        for entry in receipt["runtime_files"]:
            path = root / entry["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                integrity.append(entry["path"])
        for entry in receipt.get("plugin_files", []):
            path = PLUGIN_ROOT / entry["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                integrity.append(entry["path"])
    import platform
    office_ready = (root / "app/application/agent_workflow/office_source_evidence.py").is_file()
    input_formats = {
        "PDF": {"pipeline": "NATIVE_PDF", "available": all(deps.values()), "semantic_validation": "NOT_RUN"},
        "DOCX": {"pipeline": "NATIVE_OFFICE" if office_ready else "CAPTURE_ONLY", "available": office_ready and all(deps.values()), "semantic_validation": "NOT_RUN"},
        "XLSX": {"pipeline": "NATIVE_OFFICE" if office_ready else "CAPTURE_ONLY", "available": office_ready and all(deps.values()), "semantic_validation": "NOT_RUN"},
        "XLSB": {"pipeline": "OPTIONAL_CAPTURE_CONVERSION_ONLY", "full_source_item_pipeline": False,
            "required_platform": "Darwin", "current_platform_supported": platform.system() == "Darwin",
            "external_dependencies": {"/opt/homebrew/bin/soffice": Path("/opt/homebrew/bin/soffice").is_file(),
                                      "/usr/bin/sandbox-exec": Path("/usr/bin/sandbox-exec").is_file()},
            "required_libreoffice_version": "26.2.4.2", "binary_version_validation": "NOT_RUN"},
    }
    tool_files = {"capture": (root / "scripts/fast_source_capture.py").is_file(), "delivery": (root / "app/application/agent_workflow/source_item_delivery.py").is_file(), "decomposition": (root / "app/application/agent_workflow/source_item_decomposition.py").is_file()}
    local_ocr={'tesseract_available':shutil.which('tesseract') is not None,
        'table_dependencies_available':all(importlib.util.find_spec(name) is not None for name in ('cv2','numpy')),
        'optional_table_requirements':str(PLUGIN_ROOT/'requirements-ocr.txt'),
        'tessdata_override_configured':bool(os.environ.get('REQPILOT_TESSDATA_DIR')),
        'source_semantics_verified':False}
    return {"status": "READY" if all(deps.values()) and all(item["satisfied"] for item in dependency_versions.values()) and all(tool_files.values()) and not integrity else "NOT_READY", "runtime": "BUNDLED" if receipt_path.is_file() else "EXPLICIT_DEVELOPMENT_CHECKOUT", "dependencies": deps, "dependency_versions": dependency_versions, "tools": tool_files, "local_ocr":local_ocr, "integrity_mismatches": integrity, "semantic_validation": "NOT_RUN", "independent_agent_verification": "NOT_RUN", "input_formats": input_formats, "codex_available": shutil.which("codex") is not None}


def bootstrap(args: argparse.Namespace) -> dict:
    workspace = args.workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    targets = [workspace / "AGENTS.md", workspace / ".reqpilot", workspace / ".agents/skills"]
    if any(path.exists() for path in targets):
        raise ValueError("Bootstrap refuses to replace AGENTS, plugin or skills; use a new workspace")
    runtime_root(args.project_root)
    if not (PLUGIN_ROOT / "build-receipt.json").is_file():
        raise ValueError("Cold bootstrap needs a built bundle, not a development checkout")
    shutil.copytree(PLUGIN_ROOT, workspace / ".reqpilot", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "runtime-data", ".pytest_cache"))
    shutil.copyfile(PLUGIN_ROOT / "AGENTS.template.md", workspace / "AGENTS.md")
    skills = workspace / ".agents/skills"
    skills.mkdir(parents=True)
    discovery_modes = {}
    for source in sorted((workspace / ".reqpilot/skills").iterdir()):
        if source.is_dir() and (source / "SKILL.md").is_file():
            # Relative symlinks remain relocatable with the entire workspace.
            try:
                (skills / source.name).symlink_to(Path("../../.reqpilot/skills") / source.name, target_is_directory=True)
                discovery_modes[source.name] = "RELATIVE_SYMLINK"
            except (OSError, NotImplementedError):
                # Windows may lack symlink privileges; skills are small, portable
                # reference files. The canonical runtime remains in .reqpilot.
                shutil.copytree(source, skills / source.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                discovery_modes[source.name] = "VERIFIED_COPY"
    ignore = workspace / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    additions = [line for line in ("/runtime-data/", "/.venv/", "__pycache__/") if line not in existing.splitlines()]
    if additions:
        ignore.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(additions) + "\n", encoding="utf-8")
    inputs = []
    (workspace / "runtime-data").mkdir(mode=0o700, exist_ok=True)
    for index, source in enumerate(args.source or [], start=1):
        source = source.expanduser().resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise ValueError("Input must be a regular raw source file")
        destination = workspace / "runtime-data/input" / f"{index:02d}-{source.name}"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        inputs.append({"path": str(destination.relative_to(workspace)), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "role": "CUSTOMER_SOURCE"})
    write_json(workspace / "runtime-data/input-manifest.json", {"schema_version": "reqpilot-cold-inputs/v1", "inputs": inputs, "prior_results_provided": False, "skill_discovery_modes": discovery_modes})
    request = "Use the project AGENTS and ReqPilot skills to process the raw customer files listed in runtime-data/input-manifest.json. Identify the intended workspace, source document and internal knowledge at entry. Use workbench to discover and guide the actual ReqPilotX review surface; opening a URL does not import local artifacts. Preserve complete original wording, numbering, tables, images and governing conditions in actual source reading order, with summaries and interpretations separate. By default complete the authorized task autonomously: publish a reviewable Document Map, continue with the source ledger and classification, self-check and repair, and deliver the best supported complete draft. Progressive workbench output is not a pause for human approval. Wait at a Map confirmation point only when the user explicitly requested it or the existing task retains that policy. Treat covers/contents as whole structural blocks without per-entry SRC. Apply meaningful authorized decomposition. Run cited compliance analysis only with supplied internal evidence; otherwise mark it not run. Export the same-version Excel through the internal program. Use delegation only when the user has authorized it. Preserve originals and report actual execution evidence, remaining gaps and what was not run."
    (workspace / "request.md").write_text(request + "\n", encoding="utf-8")
    return {"status": "PREPARED", "workspace": str(workspace), "input_count": len(inputs), "fresh_session_command": ["codex", "exec", "--cd", str(workspace), "--skip-git-repo-check", "--enable", "multi_agent", "--json", "-"], "prompt_file": "request.md", "execution": "NOT_RUN"}


def catalog(_: argparse.Namespace) -> dict:
    return {"schema_version": "reqpilot-tool-catalog/v1", "tools": [
        {"command":"task " + '/'.join(read_json(PLUGIN_ROOT/'scripts/task-api-capabilities.json')['actions']), "effect":"TYPED_TASK_REQUEST", "role":"Shared typed CLI/MCP business entry. Binds source, artifacts and identities; existing review changes compile, export and close one recoverable command. Preserves fixed task runtime, expected revision and distinct publication/delivery checks.", "arguments":["--working-root","--input"]},
        {"command":"workspace init/status/recover/run", "effect":"TASK_SCOPED_PRIVATE_WORKSPACE", "role":"Initialize a physical document task, inspect/recover validated selections, or run existing compilers with automatic input/output paths. Local commit precedes optional workbench sync; saved candidates are not published.", "operations":["capture","reading-workspace","read-pdf-visual","revise-visual-text","compile-map","compile-items","compile-images","compile-annotations","compile-decomposition","deliver","review-open"], "review_close":"review-close is for an actual revision candidate. workspace run deliver automatically registers an unchanged current review; review-verify can then finish the handoff."},
        {"command": "workbench", "effect": "READ_ONLY_OR_BOUND_DRAFT_SYNC", "role": "Discover the local UI, or bind a PDF source and synchronize saved Map/source-review artifacts into a versioned draft in the existing workbench; no model dispatch or overwrite of the application working baseline", "arguments": ["--url", "--open", "--source", "--map-dir", "--baseline-dir", "--review-dir", "--project-id", "--document-id", "--workspace", "--binding-file", "--require-map-confirmation", "--no-map-confirmation"], "default_map_confirmation": False, "review_import_supported": True, "supported_review_state": "PDF source items, image annotations, decomposition and analysis policy; evidence-bearing assessments and engineer opinions are explicitly rejected rather than dropped"},
        {"command": "workbench-pull", "effect": "READ_AND_LOCAL_FEEDBACK_IMPORT", "role": "Retrieve real source/version-bound workbench confirmation and feedback; optionally import matching-baseline user feedback into the existing local review once. Does not restart an ended model conversation."},
        {"command": "capture", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Deterministic source capture, no requirement judgment"},
        {"command": "capture-pdf-text", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Append original PDF drawing-text observations for explicit pages/query; preserve old capture indexes and verify against original on Map compilation"},
        {"command": "capture-visual-source", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Project source-bound VisualText text/table observations to ordinary authoring refs; preserve existing capture nodes and visual provenance; does not certify OCR accuracy"},
        {"command": "read-pdf-visual", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Batch local Tesseract reading for explicit PDF pages and optional ruled table regions; save HD pages, raw OCR, typed candidates and ready Map/Items source refs"},
        {"command": "revise-visual-text", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Record source-reviewed text or cell corrections against existing visual refs; preserve raw recognition, page geometry and correction lineage"},
        {"command": "inspect-source", "effect": "READ_ONLY", "role": "Query current SourceIR by real page or exact source handle, including native tables"},
        {"command": "reading-packet", "effect": "READ_ONLY_PROJECTION", "role": "Prepare page-indexed complete text/cell/asset reading parts with exact SourceIR backreferences"},
        {"command": "reading-workspace", "effect": "READ_ONLY_PROJECTION", "role": "Reuse a PDF capture as page Markdown, high renders, full table views and image placement indexes"},
        {"command": "worked-example", "effect": "SYNTHETIC_EXAMPLE", "role": "Generate the packaged example and its complete tool inputs/outputs in a fresh directory"},
        {"command": "compile-map", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Resolve Map v1/v2 decisions or incremental reqpilot-map-edit/v1 inputs with optional map_dir base into the existing Document Map and scope requests"},
        {"command": "audit-map", "effect": "READ_ONLY_PROJECTION", "role": "Inspect source-first unassigned text/row/image ranges, numeric body-peer numbering leads and tree-order backtracks; these remain source-review leads, not original completeness"},
        {"command": "compile-items", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Compile initial or incremental uncovered text/table scopes while preserving existing SRC/REQ, images and decisions"},
        {"command": "image-packet", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Read Map-bound image scopes and optionally render source-bound original pages"},
        {"command": "compile-images", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Compile image SRC/REQ records; keep original pixels separate from Agent visual readings"},
        {"command": "compile-annotations", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Compile exact original numbering, controlled topics/context and visual readings against an existing candidate baseline"},
        {"command": "decomposition-packet", "effect": "READ_ONLY", "role": "Resolve selected parents to exact source segments and shared context"},
        {"command": "compile-decomposition", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Compile an explicitly authorized SPLIT/KEEP/UNDO with stable Snn children and exact source coverage"},
        {"command": "analysis-packet", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Freeze original, parent/image context and a supplied registered knowledge catalog for assessment"},
        {"command": "register-evidence", "effect": "PRIVATE_EVIDENCE_WRITE", "role": "Capture actual user/host registered internal files with exact source hashes and locators; metadata is declared, never fabricated by assessment candidates"},
        {"command": "compile-assessments", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Validate evidence-bound decisions and concrete missing-input questions; models cannot replace the catalog"},
        {"command": "manual-context", "effect": "READ_ONLY", "role": "Read exact current SRC/Snn context for an explicit user working opinion"},
        {"command": "import-manual-decisions", "effect": "EXPLICIT_USER_ARTIFACT_WRITE", "role": "Import only actual user-provided working opinions; not a model assessment or formal customer approval"},
        {"command": "deliver", "effect": "PRIVATE_DRAFT_EXPORT", "role": "Shared Matrix/Excel projection from the exact baseline and supplied review inputs"},
        {"command": "review-open", "effect": "PRIVATE_WORKSPACE_WRITE", "role": "Import an authorized existing source/Map/baseline and optional review state into a portable versioned loop"},
        {"command": "review-feedback", "effect": "PRIVATE_FEEDBACK_WRITE", "role": "Record version-bound user feedback or explicit Agent self-review findings without claiming acceptance"},
        {"command": "review-packet", "effect": "READ_ONLY", "role": "Read current feedback, complete selected originals, ancestor context, tables, images and artifact paths"},
        {"command": "review-revise", "effect": "PRIVATE_ARTIFACT_WRITE", "role": "Compile explicit classification, annotations, source-boundary or source-replayed Map revisions; preserve compatible state and disclose retired/stale subjects"},
        {"command": "review-close", "effect": "PRIVATE_REVIEW_ADVANCE", "role": "Check candidate differences and exact exported state, reject stale feedback and advance an immutable local draft"},
        {"command":"review-attach-delivery", "effect":"PRIVATE_DELIVERY_REGISTRATION", "role":"Verify and attach an already exported unchanged current review without creating a candidate. workspace run deliver does this automatically for a committed current-review export.", "arguments":["--review-dir","--delivery-dir","--expected-review-sha256","--workspace"]},
        {"command": "review-verify", "effect": "READ_ONLY", "role": "Verify complete source-bound review ancestry, preserved feedback, current effective artifacts and exact exported state for a host publisher"},
        {"command": "schemas", "effect": "READ_ONLY", "role": "Current shared model schemas, not a semantic validator"},
        {"command": "host", "effect": "EXPLICIT_HOST_ACTION", "role": "Optional existing ReqPilotX API/App Server adapter; requires --project-root"},
        {"command": "bootstrap", "effect": "PRIVATE_WORKSPACE_WRITE", "role": "Prepare a relocatable project AGENTS, skills, runtime and raw inputs for a new session"}
    ], "model_dispatch": "AGENT_HOST", "source_checkout_required_for_portable_path": False}


def capture(args: argparse.Namespace) -> int:
    root = runtime_root(args.project_root)
    command = [sys.executable, str(root / "scripts/fast_source_capture.py"), "--source", str(args.source.expanduser().resolve(strict=True)), "--output-dir", str(args.output_dir.expanduser().resolve())]
    if args.expected_sha256:
        command.extend(["--expected-sha256", args.expected_sha256])
    if args.capture_only:
        command.append("--capture-only")
    if args.progress:
        command.append("--progress")
    return subprocess.run(command, check=False).returncode


def host(args: argparse.Namespace) -> int:
    if args.project_root is None:
        raise ValueError("The optional host adapter requires an explicit --project-root; portable tools do not")
    script = args.project_root.expanduser().resolve() / "prototype/scripts/agent_workflow.py"
    if not script.is_file():
        raise ValueError("This checkout does not provide the App Server host adapter")
    return subprocess.run([sys.executable, str(script), *args.arguments], check=False).returncode


def workbench(args: argparse.Namespace) -> dict:
    """Find the installed local application's UI; never import a task silently."""
    if any(getattr(args,key,None) for key in ('source','map_dir','baseline_dir','review_dir','workspace','binding_file')):
        load_runtime(args.project_root)
        pending_overlays=[]
        if getattr(args,'workspace',None) and not any(getattr(args,key,None) for key in ('source','map_dir','baseline_dir','review_dir')):
            from app.application.agent_workflow.task_workspace import find_task_workspace,workspace_status
            found=find_task_workspace(explicit=args.workspace)
            if found is None:raise ValueError('Explicit --workspace has no task-context.json')
            state=workspace_status(found[0]);selected=state['selected'];args=argparse.Namespace(**vars(args))
            if state['current_working'].get('owner')=='portable_review':
                args.review_dir=Path(state['current_working']['review_root'])
            elif selected.get('ITEMS'):
                args.baseline_dir=Path(selected['ITEMS']['path']).parent
                pending_overlays=[{'kind':kind,'path':selected[kind]['path'],'state':'LOCAL_ONLY_UNTIL_REVIEW_OPEN'}
                    for kind in ('ANNOTATIONS','DECOMPOSITION') if selected.get(kind) and Path(selected[kind]['path']).parent!=args.baseline_dir]
            elif selected.get('MAP'):
                args.map_dir=Path(selected['MAP']['path']).parent
        from workbench_tools import sync_workbench
        result=sync_workbench(args)
        if pending_overlays:
            result['pending_local_overlays']=pending_overlays
            result['overlay_next']='Use workspace run review-open to gather the selected overlays into the source-bound review bundle; this synchronization sent the saved item bundle.'
        return result
    from urllib.parse import urlsplit, urlunsplit
    from urllib.request import urlopen
    import webbrowser
    url = args.url or os.environ.get("REQPILOT_WORKBENCH_URL", "http://127.0.0.1:8000/workspace")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
        raise ValueError("The workbench opener accepts the configured local application URL only")
    try:
        with urlopen(urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")), timeout=3) as response:
            html = response.read(128 * 1024).decode("utf-8", errors="replace")
        if "reqpilotx" not in html.lower():
            raise ValueError("The local service does not identify itself as ReqPilotX")
    except OSError:
        return {"status": "NOT_RUNNING", "url": url,
            "artifact_import": "NOT_PERFORMED", "document_and_stage_verification": "NOT_PERFORMED",
            "next": "Use the configured application startup procedure, then run workbench again. Local drafts may still be saved; frontend review is not available yet. This command does not start the application or import a review directory."}
    opened = webbrowser.open(url) if args.open else False
    return {"status": "AVAILABLE", "url": url, "opened": opened,
        "artifact_import": "NOT_PERFORMED", "document_and_stage_verification": "NOT_PERFORMED",
        "next": "Open the returned URL and check the intended project/document and actual visible stage. opened only reports the browser launch request. Supply --workspace and a saved --map-dir, --baseline-dir or --review-dir to synchronize a source-bound result."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, help="Explicit development or optional host checkout; never required by a built portable bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("doctor", doctor), ("catalog", catalog)):
        sub.add_parser(name).set_defaults(func=func)
    task_api = sub.add_parser('task', help='Typed business requests shared by CLI and the optional MCP adapter')
    task_api.add_argument('task_action', choices=read_json(PLUGIN_ROOT/'scripts/task-api-capabilities.json')['actions'])
    task_api.add_argument('--working-root', type=Path, required=True)
    task_api.add_argument('--input', type=Path, required=True, help='Structured task request; program supplies artifact paths and identities')
    task_api.set_defaults(func=typed_task_operation)
    workspace=sub.add_parser('workspace',help='Physical document task with automatic artifact locations; existing compiler commands remain available')
    actions=workspace.add_subparsers(dest='workspace_command',required=True)
    initialize=actions.add_parser('init',help='Create one durable task beneath a project/document directory')
    initialize.add_argument('--working-root',type=Path,required=True)
    initialize.add_argument('--source',type=Path,required=True)
    initialize.add_argument('--goal',required=True,help='The actual user request for this task')
    initialize.add_argument('--project-key');initialize.add_argument('--document-key')
    initialize.add_argument('--creation-key', help='Replay the same logical task creation without creating another task')
    initialize.add_argument('--project-id');initialize.add_argument('--document-id')
    confirmation=initialize.add_mutually_exclusive_group()
    confirmation.add_argument('--require-map-confirmation',action='store_true',default=None,help='Explicitly require this Map to be confirmed before compiling items; new tasks otherwise proceed autonomously')
    confirmation.add_argument('--no-map-confirmation',action='store_true',default=None,help='Compatibility alias for autonomous new work; never changes a saved task policy')
    initialize.add_argument('--workbench-url',help='Actual application URL with projectId/documentId; initialization remains saved if the service is offline')
    initialize.add_argument('--binding-file',type=Path);initialize.add_argument('--open',action='store_true')
    initialize.add_argument('--full',action='store_true',help='Include complete index/history rather than compact task references')
    initialize.set_defaults(func=workspace_operation)
    for action in ('status','recover'):
        task=actions.add_parser(action)
        task.add_argument('--workspace',type=Path,help='Task root or child directory; otherwise discover from the current directory')
        task.add_argument('--full',action='store_true',help='Include complete index/history rather than compact task references')
        task.set_defaults(func=workspace_operation)
    execute=actions.add_parser('run',description='Run an existing compiler with saved task inputs and a program-assigned output directory. deliver automatically attaches the committed unchanged-current-review export. review-close is separate and applies only to a real revision candidate.')
    execute.add_argument('operation',choices=('capture','reading-workspace','read-pdf-visual','revise-visual-text','compile-map','compile-items','compile-images','compile-annotations','compile-decomposition','deliver','review-open'))
    execute.add_argument('--workspace',type=Path)
    execute.add_argument('--expected-revision', help='The opaque revision returned by workspace status')
    execute.add_argument('--command-id', help='Stable key for replaying one logical operation')
    execute.add_argument('--binding-file',type=Path)
    for flag in ('input','source-ir','source','map-dir','baseline-dir','annotations','decomposition','previous-decomposition','knowledge-catalog','analysis-packet','analysis-policy','assessments','manual-decisions'):
        execute.add_argument('--'+flag,type=Path)
    execute.add_argument('--expected-sha256');execute.add_argument('--capture-only',action='store_true');execute.add_argument('--progress',action='store_true')
    execute.add_argument('--full',action='store_true',help='Include complete index/history rather than compact task references')
    execute.set_defaults(func=workspace_operation)
    ui = sub.add_parser("workbench")
    ui.add_argument("--url")
    ui.add_argument("--open", action="store_true")
    ui.add_argument('--source',type=Path)
    selection=ui.add_mutually_exclusive_group()
    for flag in ('--map-dir','--baseline-dir','--review-dir'):selection.add_argument(flag,type=Path)
    ui.add_argument('--project-id');ui.add_argument('--document-id')
    ui.add_argument('--binding-file',type=Path)
    ui.add_argument('--workspace',type=Path)
    confirmation=ui.add_mutually_exclusive_group()
    confirmation.add_argument('--require-map-confirmation',action='store_true',default=None,help='Require Map confirmation on a new connection; saved policies are preserved when no flag is supplied')
    confirmation.add_argument('--no-map-confirmation',action='store_true',default=None,help='Compatibility alias for autonomous new connections; cannot change an existing connection policy')
    ui.add_argument('--goal',default='')
    ui.set_defaults(func=workbench)
    pull=sub.add_parser('workbench-pull')
    pull.add_argument('--binding-file',type=Path);pull.add_argument('--source',type=Path)
    pull.add_argument('--review-dir',type=Path)
    pull.add_argument('--workspace',type=Path)
    pull.set_defaults(func=workbench_pull)
    init = sub.add_parser("bootstrap")
    init.add_argument("--workspace", type=Path, required=True)
    init.add_argument("--source", type=Path, action="append")
    init.set_defaults(func=bootstrap)
    cap = sub.add_parser("capture")
    cap.add_argument("--source", type=Path, required=True)
    cap.add_argument("--output-dir", type=Path, required=True)
    cap.add_argument("--expected-sha256")
    cap.add_argument("--capture-only", action="store_true")
    cap.add_argument("--progress", action="store_true")
    cap.add_argument('--workspace',type=Path);cap.add_argument('--binding-file',type=Path)
    cap.set_defaults(func=capture)
    attach=sub.add_parser('review-attach-delivery',description='Register an already exported unchanged current review. No candidate or review-revise is needed; actual corrections still use review-close.')
    attach.add_argument('--review-dir',type=Path,required=True)
    attach.add_argument('--delivery-dir',type=Path,required=True,help='Complete existing export, including a COMMITTED task output; original files remain unchanged')
    attach.add_argument('--expected-review-sha256',required=True,help='Exact current review SHA, or the same successful request SHA on retry')
    attach.add_argument('--workspace',type=Path,help='Task owning this review and committed export, when applicable')
    attach.set_defaults(func=artifact_operation)
    for name in ("compile-map", "audit-map", "compile-items", "compile-images", "compile-annotations", "image-packet", "register-evidence", "analysis-packet", "compile-assessments", "deliver", "schemas", "inspect-source", "capture-pdf-text", "capture-visual-source", "read-pdf-visual", "revise-visual-text", "reading-packet", "reading-workspace", "worked-example", "decomposition-packet", "compile-decomposition", "review-open", "review-feedback", "review-packet", "review-revise", "review-close", "review-verify", "manual-context", "import-manual-decisions"):
        operation = sub.add_parser(name)
        operation.add_argument("--input", type=Path)
        operation.add_argument("--source-ir", type=Path)
        operation.add_argument("--source", type=Path)
        operation.add_argument("--map-dir", type=Path)
        operation.add_argument("--baseline-dir", type=Path)
        operation.add_argument("--review-dir", type=Path)
        operation.add_argument("--annotations", type=Path)
        operation.add_argument("--output-dir", type=Path)
        operation.add_argument("--page", type=int, action="append")
        operation.add_argument("--source-id", action="append")
        operation.add_argument("--view", choices=("raw", "authoring"), default="raw")
        operation.add_argument("--query")
        operation.add_argument("--capture-index", type=int, action="append")
        operation.add_argument("--offset", type=int, default=0)
        operation.add_argument("--limit", "--max-results", dest="limit", type=int, default=40)
        operation.add_argument("--source-item-id", action="append")
        operation.add_argument("--node-id", action="append")
        operation.add_argument("--decomposition", type=Path)
        operation.add_argument("--previous-decomposition", type=Path)
        operation.add_argument("--knowledge-catalog", type=Path)
        operation.add_argument("--analysis-packet", type=Path)
        if name in {'review-open', 'review-revise', 'deliver'}:
            operation.add_argument('--analysis-policy', type=Path, help='Explicit source-bound analysis-policy.json; imported into the review version or used by direct delivery')
        operation.add_argument("--assessments", type=Path)
        operation.add_argument("--previous-assessments", type=Path)
        operation.add_argument("--manual-decisions", type=Path)
        operation.add_argument("--previous-manual-decisions", type=Path)
        operation.add_argument("--unit-id", action="append")
        operation.add_argument('--workspace',type=Path,help='Bind confirmation and sync to this task; explicit compiler input/output paths keep their existing meaning')
        operation.add_argument('--binding-file',type=Path)
        operation.set_defaults(func=artifact_operation)
    native = sub.add_parser("host")
    native.add_argument("arguments", nargs=argparse.REMAINDER)
    native.set_defaults(func=host)
    args = parser.parse_args()
    try:
        if args.command in {'compile-items','compile-images','review-open'}:
            load_runtime(args.project_root)
            from workbench_tools import before_operation
            waiting=before_operation(args)
            if waiting:
                print(json.dumps(waiting,ensure_ascii=False,indent=2));return 0
        result = args.func(args)
        if isinstance(result, int):
            return result
        from workbench_tools import sync_after_operation
        result=sync_after_operation(args,result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in {"NOT_READY","FAILED"} else 2
    except (ValueError, OSError, ImportError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAILED", "command": args.command, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def workbench_pull(args: argparse.Namespace) -> dict:
    load_runtime(args.project_root)
    from workbench_tools import pull_workbench
    return pull_workbench(args)


def workspace_operation(args: argparse.Namespace) -> dict:
    load_runtime(args.project_root)
    from workspace_tools import dispatch
    return dispatch(args,Path(__file__))


def typed_task_operation(args: argparse.Namespace) -> dict:
    load_runtime(args.project_root)
    from task_service import execute_cli
    return execute_cli(args)


def artifact_operation(args: argparse.Namespace) -> dict:
    load_runtime(args.project_root)
    if os.environ.get('REQPILOT_NATIVE_AGENT_TOOLS') == '1' and getattr(args,'analysis_policy',None):
        if not args.analysis_policy.resolve(strict=True).is_relative_to(Path.cwd().resolve()):
            raise ValueError('Native analysis policy must stay inside the task workspace')
    if args.command == 'audit-map':
        from map_audit_tools import audit_map
        return audit_map(args)
    if args.command == 'capture-pdf-text':
        from pdf_text_tools import capture_pdf_text
        return capture_pdf_text(args)
    if args.command == 'capture-visual-source':
        from visual_source_tools import capture_visual_source
        return capture_visual_source(args)
    if args.command == 'read-pdf-visual':
        from visual_source_tools import read_pdf_visual
        return read_pdf_visual(args)
    if args.command == 'revise-visual-text':
        from visual_source_tools import revise_visual_text
        return revise_visual_text(args)
    if args.command == "reading-workspace":
        from reading_workspace_tools import reading_workspace
        return reading_workspace(args)
    if args.command == "worked-example":
        if args.output_dir is None:
            raise ValueError('worked-example requires a new --output-dir')
        from worked_example import generate
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            result = generate(args.project_root, args.output_dir.resolve())
        return {**result, 'output_dir': str(args.output_dir.resolve()),
                'example_only': True, 'next': 'Read generated map-decisions.json, item-decisions.json and requests for complete syntax. These are synthetic worked answers, not customer results.'}
    if args.command == "reading-packet":
        from reading_tools import reading_packet
        return reading_packet(args)
    if args.command in {"manual-context", "import-manual-decisions"}:
        from manual_tools import run_manual
        return run_manual(args)
    if args.command == "register-evidence":
        from evidence_tools import register
        return register(args)
    if args.command.startswith("review-"):
        from revision_tools import run_review
        return run_review(args)
    from artifact_tools import run_operation
    return run_operation(args)


if __name__ == "__main__":
    raise SystemExit(main())
