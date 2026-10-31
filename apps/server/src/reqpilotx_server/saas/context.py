from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountScope:
    account_id: str
    project_id: str = ""


_scope: ContextVar[AccountScope | None] = ContextVar("reqpilotx_account_scope", default=None)


def current_scope() -> AccountScope | None:
    return _scope.get()


@contextmanager
def account_scope(account_id: str, project_id: str = ""):
    token = _scope.set(AccountScope(account_id, project_id))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


def project_command_id(command_id: str | None) -> str | None:
    scope = current_scope()
    if command_id is None or scope is None:
        return command_id
    import hashlib
    return "account:" + hashlib.sha256((scope.account_id + "\0" + command_id).encode()).hexdigest()


def owned_task_identity(value: dict) -> dict:
    scope = current_scope()
    if scope is None:
        return value
    if value.get("project_id", scope.project_id) != scope.project_id:
        raise PermissionError("saas_task_project_mismatch")
    return {**value, "account_id": scope.account_id, "project_id": scope.project_id}


def verify_owned_task(value: dict) -> None:
    from reqpilotx_server.saas.config import SaaSSettings
    if not SaaSSettings.from_environment().enabled:
        return
    scope = current_scope()
    if scope is None or value.get("account_id") != scope.account_id or value.get("project_id") != scope.project_id:
        from reqpilotx_server.application.errors import ApplicationError
        raise ApplicationError(status_code=404, detail="saas_task_binding_not_found")
