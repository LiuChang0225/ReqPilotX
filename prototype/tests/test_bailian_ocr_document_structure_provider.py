from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import unittest

import httpx
from pydantic import ValidationError

from app.adapters.documents.bailian_ocr_document_structure_provider import (
    BAILIAN_OCR_DOCUMENT_STRUCTURE_PROVIDER_ID,
    BailianOcrDocumentStructureProvider,
    parse_qwen_document_latex,
)
from app.domain.ocr_document_structure import OcrDocumentStructureSnapshot
from app.domain.source_visual_text import VisualTextPageInput
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


class BailianOcrDocumentStructureProviderTests(unittest.TestCase):
    def test_document_task_maps_latex_to_geometry_free_source_bound_facts(self) -> None:
        client = _Client(_Response(200, _response(_document_latex())))
        provider = _provider(client)
        page_input = _page_input()

        snapshot = provider.parse_page(page_input)

        self.assertEqual(
            snapshot.provider_id,
            BAILIAN_OCR_DOCUMENT_STRUCTURE_PROVIDER_ID,
        )
        self.assertEqual(snapshot.source_sha256, page_input.source_sha256)
        self.assertEqual(snapshot.page_image_sha256, page_input.page_image_sha256)
        self.assertEqual(snapshot.page_number, 2)
        self.assertEqual(snapshot.geometry_status, "NOT_PROVIDED_BY_TASK")
        self.assertEqual(
            [block.content_type for block in snapshot.blocks],
            ["HEADING", "HEADING", "TEXT", "TABLE", "FORMULA"],
        )
        self.assertEqual(snapshot.blocks[0].text, "System {Specification}")
        self.assertEqual(snapshot.blocks[1].heading_level, 1)
        self.assertTrue(
            all(
                block.geometry_status == "NOT_PROVIDED_BY_TASK"
                for block in snapshot.blocks
            )
        )
        self.assertFalse(
            any("bbox" in block.model_dump(mode="json") for block in snapshot.blocks)
        )
        coverage = {item.content_type: item for item in snapshot.coverage}
        self.assertEqual(coverage["TABLE"].status, "CAPTURED")
        self.assertEqual(coverage["FORMULA"].status, "CAPTURED")
        self.assertEqual(coverage["HEADER"].status, "NOT_EVALUATED")
        self.assertEqual(coverage["HEADER"].next_action, "GROUNDED_LAYOUT")
        self.assertEqual(coverage["FIGURE"].status, "NOT_EVALUATED")
        self.assertEqual(coverage["FIGURE"].next_action, "COMPLEX_VISION")
        self.assertEqual(snapshot.diagnostics, ())

        self.assertEqual(len(client.calls), 1)
        endpoint, kwargs = client.calls[0]
        self.assertTrue(
            endpoint.endswith("/api/v1/services/aigc/multimodal-generation/generation")
        )
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "qwen3.5-ocr")
        self.assertEqual(
            payload["parameters"]["ocr_options"],
            {"task": "document_parsing"},
        )
        image = payload["input"]["messages"][0]["content"][0]["image"]
        self.assertTrue(image.startswith("data:image/png;base64,"))

    def test_parse_is_deterministic_and_domain_rejects_identity_tampering(self) -> None:
        page_input = _page_input()
        first = parse_qwen_document_latex(
            _document_latex(),
            page_input=page_input,
            provider_id="provider",
            provider_version="version",
        )
        second = parse_qwen_document_latex(
            _document_latex(),
            page_input=page_input,
            provider_id="provider",
            provider_version="version",
        )
        self.assertEqual(first, second)

        tampered = first.model_dump(mode="python")
        tampered["blocks"][0]["latex"] = "different"
        with self.assertRaises(ValidationError):
            OcrDocumentStructureSnapshot.model_validate(tampered)

    def test_figure_and_image_placeholders_never_become_formal_blocks(self) -> None:
        secret_placeholder = "SECRET-IMAGE-CAPTION-OR-PATH"
        latex = rf"""```latex
\begin{{document}}
Visible body text.

\begin{{figure}}
\includegraphics[width=1cm]{{{secret_placeholder}.png}}
\caption{{{secret_placeholder}}}
\end{{figure}}

![{secret_placeholder}](https://example.invalid/{secret_placeholder}.png)

\includegraphics{{{secret_placeholder}-standalone.png}}

Visible ending.
\end{{document}}
```"""

        snapshot = parse_qwen_document_latex(
            latex,
            page_input=_page_input(),
            provider_id="provider",
            provider_version="version",
        )

        self.assertEqual(
            snapshot.diagnostics,
            ("IMAGE_PLACEHOLDER_IGNORED_REQUIRES_COMPLEX_VISION",),
        )
        self.assertNotIn(
            secret_placeholder,
            "\n".join(block.latex + block.text for block in snapshot.blocks),
        )
        self.assertFalse(
            any(block.content_type in {"FIGURE", "IMAGE"} for block in snapshot.blocks)
        )
        coverage = {item.content_type: item for item in snapshot.coverage}
        for content_type in ("FIGURE", "IMAGE"):
            self.assertEqual(coverage[content_type].status, "NOT_EVALUATED")
            self.assertEqual(coverage[content_type].block_count, 0)
            self.assertEqual(coverage[content_type].next_action, "COMPLEX_VISION")

    def test_incomplete_or_ambiguous_provider_output_fails_closed(self) -> None:
        cases = (
            _response(_document_latex(), finish_reason="length"),
            _response(_document_latex(), duplicate=True),
            _response("```latex\n\\begin{document}\ntext\n```"),
            _response("```latex\n\\begin{tabular}{l}\ntext\n```"),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                provider = _provider(_Client(_Response(200, payload)))
                with self.assertRaises(VisualTextProviderRejected) as error:
                    provider.parse_page(_page_input())
                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )

    def test_source_hash_mismatch_is_rejected_before_http(self) -> None:
        page_input = _page_input()
        invalid = page_input.model_copy(update={"source_sha256": "0" * 64})
        client = _Client(_Response(200, _response(_document_latex())))

        with self.assertRaises(VisualTextProviderRejected) as error:
            _provider(client).parse_page(invalid)

        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.SOURCE_HASH_MISMATCH,
        )
        self.assertEqual(client.calls, [])

    def test_transport_and_http_failures_are_machine_readable(self) -> None:
        cases = (
            (401, VisualTextProviderRejectionCode.AUTHENTICATION_FAILED),
            (403, VisualTextProviderRejectionCode.ACCESS_DENIED),
            (413, VisualTextProviderRejectionCode.INVALID_INPUT),
            (429, VisualTextProviderRejectionCode.RATE_LIMITED),
            (500, VisualTextProviderRejectionCode.PROVIDER_FAILED),
        )
        for status_code, expected in cases:
            with self.subTest(status_code=status_code):
                provider = _provider(_Client(_Response(status_code, {"secret": "x"})))
                with self.assertRaises(VisualTextProviderRejected) as error:
                    provider.parse_page(_page_input())
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn("secret", str(error.exception))

        provider = _provider(_Client(httpx.ConnectError("offline")))
        with self.assertRaises(VisualTextProviderRejected) as error:
            provider.parse_page(_page_input())
        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )

    def test_provider_configuration_is_bounded(self) -> None:
        for kwargs in (
            {"model": "qwen3.7-flash"},
            {"seed": -1},
            {"temperature": float("nan")},
            {"timeout_seconds": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BailianOcrDocumentStructureProvider(
                        api_key="sk-test-0000000000000000000000000000",
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        execution_identity="plan=abc;connection_revision=7",
                        **kwargs,
                    )
        with self.assertRaises(ValueError):
            BailianOcrDocumentStructureProvider(
                api_key="sk-test-0000000000000000000000000000",
                base_url="https://example.com/compatible-mode/v1",
                execution_identity="plan=abc;connection_revision=7",
            )

        first = BailianOcrDocumentStructureProvider(
            api_key="sk-test-0000000000000000000000000000",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            execution_identity="plan=abc;connection_revision=7",
        )
        second = BailianOcrDocumentStructureProvider(
            api_key="sk-test-0000000000000000000000000000",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            execution_identity="plan=def;connection_revision=8",
        )
        self.assertNotEqual(first.provider_version, second.provider_version)
        self.assertNotIn("connection_revision", first.provider_version)
        self.assertEqual(len(first.execution_identity_sha256), 64)
        with self.assertRaises(ValueError):
            BailianOcrDocumentStructureProvider(
                api_key="sk-test-0000000000000000000000000000",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                execution_identity="",
            )


def _provider(client: "_Client") -> BailianOcrDocumentStructureProvider:
    return BailianOcrDocumentStructureProvider(
        api_key="sk-test-0000000000000000000000000000",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        execution_identity="plan=abc;connection_revision=7",
        http_client=client,
    )


def _page_input() -> VisualTextPageInput:
    source = b"%PDF-1.7\nsource-bound-test"
    image = b"\x89PNG\r\nsource-bound-page-image"
    return VisualTextPageInput(
        source_bytes=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        page_number=2,
        page_bbox=(0.0, 0.0, 300.0, 400.0),
        page_image_bytes=image,
        page_image_sha256=hashlib.sha256(image).hexdigest(),
        page_image_width_px=600,
        page_image_height_px=800,
    )


def _document_latex() -> str:
    return r"""```latex
\documentclass{article}
\title{System {Specification}}
\begin{document}
\maketitle
\section{Scope}

Requirement text with inline $x$.

\begin{tabular}{ll}
Item & Value \\
A & 1
\end{tabular}

\[E=mc^2\]
\end{document}
```"""


def _response(
    latex: str,
    *,
    finish_reason: str = "stop",
    duplicate: bool = False,
) -> dict[str, object]:
    content: list[dict[str, object]] = [{"text": latex}]
    if duplicate:
        content.append({"ocr_result": {"text": copy.deepcopy(latex)}})
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }
    }


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return copy.deepcopy(self._payload)


class _Client:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, endpoint: str, **kwargs: object) -> object:
        self.calls.append((endpoint, kwargs))
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


if __name__ == "__main__":
    unittest.main()
