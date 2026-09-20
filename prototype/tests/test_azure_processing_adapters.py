from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import fitz

from app.adapters.documents.azure_document_intelligence_source_ir_adapter import (
    AZURE_DI_SOURCE_IR_ADAPTER_ID,
    AzureDocumentIntelligenceSourceIRAdapter,
)
from app.adapters.documents.azure_openai_semantic_provider import (
    AzureOpenAISemanticProvider,
)
from app.adapters.documents.azure_openai_structure_optimizer_provider import (
    AzureOpenAIStructureOptimizerProvider,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.domain.extraction.source_ir import SourceNodeKind


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: object = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _DocumentIntelligenceClient:
    def __init__(self, *, post_status: int = 202) -> None:
        self.post_status = post_status
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.operation_url = (
            "https://reqpilot.cognitiveservices.azure.com/"
            "documentintelligence/documentModels/prebuilt-layout/"
            "analyzeResults/operation-1"
        )

    def post(self, url: str, **kwargs: object) -> _Response:
        self.post_calls.append((url, kwargs))
        return _Response(
            self.post_status,
            headers=(
                {"Operation-Location": self.operation_url}
                if self.post_status == 202
                else {}
            ),
        )

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        region = {
            "pageNumber": 1,
            "polygon": [40, 40, 560, 40, 560, 100, 40, 100],
        }
        cell_regions = [
            {
                "pageNumber": 1,
                "polygon": [40, 140, 300, 140, 300, 180, 40, 180],
            },
            {
                "pageNumber": 1,
                "polygon": [300, 140, 560, 140, 560, 180, 300, 180],
            },
            {
                "pageNumber": 1,
                "polygon": [40, 180, 300, 180, 300, 220, 40, 220],
            },
            {
                "pageNumber": 1,
                "polygon": [300, 180, 560, 180, 560, 220, 300, 220],
            },
        ]
        payload = {
            "status": "succeeded",
            "analyzeResult": {
                "modelId": "prebuilt-layout",
                "pages": [{"pageNumber": 1, "width": 612, "height": 792}],
                "paragraphs": [
                    {
                        "role": "title",
                        "content": "1 Scope",
                        "spans": [{"offset": 0, "length": 7}],
                        "boundingRegions": [region],
                    },
                    {
                        "content": "The supplier shall provide a pump.",
                        "spans": [{"offset": 8, "length": 35}],
                        "boundingRegions": [region],
                    },
                ],
                "tables": [
                    {
                        "rowCount": 2,
                        "columnCount": 2,
                        "spans": [{"offset": 44, "length": 8}],
                        "boundingRegions": [
                            {
                                "pageNumber": 1,
                                "polygon": [
                                    40,
                                    140,
                                    560,
                                    140,
                                    560,
                                    220,
                                    40,
                                    220,
                                ],
                            }
                        ],
                        "cells": [
                            {
                                "rowIndex": index // 2,
                                "columnIndex": index % 2,
                                "content": text,
                                "kind": (
                                    "columnHeader" if index < 2 else "content"
                                ),
                                "boundingRegions": [cell_regions[index]],
                            }
                            for index, text in enumerate(("A", "B", "1", "2"))
                        ],
                    }
                ],
            },
        }
        return _Response(200, payload=payload)


class _CapturingAzureClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.response = SimpleNamespace(status_code=200)

    def post(self, url: str, **kwargs: object) -> object:
        self.calls.append((url, kwargs))
        return self.response


def _pdf_source(directory: str) -> Path:
    source = Path(directory) / "source.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Original bytes remain immutable")
    document.save(source)
    document.close()
    return source


class AzureDocumentIntelligenceSourceIRTests(unittest.TestCase):
    def test_full_pdf_layout_maps_to_existing_source_ir_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _pdf_source(temporary)
            report = LocalDocumentPreflight().inspect(source)
            client = _DocumentIntelligenceClient()
            adapter = AzureDocumentIntelligenceSourceIRAdapter(
                endpoint="https://reqpilot.cognitiveservices.azure.com",
                api_key="test-key",
                plan_identity_sha256="a" * 64,
                http_client=client,
                poll_interval_seconds=0,
            )
            first = adapter.extract(
                source,
                preflight=report,
                created_at=datetime(2026, 7, 29, tzinfo=UTC),
            )
            replay = adapter.extract(
                source,
                preflight=report,
                created_at=datetime(2026, 7, 29, tzinfo=UTC),
            )

        self.assertEqual(first.adapter_id, AZURE_DI_SOURCE_IR_ADAPTER_ID)
        self.assertIn("+plan.aaaaaaaaaaaa", first.adapter_version)
        self.assertEqual(first.source_sha256, report.source_sha256)
        self.assertEqual(first.source_ir_id, replay.source_ir_id)
        self.assertEqual(
            [item.source_node_id for item in first.nodes],
            [item.source_node_id for item in replay.nodes],
        )
        self.assertEqual(
            [item.kind for item in first.nodes[:2]],
            [SourceNodeKind.HEADING, SourceNodeKind.PARAGRAPH],
        )
        table_nodes = [
            item for item in first.nodes if item.kind is SourceNodeKind.TABLE
        ]
        self.assertEqual(len(table_nodes), 1)
        table = table_nodes[0].table_payload
        assert table is not None
        self.assertEqual(table.row_count, 2)
        self.assertEqual(table.column_count, 2)
        self.assertEqual(len(table.cells), 4)
        self.assertEqual(len(client.post_calls), 2)
        posted_json = client.post_calls[0][1]["json"]
        self.assertIn("base64Source", posted_json)
        self.assertNotIn("test-key", str(posted_json))

    def test_primary_parser_failure_propagates_when_no_upstream_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _pdf_source(temporary)
            report = LocalDocumentPreflight().inspect(source)
            adapter = AzureDocumentIntelligenceSourceIRAdapter(
                endpoint="https://reqpilot.cognitiveservices.azure.com",
                api_key="test-key",
                http_client=_DocumentIntelligenceClient(post_status=503),
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "azure_di_primary_request_rejected",
            ):
                adapter.extract(
                    source,
                    preflight=report,
                    created_at=datetime.now(UTC),
                )


class AzureOpenAIProviderTransportTests(unittest.TestCase):
    def test_semantic_transport_uses_rbac_deployment_and_azure_payload(
        self,
    ) -> None:
        client = _CapturingAzureClient()
        provider = AzureOpenAISemanticProvider(
            endpoint="https://reqpilot.openai.azure.com",
            deployment="gpt5.6-terra",
            api_version="2026-06-01-preview",
            token_provider=lambda: "rbac-token",
            http_client=client,
        )

        returned = provider._post(
            {
                "model": "must-not-be-sent",
                "messages": [{"role": "user", "content": "bounded input"}],
                "seed": 17,
                "temperature": 0,
                "enable_thinking": False,
                "stream": True,
            }
        )

        self.assertIs(returned, client.response)
        url, kwargs = client.calls[0]
        self.assertIn(
            "/openai/deployments/gpt5.6-terra/chat/completions",
            url,
        )
        self.assertIn("api-version=2026-06-01-preview", url)
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer rbac-token",
        )
        payload = kwargs["json"]
        self.assertNotIn("model", payload)
        self.assertNotIn("seed", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("enable_thinking", payload)
        self.assertTrue(payload["stream"])

    def test_structure_optimizer_freezes_sol_and_terra_separately(self) -> None:
        overview_client = _CapturingAzureClient()
        repair_client = _CapturingAzureClient()
        provider = AzureOpenAIStructureOptimizerProvider(
            endpoint="https://reqpilot.services.ai.azure.com",
            sol_deployment="gpt5.6-sol",
            terra_deployment="gpt5.6-terra",
            api_version="2026-06-01-preview",
            token_provider=lambda: "rbac-token",
            overview_http_client=overview_client,
            repair_http_client=repair_client,
        )

        self.assertEqual(provider.overview_model_id, "gpt5.6-sol")
        self.assertEqual(provider.repair_model_id, "gpt5.6-terra")
        self.assertEqual(provider.model_id, "gpt5.6-sol|gpt5.6-terra")
        self.assertIn(
            "/deployments/gpt5.6-sol/",
            provider._overview._endpoint,
        )
        self.assertIn(
            "/deployments/gpt5.6-terra/",
            provider._repair._endpoint,
        )

    def test_openai_endpoint_rejects_unapproved_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved HTTPS origin"):
            AzureOpenAISemanticProvider(
                endpoint="https://example.com",
                deployment="gpt5.6-terra",
                api_version="2026-06-01-preview",
                token_provider=lambda: "token",
            )


if __name__ == "__main__":
    unittest.main()
