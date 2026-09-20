from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import json
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import jsonschema
from PIL import Image

import scripts.document_visual_review_bundle as bundle_module
from scripts.document_visual_review_bundle import (
    BUNDLE_SCHEMA_VERSION,
    PIPELINE_PROFILE_SCHEMA_VERSION,
    PROFILE_HASH_CONTRACT,
    RENDER_PROFILE_SCHEMA_VERSION,
    DocumentVisualReviewBundleRejected,
    canonical_bundle_sha256,
    canonical_pipeline_fingerprint,
    canonical_profile_sha256,
    export_document_visual_review_bundle,
    verify_document_visual_review_bundle,
)


class DocumentVisualReviewBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.corpus_root = self.root / "corpus"
        self.corpus_root.mkdir()
        self.inputs_root = self.root / "controlled-inputs"
        self.inputs_root.mkdir()
        self.output_root = self.root / "controlled-review"
        self.output_root.mkdir()

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            DocumentVisualReviewBundleRejected,
            rf"^{re.escape(code)}$",
        )

    def make_inputs(
        self,
        document_format: str,
        *,
        page_count: int = 3,
        name: str | None = None,
    ) -> dict[str, object]:
        token = name or document_format
        case_root = self.inputs_root / token
        case_root.mkdir()
        source = self.corpus_root / f"customer-{token}.{document_format}"
        source.write_bytes(f"synthetic-{token}".encode())
        source_sha256 = _sha256(source)

        render_root = case_root / "render"
        render_root.mkdir()
        pages = []
        for page_number in range(1, page_count + 1):
            artifact_path = render_root / f"input-{page_number}.png"
            payload = _png_bytes(
                width=80 + page_number,
                height=100 + page_number,
                color=(page_number * 20, 40, 80),
            )
            artifact_path.write_bytes(payload)
            if document_format == "tif":
                locator = {
                    "kind": "TIF_FRAME",
                    "frame_index": page_number - 1,
                }
            elif document_format in {"xlsx", "xlsb"}:
                locator = {
                    "kind": "OFFICE_PAGE",
                    "sheet_locator": f"sheet-{page_number}",
                    "sheet_page_number": 1,
                }
            else:
                locator = {"kind": "OFFICE_PAGE"}
            pages.append(
                {
                    "page_number": page_number,
                    "locator": locator,
                    "artifact": {
                        "path": artifact_path.name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "media_type": "image/png",
                        "width_px": 80 + page_number,
                        "height_px": 100 + page_number,
                    },
                }
            )
        render_profile = {
            "schema_version": RENDER_PROFILE_SCHEMA_VERSION,
            "profile_hash_contract": PROFILE_HASH_CONTRACT,
            "source": {
                "source_sha256": source_sha256,
                "format": document_format,
            },
            "renderer": {
                "id": f"controlled-{document_format}-renderer",
                "version": "1.0.0",
                "config_sha256": "1" * 64,
            },
            "page_count": page_count,
            "pages": pages,
        }
        render_profile["profile_sha256"] = canonical_profile_sha256(
            render_profile
        )
        render_profile_path = render_root / "render-profile.json"
        _write_json(render_profile_path, render_profile)

        pipeline_root = case_root / "pipeline"
        pipeline_root.mkdir()
        pipeline_payload_path = pipeline_root / "snapshot.json"
        pipeline_payload = json.dumps(
            {"synthetic": True, "document": token},
            sort_keys=True,
        ).encode()
        pipeline_payload_path.write_bytes(pipeline_payload)
        component = {
            "id": "reqpilotx-component",
            "version": "1.0.0",
            "status": "COMPLETE",
            "snapshot_id": "snapshot-001",
            "fingerprint_sha256": "2" * 64,
        }
        pipeline_profile = {
            "schema_version": PIPELINE_PROFILE_SCHEMA_VERSION,
            "profile_hash_contract": PROFILE_HASH_CONTRACT,
            "source": {
                "source_sha256": source_sha256,
                "format": document_format,
            },
            "pipeline": {
                "status": "COMPLETE",
                "adapter": dict(component),
                "resolver": dict(component),
                "classifier": dict(component),
                "projector": dict(component),
                "pipeline_fingerprint_sha256": "0" * 64,
            },
            "artifacts": [
                {
                    "kind": "PIPELINE_SNAPSHOT",
                    "path": pipeline_payload_path.name,
                    "sha256": hashlib.sha256(pipeline_payload).hexdigest(),
                    "media_type": "application/json",
                    "customer_sensitive": True,
                }
            ],
        }
        pipeline_profile["pipeline"]["pipeline_fingerprint_sha256"] = (
            canonical_pipeline_fingerprint(pipeline_profile)
        )
        pipeline_profile["profile_sha256"] = canonical_profile_sha256(
            pipeline_profile
        )
        pipeline_profile_path = pipeline_root / "pipeline-profile.json"
        _write_json(pipeline_profile_path, pipeline_profile)
        return {
            "source": source,
            "source_sha256": source_sha256,
            "format": document_format,
            "render_profile": render_profile,
            "render_profile_path": render_profile_path,
            "render_profile_file_sha256": _sha256(render_profile_path),
            "pipeline_profile": pipeline_profile,
            "pipeline_profile_path": pipeline_profile_path,
            "pipeline_profile_file_sha256": _sha256(pipeline_profile_path),
        }

    def export(
        self,
        inputs: dict[str, object],
        *,
        destination_name: str = "review",
    ) -> tuple[Path, dict]:
        destination = self.output_root / destination_name
        manifest = export_document_visual_review_bundle(
            inputs["source"],
            document_id=f"DOC-{destination_name}",
            document_format=inputs["format"],
            expected_source_sha256=inputs["source_sha256"],
            render_profile_path=inputs["render_profile_path"],
            expected_render_profile_sha256=(
                inputs["render_profile_file_sha256"]
            ),
            pipeline_profile_path=inputs["pipeline_profile_path"],
            expected_pipeline_profile_sha256=(
                inputs["pipeline_profile_file_sha256"]
            ),
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        return destination, manifest

    def verify(
        self,
        destination: Path,
        manifest: dict,
        inputs: dict[str, object],
    ) -> dict:
        return verify_document_visual_review_bundle(
            destination,
            source=inputs["source"],
            document_format=inputs["format"],
            expected_source_sha256=inputs["source_sha256"],
            render_profile_path=inputs["render_profile_path"],
            expected_render_profile_sha256=(
                inputs["render_profile_file_sha256"]
            ),
            pipeline_profile_path=inputs["pipeline_profile_path"],
            expected_pipeline_profile_sha256=(
                inputs["pipeline_profile_file_sha256"]
            ),
            expected_bundle_sha256=manifest["bundle_sha256"],
            expected_artifact_inventory_sha256=(
                manifest["artifact_inventory_sha256"]
            ),
        )

    def replace_render_profile(
        self,
        inputs: dict[str, object],
        profile: dict,
        *,
        recompute_profile_hash: bool = True,
        refresh_file_pin: bool = True,
    ) -> None:
        if recompute_profile_hash:
            profile["profile_sha256"] = canonical_profile_sha256(profile)
        path = Path(inputs["render_profile_path"])
        _write_json(path, profile)
        inputs["render_profile"] = profile
        if refresh_file_pin:
            inputs["render_profile_file_sha256"] = _sha256(path)

    def replace_pipeline_profile(
        self,
        inputs: dict[str, object],
        profile: dict,
        *,
        recompute_pipeline_fingerprint: bool = True,
        recompute_profile_hash: bool = True,
        refresh_file_pin: bool = True,
    ) -> None:
        if recompute_pipeline_fingerprint:
            profile["pipeline"]["pipeline_fingerprint_sha256"] = (
                canonical_pipeline_fingerprint(profile)
            )
        if recompute_profile_hash:
            profile["profile_sha256"] = canonical_profile_sha256(profile)
        path = Path(inputs["pipeline_profile_path"])
        _write_json(path, profile)
        inputs["pipeline_profile"] = profile
        if refresh_file_pin:
            inputs["pipeline_profile_file_sha256"] = _sha256(path)

    def test_four_formats_export_and_strictly_verify_full_document(self) -> None:
        schema_root = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "document_visual_review"
        )
        render_schema = _load_schema(schema_root / "render_profile.schema.json")
        pipeline_schema = _load_schema(
            schema_root / "pipeline_profile.schema.json"
        )
        bundle_schema = _load_schema(schema_root / "bundle.schema.json")
        for schema in (render_schema, pipeline_schema, bundle_schema):
            jsonschema.Draft202012Validator.check_schema(schema)
        for document_format in ("docx", "xlsx", "xlsb", "tif"):
            with self.subTest(document_format=document_format):
                inputs = self.make_inputs(document_format)
                destination, manifest = self.export(
                    inputs,
                    destination_name=document_format,
                )

                self.assertEqual(
                    manifest["schema_version"],
                    BUNDLE_SCHEMA_VERSION,
                )
                self.assertEqual(
                    manifest["coverage"],
                    {
                        "full_document": True,
                        "page_numbers": [1, 2, 3],
                        "page_count": 3,
                    },
                )
                self.assertEqual(
                    manifest["bundle_sha256"],
                    canonical_bundle_sha256(manifest),
                )
                self.assertEqual(
                    self.verify(destination, manifest, inputs),
                    manifest,
                )
                jsonschema.Draft202012Validator(render_schema).validate(
                    inputs["render_profile"]
                )
                jsonschema.Draft202012Validator(pipeline_schema).validate(
                    inputs["pipeline_profile"]
                )
                jsonschema.Draft202012Validator(
                    bundle_schema,
                    format_checker=jsonschema.FormatChecker(),
                ).validate(manifest)
                self.assertEqual(
                    [page["page_number"] for page in manifest["pages"]],
                    [1, 2, 3],
                )
                self.assertEqual(
                    [
                        record["path"]
                        for record in manifest["artifact_inventory"]
                    ],
                    [
                        "renders/page-0001.png",
                        "renders/page-0002.png",
                        "renders/page-0003.png",
                        "pipeline/artifact-0001.json",
                        "index.html",
                    ],
                )
                serialized = (
                    destination / "manifest.json"
                ).read_text(encoding="utf-8") + (
                    destination / "index.html"
                ).read_text(encoding="utf-8")
                self.assertNotIn(str(inputs["source"]), serialized)
                self.assertNotIn(
                    Path(inputs["source"]).name,
                    serialized,
                )
                self.assertEqual(
                    manifest["privacy"]["classification"],
                    "CUSTOMER_SENSITIVE",
                )
                self.assertTrue(
                    all(
                        value is False
                        for value in manifest["review_provenance"].values()
                    )
                )

    def test_missing_out_of_order_and_duplicate_page_inventories_reject(
        self,
    ) -> None:
        cases = ("missing", "out_of_order", "duplicate")
        expected_codes = {
            "missing": "render_profile_page_coverage_incomplete",
            "out_of_order": "render_profile_page_out_of_order",
            "duplicate": "render_profile_page_duplicate",
        }
        for case in cases:
            with self.subTest(case=case):
                inputs = self.make_inputs("docx", name=f"pages-{case}")
                profile = copy.deepcopy(inputs["render_profile"])
                if case == "missing":
                    profile["pages"].pop(1)
                    profile["page_count"] = 2
                elif case == "out_of_order":
                    profile["pages"][0], profile["pages"][1] = (
                        profile["pages"][1],
                        profile["pages"][0],
                    )
                else:
                    profile["pages"][1] = copy.deepcopy(profile["pages"][0])
                self.replace_render_profile(inputs, profile)

                with self.assert_contract_error(expected_codes[case]):
                    self.export(
                        inputs,
                        destination_name=f"pages-{case}",
                    )

    def test_source_artifact_and_dimension_drift_are_rejected(self) -> None:
        source_inputs = self.make_inputs("docx", name="source-drift")
        Path(source_inputs["source"]).write_bytes(b"changed source")
        with self.assert_contract_error("bundle_source_hash_mismatch"):
            self.export(source_inputs, destination_name="source-drift")

        artifact_inputs = self.make_inputs("docx", name="artifact-drift")
        first_artifact = (
            Path(artifact_inputs["render_profile_path"]).parent
            / artifact_inputs["render_profile"]["pages"][0]["artifact"]["path"]
        )
        first_artifact.write_bytes(first_artifact.read_bytes() + b"drift")
        with self.assert_contract_error("render_page_0_hash_mismatch"):
            self.export(artifact_inputs, destination_name="artifact-drift")

        dimensions_inputs = self.make_inputs("docx", name="dimension-drift")
        profile = copy.deepcopy(dimensions_inputs["render_profile"])
        profile["pages"][0]["artifact"]["width_px"] += 1
        self.replace_render_profile(dimensions_inputs, profile)
        with self.assert_contract_error(
            "render_page_0_dimensions_mismatch"
        ):
            self.export(
                dimensions_inputs,
                destination_name="dimension-drift",
            )

    def test_profile_source_format_version_self_hash_and_file_pin_reject(
        self,
    ) -> None:
        render_format = self.make_inputs("docx", name="render-format")
        profile = copy.deepcopy(render_format["render_profile"])
        profile["source"]["format"] = "xlsx"
        self.replace_render_profile(render_format, profile)
        with self.assert_contract_error("render_profile_source_mismatch"):
            self.export(render_format, destination_name="render-format")

        pipeline_source = self.make_inputs("docx", name="pipeline-source")
        profile = copy.deepcopy(pipeline_source["pipeline_profile"])
        profile["source"]["source_sha256"] = "a" * 64
        self.replace_pipeline_profile(pipeline_source, profile)
        with self.assert_contract_error("pipeline_profile_source_mismatch"):
            self.export(pipeline_source, destination_name="pipeline-source")

        render_version = self.make_inputs("docx", name="render-version")
        profile = copy.deepcopy(render_version["render_profile"])
        profile["schema_version"] = "document-visual-render-profile/v999"
        self.replace_render_profile(render_version, profile)
        with self.assert_contract_error(
            "render_profile_schema_version_mismatch"
        ):
            self.export(render_version, destination_name="render-version")

        pipeline_version = self.make_inputs("docx", name="pipeline-version")
        profile = copy.deepcopy(pipeline_version["pipeline_profile"])
        profile["schema_version"] = "document-visual-pipeline-profile/v999"
        self.replace_pipeline_profile(pipeline_version, profile)
        with self.assert_contract_error(
            "pipeline_profile_schema_version_mismatch"
        ):
            self.export(pipeline_version, destination_name="pipeline-version")

        internal_hash = self.make_inputs("docx", name="internal-hash")
        profile = copy.deepcopy(internal_hash["render_profile"])
        profile["renderer"]["version"] = "2.0.0"
        self.replace_render_profile(
            internal_hash,
            profile,
            recompute_profile_hash=False,
        )
        with self.assert_contract_error("render_profile_sha256_mismatch"):
            self.export(internal_hash, destination_name="internal-hash")

        external_pin = self.make_inputs("docx", name="external-pin")
        profile = copy.deepcopy(external_pin["render_profile"])
        profile["renderer"]["version"] = "2.0.0"
        self.replace_render_profile(
            external_pin,
            profile,
            refresh_file_pin=False,
        )
        with self.assert_contract_error("render_profile_hash_mismatch"):
            self.export(external_pin, destination_name="external-pin")

        pipeline_fingerprint = self.make_inputs(
            "docx",
            name="pipeline-fingerprint",
        )
        profile = copy.deepcopy(pipeline_fingerprint["pipeline_profile"])
        profile["pipeline"]["adapter"]["version"] = "2.0.0"
        self.replace_pipeline_profile(
            pipeline_fingerprint,
            profile,
            recompute_pipeline_fingerprint=False,
        )
        with self.assert_contract_error(
            "pipeline_profile_fingerprint_mismatch"
        ):
            self.export(
                pipeline_fingerprint,
                destination_name="pipeline-fingerprint",
            )

    def test_path_traversal_and_input_symlink_are_rejected(self) -> None:
        traversal = self.make_inputs("docx", name="traversal")
        profile = copy.deepcopy(traversal["render_profile"])
        profile["pages"][0]["artifact"]["path"] = "../outside.png"
        self.replace_render_profile(traversal, profile)
        with self.assert_contract_error(
            "render_profile_page_0_artifact_path_invalid"
        ):
            self.export(traversal, destination_name="traversal")

        symlink = self.make_inputs("docx", name="symlink")
        profile = copy.deepcopy(symlink["render_profile"])
        original_path = (
            Path(symlink["render_profile_path"]).parent
            / profile["pages"][0]["artifact"]["path"]
        )
        symlink_path = original_path.with_name("linked-page.png")
        symlink_path.symlink_to(original_path)
        profile["pages"][0]["artifact"]["path"] = symlink_path.name
        self.replace_render_profile(symlink, profile)
        with self.assert_contract_error("render_page_0_symlink"):
            self.export(symlink, destination_name="symlink")

    def test_output_inside_corpus_preexisting_and_symlink_parent_reject(
        self,
    ) -> None:
        inside = self.make_inputs("docx", name="inside-output")
        with self.assert_contract_error("bundle_output_inside_corpus"):
            export_document_visual_review_bundle(
                inside["source"],
                document_id="DOC-inside",
                document_format=inside["format"],
                expected_source_sha256=inside["source_sha256"],
                render_profile_path=inside["render_profile_path"],
                expected_render_profile_sha256=(
                    inside["render_profile_file_sha256"]
                ),
                pipeline_profile_path=inside["pipeline_profile_path"],
                expected_pipeline_profile_sha256=(
                    inside["pipeline_profile_file_sha256"]
                ),
                output_dir=self.corpus_root / "forbidden-review",
                corpus_root=self.corpus_root,
            )

    def test_document_id_cannot_record_source_filename_or_path(self) -> None:
        inputs = self.make_inputs("docx", name="document-id-privacy")
        for document_id, code in (
            (
                Path(inputs["source"]).name,
                "bundle_document_id_leaks_source_filename",
            ),
            ("C:/customer/source.docx", "bundle_document_id_invalid"),
        ):
            with self.subTest(document_id=document_id):
                with self.assert_contract_error(code):
                    export_document_visual_review_bundle(
                        inputs["source"],
                        document_id=document_id,
                        document_format=inputs["format"],
                        expected_source_sha256=inputs["source_sha256"],
                        render_profile_path=inputs["render_profile_path"],
                        expected_render_profile_sha256=(
                            inputs["render_profile_file_sha256"]
                        ),
                        pipeline_profile_path=inputs[
                            "pipeline_profile_path"
                        ],
                        expected_pipeline_profile_sha256=(
                            inputs["pipeline_profile_file_sha256"]
                        ),
                        output_dir=self.output_root / "document-id-privacy",
                        corpus_root=self.corpus_root,
                    )

        preexisting = self.make_inputs("docx", name="preexisting")
        destination = self.output_root / "preexisting"
        destination.mkdir()
        marker = destination / "marker"
        marker.write_text("preserve", encoding="utf-8")
        with self.assert_contract_error("bundle_output_exists"):
            self.export(preexisting, destination_name="preexisting")
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

        symlink_parent = self.make_inputs("docx", name="symlink-output")
        linked_parent = self.root / "linked-output-root"
        linked_parent.symlink_to(self.output_root, target_is_directory=True)
        with self.assert_contract_error("bundle_output_parent_symlink"):
            export_document_visual_review_bundle(
                symlink_parent["source"],
                document_id="DOC-symlink-output",
                document_format=symlink_parent["format"],
                expected_source_sha256=symlink_parent["source_sha256"],
                render_profile_path=symlink_parent["render_profile_path"],
                expected_render_profile_sha256=(
                    symlink_parent["render_profile_file_sha256"]
                ),
                pipeline_profile_path=symlink_parent[
                    "pipeline_profile_path"
                ],
                expected_pipeline_profile_sha256=(
                    symlink_parent["pipeline_profile_file_sha256"]
                ),
                output_dir=linked_parent / "review",
                corpus_root=self.corpus_root,
            )

    def test_verifier_recomputes_artifact_hash_and_rejects_symlink(self) -> None:
        drift = self.make_inputs("docx", name="verify-drift")
        destination, manifest = self.export(
            drift,
            destination_name="verify-drift",
        )
        page_path = destination / "renders" / "page-0001.png"
        page_path.write_bytes(page_path.read_bytes() + b"tamper")
        with self.assert_contract_error(
            "bundle_inventory_0_hash_mismatch"
        ):
            self.verify(destination, manifest, drift)

        linked = self.make_inputs("docx", name="verify-symlink")
        destination, manifest = self.export(
            linked,
            destination_name="verify-symlink",
        )
        page_path = destination / "renders" / "page-0001.png"
        page_path.unlink()
        source_artifact = (
            Path(linked["render_profile_path"]).parent
            / linked["render_profile"]["pages"][0]["artifact"]["path"]
        )
        page_path.symlink_to(source_artifact)
        with self.assert_contract_error("bundle_inventory_0_symlink"):
            self.verify(destination, manifest, linked)

    def test_input_inode_change_before_publish_is_toctou_and_atomic(self) -> None:
        inputs = self.make_inputs("docx", name="toctou")
        destination = self.output_root / "toctou"
        artifact_path = (
            Path(inputs["render_profile_path"]).parent
            / inputs["render_profile"]["pages"][0]["artifact"]["path"]
        )
        original_write_json = bundle_module._write_json

        def replace_input_after_staging(path, value):
            original_write_json(path, value)
            replacement = artifact_path.with_name("replacement.png")
            replacement.write_bytes(artifact_path.read_bytes())
            replacement.replace(artifact_path)

        with (
            mock.patch.object(
                bundle_module,
                "_write_json",
                side_effect=replace_input_after_staging,
            ),
            self.assert_contract_error("bundle_input_toctou_detected"),
        ):
            self.export(inputs, destination_name="toctou")
        self.assertFalse(destination.exists())
        self.assertEqual(
            [
                path
                for path in self.output_root.iterdir()
                if path.name.startswith(".toctou.")
            ],
            [],
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png_bytes(
    *,
    width: int,
    height: int,
    color: tuple[int, int, int],
) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), color=color).save(
        stream,
        format="PNG",
    )
    return stream.getvalue()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
