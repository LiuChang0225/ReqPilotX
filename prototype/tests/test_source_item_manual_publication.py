"""Host semantic construction and actual job-publication manual opinion fences."""

# ruff: noqa: E402 -- establish an isolated application runtime before imports.
from __future__ import annotations
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import hashlib
from threading import Barrier
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.application.agent_workflow import source_item_manual_publication as host
from app.application.agent_workflow.source_item_manual_decisions import (
    build_manual_decision_context,
)
from app.application.agent_workflow.source_item_visual import (
    compile_source_item_visual_readings,
)
from app.adapters.persistence import source_item_manual_decisions as repo
from app.adapters.persistence import postgres_jobs
from . import test_source_item_direct_revision as direct_fixture
from .test_source_item_delivery_order import fixture as decomposition_fixture


def variants():
    sample = direct_fixture.DirectRevisionTests()
    sample.setUp()
    dm, base, split = decomposition_fixture()
    return [
        ("baseline", sample.dm, sample.base, sample.new, None, None),
        ("decomposition", dm, base, base, split, None),
        (
            "image",
            sample.dm,
            sample.base,
            sample.base,
            None,
            compile_source_item_visual_readings(
                sample.dm, sample.base, [sample.reading]
            ),
        ),
    ]


class ManualPublicationHostTests(unittest.TestCase):
    def test_common_baseline_publisher_attaches_actual_annotations_context(self):
        from app.adapters.storage.local_object_store import LocalObjectStore
        from app.application.agent_workflow.source_requirement_jobs import (
            publish_agent_source_item_revision,
        )
        from .test_source_item_image_jobs import Context

        fixture = direct_fixture.DirectRevisionTests()
        fixture.setUp()
        before = host._PinnedContext(
            build_manual_decision_context(fixture.dm, fixture.base, {})
        )
        before.semantic_head_job_id = "actual-prior"
        annotations = compile_source_item_visual_readings(
            fixture.dm, fixture.new, [fixture.reading]
        )
        with (
            TemporaryDirectory() as temporary,
            patch.object(host, "manual_publication_enabled", return_value=True),
            patch.object(
                host, "current_manual_publication_context", return_value=before
            ),
            patch(
                "app.application.agent_workflow.source_requirement_reference_bridge._load_effective_reference",
                return_value=fixture.bundle,
            ),
            patch(
                "app.application.agent_workflow.source_item_agent_jobs._source_context",
                return_value=({}, fixture.bundle, None, {}),
            ),
            patch(
                "app.application.agent_workflow.source_item_delivery_bridge._reasoning",
                return_value={"decomposition": None},
            ),
        ):
            result = publish_agent_source_item_revision(
                project_id="p",
                document_id="d",
                document_map=fixture.dm,
                previous_bundle=fixture.bundle,
                baseline=fixture.new,
                validation_receipt=fixture.validation,
                operation="CLASSIFICATION_REVISION",
                affected_scope_ids=("text:clause",),
                runtime_receipts=(),
                request_sha256="c" * 64,
                candidate_sha256="d" * 64,
                domain_proof={"synthetic": True},
                context=Context(),
                image_annotations=annotations,
                container=SimpleNamespace(
                    object_store=LocalObjectStore(Path(temporary))
                ),
                direct_provenance=fixture.provenance,
            )
        expected = build_manual_decision_context(
            fixture.dm, fixture.new, {}, annotations=annotations
        )
        self.assertEqual(result["_manual_decision_contexts"]["current"], expected)
        self.assertEqual(
            result["expected_source_item_semantic_head_job_id"], "actual-prior"
        )

    def test_host_builds_changed_context_for_all_three_semantic_entry_types(self):
        for name, dm, before_base, after_base, split, image_annotations in variants():
            with (
                self.subTest(name=name),
                patch.object(host, "manual_publication_enabled", return_value=True),
            ):
                before = host._PinnedContext(
                    build_manual_decision_context(dm, before_base, {})
                )
                before.semantic_head_job_id = "old-actual-job"
                result = {
                    "source_sha256": before_base.source_sha256,
                    "baseline_sha256": after_base.artifact_sha256,
                }
                host.attach_manual_publication_contexts(
                    result,
                    container=SimpleNamespace(),
                    project_id="p",
                    document_id="d",
                    previous_context=before,
                    document_map=dm,
                    baseline=after_base,
                    table_previews={},
                    decomposition=split,
                    annotations=image_annotations,
                )
                self.assertEqual(
                    result["expected_source_item_semantic_head_job_id"],
                    "old-actual-job",
                )
                self.assertEqual(
                    result["expected_live_baseline_sha256"], before_base.artifact_sha256
                )
                self.assertNotEqual(
                    result["_manual_decision_contexts"]["current"]["units"],
                    before["units"],
                )
                self.assertNotIn("manual_decisions", result)

    def test_same_baseline_semantic_race_is_detected_while_resolving_context(self):
        context = {"source_sha256": "a" * 64}
        with (
            patch.object(host, "manual_publication_enabled", return_value=True),
            patch(
                "app.application.agent_workflow.source_item_manual_decision_api.resolve_current_manual_context",
                return_value=context,
            ),
            patch.object(host, "_semantic_head", side_effect=["job-old", "job-new"]),
        ):
            with self.assertRaisesRegex(ValueError, "semantics changed"):
                host.current_manual_publication_context(SimpleNamespace(), "p", "d")


def run_manual_publication_integration(database, queue, project_id):
    """Called only inside the UUID-isolated publication test DB, never production."""
    outcomes = []
    container = SimpleNamespace(jobs=queue)
    for name, dm, before_base, after_base, split, image_annotations in variants():
        document_id = "manual-publication-" + name
        with database.connection() as conn:
            conn.execute(
                """INSERT INTO sor_documents(id,project_id,file_name,file_type,storage_key,extracted_text,
                source_sha256,source_size,created_at,updated_at)
                SELECT %s,project_id,file_name,file_type,storage_key,extracted_text,%s,source_size,created_at,updated_at
                FROM sor_documents WHERE id='native-fence-document' """,
                (document_id, before_base.source_sha256),
            )
        payload = {
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": before_base.source_sha256,
        }
        prior = queue.enqueue(
            kind="agent.source-item.direct-revision",
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key="manual-prior-" + name,
            payload=payload,
        )
        predecessor = {
            "source_sha256": before_base.source_sha256,
            "document_map_artifact_sha256": dm.artifact_sha256,
            "baseline_revision_artifact": {
                "baseline_artifact_sha256": before_base.artifact_sha256,
                "baseline_revision": before_base.revision,
            },
        }
        with database.connection() as conn:
            conn.execute(
                "UPDATE runtime_jobs SET status='SUCCEEDED', result=%s::jsonb WHERE id=%s",
                (json.dumps(predecessor), prior.job_id),
            )
        before = host._PinnedContext(build_manual_decision_context(dm, before_base, {}))
        before.semantic_head_job_id = prior.job_id
        result = {
            "source_sha256": before_base.source_sha256,
            "baseline_sha256": after_base.artifact_sha256,
            "document_map_artifact_sha256": dm.artifact_sha256,
            "expected_live_baseline_sha256": before_base.artifact_sha256,
        }
        if name == "baseline":
            result["baseline_revision_artifact"] = {
                "baseline_artifact_sha256": after_base.artifact_sha256,
                "baseline_revision": after_base.revision,
            }
        elif name == "decomposition":
            result.update(
                publication_mode="WORKING_PROPOSAL",
                artifact={"artifact_sha256": split["artifact_sha256"]},
            )
        else:
            result["annotations_artifact"] = {
                "artifact_sha256": image_annotations["annotations_sha256"]
            }
        host.attach_manual_publication_contexts(
            result,
            container=container,
            project_id=project_id,
            document_id=document_id,
            previous_context=before,
            document_map=dm,
            baseline=after_base,
            table_previews={},
            decomposition=split,
            annotations=image_annotations,
        )
        uid = next(iter(before["units"]))
        decisions = [
            {
                "unit_id": uid,
                "expected_unit_context_sha256": before["units"][uid][
                    "unit_context_sha256"
                ],
                "decision": "PENDING_EVIDENCE",
                "reason": "First engineer opinion",
                "conditions": "",
                "evidence_requests": "Please provide test evidence.",
            }
        ]
        with patch.object(repo.db, "get_conn", database.connection):
            repo.save_manual_decisions(
                project_id,
                document_id,
                source_sha256=before_base.source_sha256,
                expected_baseline_sha256=before_base.artifact_sha256,
                expected_response_revision=0,
                command_id="first",
                decisions=decisions,
                context_resolver=lambda: before,
            )
            decisions[0]["reason"] = "Latest engineer edit while Agent is running"
            latest = repo.save_manual_decisions(
                project_id,
                document_id,
                source_sha256=before_base.source_sha256,
                expected_baseline_sha256=before_base.artifact_sha256,
                expected_response_revision=1,
                command_id="latest",
                decisions=decisions,
                context_resolver=lambda: before,
            )
        jobs = []
        kinds = (
            ("agent.source-item.decompose", "agent.source-item.loop")
            if name == "decomposition"
            else ("agent.source-item.loop", "agent.source-item.images")
        )
        for index, kind in enumerate(kinds):
            job = queue.enqueue(
                kind=kind,
                aggregate_type="sor-document",
                aggregate_id=document_id,
                idempotency_key="manual-publish-" + name + "-" + str(index),
                payload=payload,
            )
            with database.connection() as conn:
                conn.execute(
                    "UPDATE runtime_jobs SET status='RUNNING', lease_owner=%s, lease_expires_at=now()+interval '2 minutes' WHERE id=%s",
                    ("manual-worker-" + str(index), job.job_id),
                )
            jobs.append(job)
        if name == "image":
            with patch.object(
                postgres_jobs,
                "_publish_document_source_asset_references",
                side_effect=RuntimeError("synthetic rollback after manual hook"),
            ):
                try:
                    queue.succeed(
                        job_id=jobs[0].job_id,
                        worker_id="manual-worker-0",
                        result=result,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError(
                        "The post-hook exception must roll back publication"
                    )
            assert queue.get(jobs[0].job_id).status.value == "RUNNING"
            with database.connection() as conn:
                assert (
                    repo.current_manual_decisions_in_transaction(
                        conn, project_id, document_id
                    )["artifact_sha256"]
                    == latest["artifact_sha256"]
                )
        barrier = Barrier(2)

        def publish(index):
            barrier.wait(timeout=10)
            candidate = deepcopy(result)
            if name == "decomposition" and index == 1:
                candidate["decomposition_artifact"] = candidate.pop("artifact")
            return queue.succeed(
                job_id=jobs[index].job_id,
                worker_id="manual-worker-" + str(index),
                result=candidate,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            finished = list(pool.map(publish, range(2)))
        assert sorted(job.status.value for job in finished) == [
            "FAILED",
            "SUCCEEDED",
        ], (
            name,
            [(job.status.value, job.error_code, job.error_message) for job in finished],
        )
        winner = next(job for job in finished if job.status.value == "SUCCEEDED")
        assert "_manual_decision_contexts" not in winner.result
        assert winner.result["manual_decision_rebind"]["status"] == "REBOUND"
        with database.connection() as conn:
            stored = repo.current_manual_decisions_in_transaction(
                conn, project_id, document_id
            )
        assert stored["revision"] == 3
        assert stored["records"][uid]["state"] == "STALE"
        assert stored["records"][uid]["reason"] == decisions[0]["reason"]
        assert stored["history"][-1]["reason"] == decisions[0]["reason"]
        assert queue.get(prior.job_id).result == predecessor
        outcomes.append(
            {
                "entry": name,
                "same_baseline": before_base.artifact_sha256
                == after_base.artifact_sha256,
                "statuses": sorted(job.status.value for job in finished),
                "manual_revision": stored["revision"],
                "latest_user_text_retained": True,
                "staleness_persisted_in_publication_transaction": True,
                "one_visible_successor": True,
                "private_context_not_persisted_in_job": True,
                "rollback_verified": name == "image",
            }
        )
    from app.adapters.persistence.postgres_agent_structure_loop import (
        PostgresAgentStructureLoopRepository,
    )
    from .test_agent_structure_loop_repository import AgentStructureLoopRepositoryTests
    from app.application.agent_workflow.native_structure_working_revision import (
        native_structure_working_revision_bytes,
    )
    from app.application.agent_workflow.source_requirement_baseline import (
        canonical_json_sha256,
    )

    revision = AgentStructureLoopRepositoryTests._working_revisions()[0]
    doc = "manual-map-only"
    with database.connection() as conn:
        conn.execute(
            """INSERT INTO sor_documents(id,project_id,file_name,file_type,storage_key,extracted_text,
            source_sha256,source_size,created_at,updated_at)
            SELECT %s,project_id,file_name,file_type,storage_key,extracted_text,%s,source_size,created_at,updated_at
            FROM sor_documents WHERE id='native-fence-document' """,
            (doc, revision.source_sha256),
        )
    body = {
        "schema_version": "source-item-manual-context/v1",
        "source_sha256": revision.source_sha256,
        "baseline_sha256": "b" * 64,
        "units": {
            "u": {
                "unit_id": "u",
                "source_item_id": "synthetic-source",
                "requirement_display_id": "REQ-0001",
                "unit_context_sha256": "a" * 64,
            }
        },
    }
    context = {**body, "context_sha256": canonical_json_sha256(body)}
    with patch.object(repo.db, "get_conn", database.connection):
        original = repo.save_manual_decisions(
            project_id,
            doc,
            source_sha256=revision.source_sha256,
            expected_baseline_sha256=context["baseline_sha256"],
            expected_response_revision=0,
            command_id="map-opinion",
            context_resolver=lambda: context,
            decisions=[
                {
                    "unit_id": "u",
                    "expected_unit_context_sha256": "a" * 64,
                    "decision": "PENDING_EVIDENCE",
                    "reason": "Retained engineer opinion",
                    "conditions": "",
                    "evidence_requests": "Test report",
                }
            ],
        )
    encoded = native_structure_working_revision_bytes(revision)
    args = {
        "project_id": project_id,
        "document_id": doc,
        "source_sha256": revision.source_sha256,
        "base_artifact_id": revision.base_artifact_id,
        "base_projection_sha256": revision.base_projection_sha256,
        "revision": revision,
        "object_key": "synthetic/map-only.json",
        "object_sha256": hashlib.sha256(encoded).hexdigest(),
        "object_size": len(encoded),
        "media_type": "application/vnd.reqpilotx.native-structure-working-revision+json",
        "expected_head_revision_sha256": None,
    }
    maps = PostgresAgentStructureLoopRepository(database)
    original_hook = host.invalidate_manual_for_unbound_map_in_transaction

    def reject_after_hook(*a, **kw):
        original_hook(*a, **kw)
        raise RuntimeError("synthetic map rollback")

    with patch.object(
        host,
        "invalidate_manual_for_unbound_map_in_transaction",
        side_effect=reject_after_hook,
    ):
        try:
            maps.append_working_revision(**args)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Map rollback must propagate")
    assert (
        maps.get_working_revision_head(
            project_id=project_id, document_id=doc, source_sha256=revision.source_sha256
        )
        is None
    )
    with database.connection() as conn:
        assert (
            repo.current_manual_decisions_in_transaction(conn, project_id, doc)
            == original
        )
    _, created = maps.append_working_revision(**args)
    assert created
    _, replay = maps.append_working_revision(**args)
    assert replay is False
    with database.connection() as conn:
        changed = repo.current_manual_decisions_in_transaction(conn, project_id, doc)
    assert changed["revision"] == 2 and changed["records"]["u"]["state"] == "STALE"
    assert changed["records"]["u"]["stale_reason"] == "MISSING_PREVIOUS_CONTEXT"
    outcomes.append(
        {
            "entry": "legacy_map_without_verified_source_successor",
            "stale_persisted": True,
            "rollback_verified": True,
            "idempotent_replay": True,
            "new_source_extraction_claimed": False,
        }
    )
    return {"status": "PASS", "cases": outcomes}
