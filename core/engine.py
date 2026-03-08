"""UnifiedEngine — 단일 LED 엔진 (미러링 + 오디오 + 하이브리드)

[Step 3] mirror 모드 구현
MirrorThread의 모든 기능을 포함:
- 화면 캡처 (네이티브 DLL / dxcam 폴백)
- weight_matrix 기반 per-LED 색상 계산
- ColorPipeline (감마/WB/채널믹싱/스무딩/밝기)
- 캡처 세션 사망 감지 + 자동 복구
- 모니터 분리/재연결 대응
- 디버그 프로파일링
- 실시간 레이아웃 파라미터 변경

[Step 4] audio 모드 추가
AudioVisualizer의 모든 기능을 포함:
- AudioEngine 초기화/관리 (WASAPI Loopback)
- pulse/spectrum/bass_detail 렌더링 로직
- 색상 소스: solid(단색) / rainbow(무지개)
- attack/release 스무딩
- bass_detail: raw FFT → 저역 16밴드 자체 처리
- 감도 조절 (bass/mid/high)
- 대역 비율 (zone_weights)
- energy_updated, spectrum_updated 시그널 활성화
- LED 둘레 매핑 (perimeter_t, band_mapping)

[Step 5] hybrid 모드 추가
HybridVisualizer의 모든 기능을 포함:
- 색상 소스: solid/rainbow + screen(화면 연동)
- ScreenSampler 통합 — zone 기반 또는 per-LED 미러링
- zone 매핑 (_build_led_zone_map_by_side)
- per-LED 미러링 (weight_matrix 기반)
- set_color_source() — 실행 중 색상 소스/구역 수 전환
- min_brightness — 화면 연동 시 최소 밝기 조절
- screen_colors_updated 시그널 — 프리뷰 위젯용
- 렌더링 메서드 공유 — audio/hybrid가 동일한 _render_pulse/_render_spectrum 사용

Signals:
    fps_updated(float): 1초마다 현재 FPS
    error(str, str): (메시지, 심각도) — "critical"=팝업, "warning"=상태바
    status_changed(str): 상태 변경 알림
    energy_updated(float, float, float): bass, mid, high (오디오/하이브리드 모드용)
    spectrum_updated(object): 16밴드 스펙트럼 (오디오/하이브리드 모드용)
    screen_colors_updated(object): 구역별 색상 (하이브리드 프리뷰용)
"""

import time
import os
import copy
import ctypes
import logging
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

try:
    from native_capture import NativeScreenCapture as ScreenCapture
    _NATIVE_CAPTURE = True
except ImportError:
    from core.capture import ScreenCapture
    _NATIVE_CAPTURE = False

from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.color import ColorPipeline
from core.color_correction import ColorCorrection
from core.audio_engine import AudioEngine, _build_log_bands
from core.screen_sampler import ScreenSampler

# ── 엔진 모드 상수 ────────────────────────────────────────────────
MODE_MIRROR = "mirror"
MODE_AUDIO = "audio"
MODE_HYBRID = "hybrid"

# ── 오디오 서브모드 상수 ──────────────────────────────────────────
AUDIO_PULSE = "pulse"
AUDIO_SPECTRUM = "spectrum"
AUDIO_BASS_DETAIL = "bass_detail"

# ── 색상 소스 상수 (hybrid 모드) ──────────────────────────────────
COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

# ── 특수 구역 수: LED 개별 미러링 ──────────────────────────────────
N_ZONES_PER_LED = -1

# ── 스크린 갱신 간격 (오디오 프레임 수 기준) ───────────────────────
SCREEN_UPDATE_INTERVAL = 3

# ── stale 복구 관련 상수 ──────────────────────────────────────────
_STALE_RECREATE_COOLDOWN = 3.0   # recreate 재시도 최소 간격 (초)
_STALE_LED_OFF_THRESHOLD = 10.0  # 이 시간 동안 프레임 없으면 LED 끄기 (초)

# ── 오디오 관련 상수 ──────────────────────────────────────────────
DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)

# 저역 세밀 모드 주파수 범위
BASS_DETAIL_FREQ_MIN = 20
BASS_DETAIL_FREQ_MAX = 500
BASS_DETAIL_N_BANDS = 16


# ══════════════════════════════════════════════════════════════════
#  오디오 모드용 유틸리티 (audio_visualizer.py에서 가져옴)
# ══════════════════════════════════════════════════════════════════

def _remap_t(t, zone_weights):
    """균등 둘레 비율 t(0~1)를 대역 비율에 맞게 색상/밴드 t로 변환."""
    b_pct = zone_weights[0] / 100.0
    m_pct = zone_weights[1] / 100.0
    h_pct = zone_weights[2] / 100.0

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


class UnifiedEngine(QThread):
    """단일 LED 엔진 — 모드에 따라 화면/오디오/하이브리드 소스 사용.

    Step 3: mirror 모드 완전 구현.
    Step 4: audio 모드 완전 구현.
    Step 5: hybrid 모드 완전 구현.
    """

    # ── 시그널 ─────────────────────────────────────────────────────
    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)

    # 오디오 모드용 시그널
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    screen_colors_updated = pyqtSignal(object)

    def __init__(self, config, audio_device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()
        self._paused = False

        # ── 엔진 모드 ──
        self.mode = MODE_MIRROR

        # ── 미러링 파라미터 (외부에서 실시간 변경 가능) ──
        self.brightness = config["mirror"]["brightness"]
        self.smoothing_enabled = True
        self.smoothing_factor = config["mirror"]["smoothing_factor"]

        # 밝기 변경 감지
        self._last_brightness = self.brightness

        # 레이아웃 재계산 플래그 + 락
        self._layout_dirty = False
        self._layout_lock = threading.Lock()

        # ── 오디오 파라미터 (외부에서 실시간 변경 가능) ──
        self.audio_mode = AUDIO_PULSE          # pulse / spectrum / bass_detail
        self.audio_brightness = 1.0            # 오디오 모드 밝기 (0~1)
        self.bass_sensitivity = 1.0
        self.mid_sensitivity = 1.0
        self.high_sensitivity = 1.0
        self.attack = 0.5
        self.release = 0.1
        self.input_smoothing = 0.3
        self.target_fps = DEFAULT_FPS

        # 색상 소스
        self.base_color = np.array([255, 0, 80], dtype=np.float32)
        self.rainbow = False

        # 대역 비율
        self._zone_weights = list(DEFAULT_ZONE_WEIGHTS)
        self._zone_dirty = False

        # 오디오 디바이스 인덱스
        self._audio_device_index = audio_device_index

        # ── 하이브리드 파라미터 (외부에서 실시간 변경 가능) ──
        self.color_source = COLOR_SOURCE_SOLID   # solid / screen
        self.n_zones = 4                         # 화면 구역 수
        self.min_brightness = MIN_BRIGHTNESS     # 최소 밝기 (screen 모드)

        # ── _init_resources()에서 초기화 ──
        self._capture = None
        self._device = None
        self._pipeline = None
        self._weight_matrix = None
        self._active_w = 0
        self._active_h = 0
        self._logger = None
        self._debug_profile = False
        self._expected_monitors = 0
        self._monitor_disconnected = False

        # 오디오 관련 — _init_audio_resources()에서 초기화
        self._audio_engine = None
        self._led_count = 0
        self._perimeter_t = None
        self._led_band_indices = None
        self._led_order = []
        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None
        self._cc = None  # ColorCorrection 인스턴스

        # bass_detail 모드 전용 상태
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # 하이브리드 관련 — _init_hybrid_resources()에서 초기화
        self._screen_sampler = None
        self._led_zone_map = None
        self._per_led_colors = None  # per-LED 미러링용

    @property
    def _running(self):
        return not self._stop_event.is_set()

    # ══════════════════════════════════════════════════════════════
    #  외부 제어 API (메인 스레드에서 호출)
    # ══════════════════════════════════════════════════════════════

    def update_layout_params(self, decay_radius=None, parallel_penalty=None,
                             decay_per_side=None, penalty_per_side=None):
        """레이아웃 파라미터를 실시간으로 변경."""
        with self._layout_lock:
            mirror_cfg = self.config["mirror"]
            if decay_radius is not None:
                mirror_cfg["decay_radius"] = decay_radius
            if parallel_penalty is not None:
                mirror_cfg["parallel_penalty"] = parallel_penalty
            if decay_per_side is not None:
                mirror_cfg["decay_radius_per_side"] = decay_per_side
            if penalty_per_side is not None:
                mirror_cfg["parallel_penalty_per_side"] = penalty_per_side
            self._layout_dirty = True

    def set_zone_weights(self, bass, mid, high):
        """오디오 대역 비율 변경."""
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def set_color(self, r, g, b):
        """오디오 단색 설정."""
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        """무지개 모드 설정."""
        self.rainbow = enabled

    def set_audio_mode(self, mode):
        """오디오 서브모드 변경 (pulse/spectrum/bass_detail)."""
        self.audio_mode = mode

    def set_color_source(self, source, n_zones=None):
        """색상 소스 변경 (solid/screen) + 구역 수 변경.

        실행 중에도 호출 가능 — 다음 프레임부터 반영됩니다.
        n_zones가 N_ZONES_PER_LED이면 per-LED weight_matrix 사용.
        """
        self.color_source = source

        if n_zones is not None and n_zones != self.n_zones:
            self.n_zones = n_zones

            if n_zones == N_ZONES_PER_LED:
                # per-LED 미러링용 weight_matrix 필요
                if self._weight_matrix is None and self._perimeter_t is not None:
                    mirror_cfg = self.config.get("mirror", {})
                    self._build_hybrid_weight_matrix(mirror_cfg)
            else:
                # zone 매핑 재계산
                if self._perimeter_t is not None:
                    self._led_zone_map = _build_led_zone_map_by_side(
                        self.config, n_zones
                    )
                # ScreenSampler에 구역 수 반영
                if self._screen_sampler is not None:
                    self._screen_sampler.set_n_zones(n_zones)

    def pause(self):
        self._paused = True
        self.status_changed.emit("일시정지")

    def resume(self):
        self._paused = False
        self.status_changed.emit("실행 중")

    def toggle_pause(self):
        if self._paused:
            self.resume()
        else:
            self.pause()

    def stop_engine(self):
        """엔진 중지 요청."""
        self._stop_event.set()

    # ══════════════════════════════════════════════════════════════
    #  모니터 연결 감지 (mirror 모드)
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _get_monitor_count():
        try:
            return ctypes.windll.user32.GetSystemMetrics(80)
        except Exception:
            return -1

    def _start_monitor_watcher(self):
        self._monitor_watcher_tick()

    def _monitor_watcher_tick(self):
        if self._stop_event.is_set():
            return

        current_monitors = self._get_monitor_count()

        if (not self._monitor_disconnected
                and current_monitors < self._expected_monitors):
            self._monitor_disconnected = True
            self.status_changed.emit("외부 모니터 분리 감지 — LED 대기 중...")

            try:
                self._device.turn_off()
            except (OSError, IOError, ValueError):
                pass

            if self._capture:
                try:
                    self._capture.stop()
                except Exception:
                    pass

        elif (self._monitor_disconnected
              and current_monitors >= self._expected_monitors):
            self.status_changed.emit("외부 모니터 재연결 — 캡처 재초기화...")

            try:
                mirror_cfg = self.config["mirror"]
                if _NATIVE_CAPTURE:
                    self._capture = ScreenCapture(
                        monitor_index=mirror_cfg["monitor_index"],
                        grid_cols=mirror_cfg["grid_cols"],
                        grid_rows=mirror_cfg["grid_rows"],
                    )
                else:
                    self._capture = ScreenCapture(mirror_cfg["monitor_index"])
                self._capture.start(target_fps=mirror_cfg["target_fps"])
                self._active_w = self._capture.screen_w
                self._active_h = self._capture.screen_h
                self._weight_matrix = self._build_layout(
                    self._active_w, self._active_h
                )
                self._rebuild_pipeline()
                self._monitor_disconnected = False
                self.status_changed.emit("실행 중")
            except Exception:
                pass

        if not self._stop_event.is_set():
            timer = threading.Timer(1.0, self._monitor_watcher_tick)
            timer.daemon = True
            timer.start()

    # ══════════════════════════════════════════════════════════════
    #  레이아웃 계산 (mirror 모드)
    # ══════════════════════════════════════════════════════════════

    def _build_layout(self, w, h):
        """LED 위치 + 가중치 행렬을 (w, h) 해상도 기준으로 계산."""
        mirror_cfg = self.config["mirror"]
        layout_cfg = self.config["layout"]
        led_count = self.config["device"]["led_count"]

        base_decay = mirror_cfg["decay_radius"]
        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        decay_param = (
            {s: per_decay.get(s, base_decay)
             for s in ("top", "bottom", "left", "right")}
            if per_decay else base_decay
        )

        base_penalty = mirror_cfg["parallel_penalty"]
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        penalty_param = (
            {s: per_penalty.get(s, base_penalty)
             for s in ("top", "bottom", "left", "right")}
            if per_penalty else base_penalty
        )

        positions, sides = get_led_positions(
            w, h,
            layout_cfg["segments"], led_count,
            orientation=mirror_cfg.get("orientation", "auto"),
            portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
        )
        wmat = build_weight_matrix(
            w, h, positions, sides,
            mirror_cfg["grid_cols"], mirror_cfg["grid_rows"],
            decay_param, penalty_param,
        )
        return wmat

    def _rebuild_pipeline(self):
        """가중치 행렬 변경 후 ColorPipeline 재생성."""
        color_cfg = self.config["color"]
        mirror_cfg = self.config["mirror"]
        mirror_cfg_copy = dict(mirror_cfg)
        mirror_cfg_copy["brightness"] = self.brightness
        mirror_cfg_copy["smoothing_factor"] = self.smoothing_factor

        self._pipeline = ColorPipeline(
            self._weight_matrix, color_cfg, mirror_cfg_copy
        )

    # ══════════════════════════════════════════════════════════════
    #  오디오 밴드 매핑
    # ══════════════════════════════════════════════════════════════

    def _rebuild_band_mapping(self):
        """대역 비율 변경 시 LED→밴드 매핑 재계산."""
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        self._zone_dirty = False

    # ══════════════════════════════════════════════════════════════
    #  리소스 초기화
    # ══════════════════════════════════════════════════════════════

    def _init_resources(self):
        """모드에 따라 필요한 리소스를 초기화."""
        cfg = self.config
        dev_cfg = cfg["device"]
        mirror_cfg = cfg["mirror"]

        self._led_count = dev_cfg["led_count"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)

        # 디버그 프로파일 설정
        self._debug_profile = cfg.get("options", {}).get("debug_profile", False)
        log_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log"
        )
        self._logger = logging.getLogger("nanoleaf.engine")
        if self._debug_profile:
            self._logger.setLevel(logging.DEBUG)
            if not self._logger.handlers:
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self._logger.addHandler(fh)
            self._logger.propagate = False

        try:
            # ── 화면 캡처 (mirror/hybrid 모드) ──
            if self.mode in (MODE_MIRROR, MODE_HYBRID):
                self._init_mirror_resources(mirror_cfg)

            # ── 오디오 (audio/hybrid 모드) ──
            if self.mode in (MODE_AUDIO, MODE_HYBRID):
                self._init_audio_resources()

            # ── 하이브리드 전용 (ScreenSampler + zone map) ──
            if self.mode == MODE_HYBRID:
                self._init_hybrid_resources()

            # ── Nanoleaf 장치 ──
            self.status_changed.emit("Nanoleaf 연결 중...")
            self._device = NanoleafDevice(vendor_id, product_id, self._led_count)
            self._device.connect()

            self._expected_monitors = self._get_monitor_count()
            return True

        except (OSError, IOError, ValueError, ConnectionError) as e:
            self.error.emit(str(e), "critical")
            self._cleanup_partial()
            return False

    def _init_mirror_resources(self, mirror_cfg):
        """미러링용 리소스 초기화 — 캡처 + weight_matrix + pipeline."""
        target_fps = mirror_cfg["target_fps"]

        self.status_changed.emit("화면 캡처 초기화...")
        if _NATIVE_CAPTURE:
            self._capture = ScreenCapture(
                monitor_index=mirror_cfg["monitor_index"],
                grid_cols=mirror_cfg["grid_cols"],
                grid_rows=mirror_cfg["grid_rows"],
            )
        else:
            self._capture = ScreenCapture(mirror_cfg["monitor_index"])
        self._capture.start(target_fps=target_fps)

        if self._debug_profile:
            self._logger.debug(
                f"screen: {self._capture.screen_w}x{self._capture.screen_h}"
            )

        # 가중치 행렬
        self.status_changed.emit("가중치 행렬 생성...")
        self._active_w = self._capture.screen_w
        self._active_h = self._capture.screen_h
        self._weight_matrix = self._build_layout(
            self._active_w, self._active_h
        )

        # ColorPipeline 생성
        self._rebuild_pipeline()

    def _init_audio_resources(self):
        """오디오용 리소스 초기화 — AudioEngine + LED 매핑 + ColorCorrection."""
        # ColorCorrection — 감마·채널 믹싱·WB 보정
        self._cc = ColorCorrection(self.config.get("color", {}))

        # 오디오 엔진
        self.status_changed.emit("오디오 캡처 초기화...")
        self._audio_engine = AudioEngine(
            device_index=self._audio_device_index,
            sensitivity=1.0,
            smoothing=0.15,
        )
        self._audio_engine.bass_sensitivity = self.bass_sensitivity
        self._audio_engine.mid_sensitivity = self.mid_sensitivity
        self._audio_engine.high_sensitivity = self.high_sensitivity
        self._audio_engine.start()

        n_bands = self._audio_engine.n_bands

        # LED 둘레 매핑
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(
            segments, self._led_count
        )

        # 스무딩 상태
        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        # bass_detail 밴드 분할 초기화
        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX,
            fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)

    def _init_hybrid_resources(self):
        """하이브리드 모드 전용 리소스 초기화 — ScreenSampler + zone map + weight_matrix."""
        mirror_cfg = self.config.get("mirror", {})

        # zone 매핑
        if self.n_zones != N_ZONES_PER_LED:
            self._led_zone_map = _build_led_zone_map_by_side(
                self.config, self.n_zones
            )

        # per-LED 미러링용 weight matrix
        if self.n_zones == N_ZONES_PER_LED:
            self._build_hybrid_weight_matrix(mirror_cfg)

        # ScreenSampler 초기화 (color_source가 screen일 때)
        if self.color_source != COLOR_SOURCE_SOLID:
            self._init_screen_sampler(mirror_cfg)

    def _build_hybrid_weight_matrix(self, mirror_cfg):
        """per-LED 미러링용 가중치 행렬 생성 (hybrid 모드)."""
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
        """ScreenSampler 초기화 — 실패 시 solid 모드로 폴백."""
        self.status_changed.emit("화면 캡처 초기화...")
        try:
            self._screen_sampler = ScreenSampler(
                n_zones=self.n_zones if self.n_zones != N_ZONES_PER_LED else 4,
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
        """screen 소스인데 sampler가 없으면 초기화 시도."""
        if self._screen_sampler is not None:
            return True
        if self.color_source == COLOR_SOURCE_SOLID:
            return True

        mirror_cfg = self.config.get("mirror", {})
        self._init_screen_sampler(mirror_cfg)
        return self._screen_sampler is not None

    def _cleanup_partial(self):
        """초기화 도중 실패 시 이미 생성된 리소스만 정리."""
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        if self._screen_sampler:
            self._screen_sampler.stop()
            self._screen_sampler = None
        if self._capture:
            self._capture.stop()
            self._capture = None

    # ══════════════════════════════════════════════════════════════
    #  QThread 진입점
    # ══════════════════════════════════════════════════════════════

    def run(self):
        if not self._init_resources():
            return

        try:
            if self.mode == MODE_MIRROR:
                self._run_mirror()
            elif self.mode == MODE_AUDIO:
                self._run_audio()
            elif self.mode == MODE_HYBRID:
                self._run_hybrid()
            else:
                self.error.emit(f"알 수 없는 모드: {self.mode}", "critical")
        except Exception as e:
            self.error.emit(f"엔진 오류: {e}", "warning")
        finally:
            self._cleanup()

    # ══════════════════════════════════════════════════════════════
    #  오디오 루프 (Step 4)
    # ══════════════════════════════════════════════════════════════

    def _run_audio(self):
        """오디오 비주얼라이저 메인 루프."""
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        self.status_changed.emit("오디오 비주얼라이저 실행 중")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ── 일시정지 ──
            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 대역 비율 변경 반영 ──
            if self._zone_dirty:
                self._rebuild_band_mapping()

            # ── 오디오 엔진 파라미터 실시간 반영 ──
            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            # ── 오디오 데이터 획득 ──
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # ── Attack/Release 스무딩 ──
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

            mode = self.audio_mode

            # ── 렌더링 ──
            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data = self._render_spectrum(spec)
            else:  # pulse
                grb_data = self._render_pulse(bass, mid, high)

            # ── USB 전송 ──
            try:
                self._device.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── UI 시그널 (매 3프레임) ──
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  하이브리드 루프 (Step 5)
    # ══════════════════════════════════════════════════════════════

    def _run_hybrid(self):
        """하이브리드 비주얼라이저 메인 루프 — 오디오 + 화면 색상 소스."""
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ── 일시정지 ──
            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 대역 비율 변경 반영 ──
            if self._zone_dirty:
                self._rebuild_band_mapping()

            # ── screen 소스 보장 ──
            if self.color_source != COLOR_SOURCE_SOLID:
                self._ensure_screen_sampler()

            # ── 오디오 엔진 파라미터 실시간 반영 ──
            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            # ── 오디오 데이터 획득 ──
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # ── Attack/Release 스무딩 ──
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

            # ── 스크린 갱신 (매 N프레임) ──
            if (self._screen_sampler is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                self._screen_sampler.update()

                # per-LED 미러링: weight_matrix로 frame → per-LED 색상
                if (self.n_zones == N_ZONES_PER_LED
                        and self._weight_matrix is not None):
                    frame = self._screen_sampler.get_last_frame()
                    if frame is not None:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat

            # ── 렌더링 (렌더 메서드는 audio와 공유) ──
            mode = self.audio_mode
            bd_spec = None

            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data = self._render_spectrum(spec)
            else:  # pulse
                grb_data = self._render_pulse(bass, mid, high)

            # ── USB 전송 ──
            try:
                self._device.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── UI 시그널 (매 3프레임) ──
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

                # screen_colors_updated 시그널 (프리뷰용)
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

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  오디오 렌더링 — Pulse
    # ══════════════════════════════════════════════════════════════

    def _render_pulse(self, bass, mid, high):
        """Bass 에너지 기반 전체 밝기 + mid/high 색상 변조."""
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        min_b = self.min_brightness if self.mode == MODE_HYBRID else MIN_BRIGHTNESS
        intensity = max(min_b, bass) * self.audio_brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_base_color(led_idx, n_bands)
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix
            leds[led_idx] = c * intensity

        # ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ══════════════════════════════════════════════════════════════
    #  오디오 렌더링 — Spectrum (spectrum + bass_detail 공용)
    # ══════════════════════════════════════════════════════════════

    def _render_spectrum(self, spec):
        """주파수 밴드별 밝기 — 각 LED의 둘레 위치에 매핑."""
        n_leds = self._led_count
        n_bands = len(spec)
        min_b = self.min_brightness if self.mode == MODE_HYBRID else MIN_BRIGHTNESS
        leds = np.zeros((n_leds, 3), dtype=np.float32)

        for led_idx in range(n_leds):
            band_f = self._led_band_indices[led_idx]
            band_lo = max(0, min(int(band_f), n_bands - 1))
            band_hi = min(band_lo + 1, n_bands - 1)
            frac = band_f - int(band_f)
            energy = spec[band_lo] * (1 - frac) + spec[band_hi] * frac

            color = self._get_base_color(led_idx, n_bands)
            intensity = max(min_b, energy) * self.audio_brightness
            leds[led_idx] = color * intensity

        # ColorCorrection 적용 후 GRB 변환
        self._cc.apply(leds)
        return self._leds_to_grb(leds)

    # ══════════════════════════════════════════════════════════════
    #  오디오 — Bass Detail 처리
    # ══════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════
    #  오디오 — 색상 헬퍼
    # ══════════════════════════════════════════════════════════════

    def _get_base_color(self, led_idx, n_bands):
        """LED 인덱스 → RGB 색상.

        색상 소스에 따라 분기:
        - solid: 단색 또는 무지개
        - screen: 화면 구역색 또는 per-LED 미러링색
        """
        source = self.color_source

        if source == COLOR_SOURCE_SCREEN and self.mode == MODE_HYBRID:
            return self._get_screen_color(led_idx, n_bands)

        # solid / rainbow (audio 모드 또는 hybrid solid)
        return self._get_solid_color(led_idx, n_bands)

    def _get_solid_color(self, led_idx, n_bands):
        """단색 또는 무지개 색상."""
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    def _get_screen_color(self, led_idx, n_bands):
        """화면 색상 소스 — zone 매핑 또는 per-LED 미러링."""
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
    def _ar(current, target, attack_rate, release_rate):
        """Attack/Release 스무딩."""
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    @staticmethod
    def _leds_to_grb(leds):
        """RGB float32 배열 → GRB bytes."""
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]
        grb[:, 1] = u8[:, 0]
        grb[:, 2] = u8[:, 2]
        return grb.tobytes()

    # ══════════════════════════════════════════════════════════════
    #  미러링 루프
    # ══════════════════════════════════════════════════════════════

    def _run_mirror(self):
        """미러링 메인 루프 — 디버그/비디버그 분기."""
        mirror_cfg = self.config["mirror"]
        target_fps = mirror_cfg["target_fps"]
        frame_interval = 1.0 / target_fps

        prev_colors = None
        frame_count = 0
        fps_start_time = time.monotonic()
        fps_display_time = fps_start_time
        last_good_frame_time = time.monotonic()
        STALE_THRESHOLD = 3.0
        pipeline = self._pipeline

        self.status_changed.emit("미러링 실행 중")
        self._start_monitor_watcher()

        if self._debug_profile:
            self._mirror_loop_debug(
                pipeline, prev_colors, frame_count,
                fps_start_time, fps_display_time,
                last_good_frame_time, frame_interval,
                STALE_THRESHOLD, mirror_cfg,
            )
        else:
            self._mirror_loop_fast(
                pipeline, prev_colors, frame_count,
                fps_start_time, fps_display_time,
                last_good_frame_time, frame_interval,
                STALE_THRESHOLD, mirror_cfg,
            )

    def _mirror_loop_fast(self, pipeline, prev_colors, frame_count,
                          fps_start_time, fps_display_time,
                          last_good_frame_time, frame_interval,
                          STALE_THRESHOLD, mirror_cfg):
        """비디버그 고속 미러링 루프."""

        stop_wait = self._stop_event.wait
        last_recreate_time = 0.0
        led_turned_off = False

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ── 일시정지 ──
            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 외부 모니터 분리 ──
            if self._monitor_disconnected:
                if stop_wait(timeout=0.5):
                    break
                continue

            # ── 레이아웃 파라미터 실시간 반영 ──
            if self._layout_dirty:
                with self._layout_lock:
                    self._layout_dirty = False
                try:
                    self._weight_matrix = self._build_layout(
                        self._active_w, self._active_h
                    )
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass

            # ── 밝기 실시간 반영 ──
            current_brightness = self.brightness
            if current_brightness != self._last_brightness:
                pipeline.update_brightness(current_brightness)
                self._last_brightness = current_brightness

            # ── 스무딩 실시간 반영 ──
            pipeline.smoothing = self.smoothing_factor
            pipeline.smoothing_enabled = self.smoothing_enabled

            # ── 캡처 ──
            frame = self._capture.grab()

            if frame is None:
                now = time.monotonic()
                stale_duration = now - last_good_frame_time

                if stale_duration > STALE_THRESHOLD:
                    if now - last_recreate_time >= _STALE_RECREATE_COOLDOWN:
                        last_recreate_time = now
                        self.status_changed.emit("캡처 복구 중...")

                        self._capture._recreate()

                        if (self._capture.screen_w > 0
                                and self._capture.screen_h > 0
                                and (self._capture.screen_w != self._active_w
                                     or self._capture.screen_h != self._active_h)):
                            self._active_w = self._capture.screen_w
                            self._active_h = self._capture.screen_h
                            try:
                                self._weight_matrix = self._build_layout(
                                    self._active_w, self._active_h
                                )
                                self._rebuild_pipeline()
                                pipeline = self._pipeline
                                prev_colors = None
                            except (ValueError, IndexError):
                                pass

                if stale_duration > _STALE_LED_OFF_THRESHOLD and not led_turned_off:
                    try:
                        self._device.turn_off()
                        led_turned_off = True
                        self.status_changed.emit("캡처 없음 — LED 대기 중")
                    except (OSError, IOError, ValueError):
                        pass

                if stop_wait(timeout=0.01):
                    break
                continue

            # ── 프레임 수신 성공 ──
            last_good_frame_time = time.monotonic()
            if led_turned_off:
                led_turned_off = False
                self.status_changed.emit("미러링 실행 중")

            # ── 해상도/회전 변경 감지 ──
            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                continue

            if current_h != self._active_h or current_w != self._active_w:
                self._active_w, self._active_h = current_w, current_h
                self._capture.screen_w = current_w
                self._capture.screen_h = current_h
                try:
                    self._weight_matrix = self._build_layout(current_w, current_h)
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass

            # ── 색상 연산 ──
            try:
                grb_data, rgb_colors = pipeline.process(frame, prev_colors)
                prev_colors = rgb_colors
            except (ValueError, IndexError, FloatingPointError):
                prev_colors = None
                continue

            # ── USB 전송 ──
            try:
                self._device.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 대기 중...")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display_time >= 1.0:
                elapsed = now - fps_start_time
                fps = frame_count / elapsed if elapsed > 0 else 0
                self.fps_updated.emit(fps)
                fps_display_time = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    def _mirror_loop_debug(self, pipeline, prev_colors, frame_count,
                           fps_start_time, fps_display_time,
                           last_good_frame_time, frame_interval,
                           STALE_THRESHOLD, mirror_cfg):
        """디버그 프로파일링 미러링 루프."""
        PROFILE_INTERVAL = 60
        t_capture_acc = 0.0
        t_color_acc = 0.0
        t_usb_acc = 0.0
        t_total_acc = 0.0
        fps = 0.0

        stop_wait = self._stop_event.wait
        last_recreate_time = 0.0
        led_turned_off = False

        while not self._stop_event.is_set():
            loop_start = time.perf_counter()

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            if self._monitor_disconnected:
                if stop_wait(timeout=0.5):
                    break
                continue

            if self._layout_dirty:
                with self._layout_lock:
                    self._layout_dirty = False
                try:
                    self._weight_matrix = self._build_layout(
                        self._active_w, self._active_h
                    )
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                    self._logger.debug(
                        f"layout rebuilt (live): wmat={self._weight_matrix.shape}"
                    )
                except (ValueError, IndexError, np.linalg.LinAlgError) as e:
                    self._logger.debug(f"live layout rebuild error: {e}")

            # 밝기/스무딩 반영
            current_brightness = self.brightness
            if current_brightness != self._last_brightness:
                pipeline.update_brightness(current_brightness)
                self._last_brightness = current_brightness
            pipeline.smoothing = self.smoothing_factor
            pipeline.smoothing_enabled = self.smoothing_enabled

            # 캡처
            t0 = time.perf_counter()
            frame = self._capture.grab()
            t_capture_acc += time.perf_counter() - t0

            if frame is None:
                now_mono = time.monotonic()
                stale_duration = now_mono - last_good_frame_time

                if stale_duration > STALE_THRESHOLD:
                    if now_mono - last_recreate_time >= _STALE_RECREATE_COOLDOWN:
                        last_recreate_time = now_mono
                        self._logger.debug("stale detected, recreating capture...")

                        self._capture._recreate()
                        if (self._capture.screen_w > 0
                                and self._capture.screen_h > 0
                                and (self._capture.screen_w != self._active_w
                                     or self._capture.screen_h != self._active_h)):
                            self._active_w = self._capture.screen_w
                            self._active_h = self._capture.screen_h
                            try:
                                self._weight_matrix = self._build_layout(
                                    self._active_w, self._active_h
                                )
                                self._rebuild_pipeline()
                                pipeline = self._pipeline
                                prev_colors = None
                            except (ValueError, IndexError):
                                pass

                if stale_duration > _STALE_LED_OFF_THRESHOLD and not led_turned_off:
                    try:
                        self._device.turn_off()
                        led_turned_off = True
                        self._logger.debug("LED turned off due to stale capture")
                    except (OSError, IOError, ValueError):
                        pass

                if stop_wait(timeout=0.01):
                    break
                continue

            last_good_frame_time = time.monotonic()
            if led_turned_off:
                led_turned_off = False
                self._logger.debug("capture restored, LED resuming")

            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                continue

            if current_h != self._active_h or current_w != self._active_w:
                self._active_w, self._active_h = current_w, current_h
                self._capture.screen_w = current_w
                self._capture.screen_h = current_h
                try:
                    self._weight_matrix = self._build_layout(current_w, current_h)
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                    self._logger.debug(
                        f"layout rebuilt: {current_w}x{current_h}"
                    )
                except (ValueError, IndexError, np.linalg.LinAlgError) as e:
                    self._logger.debug(f"layout rebuild error: {e}")

            # 색상 연산
            t1 = time.perf_counter()
            try:
                grb_data, rgb_colors = pipeline.process(frame, prev_colors)
                prev_colors = rgb_colors
            except (ValueError, IndexError, FloatingPointError):
                prev_colors = None
                continue
            t_color_acc += time.perf_counter() - t1

            # USB 전송
            t2 = time.perf_counter()
            try:
                self._device.send_rgb(grb_data)
            except (OSError, IOError, ValueError):
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 대기 중...")
                if stop_wait(timeout=1.0):
                    break
                continue
            t_usb_acc += time.perf_counter() - t2

            # FPS + 프로파일링
            frame_count += 1
            t_total_acc += time.perf_counter() - loop_start

            now = time.monotonic()
            if now - fps_display_time >= 1.0:
                elapsed = now - fps_start_time
                fps = frame_count / elapsed if elapsed > 0 else 0
                self.fps_updated.emit(fps)
                fps_display_time = now

            if frame_count % PROFILE_INTERVAL == 0:
                n = PROFILE_INTERVAL
                avg_cap = t_capture_acc / n * 1000
                avg_color = t_color_acc / n * 1000
                avg_usb = t_usb_acc / n * 1000
                avg_total = t_total_acc / n * 1000
                avg_sleep = max(0, frame_interval - avg_total / 1000) * 1000
                self._logger.debug(
                    f"[PROFILE] capture={avg_cap:.2f}ms  "
                    f"color={avg_color:.2f}ms  "
                    f"usb={avg_usb:.2f}ms  "
                    f"total={avg_total:.2f}ms  "
                    f"sleep≈{avg_sleep:.2f}ms  "
                    f"fps={fps:.1f}"
                )
                t_capture_acc = t_color_acc = t_usb_acc = t_total_acc = 0.0

            elapsed = time.perf_counter() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  정리
    # ══════════════════════════════════════════════════════════════

    def _cleanup(self):
        """모든 리소스 안전하게 해제."""
        # 오디오 엔진 정리
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None

        # ScreenSampler 정리
        if self._screen_sampler:
            self._screen_sampler.stop()
            self._screen_sampler = None

        # Nanoleaf 장치 정리
        try:
            if self._device:
                self._device.turn_off()
                self._device.disconnect()
        except (OSError, IOError, ValueError):
            pass

        # 캡처 정리
        if self._capture:
            self._capture.stop()

        self.status_changed.emit("엔진 중지됨")
