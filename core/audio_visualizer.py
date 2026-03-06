"""오디오 비주얼라이저 v5 — LED 위치 기반 주파수 그라데이션

[주요 변경 v5]
- 하단 중앙 = 저음(밴드 0), 상단 중앙 = 고음(밴드 N-1)
- layout.py의 get_led_positions()로 LED 화면 좌표를 계산하여
  "하단 중앙으로부터 모니터 둘레를 따른 거리"로 밴드 인덱스 결정
- multi-wrap: 같은 좌표 위치의 LED는 같은 밴드 → 자동 동기화
- 겹치는 밴드 범위 문제 해소 — 연속적 그라데이션
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


def _compute_led_band_mapping(config, n_bands):
    """각 LED의 스펙트럼 밴드 인덱스를 화면 좌표 기반으로 계산합니다.

    원리:
    1. get_led_positions()로 각 LED의 (x, y) 화면 좌표 + side 정보 획득
    2. 하단 중앙(bottom_center)에서 모니터 둘레를 시계방향으로 따라가면서
       거리 비율(0.0~1.0)을 계산
    3. 하단 중앙 = 0.0 (저음), 상단 중앙 = 1.0 (고음)
       좌측/우측을 대칭으로 처리하여 양쪽에서 같은 높이면 같은 밴드

    거리 계산 방법 — y좌표 기반 (가장 직관적):
    - 하단(y=screen_h) = 0.0 (저음)
    - 상단(y=0) = 1.0 (고음)
    - 좌/우 측면은 y에 따라 선형 보간

    이렇게 하면 side 경계에서 겹침 없이 연속 그라데이션이 됩니다.

    Returns:
        band_indices: np.array (led_count,) float — 각 LED의 밴드 인덱스
    """
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    # 화면 해상도 — 비율만 중요하므로 기본값 사용
    screen_w = mirror_cfg.get("grid_cols", 64) * 40  # 대략적 비율
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    # y좌표 → 밴드 인덱스
    # y=screen_h (하단) → 0.0, y=0 (상단) → 1.0
    band_indices = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        y = positions[i, 1]
        # y 정규화: 0(상단)~screen_h(하단) → 1.0(고음)~0.0(저음)
        t = 1.0 - (y / screen_h) if screen_h > 0 else 0.5
        t = max(0.0, min(1.0, t))
        band_indices[i] = t * (n_bands - 1)

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
    """오디오 반응 LED 비주얼라이저 v4."""

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
        self.rainbow = False  # True면 LED 위치 기반 무지개 그래디언트
        self.brightness = 1.0
        self.bass_sensitivity = 1.0
        self.mid_sensitivity = 1.0
        self.high_sensitivity = 1.0
        self.mode = MODE_PULSE
        self.target_fps = DEFAULT_FPS
        self.attack = 0.5
        self.release = 0.1

        self._device_index = device_index
        self._audio_engine = None
        self._nanoleaf = None
        self._led_count = 0

        # 세그먼트 기반 매핑
        self._led_band_indices = None  # 각 LED의 밴드 인덱스
        self._led_order = []           # 물리적 순서

        # 출력 스무딩
        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

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

        # Nanoleaf
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

        # AudioEngine
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

        # LED 위치 기반 밴드 매핑 (하단=저음, 상단=고음 그라데이션)
        self._led_band_indices = _compute_led_band_mapping(self.config, n_bands)
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

            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity

            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # Attack/Release 스무딩
            # attack 슬라이더: 0=느린 반응, 1=즉각 반응
            #   → 내부 계수: 0.15 ~ 0.85 (프레임당 차이 반영 비율)
            # release 슬라이더: 0=즉시 소멸, 1=긴 잔향
            #   → 내부 계수: 0.25 ~ 0.005 (반전 — 올릴수록 느리게 감쇠)
            atk = 0.15 + self.attack * 0.70      # 0.15 ~ 0.85
            rel = 0.25 - self.release * 0.245     # 0.25 ~ 0.005

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

    # ── Pulse (단색 모드) ────────────────────────────────────────

    def _render_pulse(self, bass, mid, high):
        """전체 LED — bass 밝기 + mid/high 색상 변조."""
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        intensity = max(MIN_BRIGHTNESS, bass) * self.brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_led_color(led_idx, n_bands)
            # mid/high 변조
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix
            leds[led_idx] = c * intensity

        return self._leds_to_grb(leds)

    # ── Spectrum ──────────────────────────────────────────────────

    def _render_spectrum(self, spec):
        """각 LED의 밴드 에너지로 밝기, 색상은 팔레트(단색 or 무지개)."""
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
        """LED 인덱스에 대한 색상을 반환.

        - rainbow=True: 밴드 위치 기반 그래디언트 (빨→초→파)
        - rainbow=False: base_color (단색)
        """
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    @staticmethod
    def _band_color(t):
        """밴드 위치(0=저음, 1=고음) → RGB 무지개색.

        7개 키포인트를 직접 RGB로 지정하고 선형 보간합니다.
        HSV 변환 없이 원하는 색이 정확히 나옵니다.
        """
        # (위치, R, G, B) — 빨주노초파남보
        keypoints = [
            (0.000, 255,   0,   0),  # 빨강
            (0.167, 255, 127,   0),  # 주황
            (0.333, 255, 255,   0),  # 노랑
            (0.500,   0, 255,   0),  # 초록
            (0.667,   0, 130, 255),  # 파랑 (하늘)
            (0.833,   0,   0, 255),  # 남색
            (1.000, 148,   0, 211),  # 보라
        ]

        t = max(0.0, min(1.0, t))

        # t 위치에 해당하는 두 키포인트 사이를 보간
        for i in range(len(keypoints) - 1):
            t0, r0, g0, b0 = keypoints[i]
            t1, r1, g1, b1 = keypoints[i + 1]
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                r = r0 + (r1 - r0) * f
                g = g0 + (g1 - g0) * f
                b = b0 + (b1 - b0) * f
                return np.array([r, g, b], dtype=np.float32)

        # 끝점
        return np.array([148, 0, 211], dtype=np.float32)

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
        """단색 설정. rainbow=False로 전환."""
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        """무지개 모드 토글."""
        self.rainbow = enabled

    def set_mode(self, mode):
        self.mode = mode
