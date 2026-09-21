from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from pathlib import Path


_PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
_FRONTEND = _PROTOTYPE_DIR.parent / "apps/web"
_SOURCE = _PROTOTYPE_DIR.parent / "packages/workbench-ui/src"


class WorkspaceManagementFrontendTests(unittest.TestCase):
    def test_react_vite_is_the_only_frontend_runtime(self) -> None:
        package = json.loads(
            (_PROTOTYPE_DIR.parent / "package.json").read_text(encoding="utf-8")
        )
        self.assertIn("npm run typecheck", package["scripts"]["build"])
        self.assertIn("@reqpilotx/web", package["scripts"]["build"])
        self.assertEqual(package["dependencies"]["react"], "19.2.8")
        self.assertEqual(package["dependencies"]["react-dom"], "19.2.8")
        self.assertEqual(package["dependencies"]["pdfjs-dist"], "4.10.38")
        self.assertNotIn("react-router-dom", package["dependencies"])

        app = (_FRONTEND / "src/app/App.tsx").read_text(encoding="utf-8")
        for route in (
            'location.pathname === "/"',
            'location.pathname === "/home"',
            'location.pathname === "/workspace"',
            'location.pathname === "/extraction-review"',
            'location.pathname === "/pdf-viewer"',
        ):
            self.assertIn(route, app)

    def test_workspace_is_split_by_product_domain(self) -> None:
        required = (
            "projects/ProjectManagementPanel.tsx",
            "extraction/RequirementTreePanel.tsx",
            "matrix/RequirementMatrixWorkbench.tsx",
            "exports/ExportsPanel.tsx",
            "settings/SettingsPanel.tsx",
        )
        for relative in required:
            with self.subTest(relative=relative):
                self.assertTrue((_SOURCE / "features" / relative).is_file())

        workspace = (
            _SOURCE / "features" / "workspace" / "WorkspacePage.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", workspace)
        self.assertNotIn("querySelector", workspace)

        shell = (
            _SOURCE / "features" / "workspace" / "WorkspaceShell.tsx"
        ).read_text(encoding="utf-8")
        for label in ("项目管理", "需求提取", "需求分析", "矩阵导出"):
            self.assertIn(f'label: "{label}"', shell)
        self.assertNotIn("切换语言", shell)

        projects = (
            _SOURCE / "features" / "projects" / "ProjectManagementPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("<ProjectDocumentLibrary", projects)
        library = (
            _SOURCE / "features" / "projects" / "ProjectDocumentLibrary.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("/pdf-viewer?embedded=1&fit=page", library)
        self.assertIn('<iframe title="客户文档预览"', library)
        self.assertIn('className="source-guidance"', projects)
        self.assertLess(
            projects.index('className="source-guidance"'),
            projects.index("PDF 可逐页核对"),
        )

        matrix = (
            _SOURCE / "features" / "matrix" / "RequirementMatrixWorkbench.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('role="treegrid"', matrix)
        self.assertIn('lens="analysis"', workspace)

        pdf_viewer = (
            _SOURCE / "features" / "pdf" / "PdfViewerPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("pdfCanvasOutput(", pdf_viewer)
        self.assertIn("transform: output.transform", pdf_viewer)
        self.assertNotIn("context.setTransform(", pdf_viewer)

    def test_vite_manifest_closes_over_hashed_assets(self) -> None:
        build = _FRONTEND / "dist"
        manifest_path = build / ".vite" / "manifest.json"
        self.assertTrue(manifest_path.is_file(), "run npm run build first")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["index.html"]
        self.assertTrue(entry["isEntry"])
        self.assertTrue((build / entry["file"]).is_file())
        for relative in entry.get("css", []) + entry.get("assets", []):
            self.assertTrue((build / relative).is_file())


if __name__ == "__main__":
    unittest.main()
