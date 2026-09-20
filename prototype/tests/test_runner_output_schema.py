from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import unittest

from pydantic import create_model

from app.application.agent_workflow.d2_decision_candidate import (
    D2LogicalStructureDecisionCandidateV2,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    D2LogicalStructureDecisionCandidateV3,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2LogicalStructureProposalV2,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3ParentBoundaryClassificationCandidateV2,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectCandidateV1,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (
    ParentProvisionObjectReconciliationCandidateV1,
)
from app.application.agent_workflow.document_map_replacement import (
    DocumentMapScopeReplacementCandidateV1,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapHeadingScopeCandidateV2,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class RunnerOutputSchemaTests(unittest.TestCase):
    def test_schema_keyword_names_do_not_remove_business_fields_or_definitions(
        self,
    ) -> None:
        child = create_model(
            "title", title=(str, ...), description=(str, ...), pattern=(str, ...)
        )
        model = create_model("Wire", default=(child, ...), examples=(list[child], ...))
        schema = compile_strict_runner_output_schema(model)
        self.assertEqual(set(schema["properties"]), {"default", "examples"})
        self.assertEqual(set(schema["$defs"]), {"title"})
        self.assertEqual(
            set(schema["$defs"]["title"]["properties"]),
            {"title", "description", "pattern"},
        )
        self.assertEqual(
            schema["$defs"]["title"]["required"], ["title", "description", "pattern"]
        )
        self.assertNotIn("title", schema["$defs"]["title"])

    def test_every_runner_retains_the_host_business_property_topology(self) -> None:
        from scripts.export_agent_kit_schemas import STRICT_RUNNER_SCHEMAS

        def compare(host: object, wire: object) -> None:
            if not isinstance(host, dict) or not isinstance(wire, dict):
                return
            for map_key in ("properties", "$defs"):
                if map_key in host:
                    self.assertEqual(set(host[map_key]), set(wire[map_key]))
                    for name in host[map_key]:
                        compare(host[map_key][name], wire[map_key][name])
            if "items" in host:
                compare(host["items"], wire["items"])
            for array_key in ("anyOf", "allOf"):
                for left, right in zip(
                    host.get(array_key, []), wire.get(array_key, []), strict=True
                ):
                    compare(left, right)

        for model in STRICT_RUNNER_SCHEMAS.values():
            with self.subTest(model=model.__name__):
                compare(
                    model.model_json_schema(),
                    compile_strict_runner_output_schema(model),
                )

    def test_every_object_property_is_required_and_host_constraints_are_deferred(
        self,
    ) -> None:
        for model in (
            D2LogicalStructureDecisionCandidateV2,
            D2LogicalStructureDecisionCandidateV3,
            D3ParentBoundaryClassificationCandidateV2,
            ParentProvisionObjectCandidateV1,
            ParentProvisionObjectReconciliationCandidateV1,
            DocumentMapScopeReplacementCandidateV1,
            DocumentMapHeadingScopeCandidateV2,
        ):
            with self.subTest(model=model.__name__):
                schema = compile_strict_runner_output_schema(model)
                _assert_strict_objects(self, schema)
                serialized = json.dumps(schema, sort_keys=True)
                self.assertNotIn('"oneOf"', serialized)
                for keyword in (
                    '"default"',
                    '"maxItems"',
                    '"maxLength"',
                    '"minimum"',
                    '"pattern"',
                ):
                    self.assertNotIn(keyword, serialized)

    def test_v3_region_wire_uses_app_server_supported_anyof(self) -> None:
        schema = compile_strict_runner_output_schema(
            D2LogicalStructureDecisionCandidateV3
        )
        items = schema["$defs"]["D2TableRegionDecisionCandidateV3"]["properties"][
            "regions"
        ]["items"]

        self.assertNotIn("oneOf", items)
        self.assertNotIn("discriminator", items)
        self.assertEqual(len(items["anyOf"]), 5)

    def test_locked_runner_schemas_equal_the_compiled_subset(self) -> None:
        from scripts.export_agent_kit_schemas import STRICT_RUNNER_SCHEMAS

        values = {
            "d2-logical-structure-decision-candidate.v2.schema.json": (
                D2LogicalStructureDecisionCandidateV2
            ),
            "d2-logical-structure-decision-candidate.v3.schema.json": (
                D2LogicalStructureDecisionCandidateV3
            ),
            "d3-parent-boundary-classification-candidate.v2.schema.json": (
                D3ParentBoundaryClassificationCandidateV2
            ),
            "parent-provision-object-candidate.v1.schema.json": (
                ParentProvisionObjectCandidateV1
            ),
            "parent-provision-object-reconciliation-candidate.v1.schema.json": (
                ParentProvisionObjectReconciliationCandidateV1
            ),
            "document-map-scope-replacement-candidate.v1.schema.json": DocumentMapScopeReplacementCandidateV1,
            "document-map-heading-scope-candidate.v2.schema.json": DocumentMapHeadingScopeCandidateV2,
        }
        # Include every exported runner contract so a new source enum cannot
        # silently leave a less frequently exercised window schema stale.
        values.update(STRICT_RUNNER_SCHEMAS)
        for filename, model in values.items():
            with self.subTest(filename=filename):
                observed = json.loads(
                    (REPOSITORY_ROOT / "agent-kit/schemas" / filename).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    observed,
                    compile_strict_runner_output_schema(model),
                )

    def test_region_capable_final_schema_equals_the_host_contract(self) -> None:
        observed = json.loads(
            (
                REPOSITORY_ROOT
                / "agent-kit/schemas/logical-structure-proposal.v2.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(observed, D2LogicalStructureProposalV2.model_json_schema())


def _assert_strict_objects(test: unittest.TestCase, value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_objects(test, item)
        return
    if not isinstance(value, dict):
        return
    properties = value.get("properties")
    if value.get("type") == "object" and isinstance(properties, dict):
        test.assertEqual(value.get("required"), list(properties))
        test.assertIs(value.get("additionalProperties"), False)
    for child in value.values():
        _assert_strict_objects(test, child)


if __name__ == "__main__":
    unittest.main()
