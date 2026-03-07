"""하이브리드 비주얼라이저 — 오디오 반응 + 화면 색상 소스

[변경 사항 v2 — ColorCorrection 통합]
★ core.color_correction.ColorCorrection으로 색상 보정 위임
  자체 _build_color_luts(), _apply_color_correction(),
  _lut_r_gamma, _lut_g_gamma, _lut_b_gamma, _wb_*, _green_red_bleed 멤버 제거
  보정 순서는 동일: 감마 → 채널 믹싱 (green→red bleed) → 화이트밸런스

[설계]
color_source == "solid"이면 AudioVisualizer와 100% 동일하게 동작하고,
"screen"이면 화면 색상을 LED base color로 사용합니다.
"""

import time
import copy
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.audio_engine import AudioEngine, _build_log_bands
from core.color_correction import ColorCorrection
from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.audio_visualizer import (
    MODE_PULSE, MODE_SPECTRUM, MODE_BASS_DETAIL, DEFAULT_FPS, MIN_BRIGHTNESS,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _build_led_order_from_segments,
)
from core.screen_sampler import ScreenSampler

# ── 색상 소스 상수 ─────────────────────────────────────────────────
COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

# ── 특수 구역 수: LED 개별 미러링 ──────────────────────────────────
N_ZONES_PER_LED = -1

# ── 스크린 갱신 간격 (오디오 프레임 수 기준) ───────────────────────
SCREEN_UPDATE_INTERVAL = 3


def _build_led_zone_map_by_side(config, n_zones):
    """각 LED가 어느 screen zone에 매핑되는지 계산.

    멀티랩 자동 처리: get_led_positions()가 모든 세그먼트의
    LED 위치와 side를 반환하므로, 같은 side의 LED는
    바깥/안쪽 바퀴 무관하게 같은 zone 그룹에 속합니다.
    """
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    mapping = np.zeros(led_count, dtype=np.int32)

    if n_zones == 1:
        pass  # 이미 0으로 초기화됨

    elif n_zones == 2:
        cy = screen_h / 2.0
        for i in range(led_count):
            side = sides[i]
            if side == "top":
                mapping[i] = 0
            elif side == "bottom":
                mapping[i] = 1
            elif side in ("left", "right"):
                y = positions[i, 1]
                mapping[i] = 0 if y <= cy else 1
            else:
                mapping[i] = 0

    elif n_zones == 4:
        side_to_zone = {"top": 0, "right": 1, "bottom": 2, "left": 3}
        for i in range(led_count):
            mapping[i] = side_to_zone.get(sides[i], 0)

    elif n_zones == 8:
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            side = sides[i]
            if side == "top":
                mapping[i] = 0 if x <= cx else 1
            elif side == "right":
                mapping[i] = 2 if y <= cy else 3
            elif side == "bottom":
                mapping[i] = 4 if x >= cx else 5
            elif side == "left":
                mapping[i] = 6 if y >= cy else 7
            else:
                mapping[i] = 0

    else:
        perimeter_t = _compute_led_perimeter_t(config)
        for i in range(led_count):
            side = sides[i]
            x, y = positions[i]

            if side == "top":
                progress = x / screen_w if screen_w > 0 else 0.5
                cw_t = 0.00 + progress * 0.25
            elif side == "right":
                progress = y / screen_h if screen_h > 0 else 0.5
                cw_t = 0.25 + progress * 0.25
            elif side == "bottom":
                progress = 1.0 - (x / screen_w if screen_w > 0 else 0.5)
                cw_t = 0.50 + progress * 0.25
            elif side == "left":
                progress = 1.0 - (y / screen_h if screen_h > 0 else 0.5)
                cw_t = 0.75 + progress * 0.25
            else:
                cw_t = 0.0

            cw_t = max(0.0, min(cw_t, 0.9999))
            mapping[i] = int(cw_t * n_zones)

    return mapping


class HybridVisualizer(QThread):
    """오디오 반응 + 화면 색상 소스 LED 비주얼라이저.

    ★ v2: ColorCorrection 모듈로 색상 보정 위임
    """

    fps_updated = pyqtSignal(float)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    screen_colors_updated = pyqtSignal(object)

    def __init__(self, config, device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()

        # ── 오디오 파라미터 ──
        self.base_color = np.array([255, 0, 80], dtype=np.float32)
        self.rainbow = False
        self.brightness = 1.0
        self.bass_sensitivity = 1.0
        self.mid_sensitivity = 1.0
        self.high_sensitivity = 1.0
        self.mode = MODE_PULSE
        self.target_fps = DEFAULT_FPS
        self.attack = 0.5
        self.release = 0.1

        self._zone_weights = [33, 33, 34]
        self._zone_dirty = False

        # ── 색상 소스 파라미터 ──
        self.color_source = COLOR_SOURCE_SOLID
        self.n_zones = 4
        self.min_brightness = MIN_BRIGHTNESS

        # ── ★ ColorCorrection — 감마·채널 믹싱·WB 보정 ──
        self._cc = ColorCorrection(config.get("color", {}))

        # ── 내부 상태 ──
        self._device_index = device_index
        self._audio_engine = None
        self._screen_sampler = None
        self._nanoleaf = None
        self._led_count = 0

        self._perimeter_t = None
        self._led_band_indices = None
        self._led_zone_map = None
        self._led_order = []

        # per-LED 미러링용
        self._weight_matrix = None
        self._per_led_colors = None

        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

        # bass_detail 모드 전용 상태
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

    # ── 외부 제어 ─────────────────────────────────────────────────

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def set_color(self, r, g, b):
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        self.rainbow = enabled

    def set_mode(self, mode):
        self.mode = mode

    def set_color_source(self, source, n_zones=None):
        self.color_source = source

        if n_zones is not None and n_zones != self.n_zones:
            self.n_zones = n_zones

            if n_zones == N_ZONES_PER_LED:
                if self._weight_matrix is None:
                    mirror_cfg = self.config.get("mirror", {})
                    self._build_weight_matrix(mirror_cfg)
            else:
                if self._perimeter_t is not None:
                    self._led_zone_map = _build_led_zone_map_by_side(
                        self.config, n_zones
                    )
                if self._screen_sampler is not None:
                    self._screen_sampler.set_n_zones(n_zones)

    def stop_visualizer(self):
        self._stop_event.set()

    # ── 밴드 매핑 재계산 ───────────────────────────────────────────

    def _rebuild_band_mapping(self):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        self._zone_dirty = False

    # ── QThread 진입점 ─────────────────────────────────────────────

    def run(self):
        if not self._init_resources():
            return
        try:
            self._run_loop()
        except Exception as e:
            self.error.emit(f"비주얼라이저 오류: {e}", "warning")
        finally:
            self._cleanup()

    # ── 리소스 초기화 ──────────────────────────────────────────────

    def _init_resources(self):
        dev_cfg = self.config["device"]
        mirror_cfg = self.config.get("mirror", {})
        self._led_count = dev_cfg["led_count"]

        # ★ ColorCorrection 빌드
        self._cc.rebuild(self.config.get("color", {}))

        # 1. Nanoleaf 연결
        self.status_changed.emit("Nanoleaf 연결 중...")
        try:
            self._nanoleaf = NanoleafDevice(
                int(dev_cfg["vendor_id"], 16),
                int(dev_cfg["product_id"], 16),
                self._led_count,
            )
            self._nanoleaf.connect()
        except (OSError, IOError, ValueError, ConnectionError) as e:
            self.error.emit(f"Nanoleaf 연결 실패: {e}", "critical")
            return False

        # 2. 오디오 엔진
        self.status_changed.emit("오디오 캡처 초기화...")
        try:
            self._audio_engine = AudioEngine(
                device_index=self._device_index,
                sensitivity=1.0,
                smoothing=0.15,
            )
            self._audio_engine.bass_sensitivity = self.bass_sensitivity
            self._audio_engine.mid_sensitivity = self.mid_sensitivity
            self._audio_engine.high_sensitivity = self.high_sensitivity
            self._audio_engine.start()
        except Exception as e:
            self.error.emit(f"오디오 캡처 실패: {e}", "critical")
            if self._nanoleaf:
                self._nanoleaf.disconnect()
            return False

        # 3. LED 둘레 매핑
        n_bands = self._audio_engine.n_bands
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        # 4. zone 매핑
        if self.n_zones != N_ZONES_PER_LED:
            self._led_zone_map = _build_led_zone_map_by_side(
                self.config, self.n_zones
            )

        # 4b. per-LED 미러링용 weight matrix
        if self.n_zones == N_ZONES_PER_LED:
            self._build_weight_matrix(mirror_cfg)

        # 5. 스크린 샘플러
        if self.color_source != COLOR_SOURCE_SOLID:
            self._init_screen_sampler(mirror_cfg)

        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        # bass_detail 밴드 분할 초기화
        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX,
            fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")
        return True

    def _build_weight_matrix(self, mirror_cfg):
        """per-LED 미러링용 가중치 행렬 생성."""
        layout_cfg = self.config["layout"]
        grid_cols = mirror_cfg.get("grid_cols", 64)
        grid_rows = mirror_cfg.get("grid_rows", 32)
        screen_w = grid_cols * 40
        screen_h = grid_rows * 40

        positions, sides = get_led_positions(
            screen_w, screen_h,
            layout_cfg["segments"], self._led_count,
            orientation=mirror_cfg.get("orientation", "auto"),
            portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
        )

        decay = mirror_cfg.get("decay_radius", 0.3)
        penalty = mirror_cfg.get("parallel_penalty", 5.0)

        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        decay_param = (
            {s: per_decay.get(s, decay) for s in ("top", "bottom", "left", "right")}
            if per_decay else decay
        )
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        penalty_param = (
            {s: per_penalty.get(s, penalty) for s in ("top", "bottom", "left", "right")}
            if per_penalty else penalty
        )

        self._weight_matrix = build_weight_matrix(
            screen_w, screen_h, positions, sides,
            grid_cols, grid_rows, decay_param, penalty_param,
        )
        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)

    def _init_screen_sampler(self, mirror_cfg):
        self.status_changed.emit("화면 캡처 초기화...")
        try:
            self._screen_sampler = ScreenSampler(
                n_zones=self.n_zones,
                grid_cols=mirror_cfg.get("grid_cols", 64),
                grid_rows=mirror_cfg.get("grid_rows", 32),
            )
            self._screen_sampler.start(
                monitor_index=mirror_cfg.get("monitor_index", 0)
            )
        except Exception as e:
            self.error.emit(
                f"화면 캡처 실패 — 단색 모드로 전환: {e}", "warning"
            )
            self._screen_sampler = None
            self.color_source = COLOR_SOURCE_SOLID

    def _ensure_screen_sampler(self):
        if self._screen_sampler is not None:
            return True
        if self.color_source == COLOR_SOURCE_SOLID:
            return True

        mirror_cfg = self.config.get("mirror", {})
        self._init_screen_sampler(mirror_cfg)
        return self._screen_sampler is not None

    # ── 메인 루프 ──────────────────────────────────────────────────

    def _run_loop(self):
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            if self._zone_dirty:
                self._rebuild_band_mapping()

            if self.color_source != COLOR_SOURCE_SOLID:
                self._ensure_screen_sampler()

            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity

            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            atk = 0.15 + self.attack * 0.70
            rel = 0.25 - self.release * 0.245

            self._smooth_bass = self._ar(self._smooth_bass, raw_bass, atk, rel)
            self._smooth_mid = self._ar(self._smooth_mid, raw_mid, atk, rel)
            self._smooth_high = self._ar(self._smooth_high, raw_high, atk, rel)
            for i in range(len(self._smooth_spectrum)):
                self._smooth_spectrum[i] = self._ar(
                    self._smooth_spectrum[i], raw_spectrum[i], atk, rel
                )

            bass = self._smooth_bass
            mid = self._smooth_mid
            high = self._smooth_high
            spec = self._smooth_spectrum

            # 스크린 갱신
            if (self._screen_sampler is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                self._screen_sampler.update()

                if self.n_zones == N_ZONES_PER_LED and self._weight_matrix is not None:
                    frame = self._screen_sampler.get_last_frame()
                    if frame is not None:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat

            # LED 렌더링
            mode = self.mode
            bd_spec = None
            if mode == MODE_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data = self._render_spectrum(bd_spec)
            elif mode == MODE_SPECTRUM:
                grb_data = self._render_spectrum(spec)
            else:
                grb_data = self._render_pulse(bass, mid, high)

            # USB 전송
            try:
                self._nanoleaf.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._nanoleaf.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # UI 시그널
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if mode == MODE_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())
                if self._screen_sampler is not None:
                    if self.n_zones == N_ZONES_PER_LED:
                        if self._per_led_colors is not None:
                            self.screen_colors_updated.emit(
                                self._per_led_colors.copy()
                            )
                    elif self.n_zones == 1:
                        gc = self._screen_sampler.get_global_color()
                        self.screen_colors_updated.emit(gc.reshape(1, 3))
                    else:
                        self.screen_colors_updated.emit(
                            self._screen_sampler.get_zone_colors()
                        )

            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ── Bass Detail 처리 ──────────────────────────────────────────

    def _process_bass_detail(self, eng, atk_rate, rel_rate):
        """raw FFT에서 저역(20~500Hz)만 16밴드로 분할 + AGC + 스무딩."""
        raw_fft = eng.get_raw_fft()
        spec_len = len(raw_fft)
        n = BASS_DETAIL_N_BANDS

        raw_vals = np.zeros(n, dtype=np.float64)
        for i, (lo, hi) in enumerate(self._bd_band_bins):
            if lo < spec_len and hi <= spec_len and hi > lo:
                band_data = raw_fft[lo:hi]
                raw_vals[i] = float(np.sqrt(np.mean(band_data ** 2)))

        agc_atk, agc_rel, agc_floor = 0.3, 0.002, 0.005
        for i in range(n):
            if raw_vals[i] > self._bd_agc[i]:
                self._bd_agc[i] += (raw_vals[i] - self._bd_agc[i]) * agc_atk
            else:
                self._bd_agc[i] *= (1.0 - agc_rel)
                self._bd_agc[i] = max(self._bd_agc[i], agc_floor)

        normalized = raw_vals / self._bd_agc
        val = np.minimum(1.0, (normalized * self.bass_sensitivity) ** 1.5)

        for i in range(n):
            self._bd_smooth[i] = self._ar(self._bd_smooth[i], val[i], atk_rate, rel_rate)

        return self._bd_smooth

    # ── 렌더링 — Pulse ─────────────────────────────────────────────

    def _render_pulse(self, bass, mid, high):
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        intensity = max(self.min_brightness, bass) * self.brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_base_color(led_idx, n_bands)
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix
            leds[led_idx] = c * intensity

        # ★ ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ── 렌더링 — Spectrum ──────────────────────────────────────────

    def _render_spectrum(self, spec):
        n_leds = self._led_count
        n_bands = len(spec)
        leds = np.zeros((n_leds, 3), dtype=np.float32)

        for led_idx in range(n_leds):
            band_f = self._led_band_indices[led_idx]
            band_lo = max(0, min(int(band_f), n_bands - 1))
            band_hi = min(band_lo + 1, n_bands - 1)
            frac = band_f - int(band_f)
            energy = spec[band_lo] * (1 - frac) + spec[band_hi] * frac

            color = self._get_base_color(led_idx, n_bands)
            intensity = max(self.min_brightness, energy) * self.brightness
            leds[led_idx] = color * intensity

        # ★ ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ── 색상 소스 분기 ─────────────────────────────────────────────

    def _get_base_color(self, led_idx, n_bands):
        source = self.color_source

        if source == COLOR_SOURCE_SOLID:
            return self._get_solid_color(led_idx, n_bands)

        elif source == COLOR_SOURCE_SCREEN:
            if self._screen_sampler is None or not self._screen_sampler.has_data:
                return self._get_solid_color(led_idx, n_bands)

            if self.n_zones == N_ZONES_PER_LED:
                if self._per_led_colors is not None:
                    return self._per_led_colors[led_idx].copy()
                return self._get_solid_color(led_idx, n_bands)

            if self.n_zones == 1:
                return self._screen_sampler.get_global_color().copy()

            zone_idx = self._led_zone_map[led_idx]
            zone_colors = self._screen_sampler.get_zone_colors()

            if zone_idx >= len(zone_colors):
                zone_idx = zone_idx % len(zone_colors)

            return zone_colors[zone_idx].copy()

        return self._get_solid_color(led_idx, n_bands)

    def _get_solid_color(self, led_idx, n_bands):
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    # ── 색상 헬퍼 ─────────────────────────────────────────────────

    @staticmethod
    def _band_color(t):
        keypoints = [
            (0.000, 255,   0,   0),
            (0.130, 255, 127,   0),
            (0.260, 255, 255,   0),
            (0.400,   0, 255,   0),
            (0.540,   0, 180, 255),
            (0.680,   0,  50, 255),
            (0.820,  80,   0, 255),
            (1.000, 160,   0, 220),
        ]
        t = max(0.0, min(1.0, t))
        for i in range(len(keypoints) - 1):
            t0, r0, g0, b0 = keypoints[i]
            t1, r1, g1, b1 = keypoints[i + 1]
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                r = r0 + (r1 - r0) * f
                g = g0 + (g1 - g0) * f
                b = b0 + (b1 - b0) * f
                return np.array([r, g, b], dtype=np.float32)
        return np.array([160, 0, 220], dtype=np.float32)

    @staticmethod
    def _ar(current, target, attack_rate, release_rate):
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    @staticmethod
    def _leds_to_grb(leds):
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]
        grb[:, 1] = u8[:, 0]
        grb[:, 2] = u8[:, 2]
        return grb.tobytes()

    # ── 정리 ───────────────────────────────────────────────────────

    def _cleanup(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        if self._screen_sampler:
            self._screen_sampler.stop()
            self._screen_sampler = None
        if self._nanoleaf:
            try:
                self._nanoleaf.turn_off()
                self._nanoleaf.disconnect()
            except (OSError, IOError, ValueError):
                pass
            self._nanoleaf = None
        self.status_changed.emit("비주얼라이저 중지됨")
