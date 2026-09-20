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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
from PIL import Image

import scripts.document_visual_tif_render_profile as render_module
from scripts.document_visual_tif_render_profile import (
    DocumentVisualTifRenderRejected,
    TIF_RENDERER_CONFIG_SHA256,
    export_tif_render_profile,
    verify_tif_render_profile,
)


class DocumentVisualTifRenderProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.corpus_root = self.root / "corpus"
        self.corpus_root.mkdir()
        self.output_root = self.root / "controlled"
        self.output_root.mkdir()
        self.source = self.corpus_root / "customer-drawing.tif"
        first = Image.new("1", (31, 41), color=1)
        second = Image.new("RGB", (32, 42), color=(20, 40, 60))
        first.save(
            self.source,
            format="TIFF",
            save_all=True,
            append_images=[second],
        )
        self.source_sha256 = _sha256(self.source)

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            DocumentVisualTifRenderRejected,
            rf"^{re.escape(code)}$",
        )

    def export(self, name: str = "render"):
        destination = self.output_root / name
        result = export_tif_render_profile(
            self.source,
            expected_source_sha256=self.source_sha256,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        return destination, result

    def test_all_frames_are_source_bound_schema_valid_and_verified(self) -> None:
        destination, result = self.export()
        profile = result["profile"]

        self.assertEqual(profile["page_count"], 2)
        self.assertEqual(
            [page["locator"]["frame_index"] for page in profile["pages"]],
            [0, 1],
        )
        self.assertEqual(
            profile["renderer"]["config_sha256"],
            TIF_RENDERER_CONFIG_SHA256,
        )
        self.assertEqual(
            verify_tif_render_profile(
                destination,
                source=self.source,
                expected_source_sha256=self.source_sha256,
                expected_profile_file_sha256=result[
                    "profile_file_sha256"
                ],
                corpus_root=self.corpus_root,
            ),
            profile,
        )
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "document_visual_review"
            / "render_profile.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(profile)
        serialized = (destination / "render-profile.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn(self.source.name, serialized)
        for page in profile["pages"]:
            path = destination / page["artifact"]["path"]
            self.assertEqual(_sha256(path), page["artifact"]["sha256"])
            with Image.open(path) as image:
                self.assertEqual(
                    image.size,
                    (
                        page["artifact"]["width_px"],
                        page["artifact"]["height_px"],
                    ),
                )

    def test_hash_drift_output_scope_preexisting_and_artifact_tamper_reject(
        self,
    ) -> None:
        with self.assert_contract_error("tif_render_source_hash_mismatch"):
            export_tif_render_profile(
                self.source,
                expected_source_sha256="f" * 64,
                output_dir=self.output_root / "hash-drift",
                corpus_root=self.corpus_root,
            )
        with self.assert_contract_error("tif_render_output_inside_corpus"):
            export_tif_render_profile(
                self.source,
                expected_source_sha256=self.source_sha256,
                output_dir=self.corpus_root / "forbidden",
                corpus_root=self.corpus_root,
            )
        existing = self.output_root / "existing"
        existing.mkdir()
        with self.assert_contract_error("tif_render_output_exists"):
            export_tif_render_profile(
                self.source,
                expected_source_sha256=self.source_sha256,
                output_dir=existing,
                corpus_root=self.corpus_root,
            )

        destination, result = self.export("tamper")
        page = destination / "page-0001.png"
        page.write_bytes(page.read_bytes() + b"tamper")
        with self.assert_contract_error("tif_render_page_0_hash_mismatch"):
            verify_tif_render_profile(
                destination,
                source=self.source,
                expected_source_sha256=self.source_sha256,
                expected_profile_file_sha256=result[
                    "profile_file_sha256"
                ],
                corpus_root=self.corpus_root,
            )

    def test_source_inode_change_before_publish_is_atomic(self) -> None:
        destination = self.output_root / "toctou"
        original_verify = render_module._verify_source_unchanged

        def replace_then_verify(pin):
            replacement = self.source.with_name("replacement.tif")
            replacement.write_bytes(self.source.read_bytes())
            replacement.replace(self.source)
            original_verify(pin)

        with (
            mock.patch.object(
                render_module,
                "_verify_source_unchanged",
                side_effect=replace_then_verify,
            ),
            self.assert_contract_error("tif_render_source_toctou_detected"),
        ):
            export_tif_render_profile(
                self.source,
                expected_source_sha256=self.source_sha256,
                output_dir=destination,
                corpus_root=self.corpus_root,
            )
        self.assertFalse(destination.exists())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
