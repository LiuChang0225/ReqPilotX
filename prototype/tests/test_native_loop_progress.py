
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.agent_workflow.native_loop_progress import native_tool_activity


class NativeLoopProgressTests(unittest.TestCase):
    def test_same_runner_distinguishes_reading_and_map_authoring(self):
        def event(operation):
            return {"type": "tool_started", "name": "reqpilot_run", "arguments": {"operation": operation}}
        self.assertEqual(native_tool_activity(event("capture")), "正在读取文档")
        self.assertEqual(native_tool_activity(event("reading-workspace")), "正在准备逐页阅读材料")
        self.assertEqual(native_tool_activity(event("compile-map")), "正在编制结构草稿")

    def test_unknown_or_invalid_arguments_do_not_become_public_text(self):
        for arguments in (None, [], {"operation": []}, {"operation": "/private/customer.pdf", "arguments": {"text": "private source"}}):
            self.assertEqual(native_tool_activity({"type": "tool_started", "name": "reqpilot_run", "arguments": arguments}), "正在运行处理工具")

    def test_observed_start_does_not_claim_success_or_invent_model_progress(self):
        for kind in ("tool_completed", "agent_message", "reasoning_delta", "turn/completed"):
            self.assertIsNone(native_tool_activity({"type": kind, "name": "reqpilot_run", "arguments": {"operation": "compile-map"}}))
        self.assertEqual(native_tool_activity({"type": "tool_started", "name": "reqpilot_publish"}), "正在准备保存工作版")


if __name__ == "__main__":
    unittest.main()
