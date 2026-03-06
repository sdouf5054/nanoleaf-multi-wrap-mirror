"""오디오 비주얼라이저 v7 — 대칭 둘레 거리 + 대역 비율 조절

[주요 변경 v7]
- ★ band_zone_weights (bass%, mid%, high%) — LED 둘레에서 각 대역이
  차지하는 비율을 조절. 기본 33/33/34.
- ★ 비율에 따라 색상 그라데이션과 주파수 밴드가 동시에 재배치
  bass 50%면 → 하단 50% LED가 빨강~노랑 색상 + 저음 밴드 할당
- ★ _remap_t() — 균등 둘레 비율(0~1)을 대역 비율에 맞게 비선형 변환
"""

import time
import copy
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.audio_engine import AudioEngine
from core.device import NanoleafDevice
from core.layout import get_led_positions

MODE_PULSE = "pulse"
MODE_SPECTRUM = "spectrum"

DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02

# 기본 대역 비율 (합계 = 100)
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)  # bass, mid, high

# 무지개 색상에서 각 대역의 기본 색상 범위 (0~1)
# bass = 빨~노 (0.0 ~ 0.33), mid = 초~시안 (0.33 ~ 0.67), high = 파~보 (0.67 ~ 1.0)
_ZONE_COLOR_RANGES = [(0.0, 0.33), (0.33, 0.67), (0.67, 1.0)]


def _remap_t(t, zone_weights):
    """균등 둘레 비율 t(0~1)를 대역 비율에 맞게 색상/밴드 t로 변환.

    zone_weights: (bass%, mid%, high%) 합계 100

    원리:
    - 둘레의 처음 bass% 구간 → 색상 0.0~0.33 (빨~노)
    - 다음 mid% 구간 → 색상 0.33~0.67 (초~시안)
    - 마지막 high% 구간 → 색상 0.67~1.0 (파~보)

    각 구간 내에서는 선형 보간으로 연속 그라데이션 유지.
    """
    b_pct, m_pct, h_pct = zone_weights[0] / 100.0, zone_weights[1] / 100.0, zone_weights[2] / 100.0

    # 둘레 경계 (입력 t 기준)
    t_bound1 = b_pct
    t_bound2 = b_pct + m_pct

    # 색상/밴드 경계 (출력 기준) — 고정 3등분
    c0, c1 = 0.0, 1.0 / 3.0
    c2, c3 = 1.0 / 3.0, 2.0 / 3.0
    c4, c5 = 2.0 / 3.0, 1.0

    t = max(0.0, min(1.0, t))

    if t <= t_bound1 and b_pct > 0:
        # bass 구간
        frac = t / b_pct
        return c0 + frac * (c1 - c0)
    elif t <= t_bound2 and m_pct > 0:
        # mid 구간
        frac = (t - t_bound1) / m_pct
        return c2 + frac * (c3 - c2)
    elif h_pct > 0:
        # high 구간
        frac = (t - t_bound2) / h_pct
        return c4 + frac * (c5 - c4)
    else:
        return t


def _compute_led_perimeter_t(config):
    """각 LED의 균등 둘레 비율 t(0~1)를 계산.

    하단 중앙 = 0.0, 상단 중앙 = 1.0, 좌우 대칭.
    zone_weights와 독립 — 물리적 위치만 반영.

    Returns:
        perimeter_t: np.array (led_count,) float — 각 LED의 둘레 비율 0~1
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
    """둘레 비율 + 대역 비율 → 각 LED의 밴드 인덱스.

    zone_weights로 remapping한 후 n_bands 범위로 스케일.
    """
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
    """오디오 반응 LED 비주얼라이저 v7."""

    fps_updated = pyqtSignal(float)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)

    def __init__(self, config, device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()

        # 외부 파라미터
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

        # ★ 대역 비율 (bass%, mid%, high%) — 실시간 변경 가능
        self._zone_weights = list(DEFAULT_ZONE_WEIGHTS)
        self._zone_dirty = False  # 변경 감지 플래그

        self._device_index = device_index
        self._audio_engine = None
        self._nanoleaf = None
        self._led_count = 0

        self._perimeter_t = None     # 물리적 둘레 비율 (고정)
        self._led_band_indices = None
        self._led_order = []

        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

    def set_zone_weights(self, bass, mid, high):
        """대역 비율 변경 (합계 100). 다음 프레임에서 반영."""
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def _rebuild_band_mapping(self):
        """zone_weights 변경 시 밴드 매핑 재계산."""
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

        # ★ 물리적 둘레 비율 (고정) + 초기 밴드 매핑
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

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

            # ★ zone_weights 변경 감지
            if self._zone_dirty:
                self._rebuild_band_mapping()

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

            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                self.spectrum_updated.emit(spec.copy())

            mode = self.mode
            if mode == MODE_SPECTRUM:
                grb_data = self._render_spectrum(spec)
            else:
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

        return self._leds_to_grb(leds)

    # ── Spectrum ──────────────────────────────────────────────────

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
            (0.000, 255,   0,   0),  # 빨강
            (0.130, 255, 127,   0),  # 주황
            (0.260, 255, 255,   0),  # 노랑
            (0.400,   0, 255,   0),  # 초록
            (0.540,   0, 180, 255),  # 시안/하늘
            (0.680,   0,  50, 255),  # 파랑
            (0.820,  80,   0, 255),  # 남색/인디고
            (1.000, 160,   0, 220),  # 보라
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
