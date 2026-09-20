if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import fidelity_benchmark


class FidelityBenchmarkTests(unittest.TestCase):
    def test_collect_tree_child_codes_preserves_parent_order(self) -> None:
        sections = [
            {"section_code": "3.2.2", "structural_parent_key": "3.2"},
            {"section_code": "1", "structural_parent_key": "3.2.2"},
            {"section_code": "2", "structural_parent_key": "3.2.2"},
            {"section_code": "6", "structural_parent_key": "3.2.2"},
            {"section_code": "6.1", "structural_parent_key": "3.2.2::6"},
            {"section_code": "6.6", "structural_parent_key": "3.2.2::6"},
            {"section_code": "6.6.4", "structural_parent_key": "3.2.2::6.6"},
        ]

        tree = fidelity_benchmark._collect_tree_child_codes(sections)

        self.assertEqual(tree["3.2.2"], ["1", "2", "6"])
        self.assertEqual(tree["3.2.2::6"], ["6.1", "6.6"])
        self.assertEqual(tree["3.2.2::6.6"], ["6.6.4"])

    def test_collect_tree_child_codes_normalizes_main_scope_prefix(self) -> None:
        sections = [
            {"section_code": "1", "structural_parent_key": "MAIN:3.2.2"},
            {"section_code": "2", "structural_parent_key": "MAIN:3.2.2"},
            {"section_code": "6.1", "structural_parent_key": "MAIN:3.2.2::6"},
            {"section_code": "6.2", "structural_parent_key": "MAIN:3.2.2::6"},
        ]

        tree = fidelity_benchmark._collect_tree_child_codes(sections)

        self.assertEqual(tree["3.2.2"], ["1", "2"])
        self.assertEqual(tree["3.2.2::6"], ["6.1", "6.2"])

    def test_load_profiles_reads_required_tree_children(self) -> None:
        profiles = fidelity_benchmark._load_profiles(
            [
                {
                    "name": "oil-pump-tree",
                    "file_name_regex": "oil-pump",
                    "required_tree_children": {
                        "3.2.2": ["1", "2", "3"],
                        "3.2.2::6": ["6.1", "6.2"],
                    },
                }
            ],
            {},
        )

        self.assertEqual(
            profiles[0].required_tree_children,
            {
                "3.2.2": ["1", "2", "3"],
                "3.2.2::6": ["6.1", "6.2"],
            },
        )

    def test_lookup_requirement_text_falls_back_to_descendants(self) -> None:
        requirement_text_by_scoped_code = {
            "4.1.2.1": "ASC_F 需实时反馈当前油压及其有效位",
            "4.1.2.3": "显示策略内容",
        }
        tree_child_codes_by_parent = {
            "4.1.2": ["4.1.2.1", "4.1.2.3"],
        }

        text = fidelity_benchmark._lookup_requirement_text_for_scoped_code(
            "4.1.2",
            requirement_text_by_scoped_code,
            tree_child_codes_by_parent,
        )

        self.assertIn("ASC_F 需实时反馈当前油压", text)
        self.assertIn("显示策略内容", text)


if __name__ == "__main__":
    unittest.main()
