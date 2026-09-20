from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import base64
import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx

from app.adapters.documents.bailian_semantic_provider import (
    _AssemblyPayload,
    BAILIAN_SEMANTIC_PROVIDER_ID,
    BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION,
    BAILIAN_SEMANTIC_PROVIDER_VERSION,
    DEFAULT_BAILIAN_SEMANTIC_SEED,
    BailianQwenSemanticProvider,
    _assembly_budget_constraints,
    _assembly_constraints,
    _assembly_system_prompt,
    _assembly_user_payload,
    _classification_system_prompt,
    _provider_request_payload,
    _structure_audit_system_prompt,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    SemanticClassificationRequest,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from app.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditContinuationInput,
    StructureAuditPhysicalOrderInput,
    StructureAuditProposalEvidence,
    StructureAuditRequest,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
TARGET_ID = "structure-node-" + "1" * 32
LEAD_ID = "structure-node-" + "8" * 32
CLAUSE_ID = "structure-node-" + "9" * 32
CHART_ID = "structure-node-" + "a" * 32
EQUIPMENT_BODY_ID = "structure-node-" + "b" * 32
PRODUCT_ID = "structure-node-" + "c" * 32
PRODUCT_BODY_ID = "structure-node-" + "d" * 32
INDEPENDENT_ID = "structure-node-" + "e" * 32
NESTED_BODY_ID = "structure-node-" + "f" * 32
LABELED_NESTED_ID = "structure-node-" + "0" * 32
SOURCE_ID = "node-" + "2" * 32
SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id=SOURCE_ID,
)
SECOND_AUDIT_ID = "structure-node-" + "6" * 32
SECOND_AUDIT_SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "5" * 32,
)


class BailianSemanticProviderTests(unittest.TestCase):
    def test_classification_uses_strict_non_thinking_json_request(self) -> None:
        response = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                        "reason_codes": ["test_equipment_signal"],
                    }
                ]
            },
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        client = Mock()
        client.post.return_value = response
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.provider_id, BAILIAN_SEMANTIC_PROVIDER_ID)
        self.assertEqual(result.run.provider_version, BAILIAN_SEMANTIC_PROVIDER_VERSION)
        self.assertEqual(
            result.run.provider_version,
            "2.18.1;s=1234;t=0;think=0;stream=1",
        )
        self.assertEqual(result.run.input_tokens, 10)
        self.assertEqual(result.run.output_tokens, 5)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(result.decisions[0].evidence_source_refs, (SOURCE_REF,))
        call = client.post.call_args
        self.assertEqual(
            call.args[0],
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        payload = call.kwargs["json"]
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["seed"], DEFAULT_BAILIAN_SEMANTIC_SEED)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        user_text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("output_json_schema", user_text)
        node_payload = json.loads(user_text)["input"]["nodes"][0]
        self.assertEqual(node_payload["allowed_evidence_indexes"], [0])
        self.assertEqual(node_payload["evidence"][0]["evidence_index"], 0)
        self.assertEqual(node_payload["evidence"][0]["scope"], "TARGET")
        self.assertNotIn("test-api-key", result.run.model_dump_json())

    def test_streaming_json_mode_is_buffered_with_finish_reason_and_usage(
        self,
    ) -> None:
        content = json.dumps(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                        "reason_codes": ["test_equipment_signal"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        split_at = len(content) // 2

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            events = (
                {
                    "id": "chatcmpl-stream",
                    "model": "qwen3.7-plus",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content[:split_at]},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "qwen3.7-plus",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content[split_at:]},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "qwen3.7-plus",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "qwen3.7-plus",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 9,
                        "total_tokens": 26,
                    },
                },
            )
            body = "".join(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
            )
            body += "data: [DONE]\n\n"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = _provider(client)
            result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.input_tokens, 17)
        self.assertEqual(result.run.output_tokens, 9)
        self.assertEqual(
            result.decisions[0].semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertEqual(result.decisions[0].evidence_source_refs, (SOURCE_REF,))

    def test_streaming_token_trickle_obeys_wall_clock_timeout_and_retry_budget(
        self,
    ) -> None:
        now = [0.0]
        request_count = [0]
        event = json.dumps(
            {
                "id": "chatcmpl-trickle",
                "model": "qwen3.7-plus",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "{"},
                        "finish_reason": None,
                    }
                ],
            }
        )
        chunks = tuple(f"data: {event}\n\n".encode() for _ in range(3))

        class _TokenTrickleStream(httpx.SyncByteStream):
            def __iter__(self):
                for chunk in chunks:
                    now[0] += 0.4
                    yield chunk

        def handler(_request: httpx.Request) -> httpx.Response:
            request_count[0] += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_TokenTrickleStream(),
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = BailianQwenSemanticProvider(
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                timeout_seconds=1.0,
                max_retries=2,
                http_client=client,
                clock=lambda: NOW,
                monotonic=lambda: now[0],
                sleep=lambda _seconds: None,
            )
            result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.TIMEOUT)
        self.assertEqual(result.run.error_code, "provider_timeout")
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.retry_count, 2)
        self.assertEqual(request_count[0], 3)

    def test_prompts_keep_table_rows_and_stage_schemas_distinct(self) -> None:
        classification_prompt = _classification_system_prompt()
        assembly_prompt = _assembly_system_prompt()

        self.assertIn("PERFORMANCE_PARAMETER_REQUIREMENT", classification_prompt)
        self.assertIn("never semantic_class values", classification_prompt)
        self.assertIn("Never return assembly fields", classification_prompt)
        self.assertIn("independently judgeable parameter rows", assembly_prompt)
        self.assertIn("Never return semantic_class", assembly_prompt)

    def test_structure_audit_returns_only_source_bound_non_mutating_findings(
        self,
    ) -> None:
        response = _response(
            {
                "findings": [
                    {
                        "category": "possible_missed_boundary",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            }
                        ],
                        "reason_codes": ["numbered_content_not_structured"],
                    }
                ]
            }
        )
        client = Mock()
        client.post.return_value = response

        result = _provider(client).audit_structure(_audit_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.operation, "audit_structure")
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(
            result.findings[0].category,
            StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
        )
        self.assertEqual(result.findings[0].evidence_source_refs, (SOURCE_REF,))
        request_payload = client.post.call_args.kwargs["json"]
        user_payload = json.loads(request_payload["messages"][1]["content"][0]["text"])[
            "input"
        ]
        self.assertEqual(
            user_payload["outline_spine_node_aliases"],
            ["n0"],
        )
        self.assertEqual(user_payload["max_findings"], 1)
        self.assertEqual(
            user_payload["source_bound_structure"]["target_nodes"][0][
                "allowed_target_categories"
            ],
            [
                "possible_missed_boundary",
                "possible_misplaced_parent",
                "possible_duplicate_content",
            ],
        )
        self.assertEqual(
            user_payload["source_bound_structure"]["context_nodes"],
            [],
        )
        self.assertNotIn(TARGET_ID, json.dumps(user_payload, ensure_ascii=False))
        self.assertRegex(user_payload["identity_binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            user_payload["physical_order"][0]["physical_source_orders"],
            [10],
        )
        self.assertIn("do not", _structure_audit_system_prompt().lower())
        self.assertNotIn("corrected_parent", request_payload)

    def test_structure_audit_maps_limited_high_evidence_continuation_proposal(
        self,
    ) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_table_continuation_error",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["adjacent_table_continuation"],
                        "proposal": {
                            "operation": "add_continuation",
                            "source_node_alias": "n0",
                            "target_node_alias": "n1",
                            "evidence_level": "HIGH",
                        },
                    }
                ]
            }
        )
        request = _audit_table_request().model_copy(
            update={"current_continuations": ()}
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        finding = result.findings[0]
        self.assertEqual(
            finding.proposal_evidence,
            StructureAuditProposalEvidence.HIGH,
        )
        self.assertEqual(len(finding.proposed_operations), 1)
        operation = finding.proposed_operations[0]
        self.assertEqual(operation.operation, "add_relation")
        self.assertEqual(operation.kind, StructureRelationKind.CONTINUES)
        self.assertEqual(
            {
                operation.source_structure_node_id,
                operation.target_structure_node_id,
            },
            {node.target_structure_node_id for node in request.evidence_request.nodes},
        )

    def test_structure_audit_maps_exact_boundary_split_proposal(self) -> None:
        request = _audit_split_request()
        text = request.evidence_request.nodes[0].evidence[0].text
        split_offset = text.index("6．4．2")
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_missed_boundary",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            }
                        ],
                        "reason_codes": ["two_explicit_numbered_items"],
                        "proposal": {
                            "operation": "boundary_split",
                            "parts": [
                                {
                                    "source_spans": [
                                        {
                                            "evidence_alias": "e0",
                                            "start_offset": 0,
                                            "end_offset": split_offset,
                                        }
                                    ]
                                },
                                {
                                    "source_spans": [
                                        {
                                            "evidence_alias": "e0",
                                            "start_offset": split_offset,
                                            "end_offset": len(text),
                                        }
                                    ]
                                },
                            ],
                            "evidence_level": "HIGH",
                        },
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        operation = result.findings[0].proposed_operations[0]
        self.assertEqual(operation.operation, "boundary_split")
        self.assertEqual(operation.structure_node_id, TARGET_ID)
        self.assertEqual(
            tuple(
                (span.start_offset, span.end_offset)
                for part in operation.parts
                for span in part.source_spans
            ),
            ((0, split_offset), (split_offset, len(text))),
        )
        request_payload = client.post.call_args.kwargs["json"]
        user_payload = json.loads(request_payload["messages"][1]["content"][0]["text"])[
            "input"
        ]
        self.assertEqual(
            user_payload["source_bound_structure"]["target_nodes"][0]["evidence"][0][
                "text_length_codepoints"
            ],
            len(text),
        )

    def test_structure_audit_rejects_out_of_bounds_split_offset(self) -> None:
        request = _audit_split_request()
        text = request.evidence_request.nodes[0].evidence[0].text
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_missed_boundary",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            }
                        ],
                        "reason_codes": ["invalid_split_offset"],
                        "proposal": {
                            "operation": "boundary_split",
                            "parts": [
                                {
                                    "source_spans": [
                                        {
                                            "evidence_alias": "e0",
                                            "start_offset": 0,
                                            "end_offset": 5,
                                        }
                                    ]
                                },
                                {
                                    "source_spans": [
                                        {
                                            "evidence_alias": "e0",
                                            "start_offset": 5,
                                            "end_offset": len(text) + 1,
                                        }
                                    ]
                                },
                            ],
                            "evidence_level": "HIGH",
                        },
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_structure_audit_maps_grounded_content_continuation_merge(self) -> None:
        request = _audit_content_continuation_request()
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_duplicate_content",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["grounded_continuation_split"],
                        "proposal": {
                            "operation": "continuation_merge",
                            "primary_node_alias": "n0",
                            "continuation_node_alias": "n1",
                            "evidence_level": "HIGH",
                        },
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        operation = result.findings[0].proposed_operations[0]
        self.assertEqual(operation.operation, "continuation_merge")
        self.assertEqual(operation.primary_structure_node_id, TARGET_ID)
        self.assertEqual(
            operation.continuation_structure_node_id,
            SECOND_AUDIT_ID,
        )

    def test_structure_audit_rejects_ungrounded_continuation_merge(self) -> None:
        request = _audit_content_continuation_request().model_copy(
            update={"current_continuations": ()}
        )
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_duplicate_content",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["ungrounded_merge"],
                        "proposal": {
                            "operation": "continuation_merge",
                            "primary_node_alias": "n0",
                            "continuation_node_alias": "n1",
                            "evidence_level": "HIGH",
                        },
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_structure_audit_invalid_evidence_binding_fails_closed(self) -> None:
        response = _response(
            {
                "findings": [
                    {
                        "category": "possible_misplaced_parent",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e99"],
                            }
                        ],
                        "reason_codes": ["parent_context_conflict"],
                    }
                ]
            }
        )
        client = Mock()
        client.post.return_value = response

        result = _provider(client).audit_structure(_audit_request())

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_structure_audit_rejects_more_than_one_finding_per_window(
        self,
    ) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_missed_boundary",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            }
                        ],
                        "reason_codes": ["numbered_content_not_structured"],
                    },
                    {
                        "category": "possible_missed_boundary",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e99"],
                            }
                        ],
                        "reason_codes": ["invalid_anchor"],
                    },
                ]
            }
        )

        result = _provider(client).audit_structure(_audit_request())

        self.assertEqual(
            result.run.status,
            SemanticProviderRunStatus.INVALID_OUTPUT,
        )
        self.assertEqual(result.findings, ())
        self.assertEqual(result.rejected_finding_count, 0)

    def test_structure_audit_empty_findings_does_not_claim_correctness(self) -> None:
        client = Mock()
        client.post.return_value = _response({"findings": []})

        result = _provider(client).audit_structure(_audit_request())

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)

    def test_structure_audit_aliases_reject_grounded_continuation_challenge(
        self,
    ) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_table_continuation_error",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["column_grid_conflict"],
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(_audit_table_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.rejected_finding_count, 1)
        request_payload = client.post.call_args.kwargs["json"]
        user_payload = json.loads(request_payload["messages"][1]["content"][0]["text"])[
            "input"
        ]
        self.assertEqual(
            user_payload["current_relations"],
            [
                {
                    "kind": "continues",
                    "source_node_alias": "n0",
                    "target_node_alias": "n1",
                    "grounding": [
                        {
                            "node_alias": "n1",
                            "evidence_alias": "e0",
                        }
                    ],
                }
            ],
        )

    def test_table_continuation_finding_requires_both_table_endpoints(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_table_continuation_error",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            }
                        ],
                        "reason_codes": ["cross_page_only"],
                    }
                ]
            }
        )

        result = _provider(client).audit_structure(_audit_table_request())

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_table_continuation_finding_must_target_a_table_endpoint(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_table_continuation_error",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["section_used_as_table_target"],
                    }
                ]
            }
        )
        request = _audit_table_request()
        first, second = request.evidence_request.nodes
        request = request.model_copy(
            update={
                "evidence_request": request.evidence_request.model_copy(
                    update={
                        "nodes": (
                            first.model_copy(
                                update={"kind": StructureNodeKind.SECTION}
                            ),
                            second,
                        )
                    }
                ),
                "current_continuations": (),
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.findings, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_structure_audit_derives_related_ids_from_evidence_aliases(
        self,
    ) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "findings": [
                    {
                        "category": "possible_source_order_error",
                        "target_node_alias": "n0",
                        "evidence": [
                            {
                                "node_alias": "n0",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n1",
                                "evidence_aliases": ["e0"],
                            },
                            {
                                "node_alias": "n2",
                                "evidence_aliases": ["e0"],
                            },
                        ],
                        "reason_codes": ["physical_order_conflict"],
                    }
                ]
            }
        )
        request = _audit_table_request()
        first, second = request.evidence_request.nodes
        third_id = "structure-node-" + "c" * 32
        third_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "d" * 32,
        )
        third_text = "third structural comparison"
        third = second.model_copy(
            update={
                "target_structure_node_id": third_id,
                "source_order": 2,
                "target_source_refs": (third_ref,),
                "evidence": (
                    SemanticEvidenceInput(
                        source_ref=third_ref,
                        text=third_text,
                        text_sha256=hashlib.sha256(third_text.encode()).hexdigest(),
                    ),
                ),
            }
        )
        request = request.model_copy(
            update={
                "evidence_request": request.evidence_request.model_copy(
                    update={
                        "scope_structure_node_ids": (
                            TARGET_ID,
                            SECOND_AUDIT_ID,
                            third_id,
                        ),
                        "nodes": (first, second, third),
                    }
                ),
                "physical_order": (
                    *request.physical_order,
                    StructureAuditPhysicalOrderInput(
                        structure_node_id=third_id,
                        physical_source_orders=(12,),
                    ),
                ),
                "current_continuations": (),
            }
        )

        result = _provider(client).audit_structure(request)

        self.assertEqual(result.rejected_finding_count, 0)
        self.assertEqual(result.findings[0].target_structure_node_id, TARGET_ID)
        self.assertEqual(
            result.findings[0].related_structure_node_ids,
            (SECOND_AUDIT_ID, third_id),
        )

    def test_out_of_range_evidence_index_fails_the_decision_closed(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [1],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("provider_evidence_index_invalid",),
        )

    def test_section_can_select_a_local_index_for_provision_context(self) -> None:
        context_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "3" * 32,
        )
        context_text = "The descendant carries the judgeable obligation."
        request = _classification_request().model_copy(
            update={
                "nodes": (
                    _classification_request()
                    .nodes[0]
                    .model_copy(
                        update={
                            "evidence": (
                                _evidence(),
                                SemanticEvidenceInput(
                                    source_ref=context_ref,
                                    text=context_text,
                                    text_sha256=hashlib.sha256(
                                        context_text.encode()
                                    ).hexdigest(),
                                ),
                            )
                        }
                    ),
                )
            }
        )
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "PRODUCT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [1],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(request)

        self.assertEqual(result.decisions[0].evidence_source_refs, (context_ref,))
        payload = client.post.call_args.kwargs["json"]
        user_text = payload["messages"][1]["content"][0]["text"]
        node_payload = json.loads(user_text)["input"]["nodes"][0]
        self.assertEqual(node_payload["allowed_evidence_indexes"], [0, 1])
        self.assertEqual(
            [item["scope"] for item in node_payload["evidence"]],
            ["TARGET", "CONTEXT"],
        )

    def test_non_requirement_continuation_flag_is_auditably_cleared(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "TECHNICAL_INFORMATION",
                        "requirement_subtype": None,
                        "is_continuation_fragment": True,
                        "evidence_indexes": [0],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(
            result.decisions[0].semantic_class,
            SemanticClass.TECHNICAL_INFORMATION,
        )
        self.assertFalse(result.decisions[0].is_continuation_fragment)
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("continuation_flag_not_applicable",),
        )

    def test_missing_or_empty_evidence_indexes_fail_the_run_closed(self) -> None:
        variants = ({}, {"evidence_indexes": []})
        for evidence_shape in variants:
            with self.subTest(evidence_shape=evidence_shape):
                client = Mock()
                client.post.return_value = _response(
                    {
                        "decisions": [
                            {
                                "target_structure_node_id": TARGET_ID,
                                "semantic_class": "TECHNICAL_INFORMATION",
                                "requirement_subtype": None,
                                "is_continuation_fragment": False,
                                **evidence_shape,
                            }
                        ]
                    }
                )
                provider = _provider(client)

                result = provider.classify_nodes(_classification_request())

                self.assertEqual(result.decisions, ())
                self.assertEqual(
                    result.run.status,
                    SemanticProviderRunStatus.INVALID_OUTPUT,
                )
                self.assertEqual(
                    result.run.error_code,
                    "provider_output_schema_invalid",
                )

    def test_invalid_output_fails_closed_without_echoing_content(self) -> None:
        client = Mock()
        client.post.return_value = _response({"unexpected": "customer secret"})
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.INVALID_OUTPUT)
        self.assertEqual(result.run.error_code, "provider_output_schema_invalid")
        self.assertNotIn("customer secret", result.run.model_dump_json())

    def test_invalid_schema_retries_once_and_returns_one_audited_answer(self) -> None:
        client = Mock()
        client.post.side_effect = [
            _response({"unexpected": "first invalid response"}),
            _response(
                {
                    "decisions": [
                        {
                            "target_structure_node_id": TARGET_ID,
                            "semantic_class": "REQUIREMENT",
                            "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                            "is_continuation_fragment": False,
                            "evidence_indexes": [0],
                        }
                    ]
                }
            ),
        ]
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 2)
        self.assertEqual(result.run.retry_count, 1)
        self.assertEqual(len(result.decisions), 1)
        self.assertNotIn("first invalid response", result.run.model_dump_json())

    def test_invalid_json_and_truncation_are_distinguished_without_content(
        self,
    ) -> None:
        client = Mock()
        client.post.return_value = _raw_content_response("not-json")
        provider = _provider(client)

        invalid = provider.classify_nodes(_classification_request())

        self.assertEqual(invalid.run.error_code, "provider_output_json_invalid")
        self.assertNotIn("not-json", invalid.run.model_dump_json())

        client.post.return_value = _raw_content_response(
            json.dumps({"decisions": []}),
            finish_reason="length",
            usage={"prompt_tokens": 21, "completion_tokens": 8},
        )
        truncated = provider.classify_nodes(_classification_request())

        self.assertEqual(truncated.run.error_code, "provider_output_truncated")
        self.assertEqual(truncated.run.input_tokens, 42)
        self.assertEqual(truncated.run.output_tokens, 16)
        self.assertEqual(truncated.run.request_count, 2)
        self.assertEqual(truncated.run.retry_count, 1)

    def test_non_object_response_envelope_fails_closed(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = []
        response.content = b"[]"
        client = Mock()
        client.post.return_value = response
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.INVALID_OUTPUT)
        self.assertEqual(
            result.run.error_code,
            "provider_response_envelope_invalid",
        )

    def test_multimodal_page_is_bound_and_sent_as_an_in_memory_data_url(self) -> None:
        image_bytes = _png_ihdr_payload(640, 480)
        image = SemanticImageInput(
            source_sha256="7" * 64,
            page_number=3,
            width_px=640,
            height_px=480,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                        "reason_codes": ["test_equipment_signal"],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request(images=(image,)))

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        content = client.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url"])
        user_payload = json.loads(content[0]["text"])["input"]
        self.assertEqual(
            user_payload["images"],
            [
                {
                    "content_part_index": 1,
                    "source_sha256": "7" * 64,
                    "page_number": 3,
                    "width_px": 640,
                    "height_px": 480,
                    "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "media_type": "image/png",
                }
            ],
        )
        self.assertEqual(user_payload["nodes"][0]["image_page_numbers"], [3])
        data_url = content[1]["image_url"]["url"]
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), image_bytes)

    def test_classification_preflight_builds_payload_without_http(self) -> None:
        client = Mock()
        provider = _provider(client)

        provider.preflight_classification_request(_classification_request())

        client.post.assert_not_called()

    def test_assembly_preflight_builds_payload_without_http(self) -> None:
        client = Mock()
        provider = _provider(client)

        provider.preflight_assembly_request(_assembly_request())

        client.post.assert_not_called()

    def test_assembly_preflight_envelope_dominates_every_semantic_pair(self) -> None:
        base = _numbered_assembly_request(
            parent_class=SemanticClass.REQUIREMENT,
            parent_subtype=RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            child_class=SemanticClass.REQUIREMENT,
            child_subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        largest_subtype = max(RequirementSubtype, key=lambda item: len(item.value))
        envelope = base.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(
                        update={
                            "semantic_class": SemanticClass.REQUIREMENT,
                            "requirement_subtype": largest_subtype,
                            "is_continuation_fragment": False,
                        }
                    )
                    for node in base.nodes
                )
            }
        )
        envelope_size = _assembly_wire_size(
            _assembly_user_payload(
                envelope,
                constraints=_assembly_budget_constraints(envelope),
            )
        )
        semantic_options = [
            *(
                (item, None)
                for item in SemanticClass
                if item is not SemanticClass.REQUIREMENT
            ),
            *((SemanticClass.REQUIREMENT, subtype) for subtype in RequirementSubtype),
        ]

        actual_sizes = []
        for parent_class, parent_subtype in semantic_options:
            for child_class, child_subtype in semantic_options:
                actual = _numbered_assembly_request(
                    parent_class=parent_class,
                    parent_subtype=parent_subtype,
                    child_class=child_class,
                    child_subtype=child_subtype,
                )
                actual_sizes.append(_assembly_wire_size(_assembly_user_payload(actual)))

        self.assertLessEqual(max(actual_sizes), envelope_size)

    def test_multimodal_pixel_budget_is_rejected_before_http(self) -> None:
        image_bytes = _png_ihdr_payload(2_000, 2_000)
        image = SemanticImageInput(
            source_sha256="7" * 64,
            page_number=3,
            width_px=2_000,
            height_px=2_000,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        client = Mock()
        provider = _provider(client)

        with self.assertRaisesRegex(ValueError, "pixel limit"):
            provider.classify_nodes(_classification_request(images=(image,)))

        client.post.assert_not_called()

    def test_preflight_exposes_typed_pixel_budget_rejection(self) -> None:
        image_bytes = _png_ihdr_payload(2_000, 2_000)
        image = SemanticImageInput(
            source_sha256="7" * 64,
            page_number=3,
            width_px=2_000,
            height_px=2_000,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        client = Mock()
        provider = _provider(client)

        with self.assertRaises(SemanticProviderRequestRejected) as caught:
            provider.preflight_classification_request(
                _classification_request(images=(image,))
            )

        self.assertIs(
            caught.exception.code,
            SemanticProviderRequestRejectionCode.IMAGE_PIXEL_BUDGET_EXCEEDED,
        )
        client.post.assert_not_called()

    def test_multimodal_wire_budget_is_rejected_before_http(self) -> None:
        image_bytes = _png_ihdr_payload(100, 100, total_size=8 * 1024 * 1024)
        image = SemanticImageInput(
            source_sha256="7" * 64,
            page_number=3,
            width_px=100,
            height_px=100,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        client = Mock()
        provider = _provider(client)

        with self.assertRaisesRegex(ValueError, "wire-byte limit"):
            provider.classify_nodes(_classification_request(images=(image,)))

        client.post.assert_not_called()

    def test_image_from_another_source_is_rejected_before_provider_call(self) -> None:
        image_bytes = _png_ihdr_payload(640, 480)
        image = SemanticImageInput(
            source_sha256="8" * 64,
            page_number=3,
            width_px=640,
            height_px=480,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )

        with self.assertRaises(ValueError):
            _classification_request(images=(image,))

    def test_orphan_image_is_rejected_before_provider_call(self) -> None:
        image_bytes = _png_ihdr_payload(640, 480)
        image = SemanticImageInput(
            source_sha256="7" * 64,
            page_number=3,
            width_px=640,
            height_px=480,
            image_bytes=image_bytes,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        )

        with self.assertRaisesRegex(ValueError, "image pages"):
            _classification_request(images=(image,), bind_images=False)

    def test_png_dimensions_are_verified_from_ihdr(self) -> None:
        image_bytes = _png_ihdr_payload(640, 480)

        with self.assertRaisesRegex(ValueError, "dimensions"):
            SemanticImageInput(
                source_sha256="7" * 64,
                page_number=3,
                width_px=1,
                height_px=1,
                image_bytes=image_bytes,
                image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            )

    def test_contradictory_assembly_shape_becomes_unresolved(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "include_in_view": False,
                        "role": "PRIMARY",
                        "parent_structure_node_id": None,
                        "status": "CANDIDATE",
                        "reason_codes": [],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.assemble_requirements(_assembly_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(
            result.decisions[0].status,
            RequirementAssemblyStatus.UNRESOLVED,
        )
        self.assertFalse(result.decisions[0].include_in_view)
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("provider_assembly_shape_invalid",),
        )

    def test_missing_requirement_subtype_becomes_unresolved(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": None,
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                        "reason_codes": ["free form output is ignored"],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("provider_requirement_subtype_missing",),
        )

    def test_missing_key_is_explicitly_unavailable_and_makes_no_request(self) -> None:
        client = Mock()
        provider = _provider(client, api_key="")

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.UNAVAILABLE)
        self.assertEqual(result.run.error_code, "provider_not_configured")
        self.assertEqual(result.run.request_count, 0)
        client.post.assert_not_called()

    def test_malicious_target_id_is_schema_invalid_without_echoing_value(self) -> None:
        canary = "synthetic_customer_canary"
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": canary,
                        "semantic_class": "HEADING",
                        "requirement_subtype": None,
                        "is_continuation_fragment": False,
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.INVALID_OUTPUT)
        self.assertEqual(result.run.error_code, "provider_output_schema_invalid")
        self.assertNotIn(canary, result.run.model_dump_json())

    def test_malicious_assembly_parent_id_is_rejected_without_echoing_value(
        self,
    ) -> None:
        canary = "synthetic_customer_canary"
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "include_in_view": True,
                        "role": "CHILD",
                        "parent_structure_node_id": canary,
                        "status": "CANDIDATE",
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.assemble_requirements(_assembly_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.INVALID_OUTPUT)
        self.assertEqual(result.run.error_code, "provider_output_schema_invalid")
        self.assertNotIn(canary, result.run.model_dump_json())

    def test_non_requirement_subtype_becomes_unresolved(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "HEADING",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("provider_requirement_subtype_invalid",),
        )

    def test_string_boolean_is_rejected_by_assembly_wire_schema(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "include_in_view": "false",
                        "role": None,
                        "parent_structure_node_id": None,
                        "status": "CANDIDATE",
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.assemble_requirements(_assembly_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.INVALID_OUTPUT)
        self.assertEqual(result.run.error_code, "provider_output_schema_invalid")

    def test_timeout_retries_are_bounded(self) -> None:
        client = Mock()
        client.post.side_effect = httpx.ReadTimeout("synthetic timeout")
        provider = BailianQwenSemanticProvider(
            api_key="test-api-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=2,
            http_client=client,
            clock=lambda: NOW,
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
        )

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.TIMEOUT)
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.retry_count, 2)

    def test_transport_error_can_recover_within_the_retry_budget(self) -> None:
        client = Mock()
        client.post.side_effect = [
            httpx.RemoteProtocolError("synthetic disconnect"),
            _response(
                {
                    "decisions": [
                        {
                            "target_structure_node_id": TARGET_ID,
                            "semantic_class": "REQUIREMENT",
                            "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                            "is_continuation_fragment": False,
                            "evidence_indexes": [0],
                            "reason_codes": ["test_equipment_signal"],
                        }
                    ]
                }
            ),
        ]
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 2)
        self.assertEqual(result.run.retry_count, 1)

    def test_transport_error_exhaustion_is_explicitly_unavailable(self) -> None:
        client = Mock()
        client.post.side_effect = httpx.RemoteProtocolError("synthetic disconnect")
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.UNAVAILABLE)
        self.assertEqual(result.run.error_code, "provider_transport_error")
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.retry_count, 2)

    def test_retryable_http_responses_can_recover_within_the_budget(self) -> None:
        client = Mock()
        client.post.side_effect = [
            _status_response(429),
            _status_response(503),
            _response(
                {
                    "decisions": [
                        {
                            "target_structure_node_id": TARGET_ID,
                            "semantic_class": "REQUIREMENT",
                            "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                            "is_continuation_fragment": False,
                            "evidence_indexes": [0],
                            "reason_codes": ["test_equipment_signal"],
                        }
                    ]
                }
            ),
        ]
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.retry_count, 2)

    def test_retryable_http_exhaustion_is_explicitly_unavailable(self) -> None:
        client = Mock()
        client.post.return_value = _status_response(503)
        provider = _provider(client)

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions, ())
        self.assertEqual(result.run.status, SemanticProviderRunStatus.UNAVAILABLE)
        self.assertEqual(result.run.error_code, "provider_http_retry_exhausted")
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.retry_count, 2)

    def test_assembly_response_is_strictly_typed(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "include_in_view": True,
                        "role": "PRIMARY",
                        "parent_structure_node_id": None,
                        "status": "CANDIDATE",
                        "reason_codes": ["model_assembly"],
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.assemble_requirements(_assembly_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.decisions[0].role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(
            result.decisions[0].status, RequirementAssemblyStatus.CANDIDATE
        )
        payload = client.post.call_args.kwargs["json"]
        user_payload = json.loads(payload["messages"][1]["content"][0]["text"])["input"]
        self.assertEqual(
            user_payload["nodes"][0]["allowed_dispositions"],
            ["PRIMARY", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            user_payload["nodes"][0]["allowed_parent_structure_node_ids_by_role"],
            {},
        )
        self.assertIn(
            "structural_ancestor_structure_node_ids", user_payload["nodes"][0]
        )
        self.assertIn(
            "structural_parent_target_structure_node_id",
            user_payload["nodes"][0],
        )
        self.assertNotIn("parent_structure_node_id", user_payload["nodes"][0])
        self.assertEqual(
            user_payload["nodes"][0]["allowed_decisions"],
            [
                {"disposition": "PRIMARY", "parent_structure_node_id": None},
                {"disposition": "EXCLUDE", "parent_structure_node_id": None},
                {"disposition": "UNRESOLVED", "parent_structure_node_id": None},
            ],
        )
        self.assertEqual(user_payload["nodes"][0]["explicit_label"], "4.12.1")

    def test_assembly_decision_outside_local_constraints_fails_closed(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "include_in_view": True,
                        "role": "CONTENT",
                        "parent_structure_node_id": "structure-node-" + "e" * 32,
                        "status": "CANDIDATE",
                    }
                ]
            }
        )
        provider = _provider(client)

        result = provider.assemble_requirements(_assembly_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(
            result.decisions[0].status, RequirementAssemblyStatus.UNRESOLVED
        )
        self.assertEqual(
            result.decisions[0].reason_codes,
            ("provider_assembly_constraint_invalid",),
        )

    def test_numbered_requirement_constraints_allow_only_expected_shape(self) -> None:
        request = RequirementAssemblyRequest(
            source_ir_id="sir-" + "3" * 32,
            structure_snapshot_id="structure-" + "4" * 32,
            semantic_snapshot_id="semantic-" + "5" * 32,
            scope_structure_node_ids=(
                TARGET_ID,
                LEAD_ID,
                CLAUSE_ID,
                CHART_ID,
                INDEPENDENT_ID,
                NESTED_BODY_ID,
                LABELED_NESTED_ID,
            ),
            nodes=(
                RequirementAssemblyNodeInput(
                    target_structure_node_id=TARGET_ID,
                    source_order=0,
                    kind=StructureNodeKind.SECTION,
                    explicit_label="4.12.2.1",
                    semantic_annotation_ids=("semantic-annotation-" + "1" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=LEAD_ID,
                    source_order=1,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=TARGET_ID,
                    ancestor_structure_node_ids=(TARGET_ID,),
                    semantic_annotation_ids=("semantic-annotation-" + "2" * 32,),
                    semantic_class=SemanticClass.BACKGROUND,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=CLAUSE_ID,
                    source_order=2,
                    kind=StructureNodeKind.CLAUSE,
                    parent_structure_node_id=TARGET_ID,
                    ancestor_structure_node_ids=(TARGET_ID,),
                    list_ordinal=1,
                    semantic_annotation_ids=("semantic-annotation-" + "3" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=CHART_ID,
                    source_order=3,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=CLAUSE_ID,
                    ancestor_structure_node_ids=(TARGET_ID, CLAUSE_ID),
                    semantic_annotation_ids=("semantic-annotation-" + "4" * 32,),
                    semantic_class=SemanticClass.TECHNICAL_INFORMATION,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=INDEPENDENT_ID,
                    source_order=4,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=TARGET_ID,
                    ancestor_structure_node_ids=(TARGET_ID,),
                    semantic_annotation_ids=("semantic-annotation-" + "5" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.ACCEPTANCE_CRITERION,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=NESTED_BODY_ID,
                    source_order=5,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=CLAUSE_ID,
                    ancestor_structure_node_ids=(TARGET_ID, CLAUSE_ID),
                    semantic_annotation_ids=("semantic-annotation-" + "6" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=LABELED_NESTED_ID,
                    source_order=6,
                    kind=StructureNodeKind.CLAUSE,
                    parent_structure_node_id=CLAUSE_ID,
                    ancestor_structure_node_ids=(TARGET_ID, CLAUSE_ID),
                    explicit_label="1.1",
                    semantic_annotation_ids=("semantic-annotation-" + "7" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
            ),
        )

        constraints = _assembly_constraints(request)

        self.assertEqual(
            constraints[TARGET_ID]["allowed_dispositions"],
            ["PRIMARY", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[LEAD_ID]["allowed_dispositions"],
            ["EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[CLAUSE_ID]["allowed_dispositions"],
            ["CHILD", "CONTENT", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[CLAUSE_ID]["allowed_parent_structure_node_ids_by_role"],
            {"CHILD": [TARGET_ID], "CONTENT": [TARGET_ID]},
        )
        self.assertEqual(
            constraints[CHART_ID]["allowed_dispositions"],
            ["EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[NESTED_BODY_ID]["allowed_dispositions"],
            ["CHILD", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[NESTED_BODY_ID]["allowed_parent_structure_node_ids_by_role"],
            {
                "CHILD": [TARGET_ID],
            },
        )
        self.assertEqual(
            constraints[LABELED_NESTED_ID]["allowed_dispositions"],
            ["CHILD", "CONTENT", "PARAMETER", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[LABELED_NESTED_ID]["allowed_parent_structure_node_ids_by_role"],
            {
                "CHILD": [TARGET_ID],
                "CONTENT": [TARGET_ID],
                "PARAMETER": [TARGET_ID],
            },
        )
        self.assertEqual(
            constraints[INDEPENDENT_ID]["allowed_dispositions"],
            ["CHILD", "CONTENT", "PARAMETER", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[INDEPENDENT_ID]["allowed_parent_structure_node_ids_by_role"],
            {
                "CHILD": [TARGET_ID],
                "CONTENT": [TARGET_ID],
                "PARAMETER": [TARGET_ID],
            },
        )

    def test_section_bodies_are_not_hard_absorbed_by_subtype(self) -> None:
        request = RequirementAssemblyRequest(
            source_ir_id="sir-" + "3" * 32,
            structure_snapshot_id="structure-" + "4" * 32,
            semantic_snapshot_id="semantic-" + "5" * 32,
            scope_structure_node_ids=(
                TARGET_ID,
                EQUIPMENT_BODY_ID,
                PRODUCT_ID,
                PRODUCT_BODY_ID,
            ),
            nodes=(
                RequirementAssemblyNodeInput(
                    target_structure_node_id=TARGET_ID,
                    source_order=0,
                    kind=StructureNodeKind.SECTION,
                    semantic_annotation_ids=("semantic-annotation-" + "1" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=EQUIPMENT_BODY_ID,
                    source_order=1,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=TARGET_ID,
                    ancestor_structure_node_ids=(TARGET_ID,),
                    semantic_annotation_ids=("semantic-annotation-" + "2" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=PRODUCT_ID,
                    source_order=2,
                    kind=StructureNodeKind.SECTION,
                    semantic_annotation_ids=("semantic-annotation-" + "3" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
                RequirementAssemblyNodeInput(
                    target_structure_node_id=PRODUCT_BODY_ID,
                    source_order=3,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=PRODUCT_ID,
                    ancestor_structure_node_ids=(PRODUCT_ID,),
                    semantic_annotation_ids=("semantic-annotation-" + "4" * 32,),
                    semantic_class=SemanticClass.REQUIREMENT,
                    requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                    evidence=(_evidence(),),
                ),
            ),
        )

        constraints = _assembly_constraints(request)

        self.assertEqual(
            constraints[EQUIPMENT_BODY_ID]["allowed_dispositions"],
            ["CHILD", "CONTENT", "PARAMETER", "EXCLUDE", "UNRESOLVED"],
        )
        self.assertEqual(
            constraints[PRODUCT_BODY_ID]["allowed_dispositions"],
            ["CHILD", "CONTENT", "PARAMETER", "EXCLUDE", "UNRESOLVED"],
        )

    def test_base_url_rejects_unapproved_credentials_or_plain_http_origins(
        self,
    ) -> None:
        for value in (
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://user@example.test/v1",
            "https://example.test/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BailianQwenSemanticProvider(
                    api_key="test-api-key",
                    base_url=value,
                )

    def test_explicit_seed_is_bound_to_request_and_provider_identity(self) -> None:
        client = Mock()
        client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                    }
                ]
            }
        )
        provider = BailianQwenSemanticProvider(
            api_key="test-api-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            seed=7,
            http_client=client,
            clock=lambda: NOW,
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
        )

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(provider.sampling_seed, 7)
        self.assertEqual(
            provider.provider_version,
            f"{BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION};"
            "s=7;t=0;think=0;stream=1",
        )
        self.assertEqual(result.run.provider_version, provider.provider_version)
        self.assertEqual(client.post.call_args.kwargs["json"]["seed"], 7)

    def test_seed_changes_the_audited_request_hash(self) -> None:
        first_client = Mock()
        second_client = Mock()
        first_client.post.return_value = _response(
            {
                "decisions": [
                    {
                        "target_structure_node_id": TARGET_ID,
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "TEST_EQUIPMENT_REQUIREMENT",
                        "is_continuation_fragment": False,
                        "evidence_indexes": [0],
                    }
                ]
            }
        )
        second_client.post.return_value = first_client.post.return_value
        first = BailianQwenSemanticProvider(
            api_key="test-api-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            seed=1,
            http_client=first_client,
            clock=lambda: NOW,
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
        ).classify_nodes(_classification_request())
        second = BailianQwenSemanticProvider(
            api_key="test-api-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            seed=2,
            http_client=second_client,
            clock=lambda: NOW,
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
        ).classify_nodes(_classification_request())

        self.assertNotEqual(first.run.request_sha256, second.run.request_sha256)

    def test_invalid_seed_is_rejected(self) -> None:
        for value in (True, -1, 2**31, 1.5, "1234"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError,
                    "semantic seed",
                ),
            ):
                BailianQwenSemanticProvider(
                    api_key="test-api-key",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    seed=value,  # type: ignore[arg-type]
                )


def _provider(
    client: Mock, *, api_key: str = "test-api-key"
) -> BailianQwenSemanticProvider:
    return BailianQwenSemanticProvider(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=client,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )


def _evidence() -> SemanticEvidenceInput:
    import hashlib

    text = "A force test instrument is required."
    return SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _classification_request(
    *,
    images: tuple[SemanticImageInput, ...] = (),
    bind_images: bool = True,
) -> SemanticClassificationRequest:
    return SemanticClassificationRequest(
        source_ir_id="sir-" + "3" * 32,
        source_sha256="7" * 64,
        structure_snapshot_id="structure-" + "4" * 32,
        scope_structure_node_ids=(TARGET_ID,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.SECTION,
                explicit_label="4.12.1",
                image_page_numbers=(
                    tuple(item.page_number for item in images) if bind_images else ()
                ),
                target_source_refs=(SOURCE_REF,),
                evidence=(_evidence(),),
            ),
        ),
        images=images,
    )


def _audit_request() -> StructureAuditRequest:
    evidence_request = _classification_request()
    audit_text = "6．4．1． A force test instrument is required."
    audit_evidence = SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=audit_text,
        text_sha256=hashlib.sha256(audit_text.encode()).hexdigest(),
    )
    evidence_request = evidence_request.model_copy(
        update={
            "nodes": (
                evidence_request.nodes[0].model_copy(
                    update={
                        "kind": StructureNodeKind.CONTENT,
                        "explicit_label": None,
                        "evidence": (audit_evidence,),
                    }
                ),
            )
        }
    )
    return StructureAuditRequest(
        evidence_request=evidence_request,
        physical_order=(
            StructureAuditPhysicalOrderInput(
                structure_node_id=TARGET_ID,
                physical_source_orders=(10,),
            ),
        ),
        outline_spine_structure_node_ids=(TARGET_ID,),
    )


def _audit_split_request() -> StructureAuditRequest:
    request = _audit_request()
    text = "6．4．1． First explicit item\n6．4．2． Second explicit item"
    evidence = SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    node = request.evidence_request.nodes[0].model_copy(
        update={"evidence": (evidence,)}
    )
    return request.model_copy(
        update={
            "evidence_request": request.evidence_request.model_copy(
                update={"nodes": (node,)}
            )
        }
    )


def _audit_table_request() -> StructureAuditRequest:
    second_text = "continued table rows"
    evidence_request = SemanticClassificationRequest(
        source_ir_id="sir-" + "3" * 32,
        source_sha256="7" * 64,
        structure_snapshot_id="structure-" + "4" * 32,
        scope_structure_node_ids=(TARGET_ID, SECOND_AUDIT_ID),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.TABLE,
                target_source_refs=(SOURCE_REF,),
                evidence=(_evidence(),),
            ),
            SemanticNodeInput(
                target_structure_node_id=SECOND_AUDIT_ID,
                source_order=1,
                kind=StructureNodeKind.TABLE,
                target_source_refs=(SECOND_AUDIT_SOURCE_REF,),
                evidence=(
                    SemanticEvidenceInput(
                        source_ref=SECOND_AUDIT_SOURCE_REF,
                        text=second_text,
                        text_sha256=hashlib.sha256(second_text.encode()).hexdigest(),
                    ),
                ),
            ),
        ),
    )
    return StructureAuditRequest(
        evidence_request=evidence_request,
        physical_order=(
            StructureAuditPhysicalOrderInput(
                structure_node_id=TARGET_ID,
                physical_source_orders=(10,),
            ),
            StructureAuditPhysicalOrderInput(
                structure_node_id=SECOND_AUDIT_ID,
                physical_source_orders=(11,),
            ),
        ),
        current_continuations=(
            StructureAuditContinuationInput(
                source_structure_node_id=TARGET_ID,
                target_structure_node_id=SECOND_AUDIT_ID,
                source_refs=(SECOND_AUDIT_SOURCE_REF,),
            ),
        ),
    )


def _audit_content_continuation_request() -> StructureAuditRequest:
    request = _audit_table_request()
    first, second = request.evidence_request.nodes
    return request.model_copy(
        update={
            "evidence_request": request.evidence_request.model_copy(
                update={
                    "nodes": (
                        first.model_copy(update={"kind": StructureNodeKind.CONTENT}),
                        second.model_copy(update={"kind": StructureNodeKind.CONTENT}),
                    )
                }
            )
        }
    )


def _assembly_request() -> RequirementAssemblyRequest:
    return RequirementAssemblyRequest(
        source_ir_id="sir-" + "3" * 32,
        structure_snapshot_id="structure-" + "4" * 32,
        semantic_snapshot_id="semantic-" + "5" * 32,
        scope_structure_node_ids=(TARGET_ID,),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.SECTION,
                explicit_label="4.12.1",
                semantic_annotation_ids=("semantic-annotation-" + "6" * 32,),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
                evidence=(_evidence(),),
            ),
        ),
    )


def _numbered_assembly_request(
    *,
    parent_class: SemanticClass,
    parent_subtype: RequirementSubtype | None,
    child_class: SemanticClass,
    child_subtype: RequirementSubtype | None,
) -> RequirementAssemblyRequest:
    return RequirementAssemblyRequest(
        source_ir_id="sir-" + "3" * 32,
        structure_snapshot_id="structure-" + "4" * 32,
        semantic_snapshot_id="semantic-" + "5" * 32,
        scope_structure_node_ids=(TARGET_ID, CLAUSE_ID),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.SECTION,
                explicit_label="4.12.1",
                semantic_annotation_ids=("semantic-annotation-" + "6" * 32,),
                semantic_class=parent_class,
                requirement_subtype=parent_subtype,
                evidence=(_evidence(),),
            ),
            RequirementAssemblyNodeInput(
                target_structure_node_id=CLAUSE_ID,
                source_order=1,
                kind=StructureNodeKind.CLAUSE,
                parent_structure_node_id=TARGET_ID,
                ancestor_structure_node_ids=(TARGET_ID,),
                list_ordinal=1,
                semantic_annotation_ids=("semantic-annotation-" + "7" * 32,),
                semantic_class=child_class,
                requirement_subtype=child_subtype,
                evidence=(_evidence(),),
            ),
        ),
    )


def _assembly_wire_size(user_payload: dict[str, object]) -> int:
    payload = _provider_request_payload(
        model_id="qwen3.7-plus",
        seed=DEFAULT_BAILIAN_SEMANTIC_SEED,
        system_prompt=_assembly_system_prompt(),
        user_payload=user_payload,
        images=(),
        payload_type=_AssemblyPayload,
    )
    return len(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class RequirementAssemblyNodeInputTests(unittest.TestCase):
    def test_invalid_continuation_fragment_shape_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuation fragments"):
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                parent_structure_node_id="structure-node-" + "9" * 32,
                explicit_label="1)",
                list_ordinal=1,
                semantic_annotation_ids=("semantic-annotation-" + "6" * 32,),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=(RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT),
                is_continuation_fragment=True,
                evidence=(_evidence(),),
            )


def _response(
    content: dict[str, object], *, usage: dict[str, int] | None = None
) -> Mock:
    response = Mock()
    response.status_code = 200
    payload: dict[str, object] = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(content, ensure_ascii=False),
                }
            }
        ]
    }
    if usage is not None:
        payload["usage"] = usage
    response.json.return_value = payload
    response.content = json.dumps(payload).encode()
    return response


def _raw_content_response(
    content: str,
    *,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> Mock:
    response = Mock()
    response.status_code = 200
    payload: dict[str, object] = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content},
            }
        ]
    }
    if usage is not None:
        payload["usage"] = usage
    response.json.return_value = payload
    response.content = json.dumps(payload).encode()
    return response


def _status_response(status_code: int) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.content = b""
    return response


def _png_ihdr_payload(
    width: int,
    height: int,
    *,
    total_size: int | None = None,
) -> bytes:
    payload = b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            (13).to_bytes(4, "big"),
            b"IHDR",
            width.to_bytes(4, "big"),
            height.to_bytes(4, "big"),
            b"\x08\x02\x00\x00\x00",
            b"\x00\x00\x00\x00",
        )
    )
    if total_size is None:
        return payload
    if total_size < len(payload):
        raise ValueError("total_size is smaller than the PNG header")
    return payload + (b"x" * (total_size - len(payload)))


if __name__ == "__main__":
    unittest.main()
