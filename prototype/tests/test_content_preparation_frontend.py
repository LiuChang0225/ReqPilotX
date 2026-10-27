from __future__ import annotations

import unittest
from pathlib import Path

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

_PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
_FRONTEND = _PROTOTYPE_DIR.parent / "packages/workbench-ui"
_SOURCE = _FRONTEND / "src"


class ContentPreparationFrontendTests(unittest.TestCase):
    """Source-level invariants for the hard-cut React extraction workspace."""

    def test_react_preparation_keeps_source_and_review_invariants(self) -> None:
        panel = (
            _SOURCE / "features" / "extraction" / "RequirementTreePanel.tsx"
        ).read_text(encoding="utf-8")
        edits = (
            _SOURCE / "features" / "preparation" / "reviewEdits.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("原文已还原，结构条目待完善", panel)
        self.assertIn("sourcePagesReady", panel)
        self.assertIn(".sourcePage(", panel)
        self.assertIn(".latestRestoration(", panel)
        self.assertIn(".parsingDiagnostics(", panel)
        self.assertNotIn(".startRestoration(", panel)
        self.assertNotIn('visual_provider: "off"', panel)
        self.assertIn("normalizeRestorationPage", panel)
        self.assertIn("<StructuredTable", panel)
        self.assertIn("tree-page-fallback-note", panel)
        self.assertIn("右侧原 PDF", panel)
        self.assertIn("REQPILOTX_PDF", (
            _SOURCE / "features" / "pdf" / "PdfViewerPage.tsx"
        ).read_text(encoding="utf-8"))
        self.assertIn("structure-undo-request/v1", panel)
        self.assertIn("AI 优化当前结构", panel)
        # W3 makes classification acceptance an explicit prerequisite and
        # keeps the legacy enrichment/whole-session atomization publisher
        # fail-closed until it can bind that exact receipt.  Generating the
        # confirmation list must not hide any of these compound operations.
        self.assertNotIn(
            "requirement-enrichment-publication-preview-request/v1",
            panel,
        )
        self.assertNotIn(
            "requirement-enrichment-publish-draft-request/v1",
            panel,
        )
        self.assertNotIn("requirement-atomization-run-request/v1", panel)
        self.assertIn(".entryClassifications(", panel)
        self.assertIn(".acceptEntryClassifications(", panel)
        self.assertIn("生成确认清单", panel)
        self.assertIn("打开原 PDF", panel)
        self.assertNotIn("api.applyReviewedExtraction(", panel)
        self.assertIn("codePointOffset", edits)
        self.assertIn("source_spans", edits)
        self.assertIn("parent_review_node_id", edits)

        client = (_SOURCE / "shared" / "api" / "client.ts").read_text(
            encoding="utf-8"
        )
        workspace = (
            _SOURCE / "features" / "workspace" / "WorkspacePage.tsx"
        ).read_text(encoding="utf-8")
        shell = (
            _SOURCE / "features" / "workspace" / "WorkspaceShell.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("/extraction/structure/optimization", client)
        self.assertIn("/extraction/structure/optimization/status", client)
        self.assertIn("structureOptimizationPolling", workspace)
        for label in ("项目管理", "AI 辅助阅读", "需求整理", "需求分析", "矩阵导出"):
            self.assertIn(f'label: "{label}"', shell)
        for legacy_label in ("原文", "条款", "结构问题", "条目审阅"):
            self.assertNotIn(f'label: "{legacy_label}"', shell)

    def test_formal_frontend_has_no_legacy_static_controller_tree(self) -> None:
        # Empty historical directories are harmless; no legacy runtime file
        # may be shipped from the backend static tree.
        self.assertFalse(any(path.is_file() for path in (_PROTOTYPE_DIR / "app" / "static").rglob("*")))
        legacy_names = {
            "app.js",
            "content-preparation.js",
            "workspace-shell.js",
            "extraction-review.js",
            "pdf-viewer.html",
        }
        current_names = {
            path.name for root in (_SOURCE, _FRONTEND / "public")
            for path in root.rglob("*") if path.is_file()
        }
        self.assertTrue(legacy_names.isdisjoint(current_names))

    def test_pdf_restoration_is_a_lazy_react_route(self) -> None:
        app = (_PROTOTYPE_DIR.parent / "apps/web/src/app/App.tsx").read_text(encoding="utf-8")
        panel = (
            _SOURCE / "features" / "extraction" / "RequirementTreePanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('lazy(async () =>', app)
        self.assertIn('location.pathname === "/pdf-viewer"', app)
        self.assertIn('location.pathname === "/extraction-review"', app)
        self.assertIn("<LegacyWorkspaceRedirect", app)
        self.assertIn("restorationPage", panel)
        self.assertIn("原 PDF 不受影响", panel)
        self.assertNotIn("SERVING IMPACT · NONE", panel)

        styles = (_SOURCE / "styles" / "index.css").read_text(encoding="utf-8")
        self.assertIn(".pdf-overlay.is-issue:not(.is-selected) em", styles)


if __name__ == "__main__":
    unittest.main()
