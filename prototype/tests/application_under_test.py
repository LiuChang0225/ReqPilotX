"""Explicit test harness over the split application modules.

Production code never imports this namespace. It lets older broad integration
tests patch a dependency once while the real API and worker composition remain
strictly domain-owned.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from app import db
from app.adapters.persistence.sqlite_llm_requirement_reviews import (
    SQLiteLlmRequirementReviewRepository,
)
from app.adapters.persistence.sqlite_requirement_enrichment import (
    SQLiteRequirementEnrichmentRepository,
)
from app.application import (
    assessment,
    assessment_contracts,
    export_contracts,
    exports,
    ingestion,
    knowledge,
    projects,
    projects_contracts,
    requirements,
    requirements_contracts,
    runtime,
    runtime_contracts,
)
from app.application.extraction import (
    contracts as extraction_contracts,
)
from app.application.extraction import (
    document_jobs,
    enrichment_workspace,
    evaluation_contracts,
    provider_support,
    restoration,
    review_sessions,
    review_support,
    structure_optimization_jobs,
    structure_workspace,
)

if not runtime.RUNTIME_SETTINGS.is_test:
    raise RuntimeError("The split application test harness requires test mode.")

runtime.RUNTIME_STATE.test_llm_review_repository_factory = (
    lambda: SQLiteLlmRequirementReviewRepository(db.DB_PATH)
)
runtime.RUNTIME_STATE.test_requirement_enrichment_repository_factory = (
    lambda: SQLiteRequirementEnrichmentRepository(db.DB_PATH)
)


_MODULES: tuple[ModuleType, ...] = (
    runtime,
    projects,
    ingestion,
    document_jobs,
    structure_workspace,
    structure_optimization_jobs,
    review_support,
    provider_support,
    review_sessions,
    enrichment_workspace,
    restoration,
    requirements,
    knowledge,
    assessment,
    exports,
    projects_contracts,
    assessment_contracts,
    extraction_contracts,
    evaluation_contracts,
    requirements_contracts,
    export_contracts,
    runtime_contracts,
)


class _ApplicationUnderTest(ModuleType):
    def __init__(self, modules: tuple[ModuleType, ...]) -> None:
        super().__init__("tests.application_under_test.namespace")
        ModuleType.__setattr__(self, "_modules", modules)
        owners: dict[str, tuple[ModuleType, ...]] = {}
        for module in modules:
            for name, value in module.__dict__.items():
                if name.startswith("__"):
                    continue
                owners[name] = (*owners.get(name, ()), module)
                if name not in self.__dict__:
                    ModuleType.__setattr__(self, name, value)
        ModuleType.__setattr__(self, "_owners", owners)

    def __setattr__(self, name: str, value: Any) -> None:
        owners = self.__dict__.get("_owners", {}).get(name, ())
        if owners:
            for module in owners:
                setattr(module, name, value)
        ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        # Test patches only delete attributes they created on this test module.
        # Production module definitions remain intact.
        ModuleType.__delattr__(self, name)


main = _ApplicationUnderTest(_MODULES)


__all__ = ["main"]
