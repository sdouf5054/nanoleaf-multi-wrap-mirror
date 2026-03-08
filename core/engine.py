"""UnifiedEngine — 단일 LED 엔진 (미러링 + 오디오 + 하이브리드)

미러링 모드의 메인 루프와 공통 리소스 관리를 담당합니다.
오디오/하이브리드 루프와 렌더링은 AudioEngineMixin(engine_audio.py)에,
유틸리티 함수와 공용 상수는 engine_utils.py에 있습니다.

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

# ── engine_utils에서 상수 + 유틸리티 함수 import ──────────────────
from core.engine_utils import (
    # 상수 — 외부 모듈(main_window, tab_control 등)에서
    # `from core.engine import MODE_MIRROR` 등으로 참조하므로 re-export
    MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN,
    N_ZONES_PER_LED, SCREEN_UPDATE_INTERVAL,
    _STALE_RECREATE_COOLDOWN, _STALE_LED_OFF_THRESHOLD,
    DEFAULT_FPS, MIN_BRIGHTNESS, DEFAULT_ZONE_WEIGHTS,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    # 유틸리티 함수
    _remap_t,
    _compute_led_perimeter_t,
    _compute_led_band_mapping,
    _build_led_order_from_segments,
    _build_led_zone_map_by_side,
)

# ── engine_audio에서 오디오 Mixin import ──────────────────────────
from core.engine_audio import AudioEngineMixin

# ── 공용 에러 튜플 ───────────────────────────────────────────────
from core.constants import HW_ERRORS, HW_CONNECT_ERRORS


class _MirrorProfiler:
    """미러링 루프 프로파일링 — debug_profile=True일 때만 생성.

    매 프레임의 캡처/색상/USB/전체 소요 시간을 누적하고,
    PROFILE_INTERVAL 프레임마다 평균값을 로그에 출력합니다.
    """

    PROFILE_INTERVAL = 60

    def __init__(self, logger, frame_interval):
        self._logger = logger
        self._frame_interval = frame_interval
        self._t_capture = 0.0
        self._t_color = 0.0
        self._t_usb = 0.0
        self._t_total = 0.0

    def add_capture(self, dt):
        self._t_capture += dt

    def add_color(self, dt):
        self._t_color += dt

    def add_usb(self, dt):
        self._t_usb += dt

    def add_total(self, dt):
        self._t_total += dt

    def maybe_log(self, frame_count, fps):
        """PROFILE_INTERVAL 프레임마다 평균 소요 시간을 로그에 출력."""
        if frame_count % self.PROFILE_INTERVAL != 0:
            return

        n = self.PROFILE_INTERVAL
        avg_cap = self._t_capture / n * 1000
        avg_color = self._t_color / n * 1000
        avg_usb = self._t_usb / n * 1000
        avg_total = self._t_total / n * 1000
        avg_sleep = max(0, self._frame_interval - avg_total / 1000) * 1000

        self._logger.debug(
            f"[PROFILE] capture={avg_cap:.2f}ms  "
            f"color={avg_color:.2f}ms  "
            f"usb={avg_usb:.2f}ms  "
            f"total={avg_total:.2f}ms  "
            f"sleep≈{avg_sleep:.2f}ms  "
            f"fps={fps:.1f}"
        )
        self._t_capture = self._t_color = self._t_usb = self._t_total = 0.0


class UnifiedEngine(AudioEngineMixin, QThread):
    """단일 LED 엔진 — 모드에 따라 화면/오디오/하이브리드 소스 사용.

    미러링 루프 + 공통 리소스 관리는 이 클래스에,
    오디오/하이브리드 루프와 렌더링은 AudioEngineMixin에 있습니다.
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
        self.n_zones = 4                         # 화면 구역 수 (하이브리드)
        self.mirror_n_zones = N_ZONES_PER_LED    # 미러링 구역 수 (-1=per-LED)
        self.min_brightness = MIN_BRIGHTNESS     # 최소 밝기 (screen 모드)
        self.audio_min_brightness = MIN_BRIGHTNESS  # 오디오/하이브리드 최소 밝기

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
            except HW_ERRORS:
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

        except HW_CONNECT_ERRORS as e:
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

        # 가중치 행렬 (항상 빌드 — per-LED 모드 + 프리뷰용)
        self.status_changed.emit("가중치 행렬 생성...")
        self._active_w = self._capture.screen_w
        self._active_h = self._capture.screen_h
        self._weight_matrix = self._build_layout(
            self._active_w, self._active_h
        )

        # 구역 기반 모드 초기화
        self._mirror_zone_map = None
        self._mirror_cc = None
        if self.mirror_n_zones != N_ZONES_PER_LED:
            self._mirror_zone_map = _build_led_zone_map_by_side(
                self.config, self.mirror_n_zones
            )
            self._mirror_cc = ColorCorrection(self.config.get("color", {}))

        # ColorPipeline 생성 (per-LED 모드용)
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
    #  미러링 루프
    # ══════════════════════════════════════════════════════════════

    # ── 구역 기반 미러링 색상 계산 ────────────────────────────────

    def _compute_mirror_zone_colors(self, frame):
        """프레임에서 구역별 평균 색상을 계산하고 LED에 할당.

        Returns:
            grb_data (bytes): GRB 바이트 데이터
            또는 None: 계산 실패 시
        """
        grid_flat = frame.reshape(-1, 3).astype(np.float32)
        n_zones = self.mirror_n_zones
        grid_rows = frame.shape[0]
        grid_cols = frame.shape[1] if frame.ndim >= 2 else 1

        # 구역별 평균 색상 계산
        zone_colors = np.zeros((n_zones, 3), dtype=np.float32)

        if n_zones == 1:
            zone_colors[0] = grid_flat.mean(axis=0)
        elif n_zones == 2:
            mid = grid_rows // 2
            zone_colors[0] = frame[:mid].reshape(-1, 3).astype(np.float32).mean(axis=0)
            zone_colors[1] = frame[mid:].reshape(-1, 3).astype(np.float32).mean(axis=0)
        elif n_zones == 4:
            mid_r, mid_c = grid_rows // 2, grid_cols // 2
            zone_colors[0] = frame[:mid_r, :].reshape(-1, 3).astype(np.float32).mean(axis=0)
            zone_colors[1] = frame[:, mid_c:].reshape(-1, 3).astype(np.float32).mean(axis=0)
            zone_colors[2] = frame[mid_r:, :].reshape(-1, 3).astype(np.float32).mean(axis=0)
            zone_colors[3] = frame[:, :mid_c].reshape(-1, 3).astype(np.float32).mean(axis=0)
        else:
            # 일반 N구역: 화면을 N등분 (시계방향 매핑과 일치)
            zone_counts = np.zeros(n_zones, dtype=np.int32)
            total = grid_rows * grid_cols
            for pi in range(total):
                r_idx = pi // grid_cols
                c_idx = pi % grid_cols
                rx = c_idx / max(1, grid_cols - 1)
                ry = r_idx / max(1, grid_rows - 1)
                if ry < 0.2:
                    cw_t = rx * 0.25
                elif rx > 0.8:
                    cw_t = 0.25 + ry * 0.25
                elif ry > 0.8:
                    cw_t = 0.50 + (1 - rx) * 0.25
                elif rx < 0.2:
                    cw_t = 0.75 + (1 - ry) * 0.25
                else:
                    cw_t = 0.0
                zi = min(int(cw_t * n_zones), n_zones - 1)
                zone_colors[zi] += grid_flat[pi]
                zone_counts[zi] += 1

            for zi in range(n_zones):
                if zone_counts[zi] > 0:
                    zone_colors[zi] /= zone_counts[zi]

        # LED에 구역 색상 할당
        leds = zone_colors[self._mirror_zone_map]  # fancy indexing

        # 밝기 적용
        leds *= self.brightness

        # 색상 보정 + GRB 변환
        self._mirror_cc.apply(leds)
        return self._leds_to_grb(leds), leds

    # ── 미러링 루프 ──────────────────────────────────────────────

    def _run_mirror(self):
        """미러링 메인 루프."""
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

        # 프로파일링 (debug 모드에서만 활성)
        profiler = _MirrorProfiler(
            self._logger, frame_interval
        ) if self._debug_profile else None

        self._mirror_loop(
            pipeline, prev_colors, frame_count,
            fps_start_time, fps_display_time,
            last_good_frame_time, frame_interval,
            STALE_THRESHOLD, profiler,
        )

    def _mirror_loop(self, pipeline, prev_colors, frame_count,
                     fps_start_time, fps_display_time,
                     last_good_frame_time, frame_interval,
                     STALE_THRESHOLD, profiler):
        """미러링 메인 루프 — 프로파일링은 profiler가 None이 아닐 때만 동작."""

        stop_wait = self._stop_event.wait
        last_recreate_time = 0.0
        led_turned_off = False
        debug = profiler is not None
        # 타이머 함수: debug 모드에서는 perf_counter (고해상도)
        _timer = time.perf_counter if debug else time.monotonic

        while not self._stop_event.is_set():
            loop_start = _timer()

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
                    if debug:
                        self._logger.debug(
                            f"layout rebuilt (live): "
                            f"wmat={self._weight_matrix.shape}"
                        )
                except (ValueError, IndexError, np.linalg.LinAlgError) as e:
                    if debug:
                        self._logger.debug(f"live layout rebuild error: {e}")

            # ── 밝기 실시간 반영 ──
            current_brightness = self.brightness
            if current_brightness != self._last_brightness:
                pipeline.update_brightness(current_brightness)
                self._last_brightness = current_brightness

            # ── 스무딩 실시간 반영 ──
            pipeline.smoothing = self.smoothing_factor
            pipeline.smoothing_enabled = self.smoothing_enabled

            # ── 캡처 ──
            if debug:
                t0 = _timer()
            frame = self._capture.grab()
            if debug:
                profiler.add_capture(_timer() - t0)

            if frame is None:
                now = time.monotonic()
                stale_duration = now - last_good_frame_time

                if stale_duration > STALE_THRESHOLD:
                    if now - last_recreate_time >= _STALE_RECREATE_COOLDOWN:
                        last_recreate_time = now
                        if debug:
                            self._logger.debug(
                                "stale detected, recreating capture..."
                            )
                        else:
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
                        if debug:
                            self._logger.debug(
                                "LED turned off due to stale capture"
                            )
                        else:
                            self.status_changed.emit("캡처 없음 — LED 대기 중")
                    except HW_ERRORS:
                        pass

                if stop_wait(timeout=0.01):
                    break
                continue

            # ── 프레임 수신 성공 ──
            last_good_frame_time = time.monotonic()
            if led_turned_off:
                led_turned_off = False
                if debug:
                    self._logger.debug("capture restored, LED resuming")
                else:
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
                    self._weight_matrix = self._build_layout(
                        current_w, current_h
                    )
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                    if debug:
                        self._logger.debug(
                            f"layout rebuilt: {current_w}x{current_h}"
                        )
                except (ValueError, IndexError, np.linalg.LinAlgError) as e:
                    if debug:
                        self._logger.debug(f"layout rebuild error: {e}")

            # ── 색상 연산 ──
            if debug:
                t1 = _timer()

            if self._mirror_zone_map is not None:
                # 구역 기반 모드
                try:
                    grb_data, leds_preview = self._compute_mirror_zone_colors(
                        frame
                    )
                    prev_colors = None  # zone 모드에서는 스무딩 미사용

                    # 프리뷰 전달 (보정 전)
                    if frame_count % 5 == 0:
                        self.screen_colors_updated.emit(leds_preview.tolist())
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue
            else:
                # per-LED 모드: 기존 pipeline 사용
                try:
                    grb_data, rgb_colors = pipeline.process(
                        frame, prev_colors
                    )
                    prev_colors = rgb_colors
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue

                # 프리뷰 전달 (5프레임마다, 보정 전 원본 색상)
                if frame_count % 5 == 0 and frame is not None:
                    try:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        raw_rgb = self._weight_matrix @ grid_flat
                        self.screen_colors_updated.emit(raw_rgb.tolist())
                    except Exception:
                        pass

            if debug:
                profiler.add_color(_timer() - t1)

            # ── USB 전송 ──
            if debug:
                t2 = _timer()

            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 대기 중...")
                if stop_wait(timeout=1.0):
                    break
                continue

            if debug:
                profiler.add_usb(_timer() - t2)

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display_time >= 1.0:
                elapsed = now - fps_start_time
                fps = frame_count / elapsed if elapsed > 0 else 0
                self.fps_updated.emit(fps)
                fps_display_time = now

            # ── 프로파일링 출력 ──
            if debug:
                profiler.add_total(_timer() - loop_start)
                profiler.maybe_log(frame_count, fps)

            # ── 프레임 간격 대기 ──
            elapsed = _timer() - loop_start
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
        except HW_ERRORS:
            pass

        # 캡처 정리
        if self._capture:
            self._capture.stop()

        self.status_changed.emit("엔진 중지됨")
