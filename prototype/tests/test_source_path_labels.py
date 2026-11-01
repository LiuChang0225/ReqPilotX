from __future__ import annotations

from types import SimpleNamespace
import unittest

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_ledger import source_item_display_label
from reqpilotx.application.agent_workflow.source_path_labels import compose_source_path_segment
from .test_source_requirement_baseline import _node, _reference_with_text_node


class SourcePathLabelTests(unittest.TestCase):
    CASES = (
        ("3.1", "3.1", "3.1"),
        ("3.1", "Power supply", "3.1 Power supply"),
        ("3.1", "3.1 Power supply", "3.1 Power supply"),
        ("3.1", "3.10 Power supply", "3.1 3.10 Power supply"),
        ("3.1", "3.1.1 Power supply", "3.1 3.1.1 Power supply"),
    )

    def test_exact_number_prefix_composition(self) -> None:
        for label, title, expected in self.CASES:
            with self.subTest(label=label, title=title):
                self.assertEqual(compose_source_path_segment(label, title), expected)

    def test_map_compiler_uses_the_same_path_segment_contract(self) -> None:
        for index, (label, title, expected) in enumerate(self.CASES):
            with self.subTest(label=label, title=title):
                parent_id = f"parent-{index}"
                reference = _reference_with_text_node(
                    node_id=parent_id,
                    label=label,
                    title=title,
                    contents=((f"content-{index}", f"{label} source body", True),),
                )
                nodes = reference["document_tree"]["nodes"]
                nodes.append(_node(f"leaf-{index}", parent_id, len(nodes), "Leaf", "", (1,), ()))
                mapping = core.compile_document_map_artifact(reference)
                leaf = next(node for node in mapping.nodes if node.node_id == f"leaf-{index}")
                self.assertEqual(leaf.ancestor_path[-1], expected)

    def test_source_item_label_does_not_treat_longer_number_as_same_prefix(self) -> None:
        record = SimpleNamespace(label="3.1", source_form=SimpleNamespace(value="NUMBERED_CLAUSE"))
        node = SimpleNamespace(label="3.1", title="3.10 Power supply", content_blocks=())
        self.assertEqual(
            source_item_display_label(record, node=node, has_structure_children=False),
            "3.1 3.10 Power supply",
        )


if __name__ == "__main__":
    unittest.main()
