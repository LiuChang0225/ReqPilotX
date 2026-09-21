"""Engine compatibility alias for reqpilotx.application.agent_workflow.native_task_brief."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.application.agent_workflow.native_task_brief")
