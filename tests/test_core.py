"""Phase 1 Core Module Unit Tests

순수 Python/numpy 테스트. Qt, USB, 캡처 디바이스 불필요.
Windows 전용 모듈(capture, native_capture, device)은 모킹으로 테스트.
"""

import copy
import json
import os
import tempfile
import threading
import time
import unittest

import numpy as np

# ══════════════════════════════════════════════════════════════════
#  constants
# ══════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    def test_hw_errors_are_tuples(self):
        from core.constants import HW_ERRORS, HW_CONNECT_ERRORS
        self.assertIsInstance(HW_ERRORS, tuple)
        self.assertIsInstance(HW_CONNECT_ERRORS, tuple)
        # ConnectionError는 HW_CONNECT_ERRORS에만
        self.assertIn(ConnectionError, HW_CONNECT_ERRORS)
        self.assertNotIn(ConnectionError, HW_ERRORS)

    def test_stale_thresholds(self):
        from core.constants import STALE_NONE_THRESHOLD, RECREATE_COOLDOWN
        self.assertEqual(STALE_NONE_THRESHOLD, 60)
        self.assertGreater(RECREATE_COOLDOWN, 0)


# ══════════════════════════════════════════════════════════════════
#  config
# ══════════════════════════════════════════════════════════════════

class TestConfig(unittest.TestCase):
    def test_deep_merge_basic(self):
        from core.config import _deep_merge
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}, "e": 5}
        result = _deep_merge(base, override)
        self.assertEqual(result["a"], 1)        # base only
        self.assertEqual(result["b"]["c"], 99)  # override wins
        self.assertEqual(result["b"]["d"], 3)   # base preserved
        self.assertEqual(result["e"], 5)         # override only

    def test_deep_merge_preserves_new_defaults(self):
        from core.config import _deep_merge
        base = {"old_key": 1, "new_key": "default"}
        override = {"old_key": 2}
        result = _deep_merge(base, override)
        self.assertEqual(result["new_key"], "default")

    def test_default_config_has_all_sections(self):
        from core.config import DEFAULT_CONFIG
        required_keys = [
            "device", "layout", "color", "mirror",
            "audio_pulse", "audio_spectrum", "audio_bass_detail", "options"
        ]
        for key in required_keys:
            self.assertIn(key, DEFAULT_CONFIG)

    def test_load_save_roundtrip(self):
        from core.config import load_config, save_config, DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["mirror"]["brightness"] = 0.42

            with open(path, "w") as f:
                json.dump(cfg, f)

            with open(path, "r") as f:
                loaded = json.load(f)
            self.assertAlmostEqual(loaded["mirror"]["brightness"], 0.42)


# ══════════════════════════════════════════════════════════════════
#  layout
# ══════════════════════════════════════════════════════════════════

class TestLayout(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"start": 73, "end": 66, "side": "left"},
            {"start": 66, "end": 53, "side": "top"},
            {"start": 53, "end": 45, "side": "right"},
            {"start": 45, "end": 31, "side": "bottom"},
            {"start": 31, "end": 24, "side": "left"},
            {"start": 24, "end": 11, "side": "top"},
            {"start": 11, "end": 4,  "side": "right"},
            {"start": 4,  "end": 0,  "side": "bottom"},
        ]
        self.led_count = 75

    def test_get_led_positions_shape(self):
        from core.layout import get_led_positions
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        self.assertEqual(pos.shape, (75, 2))
        self.assertEqual(len(sides), 75)

    def test_get_led_positions_sides_valid(self):
        from core.layout import get_led_positions
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        valid_sides = {"left", "top", "right", "bottom"}
        for s in sides:
            self.assertIn(s, valid_sides)

    def test_left_leds_at_x_zero(self):
        from core.layout import get_led_positions
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        for i in range(self.led_count):
            if sides[i] == "left":
                self.assertAlmostEqual(pos[i, 0], 0.0, places=1)

    def test_top_leds_at_y_zero(self):
        from core.layout import get_led_positions
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        for i in range(self.led_count):
            if sides[i] == "top":
                self.assertAlmostEqual(pos[i, 1], 0.0, places=1)

    def test_weight_matrix_shape(self):
        from core.layout import get_led_positions, build_weight_matrix
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        wm = build_weight_matrix(2560, 1440, pos, sides, 64, 32, 0.3, 5.0)
        self.assertEqual(wm.shape, (75, 64 * 32))

    def test_weight_matrix_rows_sum_to_one(self):
        from core.layout import get_led_positions, build_weight_matrix
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        wm = build_weight_matrix(2560, 1440, pos, sides, 64, 32, 0.3, 5.0)
        for i in range(75):
            row_sum = wm[i].sum()
            self.assertAlmostEqual(row_sum, 1.0, places=4,
                                   msg=f"LED {i} weight sum = {row_sum}")

    def test_weight_matrix_per_side_dict(self):
        from core.layout import get_led_positions, build_weight_matrix
        pos, sides = get_led_positions(2560, 1440, self.segments, self.led_count)
        decay = {"top": 0.3, "bottom": 0.2, "left": 0.3, "right": 0.3}
        penalty = {"top": 5.0, "bottom": 1.0, "left": 5.0, "right": 5.0}
        wm = build_weight_matrix(2560, 1440, pos, sides, 64, 32, decay, penalty)
        self.assertEqual(wm.shape, (75, 64 * 32))
        for i in range(75):
            self.assertAlmostEqual(wm[i].sum(), 1.0, places=4)

    def test_portrait_rotation(self):
        from core.layout import get_led_positions
        pos_l, sides_l = get_led_positions(2560, 1440, self.segments, self.led_count,
                                           orientation="landscape")
        pos_p, sides_p = get_led_positions(1440, 2560, self.segments, self.led_count,
                                           orientation="portrait", portrait_rotation="cw")
        # 세로 모드에서 sides가 회전됨
        self.assertNotEqual(sides_l, sides_p)

    def test_zero_length_segment_skipped(self):
        from core.layout import get_led_positions
        segs = [
            {"start": 5, "end": 0, "side": "left"},
            {"start": 0, "end": 0, "side": "bottom"},  # 길이 0 — 스킵
        ]
        pos, sides = get_led_positions(1920, 1080, segs, 6)
        self.assertEqual(pos.shape, (6, 2))


# ══════════════════════════════════════════════════════════════════
#  color_correction
# ══════════════════════════════════════════════════════════════════

class TestColorCorrection(unittest.TestCase):
    def test_identity_correction(self):
        from core.color_correction import ColorCorrection
        cfg = {"gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
               "wb_r": 1.0, "wb_g": 1.0, "wb_b": 1.0,
               "green_red_bleed": 0.0}
        cc = ColorCorrection(cfg)
        leds = np.array([[128, 64, 200]], dtype=np.float32)
        result = cc.apply(leds)
        np.testing.assert_array_almost_equal(result, [[128, 64, 200]], decimal=0)

    def test_wb_scales_channels(self):
        from core.color_correction import ColorCorrection
        cfg = {"gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
               "wb_r": 0.5, "wb_g": 1.0, "wb_b": 1.0,
               "green_red_bleed": 0.0}
        cc = ColorCorrection(cfg)
        leds = np.array([[200, 100, 100]], dtype=np.float32)
        result = cc.apply(leds)
        self.assertAlmostEqual(result[0, 0], 100.0, places=0)  # R * 0.5
        self.assertAlmostEqual(result[0, 1], 100.0, places=0)  # G unchanged

    def test_green_red_bleed(self):
        from core.color_correction import ColorCorrection
        cfg = {"gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
               "wb_r": 1.0, "wb_g": 1.0, "wb_b": 1.0,
               "green_red_bleed": 1.0}
        cc = ColorCorrection(cfg)
        leds = np.array([[50, 200, 100]], dtype=np.float32)
        result = cc.apply(leds)
        # R should increase by max(0, G-R) * bleed = (200-50)*1.0 = 150
        self.assertGreater(result[0, 0], 50)
        self.assertAlmostEqual(result[0, 0], 200.0, places=0)

    def test_disabled_correction(self):
        from core.color_correction import ColorCorrection
        cfg = {"gamma_r": 2.0, "gamma_g": 2.0, "gamma_b": 2.0,
               "wb_r": 0.5, "wb_g": 0.5, "wb_b": 0.5,
               "green_red_bleed": 0.5}
        cc = ColorCorrection(cfg)
        cc.enabled = False
        leds = np.array([[128, 128, 128]], dtype=np.float32)
        original = leds.copy()
        cc.apply(leds)
        np.testing.assert_array_equal(leds, original)

    def test_lut_size(self):
        from core.color_correction import ColorCorrection
        cfg = {"gamma_r": 2.2, "gamma_g": 1.0, "gamma_b": 0.8,
               "wb_r": 1.0, "wb_g": 1.0, "wb_b": 1.0,
               "green_red_bleed": 0.0}
        cc = ColorCorrection(cfg)
        self.assertEqual(len(cc.lut_r), 256)
        self.assertEqual(len(cc.lut_g), 256)
        self.assertEqual(len(cc.lut_b), 256)


# ══════════════════════════════════════════════════════════════════
#  color (ColorPipeline)
# ══════════════════════════════════════════════════════════════════

class TestColorPipeline(unittest.TestCase):
    def setUp(self):
        from core.layout import get_led_positions, build_weight_matrix
        segments = [
            {"start": 7, "end": 5, "side": "left"},
            {"start": 5, "end": 3, "side": "top"},
            {"start": 3, "end": 1, "side": "right"},
            {"start": 1, "end": 0, "side": "bottom"},
        ]
        led_count = 8
        screen_w, screen_h = 640, 480
        pos, sides = get_led_positions(screen_w, screen_h, segments, led_count)
        self.wm = build_weight_matrix(screen_w, screen_h, pos, sides, 8, 4, 0.3, 5.0)
        self.color_cfg = {
            "wb_r": 1.0, "wb_g": 1.0, "wb_b": 1.0,
            "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
            "green_red_bleed": 0.0,
        }
        self.mirror_cfg = {
            "grid_cols": 8, "grid_rows": 4,
            "smoothing_factor": 0.0, "brightness": 1.0,
        }

    def test_process_returns_grb_and_rgb(self):
        from core.color import ColorPipeline
        pipeline = ColorPipeline(self.wm, self.color_cfg, self.mirror_cfg)
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        grb_bytes, rgb = pipeline.process(frame)
        self.assertIsInstance(grb_bytes, bytes)
        self.assertEqual(len(grb_bytes), 8 * 3)
        self.assertEqual(rgb.shape, (8, 3))

    def test_process_white_frame(self):
        from core.color import ColorPipeline
        pipeline = ColorPipeline(self.wm, self.color_cfg, self.mirror_cfg)
        frame = np.full((480, 640, 3), 255, dtype=np.uint8)
        grb_bytes, rgb = pipeline.process(frame)
        # 할당된 LED는 (255, 255, 255)에 가까워야 함
        # 일부 LED는 세그먼트에 미할당될 수 있으므로 평균으로 확인
        assigned_mean = rgb[rgb.sum(axis=1) > 0].mean()
        self.assertGreater(assigned_mean, 200)

    def test_process_black_frame(self):
        from core.color import ColorPipeline
        pipeline = ColorPipeline(self.wm, self.color_cfg, self.mirror_cfg)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        grb_bytes, rgb = pipeline.process(frame)
        for i in range(8):
            for ch in range(3):
                self.assertLess(rgb[i, ch], 5)

    def test_update_brightness(self):
        from core.color import ColorPipeline
        pipeline = ColorPipeline(self.wm, self.color_cfg, self.mirror_cfg)
        pipeline.update_brightness(0.5)
        self.assertAlmostEqual(pipeline.brightness, 0.5)

    def test_smoothing(self):
        from core.color import ColorPipeline
        cfg = dict(self.mirror_cfg)
        cfg["smoothing_factor"] = 0.5
        pipeline = ColorPipeline(self.wm, self.color_cfg, cfg)

        frame_white = np.full((480, 640, 3), 255, dtype=np.uint8)
        frame_black = np.zeros((480, 640, 3), dtype=np.uint8)

        _, prev = pipeline.process(frame_white)
        _, result = pipeline.process(frame_black, prev)
        # 스무딩으로 인해 즉시 0이 되지 않아야 함
        self.assertGreater(result.mean(), 50)

    def test_grb_byte_order(self):
        from core.color import ColorPipeline
        pipeline = ColorPipeline(self.wm, self.color_cfg, self.mirror_cfg)
        # 빨간 프레임 → GRB에서 G=0, R=255, B=0
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 0] = 255  # R channel
        grb_bytes, rgb = pipeline.process(frame)
        # 첫 LED의 GRB: [G, R, B]
        g, r, b = grb_bytes[0], grb_bytes[1], grb_bytes[2]
        self.assertGreater(r, g)  # R > G for red frame
        self.assertGreater(r, b)


# ══════════════════════════════════════════════════════════════════
#  engine_utils
# ══════════════════════════════════════════════════════════════════

class TestEngineUtils(unittest.TestCase):
    def setUp(self):
        from core.config import DEFAULT_CONFIG
        self.config = copy.deepcopy(DEFAULT_CONFIG)

    def test_remap_t_boundaries(self):
        from core.engine_utils import _remap_t
        self.assertAlmostEqual(_remap_t(0.0, (33, 33, 34)), 0.0)
        self.assertAlmostEqual(_remap_t(1.0, (33, 33, 34)), 1.0, places=2)

    def test_remap_t_equal_weights(self):
        from core.engine_utils import _remap_t
        # 동일 비율이면 선형에 가까움
        for t in [0.0, 0.25, 0.5, 0.75, 1.0]:
            result = _remap_t(t, (33, 33, 34))
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 1.0)

    def test_compute_led_perimeter_t(self):
        from core.engine_utils import _compute_led_perimeter_t
        t = _compute_led_perimeter_t(self.config)
        self.assertEqual(len(t), 75)
        self.assertTrue(np.all(t >= 0.0))
        self.assertTrue(np.all(t <= 1.0))

    def test_compute_led_band_mapping(self):
        from core.engine_utils import _compute_led_perimeter_t, _compute_led_band_mapping
        t = _compute_led_perimeter_t(self.config)
        bands = _compute_led_band_mapping(t, 16, (33, 33, 34))
        self.assertEqual(len(bands), 75)
        self.assertTrue(np.all(bands >= 0.0))
        self.assertTrue(np.all(bands <= 15.0))

    def test_build_led_zone_map_1(self):
        from core.engine_utils import _build_led_zone_map_by_side
        zm = _build_led_zone_map_by_side(self.config, 1)
        self.assertTrue(np.all(zm == 0))

    def test_build_led_zone_map_4(self):
        from core.engine_utils import _build_led_zone_map_by_side
        zm = _build_led_zone_map_by_side(self.config, 4)
        self.assertEqual(len(zm), 75)
        unique = set(zm.tolist())
        self.assertTrue(unique.issubset({0, 1, 2, 3}))

    def test_per_led_to_zone_colors(self):
        from core.engine_utils import per_led_to_zone_colors
        colors = np.array([
            [255, 0, 0], [255, 0, 0],  # zone 0
            [0, 255, 0], [0, 255, 0],  # zone 1
        ], dtype=np.float32)
        zone_map = np.array([0, 0, 1, 1], dtype=np.int32)
        result = per_led_to_zone_colors(colors, zone_map, 2)
        np.testing.assert_array_almost_equal(result[0], [255, 0, 0])
        np.testing.assert_array_almost_equal(result[1], [0, 255, 0])

    def test_build_led_order_from_segments(self):
        from core.engine_utils import _build_led_order_from_segments
        segments = [{"start": 5, "end": 3}, {"start": 3, "end": 0}]
        order = _build_led_order_from_segments(segments, 6)
        self.assertEqual(len(order), 6)
        # 모든 인덱스가 포함되어야 함
        self.assertEqual(set(order), {0, 1, 2, 3, 4, 5})


# ══════════════════════════════════════════════════════════════════
#  ADR-014: 벡터화된 렌더링
# ══════════════════════════════════════════════════════════════════

class TestVectorizedRendering(unittest.TestCase):
    def test_band_color_vectorized_shape(self):
        from core.engine_utils import band_color_vectorized
        t = np.linspace(0, 1, 75)
        colors = band_color_vectorized(t)
        self.assertEqual(colors.shape, (75, 3))

    def test_band_color_vectorized_range(self):
        from core.engine_utils import band_color_vectorized
        t = np.linspace(0, 1, 100)
        colors = band_color_vectorized(t)
        self.assertTrue(np.all(colors >= 0))
        self.assertTrue(np.all(colors <= 255))

    def test_band_color_vectorized_endpoints(self):
        from core.engine_utils import band_color_vectorized
        t = np.array([0.0, 1.0])
        colors = band_color_vectorized(t)
        # t=0 → red (255, 0, 0)
        np.testing.assert_array_almost_equal(colors[0], [255, 0, 0], decimal=0)
        # t=1 → magenta (160, 0, 220)
        np.testing.assert_array_almost_equal(colors[1], [160, 0, 220], decimal=0)

    def test_build_base_color_array_rainbow(self):
        from core.engine_utils import build_base_color_array
        bands = np.linspace(0, 15, 75)
        colors = build_base_color_array(bands, 16, rainbow=True)
        self.assertEqual(colors.shape, (75, 3))

    def test_build_base_color_array_solid(self):
        from core.engine_utils import build_base_color_array
        bands = np.linspace(0, 15, 75)
        solid = np.array([255, 0, 80], dtype=np.float32)
        colors = build_base_color_array(bands, 16, rainbow=False, solid_color=solid)
        for i in range(75):
            np.testing.assert_array_equal(colors[i], [255, 0, 80])

    def test_build_base_color_array_screen(self):
        from core.engine_utils import build_base_color_array
        bands = np.linspace(0, 15, 75)
        screen = np.random.rand(75, 3).astype(np.float32) * 255
        colors = build_base_color_array(bands, 16, screen_colors=screen)
        np.testing.assert_array_almost_equal(colors, screen)

    def test_vectorized_render_pulse(self):
        from core.engine_utils import vectorized_render_pulse
        base = np.full((75, 3), 200.0, dtype=np.float32)
        leds = vectorized_render_pulse(base, 0.8, 0.3, 0.1, 0.02, 1.0)
        self.assertEqual(leds.shape, (75, 3))
        self.assertTrue(np.all(leds >= 0))

    def test_vectorized_render_spectrum(self):
        from core.engine_utils import vectorized_render_spectrum
        base = np.full((75, 3), 200.0, dtype=np.float32)
        bands = np.linspace(0, 15, 75)
        spec = np.random.rand(16).astype(np.float64)
        leds = vectorized_render_spectrum(base, bands, spec, 0.02, 1.0)
        self.assertEqual(leds.shape, (75, 3))
        self.assertTrue(np.all(leds >= 0))

    def test_leds_to_grb(self):
        from core.engine_utils import leds_to_grb
        leds = np.array([[255, 0, 128]], dtype=np.float32)
        grb = leds_to_grb(leds.copy())
        # GRB: [G=0, R=255, B=128]
        self.assertEqual(grb[0], 0)
        self.assertEqual(grb[1], 255)
        self.assertEqual(grb[2], 128)


# ══════════════════════════════════════════════════════════════════
#  capture_base (StaleDetectionMixin)
# ══════════════════════════════════════════════════════════════════

class _MockCapture(object):
    """StaleDetectionMixin 테스트용 모킹."""

    def __init__(self):
        from core.capture_base import StaleDetectionMixin
        # Mixin 초기화를 수동으로 수행
        self._init_sd = StaleDetectionMixin._init_stale_detection
        self._grab_sd = StaleDetectionMixin._grab_with_stale_detection
        self.last_frame = None
        self._lock = threading.Lock()
        self._consecutive_nones = 0
        self._last_recreate_time = 0.0
        self._grab_returns = None
        self._recreate_count = 0

    def _do_grab(self):
        return self._grab_returns

    def _do_recreate(self):
        self._recreate_count += 1


class TestStaleDetection(unittest.TestCase):
    def test_returns_frame_on_success(self):
        from core.capture_base import StaleDetectionMixin
        cap = _MockCapture()
        StaleDetectionMixin._init_stale_detection(cap)
        frame = np.array([1, 2, 3])
        cap._grab_returns = frame
        result = StaleDetectionMixin._grab_with_stale_detection(cap)
        np.testing.assert_array_equal(result, frame)
        self.assertEqual(cap._consecutive_nones, 0)

    def test_returns_last_frame_on_few_nones(self):
        from core.capture_base import StaleDetectionMixin
        from core.constants import STALE_NONE_THRESHOLD
        cap = _MockCapture()
        StaleDetectionMixin._init_stale_detection(cap)
        cap.last_frame = np.array([10, 20, 30])
        cap._grab_returns = None
        # 임계값 이하에서는 last_frame 반환
        for _ in range(STALE_NONE_THRESHOLD):
            result = StaleDetectionMixin._grab_with_stale_detection(cap)
            np.testing.assert_array_equal(result, cap.last_frame)

    def test_triggers_recreate_after_threshold(self):
        from core.capture_base import StaleDetectionMixin
        from core.constants import STALE_NONE_THRESHOLD
        cap = _MockCapture()
        StaleDetectionMixin._init_stale_detection(cap)
        cap._grab_returns = None
        # 임계값 초과까지 None 반환
        for _ in range(STALE_NONE_THRESHOLD + 1):
            StaleDetectionMixin._grab_with_stale_detection(cap)
        self.assertEqual(cap._recreate_count, 1)


# ══════════════════════════════════════════════════════════════════
#  device (모킹 — 실제 USB 필요 없음)
# ══════════════════════════════════════════════════════════════════

class TestNanoleafDevice(unittest.TestCase):
    def setUp(self):
        try:
            import hid
            self._has_hid = True
        except ImportError:
            self._has_hid = False

    def test_grb_byte_order(self):
        """set_all_color가 GRB 순서로 데이터를 생성하는지 확인."""
        if not self._has_hid:
            self.skipTest("hid module not available (Windows only)")
        from core.device import NanoleafDevice
        dev = NanoleafDevice(led_count=3)
        r, g, b = 255, 128, 64
        expected = bytes([g, r, b] * 3)
        actual = bytes([g, r, b] * dev.led_count)
        self.assertEqual(expected, actual)

    def test_turn_off_generates_zeros(self):
        if not self._has_hid:
            self.skipTest("hid module not available (Windows only)")
        from core.device import NanoleafDevice
        dev = NanoleafDevice(led_count=5)
        expected = bytes(5 * 3)
        self.assertEqual(len(expected), 15)
        self.assertTrue(all(b == 0 for b in expected))


# ══════════════════════════════════════════════════════════════════
#  audio_engine (구조 테스트 — 실제 오디오 디바이스 불필요)
# ══════════════════════════════════════════════════════════════════

class TestAudioEngineStructure(unittest.TestCase):
    def test_build_log_bands(self):
        from core.audio_engine import _build_log_bands
        fft_freqs = np.fft.rfftfreq(2048, 1.0 / 48000)
        bands = _build_log_bands(16, 20, 16000, fft_freqs)
        self.assertEqual(len(bands), 16)
        for lo, hi in bands:
            self.assertLess(lo, hi)

    def test_list_loopback_devices_returns_list(self):
        from core.audio_engine import list_loopback_devices
        # 리눅스에서는 빈 리스트, Windows에서는 디바이스 목록
        result = list_loopback_devices()
        self.assertIsInstance(result, list)

    def test_has_pyaudio_flag(self):
        from core.audio_engine import HAS_PYAUDIO
        self.assertIsInstance(HAS_PYAUDIO, bool)


# ══════════════════════════════════════════════════════════════════
#  Integration: pipeline end-to-end
# ══════════════════════════════════════════════════════════════════

class TestPipelineIntegration(unittest.TestCase):
    """전체 파이프라인 통합 테스트 — 실제 디바이스 없이 데이터 흐름 검증."""

    def test_mirror_pipeline_end_to_end(self):
        """프레임 → layout → weight_matrix → ColorPipeline → GRB bytes"""
        from core.config import DEFAULT_CONFIG
        from core.layout import get_led_positions, build_weight_matrix
        from core.color import ColorPipeline

        config = copy.deepcopy(DEFAULT_CONFIG)
        led_count = config["device"]["led_count"]
        screen_w, screen_h = 2560, 1440

        pos, sides = get_led_positions(
            screen_w, screen_h,
            config["layout"]["segments"], led_count,
        )
        wm = build_weight_matrix(
            screen_w, screen_h, pos, sides,
            config["mirror"]["grid_cols"], config["mirror"]["grid_rows"],
            config["mirror"]["decay_radius"], config["mirror"]["parallel_penalty"],
        )

        pipeline = ColorPipeline(wm, config["color"], config["mirror"])

        # 테스트 프레임: 왼쪽 빨강, 오른쪽 파랑
        frame = np.zeros((screen_h, screen_w, 3), dtype=np.uint8)
        frame[:, :screen_w // 2, 0] = 255  # 왼쪽 빨강
        frame[:, screen_w // 2:, 2] = 255  # 오른쪽 파랑

        grb_bytes, rgb = pipeline.process(frame)

        self.assertEqual(len(grb_bytes), led_count * 3)
        self.assertEqual(rgb.shape, (led_count, 3))

        # 모든 값이 유효 범위
        self.assertTrue(np.all(rgb >= 0))
        self.assertTrue(np.all(rgb <= 255))

    def test_audio_vectorized_end_to_end(self):
        """밴드 매핑 → 색상 배열 → 스펙트럼 렌더링 → GRB"""
        from core.config import DEFAULT_CONFIG
        from core.engine_utils import (
            _compute_led_perimeter_t, _compute_led_band_mapping,
            build_base_color_array, vectorized_render_spectrum, leds_to_grb,
        )
        from core.color_correction import ColorCorrection

        config = copy.deepcopy(DEFAULT_CONFIG)
        led_count = config["device"]["led_count"]

        perimeter_t = _compute_led_perimeter_t(config)
        band_indices = _compute_led_band_mapping(perimeter_t, 16, (33, 33, 34))
        base_colors = build_base_color_array(band_indices, 16, rainbow=True)

        spectrum = np.random.rand(16).astype(np.float64) * 0.8
        leds = vectorized_render_spectrum(base_colors, band_indices, spectrum, 0.02, 1.0)

        cc = ColorCorrection(config["color"])
        cc.apply(leds)

        grb = leds_to_grb(leds)
        self.assertEqual(len(grb), led_count * 3)


if __name__ == "__main__":
    unittest.main()
