from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import base64
import copy
import hashlib
import unittest
from typing import Any
from unittest import mock

import httpx

from app.adapters.documents.azure_document_intelligence_visual_text_provider import (
    AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
    AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID,
    AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID,
    AzureDocumentIntelligenceVisualTextProvider,
)
from app.domain.extraction.visual_text import VisualTextPageInput
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


_ENDPOINT = "https://example.cognitiveservices.azure.com"
_OPERATION_LOCATION = (
    f"{_ENDPOINT}/documentintelligence/documentModels/"
    f"{AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID}/analyzeResults/operation-1"
    f"?api-version={AZURE_DOCUMENT_INTELLIGENCE_API_VERSION}"
)


class AzureDocumentIntelligenceVisualTextProviderTests(unittest.TestCase):
    def test_async_read_result_is_stably_sorted_and_grounded_to_pdf_page(self) -> None:
        client = _FakeClient(
            post_response=_FakeResponse(
                202,
                headers={"Operation-Location": _OPERATION_LOCATION},
            ),
            get_responses=[
                _FakeResponse(200, payload={"status": "running"}),
                _FakeResponse(200, payload=_succeeded_payload()),
            ],
        )
        provider = _provider(client)
        page_input = _page_input()

        with mock.patch(
            "builtins.open",
            side_effect=AssertionError("provider must not write a rendered page"),
        ):
            snapshot = provider.recognize_page(page_input)

        self.assertEqual(snapshot.provider_id, AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID)
        self.assertEqual(snapshot.source_sha256, page_input.source_sha256)
        self.assertEqual(snapshot.page_image_sha256, page_input.page_image_sha256)
        self.assertEqual(snapshot.page_number, 3)
        self.assertTrue(snapshot.visual_text_snapshot_id.startswith("vts-"))
        self.assertEqual(
            tuple(item.raw_text for item in snapshot.observations),
            ("Top", "Bottom"),
        )
        self.assertEqual(snapshot.observations[0].bbox, (20.0, 50.0, 100.0, 70.0))
        self.assertEqual(snapshot.observations[1].bbox, (30.0, 170.0, 110.0, 185.0))
        self.assertAlmostEqual(snapshot.observations[0].confidence, 0.9)
        self.assertAlmostEqual(snapshot.observations[1].confidence, 0.7)
        self.assertTrue(
            all(item.observation_id.startswith("vto-") for item in snapshot.observations)
        )

        self.assertEqual(len(client.post_calls), 1)
        post_url, post_kwargs = client.post_calls[0]
        self.assertEqual(
            post_url,
            f"{_ENDPOINT}/documentintelligence/documentModels/"
            f"{AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID}:analyze",
        )
        self.assertEqual(
            post_kwargs["params"],
            {
                "_overload": "analyzeDocument",
                "api-version": AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
            },
        )
        self.assertEqual(
            post_kwargs["headers"]["Ocp-Apim-Subscription-Key"],
            "test-api-key",
        )
        self.assertEqual(
            base64.b64decode(post_kwargs["json"]["base64Source"]),
            page_input.page_image_bytes,
        )
        self.assertNotIn("source_bytes", post_kwargs["json"])
        self.assertEqual(len(client.get_calls), 2)
        self.assertTrue(
            all(call[0] == _OPERATION_LOCATION for call in client.get_calls)
        )
        self.assertTrue(
            all(
                call[1]["headers"]["Ocp-Apim-Subscription-Key"] == "test-api-key"
                for call in client.get_calls
            )
        )

    def test_input_type_and_hashes_are_rejected_before_remote_use(self) -> None:
        client = _FakeClient(post_response=_FakeResponse(202), get_responses=[])
        provider = _provider(client)

        with self.assertRaises(VisualTextProviderRejected) as invalid_input:
            provider.recognize_page(object())  # type: ignore[arg-type]
        self.assertEqual(
            invalid_input.exception.code,
            VisualTextProviderRejectionCode.INVALID_INPUT,
        )

        with self.assertRaises(VisualTextProviderRejected) as stale_source:
            provider.recognize_page(_page_input(source_sha256="0" * 64))
        self.assertEqual(
            stale_source.exception.code,
            VisualTextProviderRejectionCode.SOURCE_HASH_MISMATCH,
        )

        with self.assertRaises(VisualTextProviderRejected) as stale_image:
            provider.recognize_page(_page_input(page_image_sha256="1" * 64))
        self.assertEqual(
            stale_image.exception.code,
            VisualTextProviderRejectionCode.PAGE_IMAGE_HASH_MISMATCH,
        )
        self.assertEqual(client.post_calls, [])
        self.assertEqual(client.get_calls, [])

    def test_missing_or_unsafe_configuration_is_unavailable(self) -> None:
        for endpoint, key in (
            ("", "test-api-key"),
            ("http://example.test", "test-api-key"),
            ("https://user@example.test", "test-api-key"),
            (_ENDPOINT, ""),
        ):
            with self.subTest(endpoint=endpoint, has_key=bool(key)):
                provider = AzureDocumentIntelligenceVisualTextProvider(
                    endpoint=endpoint,
                    api_key=key,
                    http_client=_FakeClient(
                        post_response=_FakeResponse(202),
                        get_responses=[],
                    ),
                )
                with self.assertRaises(VisualTextProviderRejected) as error:
                    provider.recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.UNAVAILABLE,
                )

    def test_transport_http_and_authorization_failures_are_safely_mapped(self) -> None:
        transport_client = _FailingClient(
            httpx.ReadTimeout("customer text and secret request detail")
        )
        with self.assertRaises(VisualTextProviderRejected) as transport_error:
            _provider(transport_client).recognize_page(_page_input())
        self.assertEqual(
            transport_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertNotIn("customer", str(transport_error.exception))
        self.assertNotIn("secret", str(transport_error.exception))

        unauthorized = _FakeClient(
            post_response=_FakeResponse(401, payload={"error": {"message": "secret"}}),
            get_responses=[],
        )
        with self.assertRaises(VisualTextProviderRejected) as auth_error:
            _provider(unauthorized).recognize_page(_page_input())
        self.assertEqual(
            auth_error.exception.code,
            VisualTextProviderRejectionCode.UNAVAILABLE,
        )
        self.assertNotIn("secret", str(auth_error.exception))

        rejected = _FakeClient(
            post_response=_FakeResponse(500, payload={"error": "customer body"}),
            get_responses=[],
        )
        with self.assertRaises(VisualTextProviderRejected) as http_error:
            _provider(rejected).recognize_page(_page_input())
        self.assertEqual(
            http_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertNotIn("customer", str(http_error.exception))

    def test_operation_location_must_be_present_and_same_origin(self) -> None:
        responses = (
            _FakeResponse(202, headers={}),
            _FakeResponse(
                202,
                headers={
                    "Operation-Location": (
                        "https://attacker.example/documentintelligence/documentModels/"
                        "prebuilt-read/analyzeResults/operation-1"
                    )
                },
            ),
            _FakeResponse(
                202,
                headers={"Operation-Location": f"{_ENDPOINT}/untrusted/result"},
            ),
        )
        for response in responses:
            with self.subTest(headers=response.headers):
                client = _FakeClient(post_response=response, get_responses=[])
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )
                self.assertEqual(client.get_calls, [])

    def test_polling_is_bounded_and_provider_failure_body_is_not_exposed(self) -> None:
        client = _FakeClient(
            post_response=_accepted_response(),
            get_responses=[
                _FakeResponse(200, payload={"status": "running"}),
                _FakeResponse(200, payload={"status": "notStarted"}),
            ],
        )
        provider = _provider(client, max_poll_attempts=2)
        with self.assertRaises(VisualTextProviderRejected) as timeout_error:
            provider.recognize_page(_page_input())
        self.assertEqual(
            timeout_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertEqual(len(client.get_calls), 2)

        failed = _FakeClient(
            post_response=_accepted_response(),
            get_responses=[
                _FakeResponse(
                    200,
                    payload={
                        "status": "failed",
                        "error": {"message": "customer document text"},
                    },
                )
            ],
        )
        with self.assertRaises(VisualTextProviderRejected) as failed_error:
            _provider(failed).recognize_page(_page_input())
        self.assertEqual(
            failed_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertNotIn("customer", str(failed_error.exception))

    def test_invalid_poll_payload_and_unknown_status_are_output_invalid(self) -> None:
        for response in (
            _FakeResponse(200, payload=["not an object"]),
            _FakeResponse(200, json_error=ValueError("customer result text")),
            _FakeResponse(200, payload={"status": "mystery"}),
            _FakeResponse(200, payload={}),
        ):
            with self.subTest(response=response):
                client = _FakeClient(
                    post_response=_accepted_response(),
                    get_responses=[response],
                )
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )
                self.assertNotIn("customer", str(error.exception))

    def test_malformed_result_or_untrusted_bbox_is_output_invalid(self) -> None:
        invalid_payloads: list[dict[str, Any]] = []

        wrong_model = _succeeded_payload()
        wrong_model["analyzeResult"]["modelId"] = "prebuilt-layout"
        invalid_payloads.append(wrong_model)

        wrong_version = _succeeded_payload()
        wrong_version["analyzeResult"]["apiVersion"] = "2023-07-31"
        invalid_payloads.append(wrong_version)

        wrong_page_count = _succeeded_payload()
        wrong_page_count["analyzeResult"]["pages"] = []
        invalid_payloads.append(wrong_page_count)

        wrong_unit = _succeeded_payload()
        wrong_unit["analyzeResult"]["pages"][0]["unit"] = "inch"
        invalid_payloads.append(wrong_unit)

        wrong_dimensions = _succeeded_payload()
        wrong_dimensions["analyzeResult"]["pages"][0]["width"] = 399
        invalid_payloads.append(wrong_dimensions)

        outside_polygon = _succeeded_payload()
        outside_polygon["analyzeResult"]["pages"][0]["lines"][0]["polygon"] = [
            40,
            300,
            500,
            300,
            500,
            330,
            40,
            330,
        ]
        invalid_payloads.append(outside_polygon)

        unmatched_line_span = _succeeded_payload()
        unmatched_line_span["analyzeResult"]["pages"][0]["lines"][0]["spans"] = [
            {"offset": 100, "length": 6}
        ]
        invalid_payloads.append(unmatched_line_span)

        invalid_confidence = _succeeded_payload()
        invalid_confidence["analyzeResult"]["pages"][0]["words"][0][
            "confidence"
        ] = "0.7"
        invalid_payloads.append(invalid_confidence)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client = _FakeClient(
                    post_response=_accepted_response(),
                    get_responses=[_FakeResponse(200, payload=payload)],
                )
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )

    def test_timeout_and_attempt_settings_are_finite_and_bounded(self) -> None:
        invalid_kwargs = (
            {"request_timeout_seconds": 0},
            {"request_timeout_seconds": 121},
            {"poll_timeout_seconds": float("inf")},
            {"poll_interval_seconds": -1},
            {"max_poll_attempts": 0},
            {"max_poll_attempts": 1_201},
        )
        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    AzureDocumentIntelligenceVisualTextProvider(
                        endpoint=_ENDPOINT,
                        api_key="test-api-key",
                        http_client=_FakeClient(
                            post_response=_accepted_response(),
                            get_responses=[],
                        ),
                        **kwargs,
                    )


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: Any = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return copy.deepcopy(self._payload)


class _FakeClient:
    def __init__(
        self,
        *,
        post_response: _FakeResponse,
        get_responses: list[_FakeResponse],
    ) -> None:
        self.post_response = post_response
        self.get_responses = list(get_responses)
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.post_calls.append((url, kwargs))
        return self.post_response

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError("unexpected Azure poll request")
        return self.get_responses.pop(0)


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        raise self.error

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("polling must not start after a failed request")


def _provider(
    client: _FakeClient | _FailingClient,
    *,
    max_poll_attempts: int = 8,
) -> AzureDocumentIntelligenceVisualTextProvider:
    return AzureDocumentIntelligenceVisualTextProvider(
        endpoint=_ENDPOINT,
        api_key="test-api-key",
        http_client=client,
        request_timeout_seconds=5.0,
        poll_timeout_seconds=10.0,
        poll_interval_seconds=0.0,
        max_poll_attempts=max_poll_attempts,
    )


def _accepted_response() -> _FakeResponse:
    return _FakeResponse(
        202,
        headers={"Operation-Location": _OPERATION_LOCATION},
    )


def _succeeded_payload() -> dict[str, Any]:
    return {
        "status": "succeeded",
        "analyzeResult": {
            "apiVersion": AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
            "modelId": AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID,
            "pages": [
                {
                    "pageNumber": 1,
                    "width": 400,
                    "height": 600,
                    "unit": "pixel",
                    "words": [
                        {
                            "content": "Bottom",
                            "confidence": 0.7,
                            "span": {"offset": 4, "length": 6},
                        },
                        {
                            "content": "Top",
                            "confidence": 0.9,
                            "span": {"offset": 0, "length": 3},
                        },
                    ],
                    "lines": [
                        {
                            "content": "Bottom",
                            "polygon": [40, 300, 200, 300, 200, 330, 40, 330],
                            "spans": [{"offset": 4, "length": 6}],
                        },
                        {
                            "content": "Top",
                            "polygon": [20, 60, 180, 60, 180, 100, 20, 100],
                            "spans": [{"offset": 0, "length": 3}],
                        },
                    ],
                }
            ],
        },
    }


def _page_input(
    *,
    source_sha256: str | None = None,
    page_image_sha256: str | None = None,
) -> VisualTextPageInput:
    source_bytes = b"approved PDF bytes"
    page_image_bytes = b"\x89PNG\r\n\x1a\nin-memory page"
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=(
            source_sha256
            if source_sha256 is not None
            else hashlib.sha256(source_bytes).hexdigest()
        ),
        page_number=3,
        page_bbox=(10.0, 20.0, 210.0, 320.0),
        page_image_bytes=page_image_bytes,
        page_image_sha256=(
            page_image_sha256
            if page_image_sha256 is not None
            else hashlib.sha256(page_image_bytes).hexdigest()
        ),
        page_image_width_px=400,
        page_image_height_px=600,
    )


if __name__ == "__main__":
    unittest.main()
