from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
import json

from app.adapters.models.reading_json_chat import (
    DEFAULT_READING_MAX_RETRIES,
    DEFAULT_READING_TIMEOUT_SECONDS,
    ReadingAIConfig,
    OpenAIReadingJsonChatTransport,
    _enforce_payload_safe_openai_logging,
    _no_redirect_http_client,
    load_reading_ai_config,
    normalize_azure_endpoint,
    normalize_dashscope_base_url,
)
from app.adapters.models.reading_provider import (
    ConfiguredReadingProvider,
    build_reading_provider,
)
from app.ports.reading_provider import (
    FULL_EFFECTIVE_TEXT_DISCLOSURE,
    MAX_READING_CHAT_CONTEXT_CHUNKS,
    ReadingChatContextChunk,
    ReadingChatHistoryMessage,
    ReadingProviderFailure,
    ReadingProviderUnavailable,
)


class _FakeTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.messages: list[dict[str, str]] = []

    def complete(self, *, config, messages, response_model):
        self.messages = list(messages)
        self.config = config
        self.response_model = response_model
        return dict(self.payload)


class ReadingProviderTests(unittest.TestCase):
    def test_aliyun_action_uses_only_source_slice_and_no_retrieval(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        transport = _FakeTransport({"content": "该段说明了泵组的连续运行工况。"})
        result = ConfiguredReadingProvider(config, transport=transport).generate(
            action="EXPLANATION",
            source_text="泵组在额定工况下连续运行。",
            target_language=None,
        )

        self.assertEqual(result.output_text, "该段说明了泵组的连续运行工况。")
        self.assertEqual(result.model_name, "qwen-reading")
        serialized = repr(transport.messages)
        self.assertIn("泵组在额定工况下连续运行", serialized)
        self.assertNotIn("knowledge_docs", serialized)
        self.assertNotIn("requirement_set_id", serialized)

    def test_unconfigured_provider_fails_instead_of_using_heuristics(self) -> None:
        config = ReadingAIConfig(provider="none")
        with self.assertRaises(ReadingProviderUnavailable):
            ConfiguredReadingProvider(config).generate(
                action="TRANSLATION",
                source_text="Rated operating point",
                target_language="zh-CN",
            )

    def test_provider_output_rejects_extra_fields(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        transport = _FakeTransport({"content": "ok", "requirement_id": "REQ-1"})
        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(config, transport=transport).generate(
                action="EXPLANATION",
                source_text="Bound source",
                target_language=None,
            )

    def test_reading_provider_auto_selects_shared_aliyun_credentials(self) -> None:
        config = load_reading_ai_config(
            environment={
                "REQPILOT_AI_DEFAULT_PROFILE": "aliyun",
                "REQPILOT_DASHSCOPE_API_KEY": "shared-key",
            },
            config_values={},
        )
        self.assertEqual(config.provider, "aliyun")
        self.assertTrue(config.ready)
        self.assertEqual(config.configuration_source, "SHARED_RUNTIME")
        self.assertEqual(config.timeout_seconds, DEFAULT_READING_TIMEOUT_SECONDS)
        self.assertEqual(config.max_retries, DEFAULT_READING_MAX_RETRIES)

    def test_reading_timeout_and_retry_are_explicitly_overridable(self) -> None:
        config = load_reading_ai_config(
            environment={
                "REQPILOT_DASHSCOPE_API_KEY": "shared-key",
                "REQPILOT_READING_AI_TIMEOUT_SECONDS": "90",
                "REQPILOT_READING_AI_MAX_RETRIES": "2",
            },
            config_values={},
        )
        self.assertEqual(config.timeout_seconds, 90.0)
        self.assertEqual(config.max_retries, 2)

    def test_reading_config_can_share_credentials_after_own_selection(self) -> None:
        config = load_reading_ai_config(
            environment={
                "REQPILOT_READING_AI_PROVIDER": "aliyun",
                "REQPILOT_DASHSCOPE_API_KEY": "shared-key",
                "REQPILOT_READING_ALIYUN_MODEL": "qwen-reading",
            },
            config_values={},
        )
        self.assertTrue(config.ready)
        self.assertEqual(config.provider, "aliyun")
        self.assertEqual(config.model_name, "qwen-reading")

    def test_shared_chat_model_is_inherited_but_azure_deployment_is_not(self) -> None:
        aliyun = load_reading_ai_config(
            environment={
                "REQPILOT_READING_AI_PROVIDER": "aliyun",
                "REQPILOT_DASHSCOPE_API_KEY": "shared-key",
                "REQPILOT_ALIYUN_CHAT_MODEL": "formal-model",
            },
            config_values={},
        )
        azure = load_reading_ai_config(
            environment={
                "REQPILOT_READING_AI_PROVIDER": "azure",
                "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
                "AZURE_OPENAI_API_KEY": "shared-key",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "formal-deployment",
            },
            config_values={},
        )
        self.assertEqual(aliyun.model_name, "formal-model")
        self.assertEqual(azure.model_name, "")
        self.assertFalse(azure.ready)

    def test_openai_sdk_debug_logging_is_forced_payload_safe(self) -> None:
        import logging

        loggers = [
            logging.getLogger("openai"),
            logging.getLogger("openai._base_client"),
        ]
        previous = [logger.level for logger in loggers]
        try:
            for logger in loggers:
                logger.setLevel(logging.DEBUG)
            _enforce_payload_safe_openai_logging()
            self.assertTrue(all(logger.level >= logging.INFO for logger in loggers))
        finally:
            for logger, level in zip(loggers, previous, strict=True):
                logger.setLevel(level)

    def test_reading_http_client_never_follows_redirects(self) -> None:
        client = _no_redirect_http_client(timeout_seconds=1.0)
        try:
            self.assertFalse(client.follow_redirects)
        finally:
            client.close()

    def test_reading_http_client_does_not_replay_post_across_redirect(self) -> None:
        import httpx

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                307,
                headers={"location": "https://attacker.example/collect"},
            )

        client = _no_redirect_http_client(
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )
        try:
            response = client.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                json={"messages": [{"content": "secret-source-slice"}]},
            )
            self.assertEqual(response.status_code, 307)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.host, "dashscope.aliyuncs.com")
        finally:
            client.close()

    def test_aliyun_sdk_payload_disables_thinking_for_document_context(self) -> None:
        import httpx
        from unittest import mock

        payloads: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "chatcmpl-reading-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "qwen-reading",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"content":"原文未提供。"}',
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        def mock_client(*, timeout_seconds: float, transport=None):
            self.assertEqual(timeout_seconds, DEFAULT_READING_TIMEOUT_SECONDS)
            return httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=False,
                transport=httpx.MockTransport(handler),
            )

        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        with mock.patch(
            "app.adapters.models.reading_json_chat._no_redirect_http_client",
            side_effect=mock_client,
        ):
            result = OpenAIReadingJsonChatTransport().complete(
                config=config,
                messages=[{"role": "user", "content": "document context"}],
                response_model=object,
            )

        self.assertEqual(result, {"content": "原文未提供。"})
        self.assertEqual(len(payloads), 1)
        self.assertIs(payloads[0]["enable_thinking"], False)

    def test_engineering_impact_requires_reading_only_label(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        provider = ConfiguredReadingProvider(
            config,
            transport=_FakeTransport({"content": "必须立即修改正式设计。"}),
        )
        with self.assertRaises(ReadingProviderFailure):
            provider.generate(
                action="ENGINEERING_IMPACT",
                source_text="Bound source",
                target_language=None,
            )

    def test_free_form_chat_is_multi_turn_and_source_cited(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        transport = _FakeTransport(
            {"content": "文档说明泵组连续运行【S1】。", "citation_ids": ["S1"]}
        )
        provider = ConfiguredReadingProvider(config, transport=transport)
        result = provider.generate_chat(
            question="这意味着什么？",
            history=(
                ReadingChatHistoryMessage(role="user", content="泵组如何运行？"),
                ReadingChatHistoryMessage(role="assistant", content="上一轮已有回答。"),
            ),
            context_chunks=(
                ReadingChatContextChunk(
                    citation_id="S1",
                    source_node_id="node-a",
                    page_number=2,
                    start_offset=0,
                    end_offset=9,
                    text="泵组连续运行条件。",
                ),
            ),
            coverage_mode="FULL_DOCUMENT",
        )
        self.assertEqual(result.citation_ids, ("S1",))
        self.assertEqual(result.prompt_version, "reading-chat/v4-effective-text")
        self.assertEqual(transport.messages[-2]["role"], "assistant")
        system_prompt = transport.messages[0]["content"]
        self.assertIn("one or two most important IDs", system_prompt)
        self.assertIn("Numeric limits, exceptions, negations", system_prompt)
        self.assertIn("Do not attach a citation to every sentence", system_prompt)
        self.assertNotIn("For each claim", system_prompt)
        serialized = repr(transport.messages)
        self.assertNotIn("requirement_set", serialized.lower())
        self.assertNotIn("knowledge_docs", serialized.lower())

    def test_selected_context_requires_explicit_unreadable_page_receipt(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        chunk = ReadingChatContextChunk(
            citation_id="S1",
            source_node_id="node-a",
            page_number=2,
            start_offset=0,
            end_offset=4,
            text="所选原文",
        )
        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(
                config,
                transport=_FakeTransport(
                    {"content": "仅回答所选事实【S1】。", "citation_ids": ["S1"]}
                ),
            ).generate_chat(
                question="解释所选内容",
                history=(),
                context_chunks=(chunk,),
                coverage_mode="SELECTED_CONTEXT",
            )
        result = ConfiguredReadingProvider(
            config,
            transport=_FakeTransport(
                {
                    "content": "仅依据所选内容，文档仍有未识别页。所选事实【S1】。",
                    "citation_ids": ["S1"],
                }
            ),
        ).generate_chat(
            question="解释所选内容",
            history=(),
            context_chunks=(chunk,),
            coverage_mode="SELECTED_CONTEXT",
        )
        self.assertEqual(result.citation_ids, ("S1",))

    def test_full_effective_text_requires_visual_semantics_disclosure(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        chunk = ReadingChatContextChunk(
            citation_id="S1",
            source_node_id=None,
            page_number=1,
            start_offset=0,
            end_offset=4,
            text="视觉原文",
            locator_kind="VISUAL_TEXT",
            visual_snapshot_id="visual-1",
            observation_id="observation-1",
            bbox=(0.0, 0.0, 20.0, 20.0),
            page_bbox=(0.0, 0.0, 100.0, 100.0),
            page_image_sha256="a" * 64,
            visual_provider_id="layout-provider",
            visual_provider_version="layout-provider/1",
        )
        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(
                config,
                transport=_FakeTransport(
                    {"content": "我已完整理解页面【S1】。", "citation_ids": ["S1"]}
                ),
            ).generate_chat(
                question="总结",
                history=(),
                context_chunks=(chunk,),
                coverage_mode="FULL_EFFECTIVE_TEXT",
            )
        transport = _FakeTransport(
            {
                "content": f"{FULL_EFFECTIVE_TEXT_DISCLOSURE}。视觉原文【S1】。",
                "citation_ids": ["S1"],
            }
        )
        result = ConfiguredReadingProvider(config, transport=transport).generate_chat(
            question="总结",
            history=(),
            context_chunks=(chunk,),
            coverage_mode="FULL_EFFECTIVE_TEXT",
        )
        self.assertEqual(result.citation_ids, ("S1",))
        self.assertIn(FULL_EFFECTIVE_TEXT_DISCLOSURE, result.output_text)
        self.assertIn("FULL_EFFECTIVE_TEXT", transport.messages[0]["content"])
        self.assertIn(
            "Neither FULL_DOCUMENT nor FULL_EFFECTIVE_TEXT is a sampled mode",
            transport.messages[0]["content"],
        )
        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(
                config,
                transport=_FakeTransport(
                    {
                        "content": (
                            f"基于覆盖采样，{FULL_EFFECTIVE_TEXT_DISCLOSURE}【S1】。"
                        ),
                        "citation_ids": ["S1"],
                    }
                ),
            ).generate_chat(
                question="总结",
                history=(),
                context_chunks=(chunk,),
                coverage_mode="FULL_EFFECTIVE_TEXT",
            )

    def test_chat_accepts_more_than_twenty_four_grounded_citations(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        citation_ids = tuple(f"S{index}" for index in range(1, 32))
        chunks = tuple(
            ReadingChatContextChunk(
                citation_id=citation_id,
                source_node_id=f"node-{index}",
                page_number=index,
                start_offset=0,
                end_offset=1,
                text="文",
            )
            for index, citation_id in enumerate(citation_ids, start=1)
        )
        content = "全文直读结果：" + "".join(
            f"事实{index}【{citation_id}】。"
            for index, citation_id in enumerate(citation_ids, start=1)
        )
        result = ConfiguredReadingProvider(
            config,
            transport=_FakeTransport(
                {"content": content, "citation_ids": list(citation_ids)}
            ),
        ).generate_chat(
            question="总结整份文档",
            history=(),
            context_chunks=chunks,
            coverage_mode="FULL_DOCUMENT",
        )
        self.assertEqual(result.citation_ids, citation_ids)
        self.assertLess(len(citation_ids), MAX_READING_CHAT_CONTEXT_CHUNKS)

    def test_table_cell_context_preserves_row_column_metadata(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        transport = _FakeTransport(
            {"content": "表格给出一个限值【S1】。", "citation_ids": ["S1"]}
        )
        ConfiguredReadingProvider(config, transport=transport).generate_chat(
            question="表格限值是什么？",
            history=(),
            context_chunks=(
                ReadingChatContextChunk(
                    citation_id="S1",
                    source_node_id="table-node",
                    page_number=8,
                    start_offset=0,
                    end_offset=4,
                    text="14 V",
                    locator_kind="TABLE_CELL",
                    cell_id="cell-3-2",
                    table_row=3,
                    table_column=2,
                    table_row_span=1,
                    table_column_span=2,
                ),
            ),
            coverage_mode="FULL_DOCUMENT",
        )
        payload = transport.messages[-1]["content"]
        self.assertIn('"locator_kind":"TABLE_CELL"', payload)
        self.assertIn('"cell_id":"cell-3-2"', payload)
        self.assertIn('"table_row":3', payload)
        self.assertIn('"table_column":2', payload)
        self.assertIn('"table_column_span":2', payload)
        self.assertIn('"source_text":"14 V"', payload)

    def test_chat_canonicalizes_allowed_metadata_drift_and_rejects_ungrounded_ids(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        chunk = ReadingChatContextChunk(
            citation_id="S1",
            source_node_id="node-a",
            page_number=1,
            start_offset=0,
            end_offset=4,
            text="原文事实",
        )
        second_chunk = ReadingChatContextChunk(
            citation_id="S2",
            source_node_id="node-b",
            page_number=2,
            start_offset=0,
            end_offset=4,
            text="另一事实",
        )
        for payload, expected in (
            ({"content": "事实【S1】", "citation_ids": []}, ("S1",)),
            (
                {
                    "content": "先引用【S2】，再引用【S1】，重复【S2】。",
                    "citation_ids": ["S1", "S1", "S2"],
                },
                ("S2", "S1"),
            ),
            (
                {"content": "仅引用【S1】。", "citation_ids": ["S1", "S2"]},
                ("S1",),
            ),
        ):
            with self.subTest(payload=payload):
                result = ConfiguredReadingProvider(
                    config,
                    transport=_FakeTransport(payload),
                ).generate_chat(
                    question="问题",
                    history=(),
                    context_chunks=(chunk, second_chunk),
                    coverage_mode="FULL_DOCUMENT",
                )
                self.assertEqual(result.citation_ids, expected)

        for payload in (
            {"content": "未知正文引用【S3】", "citation_ids": ["S1"]},
            {"content": "旧格式正文引用【R1】", "citation_ids": ["S1"]},
            {"content": "越界正文引用【S1000000】", "citation_ids": ["S1"]},
            {"content": "有效正文引用【S1】", "citation_ids": ["S1", "S3"]},
            {"content": "这是一个事实", "citation_ids": ["S1"]},
        ):
            with self.subTest(ungrounded_payload=payload):
                with self.assertRaises(ReadingProviderFailure):
                    ConfiguredReadingProvider(
                        config,
                        transport=_FakeTransport(payload),
                    ).generate_chat(
                        question="问题",
                        history=(),
                        context_chunks=(chunk, second_chunk),
                        coverage_mode="FULL_DOCUMENT",
                    )

        allowed = ConfiguredReadingProvider(
            config,
            transport=_FakeTransport(
                {"content": "原文未提供。", "citation_ids": ["S1"]}
            ),
        ).generate_chat(
            question="原文没有的问题",
            history=(),
            context_chunks=(chunk,),
            coverage_mode="FULL_DOCUMENT",
        )
        self.assertEqual(allowed.citation_ids, ())

        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(
                config,
                transport=_FakeTransport(
                    {"content": "没有来源的确定性说法", "citation_ids": []}
                ),
            ).generate_chat(
                question="空上下文问题",
                history=(),
                context_chunks=(),
                coverage_mode="FULL_DOCUMENT",
            )

        full_answer = ConfiguredReadingProvider(
            config,
            transport=_FakeTransport(
                {
                    "content": "文档概览【S1】。",
                    "citation_ids": ["S1"],
                }
            ),
        ).generate_chat(
            question="总结整份文档",
            history=(),
            context_chunks=(chunk,),
            coverage_mode="FULL_DOCUMENT",
        )
        self.assertEqual(full_answer.citation_ids, ("S1",))

        with self.assertRaises(ReadingProviderFailure):
            ConfiguredReadingProvider(
                config,
                transport=_FakeTransport(
                    {
                        "content": "基于覆盖采样，文档说明了一项事实【S1】。",
                        "citation_ids": ["S1"],
                    }
                ),
            ).generate_chat(
                question="总结整份文档",
                history=(),
                context_chunks=(chunk,),
                coverage_mode="FULL_DOCUMENT",
            )

    def test_provider_refuses_legacy_sampled_coverage_modes(self) -> None:
        config = ReadingAIConfig(
            provider="aliyun",
            endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model_name="qwen-reading",
        )
        transport = _FakeTransport(
            {"content": "原文未提供。", "citation_ids": []}
        )
        with self.assertRaisesRegex(ValueError, "coverage mode"):
            ConfiguredReadingProvider(config, transport=transport).generate_chat(
                question="总结全文",
                history=(),
                context_chunks=(),
                coverage_mode="TARGETED",
            )
        self.assertEqual(transport.messages, [])

    def test_model_name_contract_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            ReadingAIConfig(
                provider="aliyun",
                endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key="test-key",
                model_name="bad model/name",
            )

    def test_remote_endpoints_are_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            normalize_dashscope_base_url("http://127.0.0.1/compatible-mode/v1")
        with self.assertRaises(ValueError):
            normalize_azure_endpoint("https://example.com")

    def test_unavailable_codex_runtime_does_not_fall_back_to_legacy_provider(
        self,
    ) -> None:
        from unittest import mock

        with (
            mock.patch(
                "app.adapters.models.reading_provider.probe_codex_reading_runtime",
                return_value=(False, "reading_codex_auth_required"),
            ),
            self.assertRaises(ReadingProviderUnavailable) as unavailable,
        ):
            build_reading_provider()
        self.assertEqual(
            unavailable.exception.reason_code,
            "reading_codex_auth_required",
        )


if __name__ == "__main__":
    unittest.main()
