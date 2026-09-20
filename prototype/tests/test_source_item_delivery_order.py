# ruff: noqa: E402 -- isolate runtime before importing application modules.
from copy import deepcopy
import json
import unittest

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_decomposition import (
    compile_source_item_decomposition,
)
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
from app.application.agent_workflow.native_source_graph import (
    _verify_published_snapshot,
)
from .test_source_requirement_baseline import (
    _reference_with_text_node,
    _text_candidate,
    _text_item,
)


def fixture():
    text = "Open.Close.Start.Stop."
    reference = _reference_with_text_node(
        node_id="clause", label="1", title="Commands", contents=(("body", text, False),)
    )
    reference["document_tree"]["nodes"] = [
        n
        for n in reference["document_tree"]["nodes"]
        if n["node_id"] in {"root", "clause"}
    ]
    reference["logical_tables"] = []
    document_map = core.compile_document_map_artifact(reference)
    request = core.build_source_item_text_scope_request(document_map, node_id="clause")
    candidate = _text_candidate(
        request,
        items=(
            _text_item(request, key="first", label="First", start=0, end=11),
            _text_item(request, key="second", label="Second", start=11, end=len(text)),
        ),
    )
    baseline, _ = core.compile_source_requirement_baseline(
        document_map=document_map,
        requests=(),
        candidates=(),
        text_requests=(request,),
        text_candidates=(candidate,),
    )
    decisions = []
    for record, words in zip(
        baseline.records, (("Open.", "Close."), ("Start.", "Stop."))
    ):
        offset, children = 0, []
        for word in words:
            children.append(
                {
                    "text": word,
                    "source_ranges": [
                        {
                            "segment_index": 0,
                            "start_offset": offset,
                            "end_offset": offset + len(word),
                        }
                    ],
                }
            )
            offset += len(word)
        decisions.append(
            {
                "source_item_id": record.source_record_id,
                "action": "SPLIT",
                "reason": "Separate observed actions.",
                "children": children,
            }
        )
    decomposition = compile_source_item_decomposition(baseline, decisions)
    return document_map, baseline, decomposition


def seal(snapshot):
    snapshot["delivery_sha256"] = core.canonical_json_sha256(
        {k: v for k, v in snapshot.items() if k != "delivery_sha256"}
    )
    return snapshot


class DeliveryOrderTests(unittest.TestCase):
    def test_legacy_image_shape_recovery_requires_exact_map_binding_and_caption(self):
        from .test_document_map_revision import fixture as image_fixture
        from app.application.agent_workflow.delivery_snapshot_compatibility import (
            verify_published_delivery_snapshot,
        )

        _, document_map, baseline, _, tables = image_fixture()
        current = build_delivery_snapshot(document_map, baseline, tables)
        old = deepcopy(current)
        image = next(
            node
            for node in old["nodes"]
            if (node.get("source_item") or {}).get("image_bindings")
        )
        self.assertTrue(image["figure_refs"])
        for ref in image["figure_refs"]:
            for field in (
                "kind",
                "page_number",
                "source_bbox",
                "location",
                "caption_authority",
            ):
                ref.pop(field, None)
        seal(old)
        unchanged = deepcopy(old)

        def verify(snapshot):
            return verify_published_delivery_snapshot(
                document_map=document_map,
                baseline=baseline,
                published_snapshot=snapshot,
                current_snapshot=current,
                expected_snapshot_sha256=core.canonical_json_sha256(snapshot),
            )

        proof = verify(old)
        self.assertEqual(
            proof["restored_original_image_shape_count"], len(image["figure_refs"])
        )
        self.assertEqual(old, unchanged)
        for field, value in (
            ("bbox", [0, 0, 1, 1]),
            ("source_handle", "unbound"),
            ("caption", "Changed caption"),
            ("unknown_field", "unaudited"),
        ):
            bad = deepcopy(old)
            target = next(
                node for node in bad["nodes"] if node["node_id"] == image["node_id"]
            )
            target["figure_refs"][0][field] = value
            seal(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify(bad)

    def test_canonical_object_roundtrip_and_reversed_parent_dict_keep_exact_snapshot(
        self,
    ):
        document_map, baseline, decomposition = fixture()
        expected = build_delivery_snapshot(
            document_map, baseline, {}, decomposition=decomposition
        )
        reversed_value = deepcopy(decomposition)
        reversed_value["records"] = dict(
            reversed(list(decomposition["records"].items()))
        )
        stored = json.loads(json.dumps(reversed_value, sort_keys=True))
        self.assertEqual(
            expected,
            build_delivery_snapshot(
                document_map, baseline, {}, decomposition=reversed_value
            ),
        )
        self.assertEqual(
            expected,
            build_delivery_snapshot(document_map, baseline, {}, decomposition=stored),
        )
        groups = [
            n["parent_id"]
            for n in expected["nodes"]
            if n["source_kind"] == "SINGULAR_REQUIREMENT"
        ]
        self.assertEqual(groups, [groups[0], groups[0], groups[2], groups[2]])
        self.assertNotEqual(groups[0], groups[2])

    def test_legacy_compatibility_checks_exact_hash_and_every_field_and_child_sequence(
        self,
    ):
        document_map, baseline, decomposition = fixture()
        current = build_delivery_snapshot(
            document_map, baseline, {}, decomposition=decomposition
        )
        old = deepcopy(current)
        old["nodes"][-4:] = old["nodes"][-2:] + old["nodes"][-4:-2]
        seal(old)
        graph = {"matrix_snapshot_sha256": core.canonical_json_sha256(old)}
        _verify_published_snapshot(graph, current, old)
        with self.assertRaisesRegex(ValueError, "exact published"):
            _verify_published_snapshot(graph, current)
        wrong_hash = deepcopy(old)
        wrong_hash["nodes"][-1]["requirement_text"] = "Different original obligation"
        seal(wrong_hash)
        with self.assertRaisesRegex(ValueError, "SHA differs"):
            _verify_published_snapshot(graph, current, wrong_hash)
        for kind in (
            "child_order",
            "text",
            "classification",
            "source_order",
            "source_ref",
            "parent_link",
        ):
            changed = deepcopy(old)
            if kind == "child_order":
                changed["nodes"][-2:] = reversed(changed["nodes"][-2:])
            elif kind == "text":
                changed["nodes"][-1]["requirement_text"] = "Altered"
            elif kind == "classification":
                changed["nodes"][-1]["classification_paths"] = ["Altered"]
            elif kind == "source_order":
                changed["nodes"][-1]["source_order"] += 1
            elif kind == "source_ref":
                changed["nodes"][-1]["source_refs"][0]["start_offset"] += 1
            else:
                changed["nodes"][-1]["parent_id"] = "different-parent"
            seal(changed)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                _verify_published_snapshot(
                    {"matrix_snapshot_sha256": core.canonical_json_sha256(changed)},
                    current,
                    changed,
                )
