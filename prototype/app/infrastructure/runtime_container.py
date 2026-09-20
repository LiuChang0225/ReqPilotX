from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters.agents.codex_app_server import (
    CODEX_APP_SERVER_RUNTIME_VERSION,
    CodexAppServerAgentRuntime,
    LocalCodexAppServerStructureDriver,
)
from app.adapters.agents.codex_app_server.structure_handoff_executor import (
    LocalCanonicalStructureHandoffExecutor,
)
from app.adapters.agents.codex_app_server.source_requirement_window_executor import (
    LocalCanonicalSourceRequirementWindowExecutor,
)
from app.adapters.agents.codex_app_server.source_item_text_executor import (
    LocalCanonicalSourceItemTextExecutor,
)
from app.adapters.agents.codex_app_server.semantic_app_server import (
    CodexAppServerConsensusSemanticProvider,
    LocalCodexAppServerSemanticDriver,
)
from app.adapters.persistence.postgres_agent_structure_proposals import (
    PostgresAgentStructureProposalRepository,
)
from app.adapters.persistence.postgres_agent_structure_loop import (
    PostgresAgentStructureLoopRepository,
)
from app.adapters.persistence.postgres_agent_structure_steers import (
    PostgresAgentStructureSteerRepository,
)
from app.adapters.persistence.postgres_agent_table_materializations import (
    PostgresAgentTableMaterializationRepository,
)
from app.adapters.persistence.postgres_jobs import PostgresJobQueue
from app.adapters.persistence.postgres_llm_requirement_reviews import (
    PostgresLlmRequirementReviewRepository,
)
from app.adapters.persistence.postgres_requirement_enrichment import (
    PostgresRequirementEnrichmentRepository,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.storage.oss_object_store import AliyunOssObjectStore
from app.infrastructure.postgres import PostgresDatabase
from app.infrastructure.runtime_settings import (
    AgentRuntimeBackend,
    DatabaseBackend,
    ObjectStoreBackend,
    RuntimeSettings,
)
from app.ports.job_queue import JobQueue
from app.ports.agent_structure_proposal_repository import (
    AgentStructureProposalRepository,
)
from app.ports.agent_structure_loop_repository import AgentStructureLoopRepository
from app.ports.agent_structure_steer_repository import (
    AgentStructureSteerRepository,
)
from app.ports.agent_table_materialization_repository import (
    AgentTableMaterializationRepository,
)
from app.ports.agent_runtime import AgentRuntimePort
from app.ports.agent_structure_handoff_executor import (
    AgentStructureHandoffExecutorPort,
)
from app.ports.llm_requirement_review_repository import (
    LlmRequirementReviewRepositoryPort,
)
from app.ports.object_store import ObjectStore
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentRepositoryPort,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderPort,
)
from app.ports.semantic_provider import PreflightSemanticProviderPort
from app.ports.source_requirement_window_executor import (
    SourceRequirementWindowExecutorPort,
)
from app.ports.source_item_text_executor import SourceItemTextExecutorPort


@dataclass(slots=True)
class RuntimeContainer:
    settings: RuntimeSettings
    object_store: ObjectStore
    database: PostgresDatabase | None
    jobs: JobQueue | None
    llm_review_repository: LlmRequirementReviewRepositoryPort
    requirement_enrichment_repository: RequirementEnrichmentRepositoryPort
    agent_table_materialization_repository: (
        AgentTableMaterializationRepository | None
    ) = None
    agent_structure_proposal_repository: AgentStructureProposalRepository | None = None
    agent_structure_loop_repository: AgentStructureLoopRepository | None = None
    agent_structure_steer_repository: AgentStructureSteerRepository | None = None
    agent_runtime: AgentRuntimePort | None = None
    agent_structure_handoff_executor: AgentStructureHandoffExecutorPort | None = None
    semantic_provider: PreflightSemanticProviderPort | None = None
    requirement_atomization_provider: RequirementAtomizationProviderPort | None = None
    source_requirement_window_executor: SourceRequirementWindowExecutorPort | None = (
        None
    )
    source_item_text_executor: SourceItemTextExecutorPort | None = None

    def close(self) -> None:
        if self.database is not None:
            self.database.close()


def build_runtime_container(
    settings: RuntimeSettings,
    *,
    apply_migrations: bool = True,
    sqlite_database_path: Path | None = None,
) -> RuntimeContainer:
    object_store: ObjectStore
    if settings.object_store_backend is ObjectStoreBackend.LOCAL:
        object_store = LocalObjectStore(settings.object_root)
    else:
        object_store = AliyunOssObjectStore(
            region=settings.oss_region,
            bucket=settings.oss_bucket,
            endpoint=settings.oss_endpoint,
            prefix=settings.oss_prefix,
        )

    database: PostgresDatabase | None = None
    jobs: JobQueue | None = None
    llm_review_repository: LlmRequirementReviewRepositoryPort
    requirement_enrichment_repository: RequirementEnrichmentRepositoryPort
    agent_structure_proposal_repository: AgentStructureProposalRepository | None = None
    agent_structure_loop_repository: AgentStructureLoopRepository | None = None
    agent_structure_steer_repository: AgentStructureSteerRepository | None = None
    agent_table_materialization_repository: (
        AgentTableMaterializationRepository | None
    ) = None
    (
        agent_runtime,
        semantic_provider,
        requirement_atomization_provider,
    ) = _build_agent_runtime_components(settings)
    agent_structure_handoff_executor = _build_structure_handoff_executor(settings)
    source_requirement_window_executor = (
        _build_source_requirement_window_executor(settings)
    )
    source_item_text_executor = _build_source_item_text_executor(settings)
    if settings.database_backend is DatabaseBackend.POSTGRESQL:
        database = PostgresDatabase(settings.database_url)
        if apply_migrations:
            migration_dir = (
                Path(__file__).resolve().parents[2] / "migrations" / "postgresql"
            )
            database.apply_migrations(migration_dir)
        jobs = PostgresJobQueue(database)
        llm_review_repository = PostgresLlmRequirementReviewRepository(database)
        requirement_enrichment_repository = PostgresRequirementEnrichmentRepository(
            database
        )
        agent_structure_proposal_repository = PostgresAgentStructureProposalRepository(
            database
        )
        agent_structure_loop_repository = PostgresAgentStructureLoopRepository(database)
        agent_structure_steer_repository = PostgresAgentStructureSteerRepository(
            database
        )
        agent_table_materialization_repository = (
            PostgresAgentTableMaterializationRepository(database)
        )
    else:
        if sqlite_database_path is None:
            raise ValueError(
                "The isolated SQLite test adapter requires sqlite_database_path."
            )
        from app.adapters.persistence.sqlite_llm_requirement_reviews import (
            SQLiteLlmRequirementReviewRepository,
        )
        from app.adapters.persistence.sqlite_requirement_enrichment import (
            SQLiteRequirementEnrichmentRepository,
        )

        llm_review_repository = SQLiteLlmRequirementReviewRepository(
            sqlite_database_path
        )
        requirement_enrichment_repository = SQLiteRequirementEnrichmentRepository(
            sqlite_database_path
        )
    return RuntimeContainer(
        settings=settings,
        object_store=object_store,
        database=database,
        jobs=jobs,
        llm_review_repository=llm_review_repository,
        requirement_enrichment_repository=requirement_enrichment_repository,
        agent_table_materialization_repository=(agent_table_materialization_repository),
        agent_structure_proposal_repository=agent_structure_proposal_repository,
        agent_structure_loop_repository=agent_structure_loop_repository,
        agent_structure_steer_repository=agent_structure_steer_repository,
        agent_runtime=agent_runtime,
        agent_structure_handoff_executor=agent_structure_handoff_executor,
        semantic_provider=semantic_provider,
        requirement_atomization_provider=requirement_atomization_provider,
        source_requirement_window_executor=source_requirement_window_executor,
        source_item_text_executor=source_item_text_executor,
    )


def _build_agent_runtime(settings: RuntimeSettings) -> AgentRuntimePort | None:
    return _build_agent_runtime_components(settings)[0]


def _build_structure_handoff_executor(
    settings: RuntimeSettings,
) -> AgentStructureHandoffExecutorPort | None:
    if settings.agent_runtime_backend is AgentRuntimeBackend.NONE:
        return None
    if settings.codex_bin is None or settings.codex_home is None:
        raise ValueError("Codex App Server paths were not validated.")
    return LocalCanonicalStructureHandoffExecutor(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=Path(__file__).resolve().parents[3],
    )


def _build_source_requirement_window_executor(
    settings: RuntimeSettings,
) -> SourceRequirementWindowExecutorPort | None:
    if settings.agent_runtime_backend is AgentRuntimeBackend.NONE:
        return None
    if settings.codex_bin is None or settings.codex_home is None:
        raise ValueError("Codex App Server paths were not validated.")
    return LocalCanonicalSourceRequirementWindowExecutor(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=Path(__file__).resolve().parents[3],
    )


def _build_source_item_text_executor(
    settings: RuntimeSettings,
) -> SourceItemTextExecutorPort | None:
    if settings.agent_runtime_backend is AgentRuntimeBackend.NONE:
        return None
    if settings.codex_bin is None or settings.codex_home is None:
        raise ValueError("Codex App Server paths were not validated.")
    return LocalCanonicalSourceItemTextExecutor(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=Path(__file__).resolve().parents[3],
    )


def _build_agent_runtime_components(
    settings: RuntimeSettings,
) -> tuple[
    AgentRuntimePort | None,
    PreflightSemanticProviderPort | None,
    RequirementAtomizationProviderPort | None,
]:
    if settings.agent_runtime_backend is AgentRuntimeBackend.NONE:
        return None, None, None
    if settings.agent_runtime_backend is not AgentRuntimeBackend.CODEX_APP_SERVER:
        raise ValueError("Unsupported Agent Runtime backend.")
    if settings.codex_bin is None or settings.codex_home is None:
        raise ValueError("Codex App Server paths were not validated.")
    repository_root = Path(__file__).resolve().parents[3]
    structure_driver = LocalCodexAppServerStructureDriver(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=repository_root,
    )
    semantic_driver = LocalCodexAppServerSemanticDriver(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=repository_root,
        model_id=settings.agent_model_id,
        reuse_document_session=True,
    )
    semantic_provider = CodexAppServerConsensusSemanticProvider(
        semantic_driver,
        repetition_count=1,
        max_concurrent_repetitions=1,
    )
    return (
        CodexAppServerAgentRuntime(
            structure_driver,
            runtime_version=CODEX_APP_SERVER_RUNTIME_VERSION,
            model_id=settings.agent_model_id,
        ),
        semantic_provider,
        semantic_provider,
    )


__all__ = ["RuntimeContainer", "build_runtime_container"]
