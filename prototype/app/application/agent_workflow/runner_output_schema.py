"""Compatibility alias for reqpilotx.application.agent_workflow.runner_output_schema."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.application.agent_workflow.runner_output_schema")
