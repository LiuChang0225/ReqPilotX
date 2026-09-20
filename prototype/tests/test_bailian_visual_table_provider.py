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

from app.adapters.documents.bailian_visual_table_provider import (
    BailianVisualTableProvider,
    parse_single_html_table,
)
from app.domain.extraction.visual_table import VisualTableCropInput
from app.ports.visual_table_provider import (
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)


class BailianVisualTableProviderTests(unittest.TestCase):
    def test_table_parsing_returns_strict_cells_and_sends_no_rotation(self) -> None:
        client = _Client(
            _Response(
                200,
                _payload(
                    """
                    ```html
                    <table>
                      <tr><th rowspan="2">A</th><th>B</th></tr>
                      <tr><td>C<br>D</td></tr>
                    </table>
                    ```
                    """
                ),
            )
        )
        provider = _provider(client)

        snapshot = provider.parse_table(_crop_input())

        self.assertEqual((snapshot.row_count, snapshot.column_count), (2, 2))
        self.assertEqual(
            [
                (cell.row, cell.column, cell.row_span, cell.column_span, cell.raw_text)
                for cell in snapshot.cells
            ],
            [
                (0, 0, 2, 1, "A"),
                (0, 1, 1, 1, "B"),
                (1, 1, 1, 1, "C\nD"),
            ],
        )
        self.assertIn("task=table_parsing", snapshot.provider_version)
        self.assertIn("parser=strict-grid/1", snapshot.provider_version)
        self.assertEqual(snapshot.render_scale, 2.0)
        self.assertFalse(snapshot.annotations_included)
        request = client.calls[0][2]["json"]
        self.assertEqual(
            request["parameters"]["ocr_options"]["task"],
            "table_parsing",
        )
        self.assertIs(
            request["input"]["messages"][0]["content"][0]["enable_rotate"],
            False,
        )

    def test_multiple_tables_are_rejected_with_safe_code(self) -> None:
        provider = _provider(
            _Client(
                _Response(
                    200,
                    _payload(
                        "<table><tr><td>A</td></tr></table><table><tr><td>B</td></tr></table>"
                    ),
                )
            )
        )

        with self.assertRaises(VisualTableProviderRejected) as caught:
            provider.parse_table(_crop_input())

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.MULTIPLE_TABLES,
        )

    def test_incomplete_rectangular_topology_is_rejected(self) -> None:
        with self.assertRaises(VisualTableProviderRejected) as caught:
            parse_single_html_table(
                "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>"
            )

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
        )

    def test_huge_rowspan_colspan_is_rejected_before_grid_materialization(self) -> None:
        html = (
            '<table><tr><td rowspan="4000" colspan="4000">X</td></tr>'
            + "<tr><td>Y</td></tr>" * 3999
            + "</table>"
        )

        with self.assertRaises(VisualTableProviderRejected) as caught:
            parse_single_html_table(html)

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
        )

    def test_malformed_response_carrier_is_rejected(self) -> None:
        provider = _provider(_Client(_Response(200, {"output": {"choices": []}})))

        with self.assertRaises(VisualTableProviderRejected) as caught:
            provider.parse_table(_crop_input())

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )

    def test_multiple_choices_are_rejected_even_when_each_contains_a_table(
        self,
    ) -> None:
        first = _payload("<table><tr><td>A</td></tr></table>")
        choice = first["output"]["choices"][0]
        first["output"]["choices"] = [choice, choice]
        provider = _provider(_Client(_Response(200, first)))

        with self.assertRaises(VisualTableProviderRejected) as caught:
            provider.parse_table(_crop_input())

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )

    def test_truncated_finish_reasons_fail_closed_with_a_closed_table(self) -> None:
        for finish_reason in ("length", "max_tokens"):
            with self.subTest(finish_reason=finish_reason):
                provider = _provider(
                    _Client(
                        _Response(
                            200,
                            _payload(
                                "<table><tr><td>A</td></tr></table>",
                                finish_reason=finish_reason,
                            ),
                        )
                    )
                )

                with self.assertRaises(VisualTableProviderRejected) as caught:
                    provider.parse_table(_crop_input())

                self.assertEqual(
                    caught.exception.code,
                    VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )

    def test_oversized_http_body_is_rejected_before_json_decode(self) -> None:
        response = _Response(
            200,
            _payload("<table><tr><td>A</td></tr></table>"),
            content=b"{" + b" " * (16 * 1024 * 1024),
        )
        provider = _provider(_Client(response))

        with self.assertRaises(VisualTableProviderRejected) as caught:
            provider.parse_table(_crop_input())

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertEqual(response.json_call_count, 0)

    def test_streaming_http_body_stops_at_byte_budget(self) -> None:
        response = _StreamingResponse(
            status_code=200,
            chunks=(b"x" * (9 * 1024 * 1024), b"y" * (9 * 1024 * 1024)),
        )
        provider = _provider(_StreamingClient(response))

        with self.assertRaises(VisualTableProviderRejected) as caught:
            provider.parse_table(_crop_input())

        self.assertEqual(
            caught.exception.code,
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertEqual(response.yielded_chunk_count, 2)

    def test_base_url_must_use_approved_dashscope_origin(self) -> None:
        invalid_urls = (
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://example.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1",
            "https://user@dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:444/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:not-a-port/compatible-mode/v1",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                BailianVisualTableProvider(
                    api_key="test-key",
                    base_url=base_url,
                    http_client=_Client(
                        _Response(
                            200,
                            _payload("<table><tr><td>A</td></tr></table>"),
                        )
                    ),
                )


class _StreamingResponse:
    def __init__(self, *, status_code: int, chunks: tuple[bytes, ...]) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._chunks = chunks
        self.yielded_chunk_count = 0

    def __enter__(self) -> _StreamingResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_bytes(self):
        for chunk in self._chunks:
            self.yielded_chunk_count += 1
            yield chunk


class _StreamingClient:
    def __init__(self, response: _StreamingResponse) -> None:
        self.response = response

    def stream(self, *args: object, **kwargs: object) -> _StreamingResponse:
        return self.response


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        content: bytes | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if content is None
            else content
        )
        self.headers: dict[str, str] = {}
        self.json_call_count = 0

    def json(self) -> object:
        self.json_call_count += 1
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, object, dict[str, object]]] = []

    def post(self, url: str, *args: object, **kwargs: object) -> _Response:
        self.calls.append((url, args, kwargs))
        return self.response


def _provider(client: _Client) -> BailianVisualTableProvider:
    return BailianVisualTableProvider(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=client,
        sleep=lambda _: None,
    )


def _crop_input() -> VisualTableCropInput:
    source_bytes = b"approved PDF bytes"
    crop_bytes = b"bounded annotation-free PNG bytes"
    return VisualTableCropInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=7,
        page_bbox=(0.0, 0.0, 600.0, 800.0),
        table_source_node_id="node-" + "1" * 32,
        table_id="table-" + "2" * 32,
        table_index=0,
        table_bbox=(50.0, 100.0, 550.0, 300.0),
        crop_image_bytes=crop_bytes,
        crop_image_sha256=hashlib.sha256(crop_bytes).hexdigest(),
        crop_image_width_px=1000,
        crop_image_height_px=400,
    )


def _payload(
    html: str,
    *,
    finish_reason: str = "stop",
) -> dict[str, object]:
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "content": [
                            {
                                "ocr_result": {},
                                "text": html,
                            }
                        ]
                    },
                }
            ]
        },
        "request_id": "safe-request-id",
    }


if __name__ == "__main__":
    unittest.main()
