from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from reqpilotx_server.domain.reading import (
    DocumentReadingOverviewV1,
    ReadingAIReadinessV1,
    ReadingActionRequestV1,
    ReadingArtifactV1,
    ReadingChatMessageV1,
    ReadingChatTurnV1,
    ReadingConversationV1,
    ReadingContextV1,
    ReadingNoteV1,
    ReadingRetrievalV1,
    ReadingStructureMapV1,
)
from reqpilotx_server.domain.reading_catalog import (
    ReadingDocumentCatalogV1,
    ReadingProjectCatalogV1,
)


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROTOTYPE_ROOT.parent / "apps/server/src/reqpilotx_server"
FRONTEND_ROOT = PROTOTYPE_ROOT / "frontend" / "src"

_READING_APPLICATION_FORBIDDEN_PREFIXES = (
    "reqpilotx_server.application.extraction",
    "reqpilotx_server.application.requirements",
    "reqpilotx_server.application.requirement_matrix",
    "reqpilotx_server.application.assessment",
    "reqpilotx_server.application.exports",
    "reqpilotx_server.application.knowledge",
    "reqpilotx_server.application.runtime",
    "reqpilotx_server.domain.extraction.structure",
    "reqpilotx_server.domain.extraction.semantics",
    "reqpilotx_server.domain.extraction.requirement",
    "reqpilotx_server.services.extractor",
    "reqpilotx_server.services.extraction_preview",
    "reqpilotx_server.services.extraction_shadow_runs",
    "reqpilotx_server.services.llm_extraction",
)

_READING_SLICE_FILES = (
    APP_ROOT / "application" / "reading.py",
    APP_ROOT / "application" / "reading_catalog.py",
    APP_ROOT / "application" / "reading_projection.py",
    APP_ROOT / "application" / "reading_retrieval.py",
    APP_ROOT / "application" / "source_capture.py",
    APP_ROOT / "application" / "source_asset_publication.py",
    APP_ROOT / "application" / "source_runtime.py",
    APP_ROOT / "application" / "build_source_ir.py",
    APP_ROOT / "adapters" / "models" / "reading_provider.py",
    APP_ROOT / "adapters" / "models" / "reading_json_chat.py",
    APP_ROOT / "adapters" / "persistence" / "sql_reading.py",
    APP_ROOT / "adapters" / "persistence" / "sql_reading_catalog.py",
    APP_ROOT / "adapters" / "persistence" / "sql_source_capture.py",
    APP_ROOT / "adapters" / "persistence" / "sql_source_asset_publication.py",
    APP_ROOT / "api" / "routers" / "reading.py",
    APP_ROOT / "domain" / "reading.py",
    APP_ROOT / "domain" / "reading_catalog.py",
    APP_ROOT / "ports" / "reading_provider.py",
    APP_ROOT / "ports" / "reading_repository.py",
    APP_ROOT / "ports" / "reading_catalog_repository.py",
    APP_ROOT / "ports" / "source_capture_repository.py",
    APP_ROOT / "ports" / "source_asset_publication.py",
)

_READING_ONLY_REPOSITORY_FILES = (
    APP_ROOT / "ports" / "reading_repository.py",
    APP_ROOT / "adapters" / "persistence" / "sql_reading.py",
)


class ReadingIsolationTests(unittest.TestCase):
    def test_reading_application_has_no_formal_pipeline_import(self) -> None:
        violations: list[str] = []
        for path in _READING_SLICE_FILES:
            if not path.is_file():
                continue
            modules = _python_imports(path)
            violations.extend(
                f"{path.relative_to(APP_ROOT)}:{module}"
                for module in modules
                if module.startswith(_READING_APPLICATION_FORBIDDEN_PREFIXES)
            )
        self.assertEqual(violations, [])

    def test_source_capture_bridge_stops_at_source_ir(self) -> None:
        modules = _python_imports(APP_ROOT / "application" / "source_capture.py")
        extraction_imports = tuple(
            module for module in modules if module.startswith("reqpilotx_server.application.extraction")
        )
        self.assertEqual(extraction_imports, ())
        self.assertIn("reqpilotx_server.application.build_source_ir", modules)
        self.assertNotIn("reqpilotx_server.application.runtime", modules)
        source = (APP_ROOT / "application" / "source_capture.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "StructureRevision",
            "EffectiveStructure",
            "RequirementView",
            "RequirementSet",
            "document.extract",
            "structure.optimize",
        ):
            self.assertNotIn(forbidden, source)

    def test_reading_repository_has_no_source_fact_write_or_cleanup_capability(self) -> None:
        forbidden_symbols = (
            "append_capture",
            "purge_capture",
            "source_capture_snapshots",
            "source_capture_asset_references",
        )
        violations: list[str] = []
        for path in _READING_ONLY_REPOSITORY_FILES:
            source = path.read_text(encoding="utf-8")
            for symbol in forbidden_symbols:
                if symbol in source:
                    violations.append(f"{path.relative_to(APP_ROOT)}:{symbol}")
        self.assertEqual(violations, [])

    def test_importing_reading_does_not_load_formal_runtime_or_ai_services(self) -> None:
        script = """
import json
import sys
import reqpilotx_server.application.reading
forbidden = [
    name for name in sys.modules
    if name == 'reqpilotx_server.application.runtime'
    or name.startswith('reqpilotx_server.application.processing_profiles')
    or name.startswith('reqpilotx_server.services.ai_copilot')
]
print(json.dumps(sorted(forbidden)))
"""
        environment = dict(os.environ)
        environment["REQPILOT_TEST_MODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROTOTYPE_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(completed.stdout.strip()), [])

    def test_formal_application_does_not_depend_on_reading(self) -> None:
        roots = [
            APP_ROOT / "application" / "extraction",
            APP_ROOT / "application" / "requirements.py",
            APP_ROOT / "application" / "requirement_matrix.py",
            APP_ROOT / "application" / "assessment.py",
            APP_ROOT / "application" / "exports.py",
            APP_ROOT / "application" / "knowledge.py",
        ]
        violations: list[str] = []
        for root in roots:
            files = tuple(root.rglob("*.py")) if root.is_dir() else (root,)
            for path in files:
                for module in _python_imports(path):
                    if module.startswith(
                        (
                            "reqpilotx_server.application.reading",
                            "reqpilotx_server.domain.reading",
                            "reqpilotx_server.ports.reading",
                            "reqpilotx_server.adapters.persistence.sql_reading",
                        )
                    ):
                        violations.append(f"{path.relative_to(APP_ROOT)}:{module}")
        self.assertEqual(violations, [])

    def test_reading_contracts_do_not_reserve_formal_bridge_fields(self) -> None:
        contracts = (
            DocumentReadingOverviewV1,
            ReadingAIReadinessV1,
            ReadingActionRequestV1,
            ReadingArtifactV1,
            ReadingChatMessageV1,
            ReadingChatTurnV1,
            ReadingConversationV1,
            ReadingContextV1,
            ReadingNoteV1,
            ReadingRetrievalV1,
            ReadingStructureMapV1,
            ReadingDocumentCatalogV1,
            ReadingProjectCatalogV1,
        )
        serialized = json.dumps(
            [contract.model_json_schema() for contract in contracts],
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        for forbidden in (
            "handoff",
            "eligible_for",
            "requirement_set_id",
            "requirement_id",
            "is_requirement",
            "structure_revision_id",
            "effective_structure",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_frontend_reading_feature_has_no_formal_feature_import(self) -> None:
        reading_root = FRONTEND_ROOT / "features" / "reading"
        forbidden_fragments = (
            "../extraction/",
            "../matrix/",
            "../assessment/",
            "../knowledge/",
            "../export/",
            "../workspace/workspacepage",
        )
        violations: list[str] = []
        for path in reading_root.rglob("*.ts*"):
            source = path.read_text(encoding="utf-8").lower()
            for fragment in forbidden_fragments:
                if fragment in source:
                    violations.append(
                        f"{path.relative_to(FRONTEND_ROOT)}:{fragment}"
                    )
        self.assertEqual(violations, [])

    def test_reading_catalog_and_source_identity_never_use_wide_rows(self) -> None:
        catalog_source = (
            APP_ROOT / "adapters" / "persistence" / "sql_reading_catalog.py"
        ).read_text(encoding="utf-8").lower()
        source_capture = (APP_ROOT / "application" / "source_capture.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("select *", catalog_source)
        for forbidden in (
            "active_requirement_set_id",
            "ai_status",
            "extracted_text",
            "extraction_locked_at",
            "preview_status",
            "processing_profile_json",
            "publication_currentness_generation",
            "requirement_set_version",
            "workflow_stage",
        ):
            self.assertNotIn(forbidden, catalog_source)
        self.assertNotIn("db.get_project(", source_capture)
        self.assertNotIn("db.get_sor_document(", source_capture)

    def test_frontend_catalog_requests_use_reading_projection_endpoints(self) -> None:
        source = (
            FRONTEND_ROOT / "features" / "reading" / "readingApi.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('"/api/reading/projects"', source)
        self.assertNotIn('request<ReadingProject[]>("/api/projects")', source)
        self.assertNotIn(
            "`/api/projects/${encodeURIComponent(projectId)}/sor-documents`",
            source,
        )
        compact = source.replace(" ", "").replace("\n", "")
        for forbidden in (
            "/requirement-sets",
            "/extraction",
            "/workspace",
            "/matrix",
            "/assessment",
            "/knowledge",
            "/export",
            "/run-ai-analysis",
        ):
            self.assertNotIn(forbidden, compact.lower())


def _python_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(sorted(modules))


if __name__ == "__main__":
    unittest.main()
