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


if __name__ == "__main__":
    unittest.main()
