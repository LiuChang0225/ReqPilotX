from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
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
from io import BytesIO

import fitz
import httpx
from PIL import Image as PILImage

from app.adapters.documents.bailian_pdf_page_parsing_provider import (
    BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID,
    DEFAULT_BAILIAN_QWEN38_PDF_MODEL,
    PDF_PAGE_PARSING_TASK,
    BailianQwen38PdfPageParsingProvider,
    derive_single_page_pdf,
)
from app.domain.source_visual_text import VISUAL_CONTENT_TYPES, VisualTextPageInput
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


class BailianQwen38PdfPageParsingProviderTests(unittest.TestCase):
    def test_contract_sends_only_one_derived_pdf_and_maps_grounded_output(self) -> None:
        client = _Client(_Response(200, _completion(_page_parse())))
        provider = _provider(client)
        page_input = _page_input()

        snapshot = provider.recognize_page(page_input)

        self.assertEqual(snapshot.provider_id, BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID)
        self.assertEqual(snapshot.provider_version, provider.provider_version)
        self.assertIn("model=qwen3.8-max", snapshot.provider_version)
        self.assertIn(f"task={PDF_PAGE_PARSING_TASK}", snapshot.provider_version)
        self.assertIn("retry=0", snapshot.provider_version)
        self.assertEqual(snapshot.page_image_sha256, page_input.page_image_sha256)
        self.assertEqual(snapshot.page_bbox, page_input.page_bbox)
        self.assertEqual(snapshot.evaluated_content_types, VISUAL_CONTENT_TYPES)
        self.assertEqual(snapshot.observations[0].raw_text, "第二页正文")
        self.assertEqual(
            snapshot.observations[0].bbox,
            (30.0, 80.0, 270.0, 200.0),
        )
        table = snapshot.observations[1].layout_table
        self.assertIsNotNone(table)
        assert table is not None
        self.assertEqual([cell.raw_text for cell in table.cells], ["项目", "参数"])

        self.assertEqual(len(client.calls), 1)
        endpoint, kwargs = client.calls[0]
        self.assertEqual(kwargs["timeout"], 300.0)
        self.assertTrue(endpoint.endswith("/compatible-mode/v1/chat/completions"))
        payload = kwargs["json"]
        self.assertEqual(payload["model"], DEFAULT_BAILIAN_QWEN38_PDF_MODEL)
        self.assertIs(payload["stream"], True)
        self.assertNotIn("image_url", json.dumps(payload))
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        json_schema = payload["response_format"]["json_schema"]
        self.assertIs(json_schema["strict"], True)
        self.assertEqual(json_schema["schema"]["additionalProperties"], False)
        file_part = payload["messages"][1]["content"][0]
        self.assertEqual(file_part["type"], "file")
        self.assertEqual(file_part["file"]["filename"], "source-page-0002.pdf")
        file_data = file_part["file"]["file_data"]
        prefix = "data:application/pdf;base64,"
        self.assertTrue(file_data.startswith(prefix))
        sent_pdf = base64.b64decode(file_data[len(prefix) :], validate=True)
        sent = fitz.open(stream=sent_pdf, filetype="pdf")
        try:
            self.assertEqual(sent.page_count, 1)
            self.assertEqual(sent[0].get_text(), "")
            self.assertEqual(len(sent[0].get_images(full=True)), 1)
        finally:
            sent.close()

    def test_single_page_derivation_is_deterministic_raster_only_and_exact(self) -> None:
        page_input = _page_input()

        first = derive_single_page_pdf(page_input)
        second = derive_single_page_pdf(page_input)

        self.assertEqual(first, second)
        document = fitz.open(stream=first, filetype="pdf")
        try:
            self.assertEqual(document.page_count, 1)
            page = document[0]
            self.assertEqual(page.get_text(), "")
            self.assertEqual(page.get_links(), [])
            self.assertEqual(list(page.annots() or ()), [])
            self.assertEqual(list(page.widgets() or ()), [])
            self.assertEqual(document.embfile_names(), [])
            self.assertEqual(tuple(page.mediabox), page_input.page_bbox)
            self.assertAlmostEqual(
                page.rect.width / page.rect.height,
                (page_input.page_bbox[2] - page_input.page_bbox[0])
                / (page_input.page_bbox[3] - page_input.page_bbox[1]),
            )
            images = page.get_images(full=True)
            self.assertEqual(len(images), 1)
            image_xref = images[0][0]
            self.assertEqual(page.get_image_rects(image_xref), [page.rect])
            extracted = document.extract_image(image_xref)["image"]
            with PILImage.open(BytesIO(extracted)) as embedded, PILImage.open(
                BytesIO(page_input.page_image_bytes)
            ) as original:
                self.assertEqual(embedded.size, original.size)
                self.assertEqual(
                    embedded.convert("RGBA").tobytes(),
                    original.convert("RGBA").tobytes(),
                )
        finally:
            document.close()

    def test_missing_or_invalid_bbox_fails_closed_without_retry(self) -> None:
        missing = _page_parse()
        missing["blocks"][0].pop("bbox")
        outside = _page_parse()
        outside["blocks"][0]["bbox"] = [-1, 100, 900, 500]
        for response in (missing, outside):
            with self.subTest(response=response):
                client = _Client(_Response(200, _completion(response)))
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )
                self.assertEqual(len(client.calls), 1)

    def test_empty_output_is_allowed_only_for_locally_blank_pixels(self) -> None:
        empty = {
            "schema_version": "pdf-page-parsing/v1",
            "coordinate_space": "NORMALIZED_1000_TOP_LEFT",
            "evaluated_types": list(VISUAL_CONTENT_TYPES),
            "blocks": [],
        }
        blank = _provider(_Client(_Response(200, _completion(empty)))).recognize_page(
            _page_input()
        )
        self.assertEqual(blank.observations, ())

        non_blank_input = _page_input(image_color=(0, 0, 0))
        client = _Client(_Response(200, _completion(empty)))
        with self.assertRaises(VisualTextProviderRejected) as error:
            _provider(client).recognize_page(non_blank_input)
        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertEqual(len(client.calls), 1)

    def test_auth_quota_access_rate_size_and_transport_failures_are_explicit(self) -> None:
        cases = (
            (401, {"error": {"message": "bad key"}}, VisualTextProviderRejectionCode.AUTHENTICATION_FAILED),
            (403, {"error": {"code": "QuotaExhausted"}}, VisualTextProviderRejectionCode.QUOTA_EXHAUSTED),
            (403, {"error": {"code": "FreeTierOnly"}}, VisualTextProviderRejectionCode.QUOTA_EXHAUSTED),
            (403, {"error": {"code": "Forbidden"}}, VisualTextProviderRejectionCode.ACCESS_DENIED),
            (429, {"error": {"code": "RateLimit"}}, VisualTextProviderRejectionCode.RATE_LIMITED),
            (413, {"error": {"code": "TooLarge"}}, VisualTextProviderRejectionCode.INVALID_INPUT),
        )
        for status, payload, code in cases:
            with self.subTest(status=status, code=code):
                client = _Client(_Response(status, payload))
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("bad key", str(error.exception))
                self.assertEqual(len(client.calls), 1)

        client = _Client(httpx.ConnectError("offline"))
        with self.assertRaises(VisualTextProviderRejected) as error:
            _provider(client).recognize_page(_page_input())
        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertEqual(len(client.calls), 1)

    def test_only_exact_qwen38_model_and_zero_retries_are_allowed(self) -> None:
        for kwargs in (
            {"model": "qwen3.8-max-preview"},
            {"model": "qwen3.7-plus"},
            {"max_retries": 1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BailianQwen38PdfPageParsingProvider(
                        api_key="test-key",
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        **kwargs,
                    )

    def test_placeholder_key_is_rejected_before_http(self) -> None:
        client = _Client(_Response(200, _completion(_page_parse())))
        provider = BailianQwen38PdfPageParsingProvider(
            api_key="aliyun-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            http_client=client,
        )

        with self.assertRaises(VisualTextProviderRejected) as error:
            provider.recognize_page(_page_input())

        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.UNAVAILABLE,
        )
        self.assertEqual(client.calls, [])


def _provider(client: "_Client") -> BailianQwen38PdfPageParsingProvider:
    return BailianQwen38PdfPageParsingProvider(
        api_key="sk-test-0000000000000000000000000000",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=client,
    )


def _page_input(
    *, image_color: tuple[int, int, int] = (255, 255, 255)
) -> VisualTextPageInput:
    source = fitz.open()
    try:
        for page_number in range(1, 4):
            page = source.new_page(width=300, height=400)
            page.insert_text((40, 50), f"PAGE-{page_number}")
        source_bytes = source.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        source.close()
    image = PILImage.new("RGB", (600, 800), color=image_color)
    output = BytesIO()
    image.save(output, format="PNG")
    image_bytes = output.getvalue()
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=2,
        page_bbox=(0.0, 0.0, 300.0, 400.0),
        page_image_bytes=image_bytes,
        page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        page_image_width_px=600,
        page_image_height_px=800,
    )


def _page_parse() -> dict[str, object]:
    return {
        "schema_version": "pdf-page-parsing/v1",
        "coordinate_space": "NORMALIZED_1000_TOP_LEFT",
        "evaluated_types": list(VISUAL_CONTENT_TYPES),
        "blocks": [
            {
                "index": 0,
                "type": "TEXT",
                "role": None,
                "bbox": [100, 200, 900, 500],
                "text": "第二页正文",
                "table": None,
            },
            {
                "index": 1,
                "type": "TABLE",
                "role": None,
                "bbox": [100, 550, 900, 850],
                "text": "",
                "table": {
                    "row_count": 1,
                    "column_count": 2,
                    "header_row_count": 1,
                    "starts_mid_table": False,
                    "continues_next_page": False,
                    "cells": [
                        _cell(0, 0, "项目"),
                        _cell(0, 1, "参数"),
                    ],
                },
            },
        ],
    }


def _cell(row: int, column: int, text: str) -> dict[str, object]:
    return {
        "row": row,
        "column": column,
        "row_span": 1,
        "column_span": 1,
        "text": text,
        "horizontal_alignment": "LEFT",
        "vertical_alignment": "TOP",
        "indent_level": 0,
    }


def _completion(payload: object) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            }
        ]
    }


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def json(self) -> object:
        return self._payload

    def iter_lines(self):
        if self.status_code != 200:
            return
        content = self._payload["choices"][0]["message"]["content"]
        midpoint = max(1, len(content) // 2)
        for part in (content[:midpoint], content[midpoint:]):
            yield "data: " + json.dumps(
                {
                    "choices": [
                        {"delta": {"content": part}, "finish_reason": None}
                    ]
                },
                ensure_ascii=False,
            )
        yield 'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}'
        yield 'data: {"choices":[],"usage":{"total_tokens":1}}'
        yield "data: [DONE]"


class _Client:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stream(self, method: str, endpoint: str, **kwargs: object) -> object:
        self.assert_post(method)
        self.calls.append((endpoint, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @staticmethod
    def assert_post(method: str) -> None:
        if method != "POST":
            raise AssertionError("PDF page parsing must use POST")

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
