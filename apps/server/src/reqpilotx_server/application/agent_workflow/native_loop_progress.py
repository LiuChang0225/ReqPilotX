"""Public task labels derived from actual tool calls, without exposing arguments."""

_TOOLS = {
    "reqpilot_context": "正在读取当前任务",
    "reqpilot_skill": "正在核对处理方法",
    "reqpilot_read": "正在查阅原文与成果",
    "reqpilot_candidate": "正在整理修订建议",
    "reqpilot_publish": "正在准备保存工作版",
    "native_commandExecution": "正在使用文档工具",
    "native_imageView": "正在核对原文图像",
}
_OPERATIONS = {
    "capture": "正在读取文档",
    "capture-pdf-text": "正在补读原件文字",
    "reading-workspace": "正在准备逐页阅读材料",
    "reading-packet": "正在准备原文阅读材料",
    "inspect-source": "正在查询原文内容",
    "compile-map": "正在编制结构草稿",
    "compile-items": "正在编制原文条目",
    "compile-images": "正在整理图片释读",
    "compile-annotations": "正在保存阅读标注",
    "compile-decomposition": "正在编制派生需求",
    "image-packet": "正在读取图片依据",
    "decomposition-packet": "正在读取需求拆分依据",
    "analysis-packet": "正在读取分析依据",
    "compile-assessments": "正在整理分析建议",
    "register-evidence": "正在整理内部依据",
    "review-open": "正在准备核对工作区",
    "review-packet": "正在读取当前核对结果",
    "review-feedback": "正在保存反馈意见",
    "review-revise": "正在修订工作版",
    "review-verify": "正在核对工作版",
    "review-close": "正在保存核对结果",
    "deliver": "正在生成交付文件",
}


def native_tool_activity(event):
    """Describe an observed start only; a call is not proof of stage completion.

    Fixed labels keep paths, source text and executable arguments out of the
    public status. Unknown operations remain generic instead of guessed.
    """
    if event.get("type") != "tool_started":
        return None
    name = event.get("name")
    if name == "reqpilot_run":
        arguments = event.get("arguments")
        operation = arguments.get("operation") if isinstance(arguments, dict) else None
        return _OPERATIONS.get(operation, "正在运行处理工具") if isinstance(operation, str) else "正在运行处理工具"
    return _TOOLS.get(name, "正在处理当前任务") if isinstance(name, str) else "正在处理当前任务"
