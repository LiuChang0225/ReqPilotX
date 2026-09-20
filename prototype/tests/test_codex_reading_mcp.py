from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.reading_mcp import (
    ReadingMcpError,
    ReadingToolSession,
    TOOL_GET_CHUNKS,
    TOOL_GET_TASK,
    TOOL_VALIDATE,
    load_reading_descriptor,
    tool_definitions,
    write_reading_descriptor,
)
from app.adapters.models.reading_provider import _messages_for_chat
from app.ports.reading_provider import ReadingChatContextChunk


class CodexReadingMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="reading-mcp-")
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        chunks = tuple(
            ReadingChatContextChunk(
                citation_id=f"S{index}",
                source_node_id=f"node-{index}",
                page_number=index,
                start_offset=0,
                end_offset=len(text := f"第 {index} 条合成事实。"),
                text=text,
            )
            for index in range(1, 26)
        )
        self.messages = _messages_for_chat(
            question="概括合成文档",
            history=(),
            context_chunks=chunks,
            coverage_mode="FULL_DOCUMENT",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_every_chunk_then_validates_grounded_answer(self) -> None:
        path = self.root / "session.json"
        descriptor = write_reading_descriptor(
            destination=path,
            messages=self.messages,
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_reading_descriptor(path), descriptor)
        session = ReadingToolSession(descriptor)

        task = session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
        self.assertEqual(task["context_chunk_count"], 2)
        for index in range(session.chunk_count):
            session.call(
                TOOL_GET_CHUNKS,
                {"document_handle": descriptor.document_handle, "chunk_index": index},
            )
        result = session.call(
            TOOL_VALIDATE,
            {
                "document_handle": descriptor.document_handle,
                "answer": {
                    "content": "合成文档包含一项事实【S1】。",
                    "citation_ids": ["S1", "S2"],
                },
            },
        )

        self.assertEqual(result["validated_answer"]["citation_ids"], ["S1"])
        self.assertEqual(result["write_audit"]["total_writes"], 0)
        self.assertEqual(
            session.completed_tools,
            [TOOL_GET_TASK, TOOL_GET_CHUNKS, TOOL_GET_CHUNKS, TOOL_VALIDATE],
        )

    def test_rejects_incomplete_coverage_and_unknown_citation(self) -> None:
        descriptor = write_reading_descriptor(
            destination=self.root / "session.json",
            messages=self.messages,
        )
        session = ReadingToolSession(descriptor)
        session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
        with self.assertRaisesRegex(ReadingMcpError, "READING_COVERAGE_INCOMPLETE"):
            session.call(
                TOOL_VALIDATE,
                {
                    "document_handle": descriptor.document_handle,
                    "answer": {"content": "原文未提供。", "citation_ids": []},
                },
            )
        for index in range(session.chunk_count):
            session.call(
                TOOL_GET_CHUNKS,
                {"document_handle": descriptor.document_handle, "chunk_index": index},
            )
        with self.assertRaisesRegex(ReadingMcpError, "ANSWER_CITATION_UNGROUNDED"):
            session.call(
                TOOL_VALIDATE,
                {
                    "document_handle": descriptor.document_handle,
                    "answer": {"content": "未知【S99】", "citation_ids": ["S99"]},
                },
            )

    def test_tools_are_read_only_and_exact(self) -> None:
        definitions = tool_definitions()
        self.assertEqual(
            [item["name"] for item in definitions],
            [TOOL_GET_TASK, TOOL_GET_CHUNKS, TOOL_VALIDATE],
        )
        for item in definitions:
            self.assertTrue(item["annotations"]["readOnlyHint"])
            self.assertFalse(item["annotations"]["destructiveHint"])
            self.assertFalse(item["annotations"]["openWorldHint"])


if __name__ == "__main__":
    unittest.main()
