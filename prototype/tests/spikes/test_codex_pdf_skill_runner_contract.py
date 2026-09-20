from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spikes.codex_app_server.lifecycle import CanaryRuntimeState, LifecycleProbeError
from spikes.codex_app_server.pdf_skill_canary import (
    _EXPECTED_SKILLS,
    _PDF_SKILL,
    _STRUCTURE_SKILL,
    _isolate_runtime_skills,
    _structure_turn_input,
    _understanding_turn_input,
    _workspace_manifest,
)
from spikes.codex_app_server.pdf_tool_host import prepare_pdf_canary
from spikes.codex_app_server.synthetic_pdf_fixture import VISUAL_CANARY_CODE
from spikes.codex_app_server.synthetic_pdf_fixture import (
    EXPECTED_VISUAL_HEADINGS,
    EXPECTED_VISUAL_REQUIREMENT,
)


class CodexPdfSkillRunnerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reqpilotx-pdf-runner-")
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.server_dir = self.root / "server"
        for directory in (self.workspace, self.server_dir):
            directory.mkdir(mode=0o700)
        self.context = prepare_pdf_canary(
            server_private_dir=self.server_dir,
            model_workspace=self.workspace,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_understanding_input_is_explicit_skill_and_local_image_only(self) -> None:
        skill_path = (
            self.workspace / ".agents/skills/reqpilotx-pdf-understanding/SKILL.md"
        )
        turn_input = _understanding_turn_input(self.context, skill_path=skill_path)

        self.assertEqual(
            [item["type"] for item in turn_input], ["text", "skill", "localImage"]
        )
        self.assertTrue(
            turn_input[0]["text"].startswith("$reqpilotx-pdf-understanding\n")
        )
        for golden in (
            *EXPECTED_VISUAL_HEADINGS,
            EXPECTED_VISUAL_REQUIREMENT,
            f"CANARY CODE: {VISUAL_CANARY_CODE}",
            VISUAL_CANARY_CODE,
            "HARNESS -> SENSOR",
            "UPWARD",
            "ABOVE_SENSOR_BODY",
        ):
            self.assertNotIn(golden.casefold(), turn_input[0]["text"].casefold())
        self.assertEqual(
            turn_input[1],
            {"type": "skill", "name": _PDF_SKILL, "path": str(skill_path)},
        )
        self.assertEqual(
            turn_input[2],
            {"type": "localImage", "path": str(self.context.page_image_path)},
        )

    def test_structure_input_is_explicit_skill_without_image(self) -> None:
        skill_path = (
            self.workspace
            / ".agents/skills/reqpilotx-structure-tree-generation/SKILL.md"
        )
        turn_input = _structure_turn_input(
            self.context,
            understanding_proposal={"synthetic": "proposal"},
            understanding_proposal_sha256="a" * 64,
            coverage_receipt={"synthetic": "coverage"},
            skill_path=skill_path,
        )

        self.assertEqual([item["type"] for item in turn_input], ["text", "skill"])
        self.assertTrue(
            turn_input[0]["text"].startswith("$reqpilotx-structure-tree-generation\n")
        )
        self.assertNotIn("localImage", {item["type"] for item in turn_input})
        self.assertEqual(
            turn_input[1],
            {"type": "skill", "name": _STRUCTURE_SKILL, "path": str(skill_path)},
        )

    def test_skill_isolation_disables_each_ambient_skill_and_keeps_exact_allowlist(
        self,
    ) -> None:
        skill_paths = {
            _PDF_SKILL: self.workspace
            / ".agents/skills/reqpilotx-pdf-understanding/SKILL.md",
            _STRUCTURE_SKILL: self.workspace
            / ".agents/skills/reqpilotx-structure-tree-generation/SKILL.md",
        }
        runtime = _runtime(self.root, self.workspace)
        ambient_paths = [
            "/opt/codex/skills/ambient-one/SKILL.md",
            "/opt/codex/skills/ambient-two/SKILL.md",
        ]
        first = _skills_list_result(
            self.workspace,
            [
                *[
                    {"name": name, "path": str(path), "enabled": True}
                    for name, path in skill_paths.items()
                ],
                *[
                    {"name": f"ambient-{index}", "path": path, "enabled": True}
                    for index, path in enumerate(ambient_paths, start=1)
                ],
            ],
        )
        second = _skills_list_result(
            self.workspace,
            [
                *[
                    {"name": name, "path": str(path), "enabled": True}
                    for name, path in skill_paths.items()
                ],
                *[
                    {"name": f"ambient-{index}", "path": path, "enabled": False}
                    for index, path in enumerate(ambient_paths, start=1)
                ],
            ],
        )

        with patch(
            "spikes.codex_app_server.pdf_skill_canary._list_skills",
            side_effect=[first, second],
        ) as list_skills:
            isolated, disabled_count = _isolate_runtime_skills(
                runtime,
                skill_paths=skill_paths,
                timeout_seconds=1.0,
            )

        self.assertEqual(disabled_count, 2)
        self.assertEqual(list_skills.call_count, 2)
        self.assertEqual(set(_EXPECTED_SKILLS), set(skill_paths))
        self.assertEqual(isolated.command[-2], "--config")
        config = isolated.command[-1]
        for path in ambient_paths:
            self.assertIn(f'path="{path}"', config)
        self.assertNotIn(str(skill_paths[_PDF_SKILL]), config)
        self.assertNotIn(str(skill_paths[_STRUCTURE_SKILL]), config)

    def test_workspace_manifest_detects_mutation_and_rejects_symlink(self) -> None:
        file_path = self.workspace / "fixture.txt"
        file_path.write_text("before", encoding="utf-8")
        before = _workspace_manifest(self.workspace)
        file_path.write_text("after", encoding="utf-8")
        self.assertNotEqual(_workspace_manifest(self.workspace), before)

        os.symlink(file_path, self.workspace / "linked-fixture.txt")
        with self.assertRaisesRegex(
            LifecycleProbeError, "MODEL_WORKSPACE_SYMLINK_FORBIDDEN"
        ):
            _workspace_manifest(self.workspace)


def _runtime(root: Path, workspace: Path) -> CanaryRuntimeState:
    return CanaryRuntimeState(
        codex_home=root / "codex-home",
        command=("codex", "app-server"),
        environment={},
        log_dir=root / "logs",
        protocol={},
        sqlite_home=root / "sqlite",
        state_path=root,
        sterile_cwd=workspace,
        tmp_dir=root / "tmp",
    )


def _skills_list_result(
    workspace: Path, skills: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "data": [
            {
                "cwd": str(workspace),
                "errors": [],
                "skills": skills,
            }
        ]
    }


if __name__ == "__main__":
    unittest.main()
