"""Bounded tool host for the same portable ReqPilotX commands used by coding agents.

The host supplies access and artifact IO, not semantic decisions. Customer files
cannot choose executable programs, escape the task workspace, or grant approval.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
from uuid import uuid4


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(_json(value) + '\n')
    path.chmod(0o600)


class NativeLoopToolHost:
    OPERATIONS = {
        'catalog', 'schemas', 'inspect-source', 'reading-packet', 'capture', 'compile-map', 'compile-items',
        'image-packet', 'compile-images', 'decomposition-packet', 'compile-decomposition',
        'analysis-packet', 'compile-assessments', 'deliver', 'review-feedback',
        'review-packet', 'review-revise', 'review-close', 'review-verify',
        'reading-workspace', 'worked-example', 'compile-annotations', 'capture-pdf-text', 'capture-visual-source',
        'read-pdf-visual', 'revise-visual-text',
    }
    PATH_ARGS = {
        'input', 'source_ir', 'source', 'map_dir', 'baseline_dir', 'output_dir',
        'decomposition', 'previous_decomposition', 'knowledge_catalog', 'analysis_packet',
        'assessments', 'previous_assessments', 'annotations',
    }
    LIST_ARGS = {'page', 'source_id', 'source_item_id', 'node_id', 'unit_id', 'capture_index'}

    def __init__(self, *, workspace: Path, plugin_root: Path, project_root: Path | None = None,
                 publish_request: Callable[[str], dict] | None = None, review_dir: Path | None = None):
        self.workspace = workspace.resolve(strict=True)
        self.task_context = {}
        self.review_dir = (review_dir or self.workspace).resolve(strict=False)
        if not self.review_dir.is_relative_to(self.workspace) or (review_dir is not None and review_dir.is_symlink()):
            raise ValueError('Review directory must belong to the task workspace')
        self.operations = self.OPERATIONS | ({'review-open'} if self.review_dir != self.workspace else set())
        self.plugin_root = plugin_root.resolve(strict=True)
        self.reading_v1 = (self.plugin_root / 'scripts/reading_workspace_tools.py').is_file()
        self.map_edit_v1 = (self.plugin_root / 'scripts/map_edit_tools.py').is_file()
        self.authoring_read_v1 = (self.plugin_root / 'scripts/source_authoring_tools.py').is_file()
        self.pdf_text_capture_v1 = (self.plugin_root / 'scripts/pdf_text_tools.py').is_file()
        if not self.pdf_text_capture_v1:
            self.operations.discard('capture-pdf-text')
        if not (self.plugin_root / 'scripts/visual_source_tools.py').is_file():
            self.operations.difference_update({'capture-visual-source','read-pdf-visual','revise-visual-text'})
        if not self.reading_v1:
            self.operations.difference_update({'reading-workspace', 'worked-example', 'compile-annotations'})
        if not (self.plugin_root / 'scripts/reading_tools.py').is_file():
            self.operations.discard('reading-packet')
        self.project_root = project_root.resolve(strict=True) if project_root else None
        self.publish_request = publish_request
        if not (self.plugin_root / 'scripts/reqpilot.py').is_file():
            raise ValueError('The portable plugin tool entry is missing')
        self.trusted_path = self.workspace / '.host-artifacts.json'
        if self.trusted_path.is_symlink():
            raise ValueError('Host artifact ledger cannot be a symlink')
        self.trusted = json.loads(self.trusted_path.read_text()) if self.trusted_path.is_file() else {}
        if not self.trusted:
            self._record_artifacts()

    def _record_artifacts(self):
        for path in self.workspace.rglob('*'):
            if path.is_file() and not path.is_symlink():
                relative = str(path.relative_to(self.workspace))
                if relative.startswith(('inputs/', 'tool-results/')) or path == self.trusted_path or relative.endswith('.lock'):
                    continue
                self.trusted[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        temporary = self.trusted_path.with_suffix('.tmp')
        if temporary.is_symlink():
            raise ValueError('Host artifact ledger temporary file cannot be a symlink')
        temporary.write_text(_json(self.trusted) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.trusted_path)

    def _trusted_input(self, path):
        files = [value for value in path.rglob('*') if value.is_file()] if path.is_dir() else [path]
        if not files:
            raise ValueError('No compiler-produced artifact is available at this path')
        for value in files:
            key = str(value.relative_to(self.workspace))
            if self.trusted.get(key) != hashlib.sha256(value.read_bytes()).hexdigest():
                raise ValueError('This evidence or sealed artifact was not supplied by the host or a shared compiler: ' + key)

    def _relative_result(self, value):
        if isinstance(value, dict):
            return {key: self._relative_result(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._relative_result(item) for item in value]
        if isinstance(value, str) and value.startswith(str(self.workspace) + '/'):
            return value[len(str(self.workspace)) + 1:]
        return value

    def _navigation(self):
        """Offer compact, pageable indexes without hiding the full source files."""
        result = []
        supported = {'source-ir.json', 'source_ir.json', 'source-requirement-baseline.json', 'analysis-packet.json'}
        clean = lambda value: str('' if value is None else value).replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
        for relative, digest in sorted(self.trusted.items()):
            if Path(relative).name not in supported:
                continue
            path = self._path(relative)
            self._trusted_input(path)
            data = json.loads(path.read_text())
            kind, rows, columns = '', [], []
            if 'source_ir_id' in data and isinstance(data.get('nodes'), list):
                kind = 'source'
                columns = ['capture_index', 'source_id', 'kind', 'parent_source_id', 'page', 'preview']
                indexed = list(enumerate(data['nodes']))
                def position(pair):
                    locator = pair[1].get('native_locator') or {}
                    box = locator.get('bbox') or [0, 0, 0, 0]
                    return (locator.get('page_number') or 0, box[1], box[0], pair[0])
                for index, node in sorted(indexed, key=position):
                    rows.append([index, node.get('source_node_id'), node.get('kind'), node.get('parent_source_node_id'),
                                 (node.get('native_locator') or {}).get('page_number'), clean(node.get('raw_text'))[:90]])
            elif isinstance(data.get('records'), list) and 'document_map_artifact_sha256' in data:
                kind = 'items'
                columns = ['source_id', 'SRC', 'REQ', 'form', 'scope', 'label']
                for record in data['records']:
                    rows.append([record.get('source_record_id'), record.get('source_display_id'), record.get('requirement_display_id'),
                                 record.get('source_form'), record.get('logical_table_scope_id') or 'text:' + record.get('node_id', ''), clean(record.get('label'))[:90]])
            elif isinstance(data.get('units'), list) and 'packet_sha256' in data:
                kind = 'analysis'
                columns = ['unit_id', 'source_id', 'REQ', 'classes', 'source_preview']
                for unit in data['units']:
                    wording = unit.get('derived_statement') or ' '.join(part.get('text', '') for part in unit.get('source_segments', []))
                    rows.append([unit.get('unit_id'), unit.get('source_item_id'), unit.get('requirement_display_id'),
                                 ','.join(unit.get('classes', [])), clean(wording)[:110]])
            if not kind:
                continue
            content = '# NAVIGATION ONLY; read complete source/conditions before deciding. Source index is spatial, not a chapter ownership rule.\n'
            content += '\t'.join(columns) + '\n' + ''.join('\t'.join(clean(value) for value in row) + '\n' for row in rows)
            index_path = f'indexes/v1-{kind}-{digest[:24]}.tsv'
            target = self._path(index_path, exists=False)
            if target.exists():
                if target.read_text() != content:
                    raise ValueError('Navigation index differs from its frozen input')
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open('x', encoding='utf-8') as stream:
                    stream.write(content)
                target.chmod(0o600)
            result.append({'kind': kind, 'artifact_path': relative, 'navigation_path': index_path,
                           'entry_count': len(rows), 'input_sha256': digest,
                           'baseline_sha256': data.get('artifact_sha256') or data.get('baseline_sha256'),
                           'is_current': str(path.parent) == str(self.review_dir / self._review_current_path())})
        return result

    def _review_current_path(self):
        path = self.review_dir / 'workspace-manifest.json'
        return (json.loads(path.read_text()).get('current') or {}).get('version_path', '') if path.is_file() else ''

    def _reading_workspaces(self):
        result = []
        for relative in sorted(self.trusted):
            if Path(relative).name != 'reading-workspace.json':
                continue
            path = self._path(relative)
            self._trusted_input(path)
            value = json.loads(path.read_text())
            if value.get('schema_version') == 'reqpilot-reading-workspace/v1':
                result.append({'manifest_path': relative, **{key: value[key] for key in
                    ('source_sha256', 'source_ir_sha256', 'counts', 'pages', 'tables', 'placements', 'contact_sheet_path')},
                    **{key: value[key] for key in ('source_path', 'source_ir_path') if key in value}})
        return result

    def _path(self, value, *, root=None, exists=True):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError('Use a nonempty relative task path')
        root = root or self.workspace
        path = root / value
        if path.is_symlink() or not path.resolve(strict=exists).is_relative_to(root):
            raise ValueError('Path leaves its authorized task workspace')
        return path.resolve(strict=exists)

    def set_task_context(self, **context):
        """Bind this turn's actual assignment, without adding access or authorization."""
        self.task_context = dict(context)

    def task_brief(self):
        if not self.task_context:
            return None
        from app.application.agent_workflow.native_task_brief import build_task_brief
        path = self.review_dir/'workspace-manifest.json'
        manifest = json.loads(path.read_text()) if path.is_file() else {}
        return build_task_brief(**self.task_context, review=manifest.get('current'))

    def specs(self):
        from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopToolSpec

        def spec(name, description, properties, required):
            return NativeLoopToolSpec(name, description, {'type': 'object', 'properties': properties,
                'required': required, 'additionalProperties': False})
        string = {'type': 'string'}
        reading_options = ({'bbox': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4},
                            'scale': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 4},
                            'annotations': {'type': 'boolean'}} if self.reading_v1 else {})
        return [
            spec('reqpilot_context', 'Read current task navigation, source/version pins and available portable operations.', {}, []),
            spec('reqpilot_skill', 'Read packaged skill Markdown; list exact paths with an empty path. Named domain paths may omit skills/. pdf/SKILL.md resolves the packaged official PDF skill when available.', {'path': string}, ['path']),
            spec('reqpilot_read', 'Read a task artifact/directory or render an original PDF page. JSON pointer and line windows can focus large evidence; all authorized source stays accessible. A line response contains at most 500 lines; larger requests return exact next_read arguments for the remainder.' + (' PDF detail accepts page, optional bbox in visible PDF coordinates, scale up to 4, and annotations (default false). Default full page is 2x. HTML is read as source text; original PDF pixels remain the visual reference.' if self.reading_v1 else ''),
                 {'path': string, 'json_pointer': string, 'start_line': {'type': 'integer', 'minimum': 1},
                  'line_count': {'type': 'integer', 'minimum': 1}, 'page': {'type': 'integer', 'minimum': 1}, **reading_options}, ['path']),
            spec('reqpilot_candidate', 'Write a new immutable JSON candidate input. Name is a plain filename base; an optional .json suffix is accepted. This does not validate, publish or mark human approval.',
                 {'name': string, 'value': {'type': 'object', 'additionalProperties': True}}, ['name', 'value']),
            spec('reqpilot_run', 'Invoke an existing portable compiler, evidence reader, review tool or draft exporter. Arguments use CLI names with underscores and task-relative paths. review_dir is supplied by host.',
                 {'operation': {'type': 'string', 'enum': sorted(self.operations)},
                  'arguments': {'type': 'object', 'additionalProperties': True}}, ['operation', 'arguments']),
            spec('reqpilot_publish', 'Request application publication of the completed, checked review version. The application validates and saves only after this Agent turn completes; no human acceptance is implied.',
                 {'review_sha256': string}, ['review_sha256']),
        ]

    def _text(self, value):
        encoded = value if isinstance(value, str) else _json(value)
        if len(encoded) > 80000:
            destination = self.workspace / 'tool-results' / (uuid4().hex + '.json')
            _save(self._path(str(destination.relative_to(self.workspace)), exists=False), value)
            encoded = _json({'status': 'FULL_RESULT_SAVED', 'path': str(destination.relative_to(self.workspace)),
                             'characters': len(encoded), 'next': 'Read this artifact with JSON pointer or line windows; nothing was omitted from the saved result.'})
        return {'success': True, 'content_items': [{'type': 'inputText', 'text': encoded}]}

    def invoke(self, name, args):
        try:
            if name == 'reqpilot_context':
                manifest = self.review_dir / 'workspace-manifest.json'
                navigation = self._navigation()
                intake = None
                task_input = self.workspace / 'task-input.json'
                if task_input.is_file() and not any(e['kind'] == 'source' for e in navigation):
                    self._trusted_input(task_input)
                    source = json.loads(task_input.read_text()).get('source', {})
                    if source.get('path'):
                        self._trusted_input(self._path(source['path']))
                        directory = 'capture' if not (self.workspace/'capture').exists() else 'capture-' + uuid4().hex[:8]
                        intake = {'operation': 'capture', 'arguments': {'source': source['path'], 'output_dir': directory},
                                  'note': 'The host supplies the pinned source hash. This is a usable intake call, not a semantic map.'}
                brief = self.task_brief()
                return self._text({**({'task_brief':brief} if brief else {}),
                    'workspace_manifest': json.loads(manifest.read_text()) if manifest.is_file() else None,
                    'review_dir': str(self.review_dir.relative_to(self.workspace)),
                    'review_manifest_path': str(manifest.relative_to(self.workspace)),
                    'root_entries': sorted(p.name for p in self.workspace.iterdir()), 'operations': sorted(self.operations),
                    'artifact_navigation': navigation,
                    **({'source_intake': intake} if intake else {}),
                    **({'source_authoring': {'reads': [{'operation': 'inspect-source',
                        'arguments': {'source_ir': entry['artifact_path'], 'view': 'authoring'}} for entry in navigation if entry['kind'] == 'source'],
                        'filters': 'page, query (source substring), capture_index; all may be combined. Results include complete selected text, direct Map refs and actual table fields. next_read continues a large selection.',
                        'guide': 'skills/reqpilot/references/tools-map-edit.md'},
                        'native_execution': {'evidence_directory': str(self.workspace),
                            'instruction': 'Dynamic tool paths are relative to evidence_directory. For native commands use this absolute workdir or absolute source paths; the native default cwd is the separate skill runtime. No shell discovery is needed for inspect-source authoring view. Native CLI availability must be checked if you choose a fallback.'}}
                      if self.authoring_read_v1 else {}),
                    **({'map_authoring': 'compile-map also accepts reqpilot-map-edit/v1: use capture_index from navigation, exact text selectors, and an optional map_dir base to change only selected nodes/tables. The program binds original IDs/text/ranges and saves a normal v2 Map. Read skills/reqpilot/references/tools-map-edit.md. Save a meaningful scope before expanding the investigation; resume from its output directory.'} if self.map_edit_v1 else {}),
                    **({'reading_workspaces': self._reading_workspaces()} if self.reading_v1 else {}),
                    **({'source_text_recovery': {'operation':'capture-pdf-text',
                        'source':json.loads(task_input.read_text()).get('source',{}).get('path') if task_input.is_file() else None,
                        'source_ir_options':[entry['artifact_path'] for entry in navigation if entry['kind']=='source'],
                        'required_arguments':['source','source_ir','page (one or more original page numbers)','output_dir (new directory)'],
                        'optional_arguments':['query (substring of original drawing text)'],
                        'effect':'Append verified original PDF drawing-text observations in a new SourceIR file, preserving existing indexes. Compare pixels; clipped/hidden visibility is not proven. Use the returned source_ir and refs with the prior map_dir; replace only overlapping old content selections.',
                        'evidence_refs':'Map node evidence_refs [{source: ref, row_range: [lo,hi]}] preserve read-only carrier evidence without creating a table/paragraph extraction scope. logical_tables always create table scopes regardless of role label.'}} if self.pdf_text_capture_v1 else {}),
                    'batch_reading': ('After PDF capture, use reading-workspace with source, source_ir and a new output_dir. Read page Markdown, full table views and high renders; use PDF page/bbox/scale for local detail. Reuse the workspace. reading-packet is an optional exact data view, not a substitute for visual reading. Use worked-example to obtain complete synthetic input/output syntax.' if self.reading_v1 else
                        'After capture, run reading-packet with source_ir and a new output_dir; consume its full-text page parts using returned read arguments. Navigation TSVs are previews only. Use exact SourceIR pointers/inspect-source for unresolved geometry, original pages for layout or visual content, and selected review-packet records for revisions. Do not repeatedly walk the entire raw nodes array.'
                        if 'reading-packet' in self.operations else 'Read navigation TSVs with start_line/line_count to obtain many IDs. Use inspect-source page/source_id batches for complete evidence; avoid hundreds of individual ID reads.'),
                    'plugin_reference': 'skills/reqpilot/references/tools.md' if manifest.is_file() else 'skills/reqpilot-source-map/SKILL.md', 'paths': 'All task paths are relative. Source, Map and historical versions are immutable.',
                    'artifact_citations': 'Cite only artifact paths actually returned by tools, relative to the task workspace. The Skill/runtime execution cwd is a different directory: never prefix business artifact paths with it. The application supplies the final workspace and download links.',
                    'review_paths': 'Manifest-internal source/version paths and candidate_path/delivery_path in review-close JSON are relative to review_dir. Tool argument paths are relative to the outer task workspace. Before review-open, capture the original and compile Map/items with the same portable tools.',
                    'workflow': ('Read feedback and full relevant evidence, propose scoped changes, run shared compilers, inspect difference/carry-forward, assess affected units, call the internal deterministic exporter, close review, request publication. Normal Excel export needs no second model reading or analysis of the workbook. Source findings are Agent self-review, not human acceptance.' if manifest.is_file() else
                        'Start with the requested source/Map scope. Read its source evidence and relevant original/context, save and compile a meaningful Map edit, then self-check the saved artifact. Load item/classification/decomposition/review guides only when entering those authorized stages. Respect the user Map confirmation point; no extra per-item approvals.')})
            if name == 'reqpilot_skill':
                path = args.get('path')
                pdf_root = self.plugin_root / 'capabilities/pdf/.agents/skills/pdf'
                if not path:
                    return self._text([str(p.relative_to(self.plugin_root)) for p in sorted((self.plugin_root / 'skills').rglob('*.md'))]
                        + [str(p.relative_to(self.plugin_root)) for p in sorted(pdf_root.rglob('*.md'))])
                if path in {'pdf/SKILL.md', 'skills/pdf/SKILL.md'} and (pdf_root / 'SKILL.md').is_file():
                    path = str((pdf_root / 'SKILL.md').relative_to(self.plugin_root))
                elif isinstance(path, str) and Path(path).parts and (self.plugin_root / 'skills' / Path(path).parts[0]).is_dir():
                    path = 'skills/' + path
                source = self._path(path, root=self.plugin_root)
                if source.suffix != '.md' or not (source.is_relative_to(self.plugin_root / 'skills') or source.is_relative_to(pdf_root)):
                    raise ValueError('Only packaged skill Markdown is available here')
                return self._text(source.read_text())
            if name == 'reqpilot_read':
                return self._read(args)
            if name == 'reqpilot_candidate':
                candidate_name = args['name']
                if isinstance(candidate_name, str) and candidate_name.lower().endswith('.json'):
                    candidate_name = candidate_name[:-5]
                if not isinstance(candidate_name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,70}', candidate_name) or not isinstance(args['value'], dict):
                    raise ValueError('Candidate needs a plain name and JSON object')
                if {'path', 'start_line', 'total_lines', 'end_line', 'text'} <= set(args['value']) and 'schema_version' not in args['value']:
                    raise ValueError('This is a read-window wrapper, not a decision object. For a local Map change use reqpilot-map-edit/v1 and a map_dir base; to read parsed JSON use reqpilot_read with json_pointer="".')
                relative = 'inputs/' + candidate_name + '-' + uuid4().hex[:12] + '.json'
                _save(self._path(relative, exists=False), args['value'])
                return self._text({'status': 'CANDIDATE_INPUT_WRITTEN', 'path': relative, 'validated': False})
            if name == 'reqpilot_run':
                return self._text(self._run(args['operation'], args['arguments']))
            if name == 'reqpilot_publish':
                if self.publish_request is None:
                    raise ValueError('This host exports private drafts only; no application publication adapter is attached')
                return self._text(self.publish_request(args['review_sha256']))
            raise ValueError('Unknown ReqPilotX tool')
        except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            return {'success': False, 'content_items': [{'type': 'inputText', 'text': _json({'status': 'TOOL_ERROR', 'error': str(exc)})}]}

    def _read(self, args):
        path = self._path(args['path'])
        if path.is_dir():
            return self._text({'path': args['path'], 'entries': sorted(p.name for p in path.iterdir())})
        suffix = path.suffix.lower()
        extras = set(args) & {'bbox', 'scale', 'annotations'}
        if extras and (not self.reading_v1 or suffix != '.pdf' or 'page' not in args):
            raise ValueError('PDF bbox/scale/annotations require a supported reader and an explicit original page')
        render_details = {}
        if suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
            mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}[suffix]
            data = path.read_bytes()
        elif suffix == '.pdf':
            import pymupdf
            with pymupdf.open(path) as document:
                if 'page' not in args:
                    return self._text({'page_count': len(document), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                       'next': 'Read any original page by passing page (1-based).'})
                page = args['page']
                if type(page) is not int or not 1 <= page <= len(document):
                    raise ValueError('Original page is outside this document')
                if self.reading_v1:
                    self._trusted_input(path)
                    from app.adapters.documents.pdf_source_crop_renderer import render_pdf_source_crop, PdfSourceCropRejected
                    bbox = args.get('bbox', list(document[page - 1].rect))
                    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(type(x) not in {int, float} for x in bbox):
                        raise ValueError('bbox needs four numeric coordinates in the visible PDF page space')
                    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    annotations = args.get('annotations', False)
                    if type(annotations) is not bool:
                        raise ValueError('annotations must be boolean')
                    try:
                        data = render_pdf_source_crop(path, page_number=page, bbox=tuple(float(x) for x in bbox),
                            expected_source_sha256=source_sha, scale=args.get('scale', 2.0), annotations=annotations)
                    except PdfSourceCropRejected as exc:
                        raise ValueError(str(exc)) from exc
                    render_details = {'source_sha256': source_sha, 'bbox': bbox,
                                      'scale': args.get('scale', 2.0), 'annotations': annotations}
                else:
                    data = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes('png')
            mime = 'image/png'
        else:
            if suffix not in {'.json', '.jsonl', '.md', '.txt', '.csv', '.tsv'} | ({'.html'} if self.reading_v1 else set()):
                raise ValueError('Use the delivery receipt to inspect binary exports')
            content = path.read_text(encoding='utf-8')
            pointer = args.get('json_pointer')
            if pointer is not None:
                value = json.loads(content)
                if pointer and not pointer.startswith('/'):
                    raise ValueError('JSON pointer must start with /')
                try:
                    for token in pointer.split('/')[1:]:
                        key = token.replace('~1', '/').replace('~0', '~')
                        value = value[int(key)] if isinstance(value, list) else value[key]
                except (KeyError, IndexError, ValueError, TypeError):
                    reply = self._text({'status': 'TOOL_ERROR',
                        'error': f'JSON pointer {pointer!r} does not resolve. Use json_pointer="" for the document root; "/" selects an empty-name key.',
                        'next_read': {'path': args['path'], 'json_pointer': ''}})
                    reply['success'] = False
                    return reply
                return self._text(value)
            lines = content.splitlines()
            start, count = args.get('start_line', 1), args.get('line_count', 160)
            if type(start) is not int or type(count) is not int or start < 1 or count < 1:
                raise ValueError('Invalid line window')
            requested_count = count
            count = min(count, 500)
            selected = lines[start - 1:start - 1 + count]
            # Pretty-print compact JSON for usable, repeatable line windows.
            if suffix == '.json' and len(lines) <= 2 and len(content) > 12000:
                lines = _json(json.loads(content)).splitlines()
                selected = lines[start - 1:start - 1 + count]
            end = start - 1 + len(selected)
            result = {'path': args['path'], 'start_line': start, 'total_lines': len(lines),
                      'end_line': end, 'text': '\n'.join(selected)}
            requested_end = min(start - 1 + requested_count, len(lines))
            if end < requested_end:
                result.update(window_limited=True, requested_end_line=requested_end,
                    next_read={'path': args['path'], 'start_line': end + 1, 'line_count': requested_end - end})
            return self._text(result)
        return {'success': True, 'content_items': [{'type': 'inputText', 'text': _json({'path': args['path'], 'page': args.get('page'), 'image_sha256': hashlib.sha256(data).hexdigest(), **render_details})},
            {'type': 'inputImage', 'imageUrl': 'data:' + mime + ';base64,' + base64.b64encode(data).decode()}]}

    def _run(self, operation, arguments):
        if operation in {'import-manual-decisions', 'manual-context'} or any(
                name.replace('-', '_') in {'manual_decisions', 'previous_manual_decisions', 'engineer_responses'} for name in arguments):
            raise ValueError('Native Agent tools cannot create, replace or withdraw engineer working opinions')
        if operation not in self.operations or not isinstance(arguments, dict):
            raise ValueError('Unknown portable operation')
        if operation == 'inspect-source' and self.authoring_read_v1:
            arguments = dict(arguments)
            if 'max_results' in arguments:
                if 'limit' in arguments and arguments['limit'] != arguments['max_results']:
                    raise ValueError('max_results is an alias of limit; supply one value or matching values')
                arguments['limit'] = arguments.pop('max_results')
            unsupported = set(arguments) - self.PATH_ARGS - self.LIST_ARGS - {'view', 'query', 'offset', 'limit'}
            if unsupported:
                raise ValueError('Unsupported inspect-source arguments: ' + ', '.join(sorted(unsupported))
                    + '. Use source_ir, view, query, page, source_id, capture_index, offset and limit (max_results is an alias).'
                    + ' For neighboring context, read its page or capture_index selection; context_before/context_after are not implemented and were not silently ignored.')
        if arguments.get('input') and operation in {'review-feedback', 'deliver'}:
            candidate = json.loads(self._path(arguments['input']).read_text())
            if operation == 'review-feedback' and any(item.get('origin') != 'AGENT_SELF_REVIEW' for item in candidate.get('feedback', [])):
                raise ValueError('Agent tools may record self-review findings only; user feedback comes from the application')
            if operation == 'deliver' and set(candidate) & {'assessments', 'decomposition', 'annotations', 'manual_decisions', 'engineer_responses'}:
                raise ValueError('Use compiler-produced artifact arguments for analysis, derivation and image readings')
        command = [sys.executable, str(self.plugin_root / 'scripts/reqpilot.py')]
        capture_source_sha = None
        if self.project_root:
            command += ['--project-root', str(self.project_root)]
        command.append(operation)
        if operation.startswith('review-'):
            command += ['--review-dir', str(self.review_dir)]
        for name, value in arguments.items():
            flag = '--' + name.replace('_', '-')
            if name in self.PATH_ARGS:
                path = self._path(value, exists=name != 'output_dir')
                if name == 'output_dir' and path == self.workspace:
                    raise ValueError('Use a fresh child output directory')
                if name not in {'input', 'output_dir'}:
                    self._trusted_input(path)
                if operation == 'capture' and name == 'source':
                    capture_source_sha = self.trusted[str(path.relative_to(self.workspace))]
                command += [flag, str(path)]
            elif name in self.LIST_ARGS:
                if name == 'capture_index' and (operation != 'inspect-source' or not self.authoring_read_v1):
                    raise ValueError('capture_index requires supported inspect-source authoring view')
                for entry in value if isinstance(value, list) else [value]:
                    if name == 'page' and (type(entry) is not int or entry < 1):
                        raise ValueError('Invalid source page')
                    if name == 'capture_index' and (type(entry) is not int or entry < 0):
                        raise ValueError('Invalid capture_index')
                    command += [flag, str(entry)]
            elif name in {'view', 'query', 'offset', 'limit'} and (operation == 'inspect-source' and self.authoring_read_v1 or operation == 'capture-pdf-text' and self.pdf_text_capture_v1 and name == 'query'):
                if name in {'view', 'query'} and not isinstance(value, str) or name in {'offset', 'limit'} and type(value) is not int:
                    raise ValueError('Invalid source inspection argument: ' + name)
                command.append(flag + '=' + str(value))
            elif name == 'expected_sha256' and operation == 'capture':
                if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                    raise ValueError('expected_sha256 must be a source SHA-256')
                command += [flag, value]
            else:
                raise ValueError('Unsupported portable argument: ' + name)
        if operation == 'capture' and capture_source_sha and 'expected_sha256' not in arguments:
            command += ['--expected-sha256', capture_source_sha]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, cwd=self.workspace,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'REQPILOT_NATIVE_AGENT_TOOLS': '1'})
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-10000:])
        try:
            value = json.loads(result.stdout)
            self._record_artifacts()
            return self._relative_result(value)
        except json.JSONDecodeError as exc:
            raise ValueError('Portable tool returned an invalid JSON response') from exc
