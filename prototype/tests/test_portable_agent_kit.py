from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from app.adapters.agents.portable_kit import (
    AgentKitRunnerOutputSchemaVariantV1,
    AgentKitStageSpecV1,
    AgentKitValidationError,
    compute_agent_kit_lock,
    load_portable_agent_kit,
)
from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.application.agent_workflow.extraction_runtime import (
    AGENT_EXTRACTION_STAGE_ORDER,
    AgentExtractionStage,
)
from app.adapters.agents.document_evidence_host import D1_TOOL_NAMES
from app.adapters.agents.stage_context import compile_agent_stage_context


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"


class PortableAgentKitTests(unittest.TestCase):
    def test_exact_bundle_loads_with_one_shared_parity_identity(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)

        self.assertEqual(kit.identity.kit_id, "reqpilotx-document-extraction")
        self.assertEqual(
            tuple(stage.stage for stage in kit.manifest.stages),
            AGENT_EXTRACTION_STAGE_ORDER,
        )
        reference = kit.runner_profile("desktop-reference")
        product = kit.runner_profile("app-server-product")
        self.assertEqual(reference.model_id, product.model_id)
        self.assertEqual(reference.reasoning_effort, product.reasoning_effort)
        self.assertEqual(reference.skill_ids, product.skill_ids)
        self.assertEqual(reference.tool_registry_id, product.tool_registry_id)
        self.assertEqual(
            reference.tool_registry_version,
            product.tool_registry_version,
        )
        self.assertTrue(reference.quality_gate_eligible)
        self.assertTrue(product.quality_gate_eligible)

    def test_discovery_profile_is_explicitly_non_gating(self) -> None:
        profile = load_portable_agent_kit(AGENT_KIT_ROOT).runner_profile(
            "desktop-discovery"
        )

        self.assertFalse(profile.quality_gate_eligible)
        self.assertTrue(profile.load_ambient_user_skills)
        self.assertTrue(profile.load_conversation_history)
        self.assertTrue(profile.allow_manual_intervention)

    def test_pdf_is_a_locked_capability_not_a_stage_semantic_owner(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        pdf_skill = next(
            item for item in kit.manifest.skills if item.skill_id == "pdf"
        )

        self.assertIsNone(pdf_skill.stage)
        self.assertEqual(
            pdf_skill.capability_for_stages,
            (
                AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                AgentExtractionStage.LOGICAL_STRUCTURE,
            ),
        )
        self.assertEqual(
            tuple(
                item.skill_id
                for item in kit.capability_skills_for_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING
                )
            ),
            ("pdf",),
        )
        provenance = kit.skill_provenance["pdf"]
        self.assertEqual(
            provenance.distribution_id,
            "pdf@openai-primary-runtime",
        )
        self.assertEqual(provenance.distribution_version, "26.904.11930")
        self.assertEqual(
            hashlib.sha256((kit.root / pdf_skill.path).read_bytes()).hexdigest(),
            provenance.upstream_skill_sha256,
        )
        self.assertEqual(
            pdf_skill.provenance_path,
            "provenance/openai-primary-runtime-pdf.v1.json",
        )

    def test_active_manifest_excludes_legacy_node_skills_and_provider_controls(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        serialized = json.dumps(kit.manifest.model_dump(mode="json"), sort_keys=True)
        policy = json.loads(
            (AGENT_KIT_ROOT / kit.manifest.policy_path).read_text(encoding="utf-8")
        )

        self.assertNotIn("requirement-view-assembly", serialized)
        self.assertNotIn("structure-tree-generation", serialized)
        self.assertNotIn("azure", serialized.lower())
        self.assertNotIn("aliyun", serialized.lower())
        self.assertIn(
            "legacy_structure_node_universe", policy["forbidden_primary_controls"]
        )
        self.assertIn("provider_readiness", policy["forbidden_primary_controls"])

    def test_tool_registry_exposes_no_raw_path_or_business_write(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        registry = json.loads(
            (AGENT_KIT_ROOT / kit.manifest.tool_registry_path).read_text(
                encoding="utf-8"
            )
        )

        self.assertFalse(registry["raw_paths_exposed"])
        self.assertFalse(registry["business_writes_exposed"])
        tools = {item["name"]: item for item in registry["tools"]}
        self.assertEqual(tools["parser.query_facts"]["authority"], "ADVISORY_EVIDENCE")
        self.assertEqual(
            tools["resolver.query_suggestions"]["authority"],
            "ADVISORY_EVIDENCE",
        )
        self.assertIn("document.render_region", tools)

    def test_d1_registry_is_typed_available_and_matches_the_host(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        tools = kit.required_tools_for_stage(
            AgentExtractionStage.DOCUMENT_UNDERSTANDING
        )

        self.assertEqual(
            tuple(item.name for item in tools), tuple(sorted(D1_TOOL_NAMES))
        )
        self.assertTrue(
            all(item.implementation_status == "AVAILABLE" for item in tools)
        )
        self.assertEqual(set(kit.tool_schema_sha256), set(D1_TOOL_NAMES))
        for tool in tools:
            self.assertIsNotNone(tool.input_schema_path)
            self.assertIsNotNone(tool.output_schema_path)
            input_sha, output_sha = kit.tool_schema_sha256[tool.name]
            self.assertEqual(len(input_sha), 64)
            self.assertEqual(len(output_sha), 64)

    def test_d2_advisory_tools_are_optional_not_an_activation_checklist(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        self.assertEqual(
            kit.required_tools_for_stage(AgentExtractionStage.LOGICAL_STRUCTURE),
            (),
        )
        optional = {
            item.name: item
            for item in kit.tool_registry.tools
            if AgentExtractionStage.LOGICAL_STRUCTURE in item.stages
            and not item.required_by_stages
        }
        self.assertTrue(
            {
                "document.get_logical_table",
                "document.get_logical_table_candidates",
                "resolver.query_suggestions",
                "source.resolve_handles",
            }.issubset(optional)
        )

    def test_d2_skill_always_selects_one_working_decision(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        skill = next(
            item
            for item in kit.manifest.skills
            if item.skill_id == "reqpilotx-logical-structure-reconstruction"
        )
        text = (AGENT_KIT_ROOT / skill.path).read_text(encoding="utf-8")

        self.assertIn("first Host-valid reversible proposal", text)
        self.assertIn("KEPT_UNRESOLVED", text)
        self.assertIn("must not block the next workflow", text)
        self.assertIn("KEEP_CURRENT_STRUCTURE", text)
        self.assertIn("never return a terminal `UNRESOLVED` status", text)
        self.assertIn("Evidence quality remains visible", text)

    def test_d1_stage_context_has_exact_reference_closure_and_is_portable(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        canonical = compile_agent_stage_context(
            kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        )

        self.assertEqual(canonical.skill_id, "reqpilotx-document-understanding")
        self.assertEqual(
            tuple(item.path for item in canonical.references),
            (
                "references/document-map-recovery.md",
                "references/evidence-authority.md",
                "references/failure-policy.md",
                "references/stage-contracts.md",
            ),
        )
        d1_stage = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.DOCUMENT_UNDERSTANDING
        )
        self.assertEqual(
            tuple(
                (item.execution_mode, item.schema_id)
                for item in d1_stage.runner_output_schema_variants
            ),
            (
                (
                    "DOCUMENT_MAP_HEADING_SCOPE",
                    "document-map-heading-scope-candidate/v2",
                ),
                (
                    "DOCUMENT_MAP_SCOPE_REPLACEMENT",
                    "document-map-scope-replacement-candidate/v2",
                ),
                ("DOCUMENT_MAP_SURVEY", "document-map-survey-candidate/v1"),
            ),
        )
        self.assertIn("BEGIN POLICY", canonical.context_text)
        self.assertIn("BEGIN REFERENCE", canonical.context_text)
        with tempfile.TemporaryDirectory() as raw:
            materialized = materialize_agent_kit(
                kit,
                Path(raw) / "desktop",
                runner_profile_id="desktop-reference",
            )
            copied = compile_agent_stage_context(
                load_portable_agent_kit(materialized.root),
                AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            )
        self.assertEqual(canonical.context_sha256, copied.context_sha256)
        self.assertEqual(canonical.context_text, copied.context_text)

        self.assertEqual(
            compile_agent_stage_context(
                kit,
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            ).stage,
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        )

    def test_d2_and_d3_have_locked_runner_and_final_schemas(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        expected = {
            AgentExtractionStage.LOGICAL_STRUCTURE: (
                "schemas/logical-structure-proposal.v2.schema.json",
                "reqpilotx-logical-structure-validator",
                "1.2.0",
                "d2-logical-structure-decision-candidate/v3",
                "AVAILABLE",
            ),
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION: (
                "schemas/parent-provision-proposal.v3.schema.json",
                "reqpilotx-parent-provision-validator",
                "1.0.0",
                "d3-parent-boundary-classification-candidate/v2",
                "AVAILABLE",
            ),
        }

        for stage, (
            schema_path,
            validator_id,
            validator_version,
            runner_schema_id,
            implementation_status,
        ) in expected.items():
            spec = next(item for item in kit.manifest.stages if item.stage is stage)
            self.assertEqual(spec.implementation_status, implementation_status)
            self.assertEqual(spec.artifact_schema_path, schema_path)
            self.assertEqual(spec.validator_id, validator_id)
            self.assertEqual(spec.validator_version, validator_version)
            self.assertEqual(spec.runner_output_schema_id, runner_schema_id)
            self.assertIsNotNone(spec.runner_output_schema_path)
            assert spec.runner_output_schema_path is not None
            self.assertTrue((kit.root / spec.runner_output_schema_path).is_file())
            self.assertTrue((kit.root / schema_path).is_file())
            if implementation_status == "AVAILABLE":
                self.assertEqual(compile_agent_stage_context(kit, stage).stage, stage)
            else:
                with self.assertRaisesRegex(AgentKitValidationError, "not available"):
                    compile_agent_stage_context(kit, stage)

        parent_spec = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        )
        structure_spec = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
        )
        self.assertEqual(
            structure_spec.artifact_schema_id,
            "logical-structure-proposal/v2",
        )
        self.assertEqual(
            structure_spec.runner_output_schema_path,
            "schemas/d2-logical-structure-decision-candidate.v3.schema.json",
        )
        self.assertEqual(
            tuple(
                (
                    item.execution_mode,
                    item.schema_id,
                    item.schema_path,
                )
                for item in parent_spec.runner_output_schema_variants
            ),
            (
                (
                    "OBJECT_BATCH",
                    "parent-provision-object-candidate/v1",
                    "schemas/parent-provision-object-candidate.v1.schema.json",
                ),
                (
                    "OBJECT_RECONCILIATION",
                    "parent-provision-object-reconciliation-candidate/v1",
                    "schemas/parent-provision-object-reconciliation-candidate.v1.schema.json",
                ),
                (
                    "OBJECT_WINDOW",
                    "parent-provision-object-window-candidate/v1",
                    "schemas/parent-provision-object-window-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_ITEM_TEXT_BATCH",
                    "source-item-text-batch-candidate/v1",
                    "schemas/source-item-text-batch-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
                    "source-item-text-boundary-agent-candidate/v1",
                    "schemas/source-item-text-boundary-agent-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
                    "source-item-text-classification-window-candidate/v1",
                    "schemas/source-item-text-classification-window-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
                    "source-requirement-boundary-agent-candidate/v1",
                    "schemas/source-requirement-boundary-agent-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
                    "source-requirement-classification-window-candidate/v1",
                    "schemas/source-requirement-classification-window-candidate.v1.schema.json",
                ),
                (
                    "SOURCE_REQUIREMENT_SLICE",
                    "source-requirement-slice-candidate/v1",
                    "schemas/source-requirement-slice-candidate.v1.schema.json",
                ),
            ),
        )
        self.assertTrue(
            (
                kit.root / "schemas/parent-provision-object-candidate.v1.schema.json"
            ).is_file()
        )
        self.assertTrue(
            (
                kit.root
                / "schemas/parent-provision-object-reconciliation-candidate.v1.schema.json"
            ).is_file()
        )

    def test_runner_output_schema_variants_are_unique_and_agent_owned(self) -> None:
        variant = AgentKitRunnerOutputSchemaVariantV1(
            execution_mode="OBJECT_BATCH",
            schema_id="parent-provision-object-candidate/v1",
            schema_path="schemas/parent-provision-object-candidate.v1.schema.json",
        )
        stage = AgentKitStageSpecV1(
            stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            owner="PRIMARY_AGENT",
            implementation_status="AVAILABLE",
            artifact_schema_id="parent-provision-proposal/v3",
            artifact_schema_path="schemas/parent-provision-proposal.v3.schema.json",
            runner_output_schema_id="d3-parent-boundary-classification-candidate/v2",
            runner_output_schema_path=(
                "schemas/d3-parent-boundary-classification-candidate.v2.schema.json"
            ),
            runner_output_schema_variants=(variant,),
            skill_id="reqpilotx-parent-provision-recognition",
            validator_id="reqpilotx-parent-provision-validator",
            validator_version="1.0.0",
        )

        self.assertEqual(stage.runner_output_schema_variants, (variant,))
        payload = stage.model_dump(mode="json")
        payload["runner_output_schema_variants"].append(
            {
                "execution_mode": "OBJECT_BATCH",
                "schema_id": "parent-provision-object-candidate/v2",
                "schema_path": "schemas/parent-provision-object-candidate.v2.schema.json",
            }
        )
        with self.assertRaisesRegex(ValueError, "unique sorted modes"):
            AgentKitStageSpecV1.model_validate(payload)

    def test_structure_handoff_variant_is_strict_output_compatible(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        stage = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
        )
        variant = next(
            item
            for item in stage.runner_output_schema_variants
            if item.execution_mode == "STRUCTURE_HANDOFF"
        )
        schema = json.loads((kit.root / variant.schema_path).read_text(encoding="utf-8"))

        self.assertEqual(variant.schema_id, "native-structure-handoff-result/v2")
        self.assertEqual(
            schema["properties"]["schema_version"],
            {
                "type": "string",
                "const": "native-structure-handoff-result/v2",
            },
        )
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["PUBLISHED", "NO_CHANGE"],
        )
        self.assertNotIn("UNRESOLVED", schema["properties"]["status"]["enum"])
        self.assertEqual(
            set(schema["required"]),
            set(schema["properties"]),
        )

    def test_byte_drift_and_extra_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "agent-kit"
            shutil.copytree(AGENT_KIT_ROOT, copied)
            skill = (
                copied
                / ".agents/skills/reqpilotx-parent-provision-recognition/SKILL.md"
            )
            skill.write_text(skill.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AgentKitValidationError, "exact lock"):
                load_portable_agent_kit(copied)

        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "agent-kit"
            shutil.copytree(AGENT_KIT_ROOT, copied)
            (copied / "linked-reference").symlink_to(copied / "references")
            with self.assertRaisesRegex(
                AgentKitValidationError, "cannot contain symlinks"
            ):
                load_portable_agent_kit(copied)

        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "agent-kit"
            shutil.copytree(AGENT_KIT_ROOT, copied)
            (copied / "unexpected.txt").write_text("drift", encoding="utf-8")
            with self.assertRaisesRegex(AgentKitValidationError, "exact lock"):
                load_portable_agent_kit(copied)

    def test_relocked_false_upstream_provenance_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            copied = Path(raw) / "agent-kit"
            shutil.copytree(AGENT_KIT_ROOT, copied)
            provenance_path = (
                copied / "provenance/openai-primary-runtime-pdf.v1.json"
            )
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["upstream_skill_sha256"] = "0" * 64
            provenance_path.write_text(
                json.dumps(provenance, indent=2) + "\n",
                encoding="utf-8",
            )
            lock = compute_agent_kit_lock(copied)
            (copied / "agent-kit.lock.json").write_text(
                lock.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AgentKitValidationError,
                "Skill provenance is invalid",
            ):
                load_portable_agent_kit(copied)

    def test_both_runners_materialize_the_same_complete_bundle(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        with tempfile.TemporaryDirectory() as raw:
            desktop = materialize_agent_kit(
                kit,
                Path(raw) / "desktop",
                runner_profile_id="desktop-reference",
            )
            product = materialize_agent_kit(
                kit,
                Path(raw) / "app-server",
                runner_profile_id="app-server-product",
            )

            self.assertEqual(desktop.identity, product.identity)
            self.assertTrue((desktop.root / "AGENTS.md").is_file())
            self.assertTrue(
                (
                    desktop.root
                    / ".agents/skills/reqpilotx-document-understanding/SKILL.md"
                ).is_file()
            )
            self.assertTrue((desktop.root / "references/core-ontology.md").is_file())
            self.assertTrue(
                (desktop.root / "scripts/canonical_json_sha256.py").is_file()
            )
            self.assertEqual(
                desktop.runner_profile.skill_ids,
                product.runner_profile.skill_ids,
            )

    def test_materializer_never_overwrites_an_existing_destination(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        with tempfile.TemporaryDirectory() as raw:
            existing = Path(raw) / "existing"
            existing.mkdir()
            marker = existing / "owned-by-user"
            marker.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(
                AgentKitValidationError,
                "must not already exist",
            ):
                materialize_agent_kit(
                    kit,
                    existing,
                    runner_profile_id="desktop-reference",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
