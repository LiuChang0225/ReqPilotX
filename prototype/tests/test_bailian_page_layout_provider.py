from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest
from dataclasses import asdict
from io import BytesIO

import httpx
from app.adapters.documents.bailian_page_layout_provider import (
    BAILIAN_PAGE_LAYOUT_PROVIDER_IMPLEMENTATION_VERSION,
    DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL,
    DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
    BailianPageLayoutProvider,
)
from app.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
)
from app.domain.extraction.visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualTextPageInput,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.services.visual_text_snapshot_cache import VisualTextProviderTelemetryV1
from PIL import Image as PILImage


class BailianPageLayoutProviderTests(unittest.TestCase):
    def test_semantically_empty_optional_fields_may_be_omitted(self) -> None:
        layout = _full_layout()
        elements = layout["elements"]
        assert isinstance(elements, list)
        for element in elements:
            assert isinstance(element, dict)
            if element["table"] is None:
                element.pop("table")
            if element["role"] is None:
                element.pop("role")
            if element["parent_table_index"] is None:
                element.pop("parent_table_index")
            if element["parent_cell"] is None:
                element.pop("parent_cell")
            if element["type"] in {"TABLE", "FIGURE"} and not element["text"]:
                element.pop("text")
        table = elements[2]["table"]
        assert isinstance(table, dict)
        cells = table["cells"]
        assert isinstance(cells, list)
        cells[0].pop("horizontal_alignment")
        cells[0].pop("vertical_alignment")
        client = _Client(_Response(200, _completion(layout)))

        snapshot = _provider(client).recognize_page(_page_input())

        self.assertEqual(
            [item.content_type for item in snapshot.observations],
            ["HEADER", "TEXT", "TABLE", "FIGURE", "FOOTER", "WATERMARK"],
        )
        table_snapshot = snapshot.observations[2].layout_table
        self.assertIsNotNone(table_snapshot)
        assert table_snapshot is not None
        self.assertIsNone(table_snapshot.cells[0].horizontal_alignment)
        self.assertIsNone(table_snapshot.cells[0].vertical_alignment)

    def test_flash_uses_the_same_strict_contract_with_a_distinct_identity(
        self,
    ) -> None:
        client = _Client(_Response(200, _completion(_full_layout())))
        provider = BailianPageLayoutProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL,
            http_client=client,
            sleep=lambda _: None,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.evaluated_content_types, VISUAL_CONTENT_TYPES)
        self.assertIn("m=qwen3.6-flash", snapshot.provider_version)
        self.assertEqual(
            client.calls[0][1]["json"]["model"],
            DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL,
        )
        self.assertEqual(
            client.calls[0][1]["json"]["response_format"],
            {"type": "json_object"},
        )

    def test_one_call_maps_six_types_table_grid_and_cell_figure(self) -> None:
        client = _Client(_Response(200, _completion(_full_layout())))
        provider = _provider(client)

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.provider_id, BAILIAN_VISUAL_TEXT_PROVIDER_ID)
        self.assertEqual(snapshot.evaluated_content_types, VISUAL_CONTENT_TYPES)
        self.assertIn("m=qwen3.7-plus", snapshot.provider_version)
        self.assertIn("dl=45", snapshot.provider_version)
        self.assertIn("tr=0", snapshot.provider_version)
        self.assertIn("cap=1", snapshot.provider_version)
        self.assertIn("or=0", snapshot.provider_version)
        self.assertIn("o=32768", snapshot.provider_version)
        self.assertIn("t=0", snapshot.provider_version)
        self.assertEqual(
            [item.content_type for item in snapshot.observations],
            ["HEADER", "TEXT", "TABLE", "FIGURE", "FOOTER", "WATERMARK"],
        )
        self.assertEqual(snapshot.observations[1].role, "TOC")
        table = snapshot.observations[2].layout_table
        self.assertIsNotNone(table)
        assert table is not None
        self.assertEqual((table.row_count, table.column_count), (2, 2))
        self.assertEqual(table.header_row_count, 1)
        self.assertEqual(table.cells[3].horizontal_alignment, "CENTER")
        self.assertEqual(table.cells[3].vertical_alignment, "MIDDLE")
        self.assertEqual(table.cells[3].indent_level, 1)
        figure = snapshot.observations[3]
        self.assertEqual(figure.raw_text, "")
        self.assertEqual(figure.parent_observation_index, 2)
        self.assertEqual(figure.parent_table_cell, (1, 1))
        self.assertEqual(
            snapshot.observations[1].bbox,
            (30.0, 50.0, 190.0, 110.0),
        )

        self.assertEqual(len(client.calls), 1)
        endpoint, kwargs = client.calls[0]
        self.assertTrue(endpoint.endswith("/compatible-mode/v1/chat/completions"))
        payload = kwargs["json"]
        self.assertEqual(payload["model"], DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL)
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_completion_tokens"], 32_768)
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(
            content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertEqual(content[0]["min_pixels"], 65_536)
        self.assertEqual(content[0]["max_pixels"], 1_572_864)
        self.assertIn("TEXT、TABLE、FIGURE", content[1]["text"])
        self.assertIn("row_count=max(row+row_span)", content[1]["text"])
        self.assertIn("column_count=max(column+column_span)", content[1]["text"])
        self.assertIn("每个坐标必须恰好由一个单元格覆盖一次", content[1]["text"])
        self.assertIn("空白边距不算表格行列", content[1]["text"])
        self.assertNotIn("source bytes", json.dumps(payload))

    def test_explicit_rotation_is_inverted_to_original_page_coordinates(self) -> None:
        layout = _empty_layout()
        layout["elements"] = [
            _element(
                0,
                "TEXT",
                [0, 0, 1_000, 1_000],
                text="整页",
            )
        ]
        client = _Client(_Response(200, _completion(layout)))
        provider = BailianPageLayoutProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            input_rotations_by_page={3: 90},
            http_client=client,
            sleep=lambda _: None,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.observations[0].bbox, (10.0, 20.0, 210.0, 320.0))
        sent_url = client.calls[0][1]["json"]["messages"][0]["content"][0]["image_url"][
            "url"
        ]
        self.assertTrue(sent_url.startswith("data:image/png;base64,"))

    def test_invalid_layout_contracts_fail_closed(self) -> None:
        invalid_layouts = []

        missing_type = _full_layout()
        missing_type["evaluated_types"] = ["TEXT"]
        invalid_layouts.append(missing_type)

        nonsequential = _full_layout()
        nonsequential["elements"][1]["index"] = 9
        invalid_layouts.append(nonsequential)

        outside = _full_layout()
        outside["elements"][0]["bbox"] = [-1, 0, 100, 100]
        invalid_layouts.append(outside)

        incomplete_grid = _full_layout()
        incomplete_grid["elements"][2]["table"]["cells"].pop()
        invalid_layouts.append(incomplete_grid)

        wrong_parent = _full_layout()
        wrong_parent["elements"][3]["parent_cell"] = [9, 9]
        invalid_layouts.append(wrong_parent)

        for layout in invalid_layouts:
            with self.subTest(layout=layout):
                client = _Client(_Response(200, _completion(layout)))
                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )
                self.assertEqual(len(client.calls), 1)

    def test_invalid_model_output_is_not_repaired_by_repeating_the_whole_page(
        self,
    ) -> None:
        incomplete_grid = _full_layout()
        incomplete_grid["elements"][2]["table"]["cells"].pop()
        client = _Client(
            _Response(200, _completion(incomplete_grid)),
            _Response(200, _completion(_full_layout())),
        )

        provider = _provider(client)
        with self.assertRaises(VisualTextProviderRejected):
            provider.recognize_page(_page_input())

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            len(client.calls[0][1]["json"]["messages"][0]["content"]),
            2,
        )
        self.assertEqual(BAILIAN_PAGE_LAYOUT_PROVIDER_IMPLEMENTATION_VERSION, "1.1.0")
        self.assertIn("or=0", provider.provider_version)
        self.assertIn("cap=1", provider.provider_version)

    def test_incomplete_grid_rejection_carries_only_safe_topology_metadata(
        self,
    ) -> None:
        incomplete_grid = _full_layout()
        incomplete_grid["elements"][2]["table"]["cells"].pop()
        client = _Client(_Response(200, _completion(incomplete_grid)))

        with self.assertRaises(VisualTextProviderRejected) as error:
            _provider(client).recognize_page(_page_input())

        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(error.exception.__cause__)
        self.assertIsNone(error.exception.__context__)
        diagnostic = asdict(error.exception.safe_diagnostic)
        self.assertEqual(
            diagnostic,
            {
                "element_index": 2,
                "table_ordinal": 0,
                "declared_row_count": 2,
                "declared_column_count": 2,
                "anchor_count": 3,
                "covered_count": 3,
                "expected_count": 4,
                "missing_coordinate_count": 1,
                "missing_coordinate_sha256": hashlib.sha256(b"[[1,1]]").hexdigest(),
                "diagnostic_code": "TABLE_GRID_INCOMPLETE",
                "schema_version": "page-layout-table-grid-diagnostic/v1",
            },
        )
        serialized = json.dumps(diagnostic, sort_keys=True)
        self.assertNotIn("text", serialized)
        self.assertNotIn("cells", serialized)
        self.assertNotIn("项目", serialized)
        self.assertNotIn(
            "invalid_output_repair",
            provider_version := _provider(
                _Client(_Response(200, _completion(_empty_layout())))
            ).provider_version,
        )
        self.assertIn("or=0", provider_version)

    def test_truncated_output_is_not_retried_and_remains_output_invalid(self) -> None:
        for finish_reason in ("length", "max_tokens"):
            with self.subTest(finish_reason=finish_reason):
                client = _Client(
                    _Response(
                        200,
                        _completion(_empty_layout()),
                        finish_reason=finish_reason,
                    ),
                    _Response(200, _completion(_empty_layout())),
                )

                with self.assertRaises(VisualTextProviderRejected) as error:
                    _provider(client).recognize_page(_page_input())

                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )
                self.assertEqual(len(client.calls), 1)
                content = client.calls[0][1]["json"]["messages"][0]["content"]
                self.assertEqual(len(content), 2)
                self.assertNotIn(
                    "上一次输出未通过严格结构校验",
                    json.dumps(client.calls[0][1]["json"], ensure_ascii=False),
                )

    def test_hash_auth_response_and_retry_failures_are_bounded(self) -> None:
        page_input = _page_input()
        changed = page_input.model_copy(
            update={"page_image_sha256": "0" * 64},
        )
        no_call_client = _Client(_Response(200, _completion(_empty_layout())))
        with self.assertRaises(VisualTextProviderRejected) as hash_error:
            _provider(no_call_client).recognize_page(changed)
        self.assertEqual(
            hash_error.exception.code,
            VisualTextProviderRejectionCode.PAGE_IMAGE_HASH_MISMATCH,
        )
        self.assertEqual(no_call_client.calls, [])

        unauthorized = _Client(_Response(401, {"error": {"message": "secret"}}))
        with self.assertRaises(VisualTextProviderRejected) as auth_error:
            _provider(unauthorized).recognize_page(page_input)
        self.assertEqual(
            auth_error.exception.code,
            VisualTextProviderRejectionCode.UNAVAILABLE,
        )
        self.assertNotIn("secret", str(auth_error.exception))

        rate_limited = _Client(_Response(429, {"error": "private"}))
        with self.assertRaises(VisualTextProviderRejected) as rate_error:
            _provider(rate_limited).recognize_page(page_input)
        self.assertEqual(
            rate_error.exception.code,
            VisualTextProviderRejectionCode.RATE_LIMITED,
        )
        self.assertNotIn("private", str(rate_error.exception))

        retrying = _Client(
            _Response(503, {"error": "temporary"}),
            _Response(200, _completion(_empty_layout())),
        )
        snapshot = _provider(retrying, max_retries=1).recognize_page(page_input)
        self.assertEqual(snapshot.observations, ())
        self.assertEqual(len(retrying.calls), 2)

        transport = _Client(httpx.ConnectError("offline"))
        with self.assertRaises(VisualTextProviderRejected) as transport_error:
            _provider(transport, max_retries=0).recognize_page(page_input)
        self.assertEqual(
            transport_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )

    def test_streaming_wall_clock_deadline_is_absolute_for_the_whole_page(
        self,
    ) -> None:
        clock = _ManualMonotonic()
        client = _Client(
            _TricklingResponse(
                _completion(_empty_layout()),
                clock=clock,
                seconds_per_line=0.6,
            ),
        )
        backoffs: list[float] = []
        provider = BailianPageLayoutProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout_seconds=1.0,
            max_retries=1,
            http_client=client,
            sleep=backoffs.append,
            monotonic=clock,
        )

        with self.assertRaises(VisualTextProviderRejected) as error:
            provider.recognize_page(_page_input())

        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertEqual(
            str(error.exception),
            "Bailian page-layout recognition failed.",
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(backoffs, [])

    def test_transient_retry_shares_deadline_and_emits_content_free_telemetry(
        self,
    ) -> None:
        clock = _ManualMonotonic()
        client = _Client(
            _Response(503, {"error": "temporary"}),
            _Response(200, _completion(_empty_layout())),
        )
        events: list[VisualTextProviderTelemetryV1] = []
        provider = BailianPageLayoutProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout_seconds=2.0,
            max_retries=1,
            http_client=client,
            sleep=clock.advance,
            monotonic=clock,
            telemetry_callback=events.append,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.observations, ())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0][1]["timeout"], 2.0)
        self.assertEqual(client.calls[1][1]["timeout"], 1.5)
        self.assertEqual(
            events,
            [
                VisualTextProviderTelemetryV1(
                    page_number=3,
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                    provider_version=provider.provider_version,
                    request_count=2,
                    latency_ms=500,
                    cache_hit=False,
                    outcome="READY",
                )
            ],
        )
        serialized = json.dumps(events[0].as_dict(), sort_keys=True)
        self.assertNotIn("test-key", serialized)
        self.assertNotIn("dashscope", serialized)
        self.assertNotIn("项目", serialized)

    def test_failed_call_emits_once_and_callback_failure_does_not_mask_result(
        self,
    ) -> None:
        events: list[VisualTextProviderTelemetryV1] = []
        failed = BailianPageLayoutProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=1,
            http_client=_Client(
                httpx.ConnectError("offline"),
                httpx.ConnectError("still offline"),
            ),
            sleep=lambda _: None,
            telemetry_callback=events.append,
        )

        with self.assertRaises(VisualTextProviderRejected):
            failed.recognize_page(_page_input())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].request_count, 2)
        self.assertEqual(events[0].outcome, "FAILED")

        callback_failed = _provider(
            _Client(_Response(200, _completion(_empty_layout()))),
            telemetry_callback=lambda _event: (_ for _ in ()).throw(
                RuntimeError("telemetry sink unavailable")
            ),
        )
        with self.assertLogs(
            "app.services.visual_text_snapshot_cache",
            level="WARNING",
        ) as observed_logs:
            self.assertEqual(
                callback_failed.recognize_page(_page_input()).observations,
                (),
            )
        self.assertNotIn(
            "telemetry sink unavailable",
            "\n".join(observed_logs.output),
        )

    def test_invalid_base_model_rotation_and_secret_are_rejected(self) -> None:
        invalid_base_urls = (
            "http://dashscope.example/v1",
            "https://user:pass@dashscope.example/v1",
            "https://dashscope.example",
            "https://dashscope.example/v1?token=x",
        )
        for base_url in invalid_base_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    BailianPageLayoutProvider(
                        api_key="test-key",
                        base_url=base_url,
                    )
        for rotations in ({0: 90}, {1: 45}, {True: 90}, []):
            with self.subTest(rotations=rotations):
                with self.assertRaises((TypeError, ValueError)):
                    BailianPageLayoutProvider(
                        api_key="test-key",
                        base_url="https://dashscope.example/v1",
                        input_rotations_by_page=rotations,  # type: ignore[arg-type]
                    )
        with self.assertRaises(ValueError):
            BailianPageLayoutProvider(
                api_key="test-key",
                base_url="https://dashscope.example/v1",
                max_retries=2,
            )

        provider = BailianPageLayoutProvider(
            api_key="密钥",
            base_url="https://dashscope.example/v1",
            http_client=_Client(_Response(200, _completion(_empty_layout()))),
        )
        with self.assertRaises(VisualTextProviderRejected) as error:
            provider.recognize_page(_page_input())
        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.UNAVAILABLE,
        )


def _provider(
    client: "_Client",
    *,
    max_retries: int = 0,
    telemetry_callback=None,
) -> BailianPageLayoutProvider:
    return BailianPageLayoutProvider(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=client,
        max_retries=max_retries,
        sleep=lambda _: None,
        telemetry_callback=telemetry_callback,
    )


def _page_input() -> VisualTextPageInput:
    image = PILImage.new("RGB", (40, 60), color=(255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    image_bytes = output.getvalue()
    source_bytes = b"approved source"
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=3,
        page_bbox=(10.0, 20.0, 210.0, 320.0),
        page_image_bytes=image_bytes,
        page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        page_image_width_px=40,
        page_image_height_px=60,
    )


def _empty_layout() -> dict[str, object]:
    return {
        "schema_version": "page-layout/v1",
        "coordinate_space": "NORMALIZED_1000_TOP_LEFT",
        "evaluated_types": list(VISUAL_CONTENT_TYPES),
        "elements": [],
    }


def _full_layout() -> dict[str, object]:
    layout = _empty_layout()
    layout["elements"] = [
        _element(0, "HEADER", [100, 0, 900, 50], text="页眉"),
        _element(1, "TEXT", [100, 100, 900, 300], text="目录\n一、范围", role="TOC"),
        _element(
            2,
            "TABLE",
            [100, 320, 900, 800],
            table={
                "row_count": 2,
                "column_count": 2,
                "header_row_count": 1,
                "starts_mid_table": False,
                "continues_next_page": True,
                "cells": [
                    _cell(0, 0, "项目"),
                    _cell(0, 1, "要求"),
                    _cell(1, 0, "图示"),
                    _cell(
                        1,
                        1,
                        "A\nB",
                        horizontal_alignment="CENTER",
                        vertical_alignment="MIDDLE",
                        indent_level=1,
                    ),
                ],
            },
        ),
        _element(
            3,
            "FIGURE",
            [500, 500, 800, 750],
            parent_table_index=2,
            parent_cell=[1, 1],
        ),
        _element(4, "FOOTER", [450, 950, 550, 990], text="24"),
        _element(5, "WATERMARK", [250, 350, 750, 650], text="受控"),
    ]
    return layout


def _element(
    index: int,
    content_type: str,
    bbox: list[int],
    *,
    text: str = "",
    role: str | None = None,
    table: dict[str, object] | None = None,
    parent_table_index: int | None = None,
    parent_cell: list[int] | None = None,
) -> dict[str, object]:
    return {
        "index": index,
        "type": content_type,
        "role": role,
        "bbox": bbox,
        "text": text,
        "table": table,
        "parent_table_index": parent_table_index,
        "parent_cell": parent_cell,
    }


def _cell(
    row: int,
    column: int,
    text: str,
    *,
    horizontal_alignment: str = "LEFT",
    vertical_alignment: str = "TOP",
    indent_level: int = 0,
) -> dict[str, object]:
    return {
        "row": row,
        "column": column,
        "row_span": 1,
        "column_span": 1,
        "text": text,
        "horizontal_alignment": horizontal_alignment,
        "vertical_alignment": vertical_alignment,
        "indent_level": indent_level,
    }


def _completion(layout: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        layout,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                },
            }
        ]
    }


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        finish_reason: str = "stop",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._finish_reason = finish_reason
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def json(self) -> object:
        return self._payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_lines(self):
        if self.status_code != 200:
            return
        choices = self._payload["choices"]
        content = choices[0]["message"]["content"]
        midpoint = max(1, len(content) // 2)
        for piece in (content[:midpoint], content[midpoint:]):
            yield "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": piece},
                            "finish_reason": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        yield "data: " + json.dumps(
            {
                "choices": [
                    {
                        "delta": {"content": ""},
                        "finish_reason": self._finish_reason,
                    }
                ]
            }
        )
        yield 'data: {"choices":[],"usage":{"total_tokens":1}}'
        yield "data: [DONE]"


class _Client:
    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stream(self, method: str, endpoint: str, **kwargs: object) -> object:
        if method != "POST":
            raise AssertionError("page-layout provider must use POST")
        self.calls.append((endpoint, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        return None


class _ManualMonotonic:
    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


class _TricklingResponse(_Response):
    def __init__(
        self,
        payload: object,
        *,
        clock: _ManualMonotonic,
        seconds_per_line: float,
    ) -> None:
        super().__init__(200, payload)
        self._clock = clock
        self._seconds_per_line = seconds_per_line

    def iter_lines(self):
        for line in super().iter_lines():
            self._clock.advance(self._seconds_per_line)
            yield line


if __name__ == "__main__":
    unittest.main()
