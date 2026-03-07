"""오디오 비주얼라이저 v10 — 대칭 둘레 거리 + 대역 비율 + 저역 세밀 모드 + 색상 보정

[주요 변경 v10 — ColorCorrection 통합]
- ★ core.color_correction.ColorCorrection으로 색상 보정 위임
  자체 _build_color_luts(), _apply_color_correction(),
  _lut_r_gamma, _lut_g_gamma, _lut_b_gamma, _wb_*, _green_red_bleed 멤버 제거
- 보정 순서는 동일: 감마 → 채널 믹싱 (green→red bleed) → 화이트밸런스

[주요 변경 v9]
- config["color"]의 색상 보정 파이프라인 적용

[주요 변경 v8]
- MODE_BASS_DETAIL: 20Hz~500Hz를 16밴드 로그 분할하여 저역만 세밀 표현
"""

import time
import copy
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.audio_engine import AudioEngine, _build_log_bands
from core.color_correction import ColorCorrection
from core.device import NanoleafDevice
from core.layout import get_led_positions

MODE_PULSE = "pulse"
MODE_SPECTRUM = "spectrum"
MODE_BASS_DETAIL = "bass_detail"

DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02

# 기본 대역 비율 (합계 = 100)
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)

# 저역 세밀 모드 주파수 범위
BASS_DETAIL_FREQ_MIN = 20
BASS_DETAIL_FREQ_MAX = 500
BASS_DETAIL_N_BANDS = 16


def _remap_t(t, zone_weights):
    """균등 둘레 비율 t(0~1)를 대역 비율에 맞게 색상/밴드 t로 변환."""
    b_pct, m_pct, h_pct = zone_weights[0] / 100.0, zone_weights[1] / 100.0, zone_weights[2] / 100.0

    t_bound1 = b_pct
    t_bound2 = b_pct + m_pct

    c0, c1 = 0.0, 1.0 / 3.0
    c2, c3 = 1.0 / 3.0, 2.0 / 3.0
    c4, c5 = 2.0 / 3.0, 1.0

    t = max(0.0, min(1.0, t))

    if t <= t_bound1 and b_pct > 0:
        frac = t / b_pct
        return c0 + frac * (c1 - c0)
    elif t <= t_bound2 and m_pct > 0:
        frac = (t - t_bound1) / m_pct
        return c2 + frac * (c3 - c2)
    elif h_pct > 0:
        frac = (t - t_bound2) / h_pct
        return c4 + frac * (c5 - c4)
    else:
        return t


def _compute_led_perimeter_t(config):
    """각 LED의 균등 둘레 비율 t(0~1)를 계산."""
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

    cx = screen_w / 2.0
    half_bottom = screen_w / 2.0
    side_height = screen_h
    half_top = screen_w / 2.0
    half_perimeter = half_bottom + side_height + half_top

    perimeter_t = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        x, y = positions[i, 0], positions[i, 1]
        side = sides[i]

        if side == "bottom":
            dist = abs(x - cx)
        elif side == "left" or side == "right":
            dist = half_bottom + (screen_h - y)
        elif side == "top":
            dist_to_center = abs(x - cx)
            dist = half_bottom + side_height + (half_top - dist_to_center)
        else:
            dist = 0.0

        t = dist / half_perimeter if half_perimeter > 0 else 0.5
        perimeter_t[i] = max(0.0, min(1.0, t))

    return perimeter_t


def _compute_led_band_mapping(perimeter_t, n_bands, zone_weights):
    """둘레 비율 + 대역 비율 → 각 LED의 밴드 인덱스."""
    led_count = len(perimeter_t)
    band_indices = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        remapped = _remap_t(perimeter_t[i], zone_weights)
        band_indices[i] = remapped * (n_bands - 1)

    return band_indices


def _build_led_order_from_segments(segments, led_count):
    """세그먼트 순서대로 LED 인덱스의 물리적 순서를 반환."""
    order = []
    for seg in segments:
        start, end = seg["start"], seg["end"]
        if start > end:
            indices = list(range(start, end, -1))
        elif start < end:
            indices = list(range(start, end))
        else:
            continue
        for idx in indices:
            if idx not in order and 0 <= idx < led_count:
                order.append(idx)
    for i in range(led_count):
        if i not in order:
            order.append(i)
    return order


class AudioVisualizer(QThread):
    """오디오 반응 LED 비주얼라이저 v10.

    ★ v10: ColorCorrection 모듈로 색상 보정 위임
    """

    fps_updated = pyqtSignal(float)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)

    def __init__(self, config, device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()

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
        self.input_smoothing = 0.3

        self._zone_weights = list(DEFAULT_ZONE_WEIGHTS)
        self._zone_dirty = False

        self._device_index = device_index
        self._audio_engine = None
        self._nanoleaf = None
        self._led_count = 0

        self._perimeter_t = None
        self._led_band_indices = None
        self._led_order = []

        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

        # bass_detail 모드 전용 상태
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # ★ ColorCorrection — 감마·채널 믹싱·WB 보정
        self._cc = ColorCorrection(config.get("color", {}))

    # ── 외부 제어 ─────────────────────────────────────────────────

    def set_zone_weights(self, bass, mid, high):
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def _rebuild_band_mapping(self):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        self._zone_dirty = False

    def run(self):
        if not self._init_resources():
            return
        try:
            self._run_loop()
        except Exception as e:
            self.error.emit(f"비주얼라이저 오류: {e}", "warning")
        finally:
            self._cleanup()

    def _init_resources(self):
        dev_cfg = self.config["device"]
        self._led_count = dev_cfg["led_count"]

        # ★ ColorCorrection 빌드 (이미 __init__에서 했지만 config 확정 후 재빌드)
        self._cc.rebuild(self.config.get("color", {}))

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

        n_bands = self._audio_engine.n_bands

        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        # bass_detail 밴드 분할 초기화
        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX,
            fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)

        self.status_changed.emit("비주얼라이저 실행 중")
        return True

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

            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

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

            mode = self.mode

            if mode == MODE_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                if frame_count % 3 == 0:
                    self.energy_updated.emit(bass, mid, high)
                    self.spectrum_updated.emit(bd_spec.copy())
                grb_data = self._render_spectrum(bd_spec)
            elif mode == MODE_SPECTRUM:
                if frame_count % 3 == 0:
                    self.energy_updated.emit(bass, mid, high)
                    self.spectrum_updated.emit(spec.copy())
                grb_data = self._render_spectrum(spec)
            else:
                if frame_count % 3 == 0:
                    self.energy_updated.emit(bass, mid, high)
                    self.spectrum_updated.emit(spec.copy())
                grb_data = self._render_pulse(bass, mid, high)

            try:
                self._nanoleaf.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._nanoleaf.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

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

    # ── 공통 ─────────────────────────────────────────────────────

    @staticmethod
    def _ar(current, target, attack_rate, release_rate):
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    # ── Pulse ────────────────────────────────────────────────────

    def _render_pulse(self, bass, mid, high):
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        intensity = max(MIN_BRIGHTNESS, bass) * self.brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_led_color(led_idx, n_bands)
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix
            leds[led_idx] = c * intensity

        # ★ ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ── Spectrum (spectrum + bass_detail 공용) ────────────────────

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

            color = self._get_led_color(led_idx, n_bands)
            intensity = max(MIN_BRIGHTNESS, energy) * self.brightness
            leds[led_idx] = color * intensity

        # ★ ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ── 색상 헬퍼 ────────────────────────────────────────────────

    def _get_led_color(self, led_idx, n_bands):
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    @staticmethod
    def _band_color(t):
        """밴드 위치(0=저음, 1=고음) → RGB 무지개색."""
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
    def _leds_to_grb(leds):
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]
        grb[:, 1] = u8[:, 0]
        grb[:, 2] = u8[:, 2]
        return grb.tobytes()

    # ── 정리 / 제어 ──────────────────────────────────────────────

    def _cleanup(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        if self._nanoleaf:
            try:
                self._nanoleaf.turn_off()
                self._nanoleaf.disconnect()
            except (OSError, IOError, ValueError):
                pass
            self._nanoleaf = None
        self.status_changed.emit("비주얼라이저 중지됨")

    def stop_visualizer(self):
        self._stop_event.set()

    def set_color(self, r, g, b):
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        self.rainbow = enabled

    def set_mode(self, mode):
        self.mode = mode
