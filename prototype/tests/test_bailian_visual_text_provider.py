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
import unittest
from base64 import b64decode
from io import BytesIO

import httpx
from app.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
    DEFAULT_BAILIAN_OCR_SEED,
    DEFAULT_BAILIAN_OCR_TEMPERATURE,
    BailianVisualTextProvider,
)
from app.adapters.documents.visual_text_provider_support import (
    GroundedVisualTextCandidate,
    build_visual_text_snapshot,
)
from app.domain.extraction.visual_text import VisualTextPageInput, VisualTextSnapshot
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.services.visual_text_snapshot_cache import VisualTextProviderTelemetryV1
from PIL import Image as PILImage


class BailianVisualTextProviderTests(unittest.TestCase):
    def test_advanced_recognition_maps_pixel_polygon_without_fake_confidence(
        self,
    ) -> None:
        client = _Client(
            _Response(
                200,
                {
                    "output": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": [
                                        {
                                            "ocr_result": {
                                                "words_info": [
                                                    {
                                                        "text": "grounded line",
                                                        "location": [
                                                            40,
                                                            60,
                                                            240,
                                                            60,
                                                            240,
                                                            120,
                                                            40,
                                                            120,
                                                        ],
                                                    }
                                                ]
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    "request_id": "safe-request-id",
                },
            )
        )
        provider = _provider(client)

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.provider_id, BAILIAN_VISUAL_TEXT_PROVIDER_ID)
        self.assertIn("model=qwen3.5-ocr", snapshot.provider_version)
        self.assertIn("task=advanced_recognition", snapshot.provider_version)
        self.assertIn("auto_rotate=1", snapshot.provider_version)
        self.assertIn("s=1234", snapshot.provider_version)
        self.assertIn("t=0.01", snapshot.provider_version)
        self.assertIn("x=pb2", snapshot.provider_version)
        self.assertIn("c=orig", snapshot.provider_version)
        self.assertLessEqual(len(snapshot.provider_version), 160)
        self.assertEqual(len(snapshot.observations), 1)
        observation = snapshot.observations[0]
        self.assertEqual(observation.raw_text, "grounded line")
        self.assertIsNone(observation.confidence)
        self.assertEqual(observation.bbox, (20.0, 30.0, 120.0, 60.0))
        receipt = observation.geometry_receipt
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(
            receipt.provider_quadrilateral_px,
            ((40.0, 60.0), (240.0, 60.0), (240.0, 120.0), (40.0, 120.0)),
        )
        self.assertEqual(
            receipt.original_page_quadrilateral_px,
            receipt.provider_quadrilateral_px,
        )
        self.assertEqual(receipt.provider_image_size_px, (400, 600))
        self.assertEqual(receipt.original_page_image_size_px, (400, 600))
        self.assertEqual(receipt.input_rotation_degrees, 0)
        self.assertEqual(receipt.transform_version, "pb2")
        self.assertEqual(receipt.target_bbox, observation.bbox)
        self.assertEqual(
            receipt.validation_status,
            "GEOMETRY_TRANSFORM_VALIDATED",
        )
        request = client.calls[0]
        self.assertTrue(
            request[0].endswith(
                "/api/v1/services/aigc/multimodal-generation/generation"
            )
        )
        payload = request[2]["json"]
        self.assertEqual(
            payload["parameters"]["ocr_options"]["task"], "advanced_recognition"
        )
        self.assertEqual(payload["parameters"]["seed"], DEFAULT_BAILIAN_OCR_SEED)
        self.assertEqual(
            payload["parameters"]["temperature"],
            DEFAULT_BAILIAN_OCR_TEMPERATURE,
        )
        self.assertNotIn("top_p", payload["parameters"])
        self.assertIs(
            payload["input"]["messages"][0]["content"][0]["enable_rotate"],
            True,
        )
        image_value = payload["input"]["messages"][0]["content"][0]["image"]
        self.assertTrue(image_value.startswith("data:image/png;base64,"))
        self.assertNotIn("approved PDF bytes", json.dumps(payload))

    def test_response_requires_finish_reason_exactly_stop(self) -> None:
        missing = object()
        for finish_reason in (missing, None, "length", "STOP", " stop"):
            with self.subTest(finish_reason=finish_reason):
                payload = _payload_with_words(
                    ("line", (40, 60, 240, 60, 240, 120, 40, 120)),
                )
                choice = payload["output"]["choices"][0]
                if finish_reason is missing:
                    choice.pop("finish_reason")
                else:
                    choice["finish_reason"] = finish_reason
                provider = _provider(_Client(_Response(200, payload)))

                with self.assertRaises(VisualTextProviderRejected) as error:
                    provider.recognize_page(_page_input())

                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )

    def test_provider_quadrilateral_must_be_unique_simple_convex_and_clockwise(
        self,
    ) -> None:
        invalid_locations = {
            "duplicate point": (40, 60, 240, 60, 240, 120, 40, 60),
            "counterclockwise": (40, 60, 40, 120, 240, 120, 240, 60),
            "self intersecting": (40, 60, 240, 120, 240, 60, 40, 120),
            "concave": (40, 60, 240, 60, 100, 80, 40, 120),
            "zero area": (40, 60, 100, 80, 160, 100, 220, 120),
        }
        for case_name, location in invalid_locations.items():
            with self.subTest(case_name=case_name):
                payload = _payload_with_words(("line", location))
                payload["output"]["choices"][0]["finish_reason"] = "stop"
                provider = _provider(_Client(_Response(200, payload)))

                with self.assertRaises(VisualTextProviderRejected) as error:
                    provider.recognize_page(_page_input())

                self.assertEqual(
                    error.exception.code,
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                )

    def test_rotation_policy_is_configurable_and_changes_snapshot_identity(
        self,
    ) -> None:
        enabled = _provider(_Client(_Response(200, _empty_payload())))
        disabled_client = _Client(_Response(200, _empty_payload()))
        disabled = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            http_client=disabled_client,
            sleep=lambda _: None,
        )

        enabled_snapshot = enabled.recognize_page(_page_input())
        disabled_snapshot = disabled.recognize_page(_page_input())

        self.assertNotEqual(
            enabled_snapshot.provider_version,
            disabled_snapshot.provider_version,
        )
        self.assertNotEqual(
            enabled_snapshot.visual_text_snapshot_id,
            disabled_snapshot.visual_text_snapshot_id,
        )
        self.assertIn("auto_rotate=0", disabled_snapshot.provider_version)
        disabled_payload = disabled_client.calls[0][2]["json"]
        self.assertIs(
            disabled_payload["input"]["messages"][0]["content"][0]["enable_rotate"],
            False,
        )

        with self.assertRaises(TypeError):
            BailianVisualTextProvider(
                api_key="test-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                enable_rotate=1,  # type: ignore[arg-type]
            )

    def test_seed_is_validated_and_bound_to_request_and_snapshot_identity(
        self,
    ) -> None:
        default_client = _Client(_Response(200, _empty_payload()))
        explicit_client = _Client(_Response(200, _empty_payload()))
        changed_client = _Client(_Response(200, _empty_payload()))
        default = _provider(default_client)
        explicit = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            seed=1234,
            http_client=explicit_client,
            sleep=lambda _: None,
        )
        changed = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            seed=4321,
            http_client=changed_client,
            sleep=lambda _: None,
        )

        default_snapshot = default.recognize_page(_page_input())
        explicit_snapshot = explicit.recognize_page(_page_input())
        changed_snapshot = changed.recognize_page(_page_input())

        self.assertEqual(default.provider_version, explicit.provider_version)
        self.assertEqual(
            default_snapshot.visual_text_snapshot_id,
            explicit_snapshot.visual_text_snapshot_id,
        )
        self.assertNotEqual(default.provider_version, changed.provider_version)
        self.assertNotEqual(
            default_snapshot.visual_text_snapshot_id,
            changed_snapshot.visual_text_snapshot_id,
        )
        self.assertEqual(default_client.calls[0][2]["json"]["parameters"]["seed"], 1234)
        self.assertEqual(changed_client.calls[0][2]["json"]["parameters"]["seed"], 4321)

        for invalid_seed in (True, -1, 2**31, 1.0, "1234"):
            with self.subTest(seed=invalid_seed):
                with self.assertRaises(ValueError):
                    BailianVisualTextProvider(
                        api_key="test-key",
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        seed=invalid_seed,  # type: ignore[arg-type]
                    )

    def test_temperature_is_validated_and_bound_to_request_and_snapshot_identity(
        self,
    ) -> None:
        default_client = _Client(_Response(200, _empty_payload()))
        explicit_client = _Client(_Response(200, _empty_payload()))
        changed_client = _Client(_Response(200, _empty_payload()))
        default = _provider(default_client)
        explicit = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.01,
            http_client=explicit_client,
            sleep=lambda _: None,
        )
        changed = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.25,
            http_client=changed_client,
            sleep=lambda _: None,
        )

        default_snapshot = default.recognize_page(_page_input())
        explicit_snapshot = explicit.recognize_page(_page_input())
        changed_snapshot = changed.recognize_page(_page_input())

        self.assertEqual(default.provider_version, explicit.provider_version)
        self.assertEqual(
            default_snapshot.visual_text_snapshot_id,
            explicit_snapshot.visual_text_snapshot_id,
        )
        self.assertNotEqual(default.provider_version, changed.provider_version)
        self.assertNotEqual(
            default_snapshot.visual_text_snapshot_id,
            changed_snapshot.visual_text_snapshot_id,
        )
        self.assertEqual(
            default_client.calls[0][2]["json"]["parameters"]["temperature"],
            0.01,
        )
        self.assertEqual(
            changed_client.calls[0][2]["json"]["parameters"]["temperature"],
            0.25,
        )

        for invalid_temperature in (True, -0.01, 2.0, float("nan"), float("inf"), "0"):
            with self.subTest(temperature=invalid_temperature):
                with self.assertRaises(ValueError):
                    BailianVisualTextProvider(
                        api_key="test-key",
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        temperature=invalid_temperature,  # type: ignore[arg-type]
                    )

    def test_explicit_clockwise_90_rotation_sends_rotated_png_and_inverts_polygons(
        self,
    ) -> None:
        page_input = _png_page_input(
            page_number=3,
            page_bbox=(10.0, 20.0, 210.0, 320.0),
        )
        client = _Client(
            _Response(
                200,
                _payload_with_words(
                    ("full page", (0, 0, 60, 0, 60, 40, 0, 40)),
                    ("internal", (54, 4, 54, 24, 48, 24, 48, 4)),
                ),
            )
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            input_rotations_by_page={3: 90},
            http_client=client,
            sleep=lambda _: None,
        )

        snapshot = provider.recognize_page(page_input)

        payload = client.calls[0][2]["json"]
        image_part = payload["input"]["messages"][0]["content"][0]
        self.assertIs(image_part["enable_rotate"], False)
        encoded_png = image_part["image"].split(",", 1)[1]
        with (
            PILImage.open(BytesIO(page_input.page_image_bytes)) as original,
            PILImage.open(BytesIO(b64decode(encoded_png))) as sent,
        ):
            expected = original.transpose(PILImage.Transpose.ROTATE_270)
            self.assertEqual(sent.size, (60, 40))
            self.assertEqual(sent.tobytes(), expected.tobytes())

        self.assertEqual(snapshot.page_image_sha256, page_input.page_image_sha256)
        self.assertEqual(snapshot.page_image_width_px, 40)
        self.assertEqual(snapshot.page_image_height_px, 60)
        self.assertEqual(snapshot.observations[0].bbox, page_input.page_bbox)
        self.assertEqual(
            snapshot.observations[1].bbox,
            (30.0, 50.0, 130.0, 80.0),
        )
        receipt = snapshot.observations[1].geometry_receipt
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(
            receipt.provider_quadrilateral_px,
            ((54.0, 4.0), (54.0, 24.0), (48.0, 24.0), (48.0, 4.0)),
        )
        self.assertEqual(
            receipt.original_page_quadrilateral_px,
            ((4.0, 6.0), (24.0, 6.0), (24.0, 12.0), (4.0, 12.0)),
        )
        self.assertEqual(receipt.provider_image_size_px, (60, 40))
        self.assertEqual(receipt.original_page_image_size_px, (40, 60))
        self.assertEqual(receipt.input_rotation_degrees, 90)
        serialized = snapshot.model_dump(mode="json")
        serialized_receipt = serialized["observations"][1]["geometry_receipt"]
        self.assertEqual(
            serialized_receipt["validation_status"],
            "GEOMETRY_TRANSFORM_VALIDATED",
        )
        self.assertEqual(VisualTextSnapshot.model_validate(serialized), snapshot)

    def test_geometry_receipt_is_optional_for_existing_provider_candidates(
        self,
    ) -> None:
        page_input = _page_input()
        snapshot = build_visual_text_snapshot(
            page_input=page_input,
            provider_id="existing-provider",
            provider_version="existing-version",
            candidates=(
                GroundedVisualTextCandidate(
                    raw_text="existing line",
                    bbox=(20.0, 30.0, 120.0, 60.0),
                    confidence=None,
                ),
            ),
        )

        self.assertIsNone(snapshot.observations[0].geometry_receipt)
        serialized = snapshot.model_dump(mode="json")
        self.assertNotIn("geometry_receipt", serialized["observations"][0])
        self.assertEqual(VisualTextSnapshot.model_validate(serialized), snapshot)

    def test_geometry_receipt_rejects_transform_or_status_tampering(self) -> None:
        snapshot = _provider(
            _Client(
                _Response(
                    200,
                    _payload_with_words(
                        ("line", (40, 60, 240, 60, 240, 120, 40, 120)),
                    ),
                )
            )
        ).recognize_page(_page_input())
        serialized = snapshot.model_dump(mode="json")

        tampered_polygon = json.loads(json.dumps(serialized))
        tampered_polygon["observations"][0]["geometry_receipt"][
            "original_page_quadrilateral_px"
        ][0][0] = 41.0
        with self.assertRaises(ValueError):
            VisualTextSnapshot.model_validate(tampered_polygon)

        overstated_status = json.loads(json.dumps(serialized))
        overstated_status["observations"][0]["geometry_receipt"][
            "validation_status"
        ] = "SEMANTICALLY_ACCURATE"
        with self.assertRaises(ValueError):
            VisualTextSnapshot.model_validate(overstated_status)

    def test_explicit_180_and_270_rotations_invert_internal_polygons(self) -> None:
        examples = (
            (180, (36, 54, 16, 54, 16, 48, 36, 48), (40, 60)),
            (270, (6, 36, 6, 16, 12, 16, 12, 36), (60, 40)),
        )
        for rotation, location, expected_sent_size in examples:
            with self.subTest(rotation=rotation):
                page_input = _png_page_input()
                client = _Client(
                    _Response(
                        200,
                        _payload_with_words(("internal", location)),
                    )
                )
                provider = BailianVisualTextProvider(
                    api_key="test-key",
                    base_url=("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                    enable_rotate=False,
                    input_rotations_by_page={3: rotation},
                    http_client=client,
                    sleep=lambda _: None,
                )

                snapshot = provider.recognize_page(page_input)

                self.assertEqual(
                    snapshot.observations[0].bbox,
                    (20.0, 30.0, 120.0, 60.0),
                )
                payload = client.calls[0][2]["json"]
                encoded_png = payload["input"]["messages"][0]["content"][0][
                    "image"
                ].split(",", 1)[1]
                with PILImage.open(BytesIO(b64decode(encoded_png))) as sent:
                    self.assertEqual(sent.size, expected_sent_size)

    def test_explicit_rotation_rejects_out_of_bounds_provider_polygon(self) -> None:
        client = _Client(
            _Response(
                200,
                _payload_with_words(
                    ("outside", (48, 4, 61, 4, 61, 24, 48, 24)),
                ),
            )
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            input_rotations_by_page={3: 90},
            http_client=client,
            sleep=lambda _: None,
        )

        with self.assertRaises(VisualTextProviderRejected) as error:
            provider.recognize_page(_png_page_input())

        self.assertEqual(
            error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )

    def test_input_rotation_configuration_rejects_invalid_values_and_double_rotation(
        self,
    ) -> None:
        invalid_maps: tuple[object, ...] = (
            [],
            {True: 90},
            {0: 90},
            {1.5: 90},
            {1: True},
            {1: 45},
        )
        for rotation_map in invalid_maps:
            with self.subTest(rotation_map=rotation_map):
                with self.assertRaises((TypeError, ValueError)):
                    BailianVisualTextProvider(
                        api_key="test-key",
                        base_url=("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                        input_rotations_by_page=rotation_map,  # type: ignore[arg-type]
                    )

        with self.assertRaises(ValueError):
            BailianVisualTextProvider(
                api_key="test-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                enable_rotate=True,
                input_rotations_by_page={3: 90},
            )

        zero_rotation = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=True,
            input_rotations_by_page={3: 0},
        )
        self.assertIn("auto_rotate=1", zero_rotation.provider_version)

    def test_provider_version_binds_complete_rotation_map_in_canonical_order(
        self,
    ) -> None:
        first = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            input_rotations_by_page={4: 270, 3: 90},
        )
        reordered = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            input_rotations_by_page={3: 90, 4: 270},
        )
        changed_page = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            enable_rotate=False,
            input_rotations_by_page={2: 90, 4: 270},
        )

        self.assertEqual(first.provider_version, reordered.provider_version)
        self.assertNotEqual(first.provider_version, changed_page.provider_version)
        self.assertRegex(first.provider_version, r";rot=[0-9a-f]{64};")
        self.assertLessEqual(len(first.provider_version), 160)

        longest_valid_model = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="m" * 96,
            enable_rotate=False,
            input_rotations_by_page={3: 90},
        )
        self.assertIn(";cfg=", longest_valid_model.provider_version)
        self.assertLessEqual(len(longest_valid_model.provider_version), 160)
        changed_seed_long_model = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="m" * 96,
            enable_rotate=False,
            input_rotations_by_page={3: 90},
            seed=4321,
        )
        self.assertNotEqual(
            longest_valid_model.provider_version,
            changed_seed_long_model.provider_version,
        )
        changed_temperature_long_model = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="m" * 96,
            enable_rotate=False,
            input_rotations_by_page={3: 90},
            temperature=0.25,
        )
        self.assertNotEqual(
            longest_valid_model.provider_version,
            changed_temperature_long_model.provider_version,
        )

    def test_hashes_are_recomputed_before_any_remote_call(self) -> None:
        client = _Client(_Response(200, _empty_payload()))
        provider = _provider(client)
        with self.assertRaises(VisualTextProviderRejected) as source_error:
            provider.recognize_page(_page_input(source_sha256="0" * 64))
        self.assertEqual(
            source_error.exception.code,
            VisualTextProviderRejectionCode.SOURCE_HASH_MISMATCH,
        )
        with self.assertRaises(VisualTextProviderRejected) as image_error:
            provider.recognize_page(_page_input(page_image_sha256="1" * 64))
        self.assertEqual(
            image_error.exception.code,
            VisualTextProviderRejectionCode.PAGE_IMAGE_HASH_MISMATCH,
        )
        self.assertEqual(client.calls, [])

    def test_missing_or_non_ascii_key_is_unavailable(self) -> None:
        for key in ("", "密钥"):
            provider = BailianVisualTextProvider(
                api_key=key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                http_client=_Client(_Response(200, _empty_payload())),
            )
            with self.assertRaises(VisualTextProviderRejected) as error:
                provider.recognize_page(_page_input())
            self.assertEqual(
                error.exception.code,
                VisualTextProviderRejectionCode.UNAVAILABLE,
            )

    def test_unapproved_dashscope_origin_is_rejected_before_http(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved DashScope"):
            BailianVisualTextProvider(
                api_key="test-key",
                base_url="https://example.test/compatible-mode/v1",
                http_client=_Client(_Response(200, _empty_payload())),
            )

    def test_retryable_status_has_a_two_call_hard_cap(self) -> None:
        sleeps: list[float] = []
        client = _Client(
            _Response(429, {"message": "customer confidential text"}),
            _Response(200, _empty_payload()),
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=1,
            http_client=client,
            sleep=sleeps.append,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.observations, ())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [0.5])

        rate_limited = _Client(_Response(429, {"message": "private"}))
        with self.assertRaises(VisualTextProviderRejected) as rate_error:
            _provider(rate_limited).recognize_page(_page_input())
        self.assertEqual(
            rate_error.exception.code,
            VisualTextProviderRejectionCode.RATE_LIMITED,
        )
        self.assertNotIn("private", str(rate_error.exception))

    def test_transport_errors_are_retried_but_local_errors_are_not(self) -> None:
        sleeps: list[float] = []
        request = httpx.Request("POST", "https://dashscope.aliyuncs.com")
        retrying_client = _Client(
            httpx.ConnectError("temporary", request=request),
            _Response(200, _empty_payload()),
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=1,
            http_client=retrying_client,
            sleep=sleeps.append,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.observations, ())
        self.assertEqual(len(retrying_client.calls), 2)
        self.assertEqual(sleeps, [0.5])

        local_error_client = _Client(RuntimeError("client programming error"))
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=1,
            http_client=local_error_client,
            sleep=sleeps.append,
        )
        with self.assertRaises(VisualTextProviderRejected) as local_error:
            provider.recognize_page(_page_input())
        self.assertEqual(
            local_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertNotIn("programming error", str(local_error.exception))
        self.assertEqual(len(local_error_client.calls), 1)

    def test_retry_shares_absolute_deadline_and_emits_exact_telemetry(self) -> None:
        clock = _ManualMonotonic()
        events: list[VisualTextProviderTelemetryV1] = []
        client = _Client(
            _Response(503, {"message": "temporary"}),
            _Response(200, _empty_payload()),
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout_seconds=2.0,
            max_retries=1,
            http_client=client,
            sleep=clock.advance,
            monotonic=clock,
            telemetry_callback=events.append,
        )

        snapshot = provider.recognize_page(_page_input())

        self.assertEqual(snapshot.observations, ())
        self.assertEqual([call[2]["timeout"] for call in client.calls], [2.0, 1.5])
        self.assertEqual(
            events,
            [
                VisualTextProviderTelemetryV1(
                    page_number=3,
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                    provider_version=provider.provider_version,
                    request_count=2,
                    latency_ms=500,
                    cache_hit=False,
                    outcome="READY",
                )
            ],
        )
        self.assertIn("dl=2", provider.provider_version)
        self.assertIn("tr=1", provider.provider_version)
        self.assertIn("cap=2", provider.provider_version)

    def test_deadline_and_failed_telemetry_do_not_reset_or_mask_result(self) -> None:
        clock = _ManualMonotonic()
        events: list[VisualTextProviderTelemetryV1] = []
        client = _AdvancingClient(
            clock,
            1.1,
            _Response(503, {"message": "temporary"}),
            _Response(200, _empty_payload()),
        )
        provider = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout_seconds=1.0,
            max_retries=1,
            http_client=client,
            sleep=clock.advance,
            monotonic=clock,
            telemetry_callback=events.append,
        )

        with self.assertRaises(VisualTextProviderRejected):
            provider.recognize_page(_page_input())

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].request_count, 1)
        self.assertEqual(events[0].outcome, "FAILED")

        callback_failed = BailianVisualTextProvider(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            http_client=_Client(_Response(200, _empty_payload())),
            telemetry_callback=lambda _event: (_ for _ in ()).throw(
                RuntimeError("telemetry unavailable")
            ),
        )
        with self.assertLogs(
            "app.services.visual_text_snapshot_cache",
            level="WARNING",
        ) as observed_logs:
            self.assertEqual(
                callback_failed.recognize_page(_page_input()).observations,
                (),
            )
        self.assertNotIn(
            "telemetry unavailable",
            "\n".join(observed_logs.output),
        )

        with self.assertRaises(ValueError):
            BailianVisualTextProvider(
                api_key="test-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                max_retries=2,
            )

    def test_non_retryable_http_and_invalid_output_are_not_retried(self) -> None:
        bad_request_client = _Client(
            _Response(400, {"message": "private"}),
            _Response(200, _empty_payload()),
        )
        with self.assertRaises(VisualTextProviderRejected) as http_error:
            _provider(bad_request_client).recognize_page(_page_input())
        self.assertEqual(
            http_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
        )
        self.assertEqual(len(bad_request_client.calls), 1)

        invalid_output_client = _Client(
            _Response(200, {"output": {"choices": []}}),
            _Response(200, _empty_payload()),
        )
        with self.assertRaises(VisualTextProviderRejected) as output_error:
            _provider(invalid_output_client).recognize_page(_page_input())
        self.assertEqual(
            output_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertEqual(len(invalid_output_client.calls), 1)

    def test_authorization_and_invalid_output_fail_closed_without_response_text(
        self,
    ) -> None:
        unauthorized = _provider(
            _Client(_Response(401, {"message": "customer secret"}))
        )
        with self.assertRaises(VisualTextProviderRejected) as auth_error:
            unauthorized.recognize_page(_page_input())
        self.assertEqual(
            auth_error.exception.code,
            VisualTextProviderRejectionCode.UNAVAILABLE,
        )
        self.assertNotIn("customer", str(auth_error.exception))

        invalid = _provider(
            _Client(
                _Response(
                    200,
                    {
                        "output": {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {
                                        "content": [
                                            {
                                                "ocr_result": {
                                                    "words_info": [
                                                        {
                                                            "text": "outside",
                                                            "location": [
                                                                0,
                                                                0,
                                                                500,
                                                                0,
                                                                500,
                                                                10,
                                                                0,
                                                                10,
                                                            ],
                                                        }
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    },
                )
            )
        )
        with self.assertRaises(VisualTextProviderRejected) as output_error:
            invalid.recognize_page(_page_input())
        self.assertEqual(
            output_error.exception.code,
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
        )
        self.assertNotIn("outside", str(output_error.exception))


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, *responses: _Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs["headers"], kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AdvancingClient(_Client):
    def __init__(
        self,
        clock: _ManualMonotonic,
        seconds_per_call: float,
        *responses: _Response | Exception,
    ) -> None:
        super().__init__(*responses)
        self._clock = clock
        self._seconds_per_call = seconds_per_call

    def post(self, url: str, **kwargs: object) -> _Response:
        response = super().post(url, **kwargs)
        self._clock.advance(self._seconds_per_call)
        return response


def _provider(client: _Client) -> BailianVisualTextProvider:
    return BailianVisualTextProvider(
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=client,
        sleep=lambda _: None,
    )


def _empty_payload() -> dict[str, object]:
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": [{"ocr_result": {"words_info": []}}]},
                }
            ]
        }
    }


def _payload_with_words(
    *items: tuple[str, tuple[int, ...]],
) -> dict[str, object]:
    return {
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            {
                                "ocr_result": {
                                    "words_info": [
                                        {"text": text, "location": list(location)}
                                        for text, location in items
                                    ]
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }


def _page_input(
    *,
    source_sha256: str | None = None,
    page_image_sha256: str | None = None,
) -> VisualTextPageInput:
    source_bytes = b"approved PDF bytes"
    page_image_bytes = b"\x89PNG\r\n\x1a\nin-memory page"
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=source_sha256 or hashlib.sha256(source_bytes).hexdigest(),
        page_number=3,
        page_bbox=(0.0, 0.0, 200.0, 300.0),
        page_image_bytes=page_image_bytes,
        page_image_sha256=(
            page_image_sha256 or hashlib.sha256(page_image_bytes).hexdigest()
        ),
        page_image_width_px=400,
        page_image_height_px=600,
    )


def _png_page_input(
    *,
    page_number: int = 3,
    page_bbox: tuple[float, float, float, float] = (0.0, 0.0, 200.0, 300.0),
) -> VisualTextPageInput:
    source_bytes = b"approved PDF bytes"
    image = PILImage.new("RGB", (40, 60), "white")
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((39, 0), (0, 255, 0))
    image.putpixel((39, 59), (0, 0, 255))
    image.putpixel((0, 59), (255, 255, 0))
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    page_image_bytes = output.getvalue()
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_bytes=page_image_bytes,
        page_image_sha256=hashlib.sha256(page_image_bytes).hexdigest(),
        page_image_width_px=40,
        page_image_height_px=60,
    )


if __name__ == "__main__":
    unittest.main()
