"""Pin the installed official PDF skill for the native read-only capability.

Copies upstream files unchanged. It does not alter the legacy Agent Kit or
install dependencies; the native runtime verifies the same upstream identity.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.agents.portable_kit import AgentKitSkillProvenanceV1
from app.adapters.agents.codex_app_server.primary_runtime_pdf import discover_openai_primary_runtime_pdf_dependencies


def package(plugin: Path, runtime: Path):
    repo = Path(__file__).resolve().parents[2]
    upstream = runtime / 'plugins/openai-primary-runtime/plugins/pdf'
    version = json.loads((runtime/'runtime.json').read_text())['bundleVersion']
    root = plugin/'capabilities/pdf'
    if root.exists():
        raise ValueError('Capability snapshot already exists; preserve or explicitly version it before replacing')
    provenance = json.loads((repo/'agent-kit/provenance/openai-primary-runtime-pdf.v1.json').read_text())
    files = {'.agents/skills/pdf/SKILL.md': upstream/'skills/pdf/SKILL.md',
             '.agents/skills/pdf/agents/openai.yaml': upstream/'skills/pdf/agents/openai.yaml',
             '.agents/skills/pdf/container_tools/mark_artifact_operation_started.mjs': upstream/'skills/pdf/container_tools/mark_artifact_operation_started.mjs',
             'provenance/openai-primary-runtime-pdf.plugin.json': upstream/'.codex-plugin/plugin.json'}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name,path in files.items()}
    provenance.update(distribution_version=version,runtime_bundle_version=version,
        vendored_skill_sha256=hashes['.agents/skills/pdf/SKILL.md'],upstream_skill_sha256=hashes['.agents/skills/pdf/SKILL.md'],
        upstream_plugin_manifest_sha256=hashes['provenance/openai-primary-runtime-pdf.plugin.json'],
        upstream_openai_yaml_sha256=hashes['.agents/skills/pdf/agents/openai.yaml'],
        upstream_operation_helper_sha256=hashes['.agents/skills/pdf/container_tools/mark_artifact_operation_started.mjs'])
    dependencies=discover_openai_primary_runtime_pdf_dependencies(provenance=AgentKitSkillProvenanceV1.model_validate(provenance),runtime_root=runtime)
    for relative,path in files.items():
        target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    (root/'provenance/openai-primary-runtime-pdf.v1.json').write_text(json.dumps(provenance,indent=2)+'\n')
    manifest={'schema_version':'reqpilot-native-pdf-reading/v1','scope':'SOURCE_READING',
              'skill_path':'.agents/skills/pdf/SKILL.md','provenance_path':'provenance/openai-primary-runtime-pdf.v1.json',
              'files':hashes,'source_write_allowed':False,'dependency_installation_allowed':False}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return {'version':version,'capability_root':str(root),'file_count':len(files),
            'attestation_id':dependencies.attestation.attestation_id,'scope':'SOURCE_READING'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,default=Path.home()/'.cache/codex-runtimes/codex-primary-runtime')
    args=parser.parse_args();print(json.dumps(package(args.plugin.resolve(),args.runtime.resolve()),indent=2))
