from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

import scripts.document_visual_pipeline_profile as profile_module
from scripts.document_visual_pipeline_profile import (
    COMPLETE_PIPELINE_ARTIFACT_KINDS,
    DocumentVisualPipelineProfileRejected,
    build_document_visual_pipeline_profile,
    verify_document_visual_pipeline_profile,
)
from scripts.document_visual_review_bundle import (
    canonical_pipeline_fingerprint,
    canonical_profile_sha256,
)
from tests import test_document_visual_review_bundle as bundle_test_support


class DocumentVisualPipelineProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = bundle_test_support.DocumentVisualReviewBundleTests(
            methodName=(
                "test_four_formats_export_and_strictly_verify_full_document"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            DocumentVisualPipelineProfileRejected,
            rf"^{re.escape(code)}$",
        )

    def make_builder_inputs(
        self,
        name: str,
        *,
        document_format: str = "docx",
    ) -> dict[str, object]:
        source_inputs = self.fixture.make_inputs(
            document_format,
            name=f"builder-source-{name}",
        )
        controlled_root = self.fixture.root / f"profile-inputs-{name}"
        controlled_root.mkdir()
        artifacts = []
        for index, kind in enumerate(
            COMPLETE_PIPELINE_ARTIFACT_KINDS,
            start=1,
        ):
            path = controlled_root / f"artifact-{index:04d}.json"
            path.write_text(
                json.dumps(
                    {
                        "artifact_kind": kind,
                        "synthetic_customer_payload": {"ordinal": index},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            artifacts.append(
                {
                    "kind": kind,
                    "path": path.name,
                    "expected_sha256": _sha256(path),
                    "media_type": "application/json",
                    "customer_sensitive": True,
                }
            )
        components = {
            name: {
                "id": f"reqpilotx-{name}",
                "version": "1.0.0",
                "status": "COMPLETE",
                "snapshot_id": f"{name}-snapshot-001",
                "fingerprint_sha256": f"{index + 1}" * 64,
            }
            for index, name in enumerate(
                ("adapter", "resolver", "classifier", "projector")
            )
        }
        return {
            "source_inputs": source_inputs,
            "controlled_root": controlled_root,
            "output_path": controlled_root / "pipeline-profile.json",
            "artifacts": artifacts,
            "components": components,
        }

    def build(
        self,
        values: dict[str, object],
        *,
        pipeline_status: str = "COMPLETE",
    ) -> dict:
        source_inputs = values["source_inputs"]
        return build_document_visual_pipeline_profile(
            source_inputs["source"],
            document_format=source_inputs["format"],
            expected_source_sha256=source_inputs["source_sha256"],
            pipeline_status=pipeline_status,
            components=values["components"],
            artifacts=values["artifacts"],
            output_path=values["output_path"],
            corpus_root=self.fixture.corpus_root,
        )

    def verify(
        self,
        values: dict[str, object],
        *,
        expected_profile_file_sha256: str | None = None,
    ) -> dict:
        source_inputs = values["source_inputs"]
        profile_path = Path(values["output_path"])
        return verify_document_visual_pipeline_profile(
            profile_path,
            source=source_inputs["source"],
            document_format=source_inputs["format"],
            expected_source_sha256=source_inputs["source_sha256"],
            expected_profile_file_sha256=(
                expected_profile_file_sha256 or _sha256(profile_path)
            ),
            corpus_root=self.fixture.corpus_root,
        )

    def test_complete_profile_is_schema_valid_verified_and_bundle_consumable(
        self,
    ) -> None:
        values = self.make_builder_inputs("happy")
        profile = self.build(values)

        self.assertEqual(
            profile["pipeline"]["pipeline_fingerprint_sha256"],
            canonical_pipeline_fingerprint(profile),
        )
        self.assertEqual(
            profile["profile_sha256"],
            canonical_profile_sha256(profile),
        )
        self.assertEqual(
            [artifact["kind"] for artifact in profile["artifacts"]],
            list(COMPLETE_PIPELINE_ARTIFACT_KINDS),
        )
        self.assertEqual(self.verify(values), profile)
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "document_visual_review"
            / "pipeline_profile.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(profile)

        source_inputs = values["source_inputs"]
        source_inputs["pipeline_profile"] = profile
        source_inputs["pipeline_profile_path"] = values["output_path"]
        source_inputs["pipeline_profile_file_sha256"] = _sha256(
            Path(values["output_path"])
        )
        destination, manifest = self.fixture.export(
            source_inputs,
            destination_name="builder-bundle",
        )
        self.assertEqual(
            self.fixture.verify(destination, manifest, source_inputs),
            manifest,
        )

    def test_source_hash_and_format_mismatch_are_rejected(self) -> None:
        hash_mismatch = self.make_builder_inputs("source-hash")
        hash_mismatch["source_inputs"]["source_sha256"] = "f" * 64
        with self.assert_contract_error(
            "pipeline_profile_source_hash_mismatch"
        ):
            self.build(hash_mismatch)

        format_mismatch = self.make_builder_inputs("source-format")
        format_mismatch["source_inputs"]["format"] = "xlsx"
        with self.assert_contract_error(
            "pipeline_profile_source_format_mismatch"
        ):
            self.build(format_mismatch)

    def test_artifact_hash_path_symlink_json_and_privacy_reject(self) -> None:
        hash_mismatch = self.make_builder_inputs("artifact-hash")
        hash_mismatch["artifacts"][0]["expected_sha256"] = "f" * 64
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_hash_mismatch"
        ):
            self.build(hash_mismatch)

        traversal = self.make_builder_inputs("artifact-traversal")
        traversal["artifacts"][0]["path"] = "../escape.json"
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_path_invalid"
        ):
            self.build(traversal)

        symlink = self.make_builder_inputs("artifact-symlink")
        root = Path(symlink["controlled_root"])
        original = root / symlink["artifacts"][0]["path"]
        linked = root / "linked.json"
        linked.symlink_to(original)
        symlink["artifacts"][0]["path"] = linked.name
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_symlink"
        ):
            self.build(symlink)

        invalid_json = self.make_builder_inputs("artifact-json")
        path = (
            Path(invalid_json["controlled_root"])
            / invalid_json["artifacts"][0]["path"]
        )
        path.write_bytes(b"{not json")
        invalid_json["artifacts"][0]["expected_sha256"] = _sha256(path)
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_json_invalid"
        ):
            self.build(invalid_json)

        privacy = self.make_builder_inputs("artifact-privacy")
        privacy["artifacts"][0]["customer_sensitive"] = False
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_privacy_not_confirmed"
        ):
            self.build(privacy)

    def test_complete_status_requires_all_artifacts_and_components(self) -> None:
        missing = self.make_builder_inputs("missing-artifact")
        missing["artifacts"].pop()
        with self.assert_contract_error(
            "pipeline_profile_complete_artifacts_incomplete"
        ):
            self.build(missing)

        out_of_order = self.make_builder_inputs("artifact-order")
        out_of_order["artifacts"][0], out_of_order["artifacts"][1] = (
            out_of_order["artifacts"][1],
            out_of_order["artifacts"][0],
        )
        with self.assert_contract_error(
            "pipeline_profile_artifact_kind_order_invalid"
        ):
            self.build(out_of_order)

        incomplete_component = self.make_builder_inputs("component-status")
        incomplete_component["components"]["resolver"]["status"] = "PARTIAL"
        with self.assert_contract_error(
            "pipeline_profile_complete_component_not_complete"
        ):
            self.build(incomplete_component)

    def test_output_corpus_preexisting_and_symlink_parent_are_rejected(
        self,
    ) -> None:
        inside = self.make_builder_inputs("output-inside")
        inside["output_path"] = self.fixture.corpus_root / "profile.json"
        with self.assert_contract_error("pipeline_profile_output_inside_corpus"):
            self.build(inside)

        preexisting = self.make_builder_inputs("output-existing")
        output = Path(preexisting["output_path"])
        output.write_text("preserve", encoding="utf-8")
        with self.assert_contract_error("pipeline_profile_output_exists"):
            self.build(preexisting)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve")

        symlink_parent = self.make_builder_inputs("output-symlink")
        linked = self.fixture.root / "linked-profile-root"
        linked.symlink_to(
            symlink_parent["controlled_root"],
            target_is_directory=True,
        )
        symlink_parent["output_path"] = linked / "pipeline-profile.json"
        with self.assert_contract_error(
            "pipeline_profile_output_parent_symlink"
        ):
            self.build(symlink_parent)

    def test_verifier_rejects_profile_and_payload_drift(self) -> None:
        payload_drift = self.make_builder_inputs("verify-payload")
        self.build(payload_drift)
        payload_path = (
            Path(payload_drift["controlled_root"])
            / payload_drift["artifacts"][0]["path"]
        )
        payload_path.write_bytes(payload_path.read_bytes() + b" ")
        with self.assert_contract_error(
            "pipeline_profile_artifact_0_hash_mismatch"
        ):
            self.verify(payload_drift)

        profile_drift = self.make_builder_inputs("verify-profile")
        self.build(profile_drift)
        original_hash = _sha256(Path(profile_drift["output_path"]))
        profile = json.loads(
            Path(profile_drift["output_path"]).read_text(encoding="utf-8")
        )
        profile["pipeline"]["adapter"]["version"] = "9.9.9"
        Path(profile_drift["output_path"]).write_text(
            json.dumps(profile, sort_keys=True),
            encoding="utf-8",
        )
        with self.assert_contract_error("pipeline_profile_file_hash_mismatch"):
            self.verify(
                profile_drift,
                expected_profile_file_sha256=original_hash,
            )

    def test_input_inode_change_before_publish_is_toctou_and_atomic(self) -> None:
        values = self.make_builder_inputs("toctou")
        artifact_path = (
            Path(values["controlled_root"])
            / values["artifacts"][0]["path"]
        )
        output = Path(values["output_path"])
        original_profile_payload = profile_module._profile_payload

        def replace_input(**kwargs):
            profile = original_profile_payload(**kwargs)
            replacement = artifact_path.with_name("replacement.json")
            replacement.write_bytes(artifact_path.read_bytes())
            replacement.replace(artifact_path)
            return profile

        with (
            mock.patch.object(
                profile_module,
                "_profile_payload",
                side_effect=replace_input,
            ),
            self.assert_contract_error(
                "pipeline_profile_input_toctou_detected"
            ),
        ):
            self.build(values)
        self.assertFalse(output.exists())
        self.assertEqual(
            [
                path
                for path in Path(values["controlled_root"]).iterdir()
                if path.name.startswith(".pipeline-profile.json.")
            ],
            [],
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
