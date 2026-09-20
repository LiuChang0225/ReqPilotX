"""Reuse the attested native PDF reading profile in the retained free loop."""
import hashlib
import json
from pathlib import Path

from app.adapters.agents.portable_kit import AgentKitSkillProvenanceV1
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError, _validate_instruction_sources
from app.adapters.agents.codex_app_server.primary_runtime_pdf import discover_openai_primary_runtime_pdf_dependencies
from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import configure_agent_native_read_only_runtime


def native_loop_skill_paths(plugin_root: Path, *, include_pdf: bool):
    paths = {'reqpilot': plugin_root / 'skills/reqpilot/SKILL.md'}
    cap = plugin_root / 'capabilities/pdf'
    if include_pdf and (cap / 'manifest.json').is_file():
        paths['pdf'] = cap / '.agents/skills/pdf/SKILL.md'
    return paths


def configure_native_loop_pdf(runtime, *, plugin_root: Path, evidence_root: Path):
    cap = plugin_root.resolve(strict=True) / 'capabilities/pdf'
    manifest = json.loads((cap/'manifest.json').read_text())
    if manifest.get('schema_version') != 'reqpilot-native-pdf-reading/v1' or manifest.get('scope') != 'SOURCE_READING':
        raise LifecycleProbeError('NATIVE_PDF_CAPABILITY_INVALID')
    if manifest.get('source_write_allowed') is not False or manifest.get('dependency_installation_allowed') is not False:
        raise LifecycleProbeError('NATIVE_PDF_CAPABILITY_SCOPE_INVALID')
    for relative, digest in manifest['files'].items():
        path = cap / relative
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(cap) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise LifecycleProbeError('NATIVE_PDF_CAPABILITY_DRIFT')
    provenance_path = cap / manifest['provenance_path']
    if not provenance_path.resolve(strict=True).is_relative_to(cap):
        raise LifecycleProbeError('NATIVE_PDF_PROVENANCE_PATH_INVALID')
    provenance = AgentKitSkillProvenanceV1.model_validate_json(provenance_path.read_bytes())
    skill = cap / manifest['skill_path']
    if skill.is_symlink() or not skill.resolve(strict=True).is_relative_to(cap):
        raise LifecycleProbeError('NATIVE_PDF_SKILL_PATH_INVALID')
    if hashlib.sha256(skill.read_bytes()).hexdigest() != provenance.vendored_skill_sha256:
        raise LifecycleProbeError('NATIVE_PDF_SKILL_PROVENANCE_DRIFT')
    dependencies = discover_openai_primary_runtime_pdf_dependencies(provenance=provenance)
    return configure_agent_native_read_only_runtime(runtime, readable_root=runtime.sterile_cwd,
        pdf_dependencies=dependencies, evidence_root=evidence_root)


def validate_native_profile_thread(result, *, model, cwd, profile_id, historical_cwd=None):
    thread, profile = result.get('thread'), result.get('activePermissionProfile')
    if (result.get('model') != model or result.get('cwd') != str(cwd)
            or result.get('approvalPolicy') != 'never' or not isinstance(profile, dict)
            or profile.get('id') != profile_id or profile.get('extends') is not None
            or not isinstance(thread, dict) or not thread.get('id') or thread.get('ephemeral') is not False
            or thread.get('cwd') not in {str(cwd), historical_cwd}):
        raise LifecycleProbeError('NATIVE_PDF_PERMISSION_PROFILE_MISMATCH')
    _validate_instruction_sources(result, sterile_cwd=cwd)
    return thread['id']
