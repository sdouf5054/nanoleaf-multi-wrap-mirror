"""UnifiedEngine — 단일 LED 엔진 (미러링 + 오디오 + 하이브리드)

미러링 모드의 메인 루프와 공통 리소스 관리를 담당합니다.
오디오/하이브리드 루프와 렌더링은 AudioEngineMixin(engine_audio.py)에,
유틸리티 함수와 공용 상수는 engine_utils.py에 있습니다.

[변경] 하이브리드 캡처 통합
- 하이브리드 모드가 미러링과 동일한 self._capture + weight_matrix 사용
- ScreenSampler 제거 → 캡처 경로 단일화
- per-LED 색상에서 구역별 평균을 사후 계산 (per_led_to_zone_colors)
- 프리뷰는 항상 색상 보정 전(raw RGB)을 전송

[변경] 세로모드 미러링 수정
- _resolve_grid_size(): 화면 방향에 따라 grid_cols/grid_rows를 swap
- _active_grid_cols, _active_grid_rows: 현재 사용 중인 grid 크기 추적

[변경] 디스플레이 변경 대응
- on_display_changed(): MainWindow에서 호출 (WM_DISPLAYCHANGE)
- _handle_display_change(): 캡처 재생성 + grid swap + layout 재빌드

Signals:
    fps_updated(float), error(str, str), status_changed(str),
    energy_updated(float, float, float), spectrum_updated(object),
    screen_colors_updated(object)
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

from core.engine_utils import (
    MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN,
    N_ZONES_PER_LED, SCREEN_UPDATE_INTERVAL,
    _STALE_RECREATE_COOLDOWN, _STALE_LED_OFF_THRESHOLD,
    DEFAULT_FPS, MIN_BRIGHTNESS, DEFAULT_ZONE_WEIGHTS,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    _remap_t,
    _compute_led_perimeter_t,
    _compute_led_band_mapping,
    _build_led_order_from_segments,
    _build_led_zone_map_by_side,
    per_led_to_zone_colors,
)

from core.engine_audio import AudioEngineMixin
from core.constants import HW_ERRORS, HW_CONNECT_ERRORS


class _MirrorProfiler:
    PROFILE_INTERVAL = 60

    def __init__(self, logger, frame_interval):
        self._logger = logger
        self._frame_interval = frame_interval
        self._t_capture = self._t_color = self._t_usb = self._t_total = 0.0

    def add_capture(self, dt): self._t_capture += dt
    def add_color(self, dt):   self._t_color += dt
    def add_usb(self, dt):     self._t_usb += dt
    def add_total(self, dt):   self._t_total += dt

    def maybe_log(self, frame_count, fps):
        if frame_count % self.PROFILE_INTERVAL != 0:
            return
        n = self.PROFILE_INTERVAL
        self._logger.debug(
            f"[PROFILE] capture={self._t_capture/n*1000:.2f}ms  "
            f"color={self._t_color/n*1000:.2f}ms  "
            f"usb={self._t_usb/n*1000:.2f}ms  "
            f"total={self._t_total/n*1000:.2f}ms  "
            f"fps={fps:.1f}"
        )
        self._t_capture = self._t_color = self._t_usb = self._t_total = 0.0


class UnifiedEngine(AudioEngineMixin, QThread):

    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    screen_colors_updated = pyqtSignal(object)

    def __init__(self, config, audio_device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()
        self._paused = False

        self.mode = MODE_MIRROR
        self.brightness = config["mirror"]["brightness"]
        self.smoothing_enabled = True
        self.smoothing_factor = config["mirror"]["smoothing_factor"]
        self._last_brightness = self.brightness

        self._layout_dirty = False
        self._layout_lock = threading.Lock()
        self._display_change_flag = threading.Event()

        self.audio_mode = AUDIO_PULSE
        self.audio_brightness = 1.0
        self.bass_sensitivity = 1.0
        self.mid_sensitivity = 1.0
        self.high_sensitivity = 1.0
        self.attack = 0.5
        self.release = 0.1
        self.input_smoothing = 0.3
        self.target_fps = DEFAULT_FPS

        self.base_color = np.array([255, 0, 80], dtype=np.float32)
        self.rainbow = False
        self._zone_weights = list(DEFAULT_ZONE_WEIGHTS)
        self._zone_dirty = False
        self._audio_device_index = audio_device_index

        self.color_source = COLOR_SOURCE_SCREEN  # ★ 하이브리드 기본값 screen
        self.n_zones = 4                          # 하이브리드 구역 수
        self.mirror_n_zones = N_ZONES_PER_LED     # 미러링 구역 수
        self.min_brightness = MIN_BRIGHTNESS
        self.audio_min_brightness = MIN_BRIGHTNESS

        self._capture = None
        self._device = None
        self._pipeline = None
        self._weight_matrix = None
        self._active_w = 0
        self._active_h = 0
        self._active_grid_cols = config["mirror"].get("grid_cols", 64)
        self._active_grid_rows = config["mirror"].get("grid_rows", 32)
        self._logger = None
        self._debug_profile = False
        self._expected_monitors = 0
        self._expected_resolution = (0, 0)
        self._monitor_disconnected = False

        self._audio_engine = None
        self._led_count = 0
        self._perimeter_t = None
        self._led_band_indices = None
        self._led_order = []
        self._smooth_bass = self._smooth_mid = self._smooth_high = 0.0
        self._smooth_spectrum = None
        self._cc = None
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # ★ per-LED 색상 (하이브리드에서 capture + weight_matrix로 계산)
        self._per_led_colors = None
        # ★ 하이브리드 구역 매핑 + 캐시
        self._hybrid_zone_map = None
        self._hybrid_zone_colors = None

    @property
    def _running(self):
        return not self._stop_event.is_set()

    # ══════════════════════════════════════════════════════════════
    #  ★ Grid 크기 결정 — 세로 모드에서 swap
    # ══════════════════════════════════════════════════════════════

    def _resolve_grid_size(self, screen_w, screen_h):
        """화면 방향에 따라 grid_cols/grid_rows를 결정."""
        mirror_cfg = self.config["mirror"]
        base_cols = mirror_cfg.get("grid_cols", 64)
        base_rows = mirror_cfg.get("grid_rows", 32)
        orientation = mirror_cfg.get("orientation", "auto")

        is_portrait = False
        if orientation == "auto":
            is_portrait = screen_h > screen_w
        elif orientation == "portrait":
            is_portrait = True

        if is_portrait:
            return base_rows, base_cols
        else:
            return base_cols, base_rows

    # ══════════════════════════════════════════════════════════════
    #  외부 제어 API
    # ══════════════════════════════════════════════════════════════

    def on_display_changed(self):
        self._display_change_flag.set()

    def update_layout_params(self, decay_radius=None, parallel_penalty=None,
                             decay_per_side=None, penalty_per_side=None):
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
        self._zone_weights = [bass, mid, high]
        self._zone_dirty = True

    def set_color(self, r, g, b):
        self.base_color = np.array([r, g, b], dtype=np.float32)
        self.rainbow = False

    def set_rainbow(self, enabled=True):
        self.rainbow = enabled

    def set_audio_mode(self, mode):
        self.audio_mode = mode

    def set_color_source(self, source, n_zones=None):
        self.color_source = source
        if n_zones is not None and n_zones != self.n_zones:
            self.n_zones = n_zones
            # ★ 구역 매핑 재계산
            if n_zones != N_ZONES_PER_LED:
                self._hybrid_zone_map = _build_led_zone_map_by_side(
                    self.config, n_zones
                )

    def pause(self):
        self._paused = True
        self.status_changed.emit("일시정지")

    def resume(self):
        self._paused = False
        self.status_changed.emit("실행 중")

    def toggle_pause(self):
        self.resume() if self._paused else self.pause()

    def stop_engine(self):
        self._stop_event.set()

    # ══════════════════════════════════════════════════════════════
    #  ★ 디스플레이 변경 처리
    # ══════════════════════════════════════════════════════════════

    def _handle_display_change(self):
        """디스플레이 변경 → 캡처 재생성 + grid swap + layout 재빌드."""
        self._display_change_flag.clear()
        self.status_changed.emit("디스플레이 변경 — 재초기화 중...")

        mirror_cfg = self.config["mirror"]

        # 1. 기존 캡처 중지
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None

        # 2. 새 해상도 조회
        new_res = self._get_primary_resolution()
        if new_res[0] <= 0 or new_res[1] <= 0:
            self.status_changed.emit("디스플레이 변경 — 해상도 조회 실패")
            return

        new_w, new_h = new_res

        # 3. 새 방향에 맞는 grid 크기
        new_grid_cols, new_grid_rows = self._resolve_grid_size(new_w, new_h)

        # 4. 캡처 재생성
        try:
            if _NATIVE_CAPTURE:
                self._capture = ScreenCapture(
                    monitor_index=mirror_cfg["monitor_index"],
                    grid_cols=new_grid_cols,
                    grid_rows=new_grid_rows,
                )
            else:
                self._capture = ScreenCapture(mirror_cfg["monitor_index"])
            self._capture.start(max_wait=10, target_fps=mirror_cfg.get("target_fps", 60))
        except Exception as e:
            self.status_changed.emit(f"캡처 재생성 실패: {e}")
            return

        # 5. 상태 갱신
        self._active_w = self._capture.screen_w if self._capture.screen_w > 0 else new_w
        self._active_h = self._capture.screen_h if self._capture.screen_h > 0 else new_h
        self._active_grid_cols = new_grid_cols
        self._active_grid_rows = new_grid_rows

        # 6. layout + pipeline 재빌드
        try:
            self._weight_matrix = self._build_layout(self._active_w, self._active_h)
            self._rebuild_pipeline()

            if self.mirror_n_zones != N_ZONES_PER_LED:
                self._mirror_zone_map = _build_led_zone_map_by_side(
                    self.config, self.mirror_n_zones
                )
                if self._mirror_cc is None:
                    self._mirror_cc = ColorCorrection(self.config.get("color", {}))

            self.status_changed.emit(
                f"디스플레이 변경 반영 완료 "
                f"({self._active_w}×{self._active_h}, "
                f"grid {new_grid_cols}×{new_grid_rows})"
            )
        except (ValueError, IndexError, np.linalg.LinAlgError) as e:
            self.status_changed.emit(f"layout 재빌드 실패: {e}")

        self._expected_resolution = new_res

    # ══════════════════════════════════════════════════════════════
    #  모니터 감지 + 해상도 폴링
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _get_monitor_count():
        try:
            return ctypes.windll.user32.GetSystemMetrics(80)
        except Exception:
            return -1

    @staticmethod
    def _get_primary_resolution():
        try:
            return (ctypes.windll.user32.GetSystemMetrics(0),
                    ctypes.windll.user32.GetSystemMetrics(1))
        except Exception:
            return (0, 0)

    def _start_monitor_watcher(self):
        self._expected_resolution = self._get_primary_resolution()
        self._monitor_watcher_tick()

    def _monitor_watcher_tick(self):
        if self._stop_event.is_set():
            return

        current_monitors = self._get_monitor_count()

        current_res = self._get_primary_resolution()
        if (current_res[0] > 0 and current_res[1] > 0
                and self._expected_resolution[0] > 0
                and current_res != self._expected_resolution):
            if not self._display_change_flag.is_set():
                self._display_change_flag.set()
            self._expected_resolution = current_res

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
            self._monitor_disconnected = False
            self._display_change_flag.set()

        if not self._stop_event.is_set():
            timer = threading.Timer(1.0, self._monitor_watcher_tick)
            timer.daemon = True
            timer.start()

    # ══════════════════════════════════════════════════════════════
    #  레이아웃 계산
    # ══════════════════════════════════════════════════════════════

    def _build_layout(self, w, h):
        mirror_cfg = self.config["mirror"]
        layout_cfg = self.config["layout"]
        led_count = self.config["device"]["led_count"]

        base_decay = mirror_cfg["decay_radius"]
        per_decay = mirror_cfg.get("decay_radius_per_side", {})
        decay_param = (
            {s: per_decay.get(s, base_decay) for s in ("top", "bottom", "left", "right")}
            if per_decay else base_decay
        )
        base_penalty = mirror_cfg["parallel_penalty"]
        per_penalty = mirror_cfg.get("parallel_penalty_per_side", {})
        penalty_param = (
            {s: per_penalty.get(s, base_penalty) for s in ("top", "bottom", "left", "right")}
            if per_penalty else base_penalty
        )

        positions, sides = get_led_positions(
            w, h, layout_cfg["segments"], led_count,
            orientation=mirror_cfg.get("orientation", "auto"),
            portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
        )
        return build_weight_matrix(
            w, h, positions, sides,
            self._active_grid_cols, self._active_grid_rows,
            decay_param, penalty_param,
        )

    def _rebuild_pipeline(self):
        color_cfg = self.config["color"]
        mirror_cfg = self.config["mirror"]
        mirror_cfg_copy = dict(mirror_cfg)
        mirror_cfg_copy["brightness"] = self.brightness
        mirror_cfg_copy["smoothing_factor"] = self.smoothing_factor
        mirror_cfg_copy["grid_cols"] = self._active_grid_cols
        mirror_cfg_copy["grid_rows"] = self._active_grid_rows
        self._pipeline = ColorPipeline(self._weight_matrix, color_cfg, mirror_cfg_copy)

    # ══════════════════════════════════════════════════════════════
    #  오디오 밴드 매핑
    # ══════════════════════════════════════════════════════════════

    def _rebuild_band_mapping(self):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        self._zone_dirty = False

    # ══════════════════════════════════════════════════════════════
    #  리소스 초기화
    # ══════════════════════════════════════════════════════════════

    def _init_resources(self):
        cfg = self.config
        dev_cfg = cfg["device"]
        mirror_cfg = cfg["mirror"]

        self._led_count = dev_cfg["led_count"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)

        self._debug_profile = cfg.get("options", {}).get("debug_profile", False)
        log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log")
        self._logger = logging.getLogger("nanoleaf.engine")
        if self._debug_profile:
            self._logger.setLevel(logging.DEBUG)
            if not self._logger.handlers:
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self._logger.addHandler(fh)
            self._logger.propagate = False

        try:
            # ★ 미러링과 하이브리드 모두 캡처 + weight_matrix 필요
            if self.mode in (MODE_MIRROR, MODE_HYBRID):
                self._init_mirror_resources(mirror_cfg)
            if self.mode in (MODE_AUDIO, MODE_HYBRID):
                self._init_audio_resources()
            if self.mode == MODE_HYBRID:
                self._init_hybrid_resources()

            self.status_changed.emit("Nanoleaf 연결 중...")
            self._device = NanoleafDevice(vendor_id, product_id, self._led_count)
            self._device.connect()

            self._expected_monitors = self._get_monitor_count()
            self._expected_resolution = self._get_primary_resolution()
            return True

        except HW_CONNECT_ERRORS as e:
            self.error.emit(str(e), "critical")
            self._cleanup_partial()
            return False

    def _init_mirror_resources(self, mirror_cfg):
        target_fps = mirror_cfg["target_fps"]

        # 초기 해상도로 grid 크기 결정
        init_res = self._get_primary_resolution()
        if init_res[0] > 0 and init_res[1] > 0:
            grid_cols, grid_rows = self._resolve_grid_size(init_res[0], init_res[1])
        else:
            grid_cols = mirror_cfg.get("grid_cols", 64)
            grid_rows = mirror_cfg.get("grid_rows", 32)

        self._active_grid_cols = grid_cols
        self._active_grid_rows = grid_rows

        self.status_changed.emit("화면 캡처 초기화...")
        if _NATIVE_CAPTURE:
            self._capture = ScreenCapture(
                monitor_index=mirror_cfg["monitor_index"],
                grid_cols=grid_cols,
                grid_rows=grid_rows,
            )
        else:
            self._capture = ScreenCapture(mirror_cfg["monitor_index"])
        self._capture.start(target_fps=target_fps)

        if self._debug_profile:
            self._logger.debug(
                f"screen: {self._capture.screen_w}x{self._capture.screen_h}, "
                f"grid: {grid_cols}x{grid_rows}"
            )

        self.status_changed.emit("가중치 행렬 생성...")
        self._active_w = self._capture.screen_w
        self._active_h = self._capture.screen_h
        self._weight_matrix = self._build_layout(self._active_w, self._active_h)

        # 미러링 구역 매핑 (per-LED이 아닌 경우)
        self._mirror_zone_map = None
        self._mirror_cc = None
        if self.mirror_n_zones != N_ZONES_PER_LED:
            self._mirror_zone_map = _build_led_zone_map_by_side(self.config, self.mirror_n_zones)
            self._mirror_cc = ColorCorrection(self.config.get("color", {}))

        self._rebuild_pipeline()

    def _init_audio_resources(self):
        self._cc = ColorCorrection(self.config.get("color", {}))
        self.status_changed.emit("오디오 캡처 초기화...")
        self._audio_engine = AudioEngine(
            device_index=self._audio_device_index, sensitivity=1.0, smoothing=0.15,
        )
        self._audio_engine.bass_sensitivity = self.bass_sensitivity
        self._audio_engine.mid_sensitivity = self.mid_sensitivity
        self._audio_engine.high_sensitivity = self.high_sensitivity
        self._audio_engine.start()

        n_bands = self._audio_engine.n_bands
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, self._zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        self._smooth_bass = self._smooth_mid = self._smooth_high = 0.0
        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)

    def _init_hybrid_resources(self):
        """★ 하이브리드 리소스 — capture/weight_matrix는 _init_mirror_resources에서 생성 완료.
        여기서는 구역 매핑 + per-LED 버퍼만 초기화.
        """
        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)

        # 구역 매핑 (per-LED이 아닌 경우)
        if self.n_zones != N_ZONES_PER_LED:
            self._hybrid_zone_map = _build_led_zone_map_by_side(
                self.config, self.n_zones
            )

    def _cleanup_partial(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
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

    def _compute_mirror_zone_colors(self, frame):
        """미러링 N구역 모드: frame → 구역별 평균 → zone_map으로 LED 할당.

        Returns: (grb_bytes, raw_leds_for_preview)
        프리뷰용으로는 색상 보정 전 값을 반환합니다.
        """
        # weight_matrix로 per-LED raw 색상 계산
        grid_flat = frame.reshape(-1, 3).astype(np.float32)
        per_led_raw = self._weight_matrix @ grid_flat  # (n_leds, 3)

        # per-LED → 구역별 평균 → zone_map으로 LED에 재할당
        n_zones = self.mirror_n_zones
        zone_colors = per_led_to_zone_colors(
            per_led_raw, self._mirror_zone_map, n_zones
        )
        leds = zone_colors[self._mirror_zone_map]

        # ★ 프리뷰용: 보정 전 raw (밝기만 적용)
        raw_preview = leds.copy()
        raw_preview *= self.brightness

        # LED 출력용: 보정 적용
        leds *= self.brightness
        self._mirror_cc.apply(leds)

        return self._leds_to_grb(leds), raw_preview

    def _run_mirror(self):
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

        profiler = _MirrorProfiler(self._logger, frame_interval) if self._debug_profile else None

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

        stop_wait = self._stop_event.wait
        last_recreate_time = 0.0
        led_turned_off = False
        debug = profiler is not None
        _timer = time.perf_counter if debug else time.monotonic

        while not self._stop_event.is_set():
            loop_start = _timer()

            if self._paused:
                if stop_wait(timeout=0.05): break
                continue

            if self._monitor_disconnected:
                if stop_wait(timeout=0.5): break
                continue

            # 디스플레이 변경 처리
            if self._display_change_flag.is_set():
                self._handle_display_change()
                pipeline = self._pipeline
                prev_colors = None
                last_good_frame_time = time.monotonic()
                led_turned_off = False

            if self._layout_dirty:
                with self._layout_lock:
                    self._layout_dirty = False
                try:
                    self._weight_matrix = self._build_layout(self._active_w, self._active_h)
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass

            current_brightness = self.brightness
            if current_brightness != self._last_brightness:
                pipeline.update_brightness(current_brightness)
                self._last_brightness = current_brightness

            pipeline.smoothing = self.smoothing_factor
            pipeline.smoothing_enabled = self.smoothing_enabled

            if debug: t0 = _timer()
            frame = self._capture.grab()
            if debug: profiler.add_capture(_timer() - t0)

            if frame is None:
                now = time.monotonic()
                stale_duration = now - last_good_frame_time

                if stale_duration > STALE_THRESHOLD:
                    if now - last_recreate_time >= _STALE_RECREATE_COOLDOWN:
                        last_recreate_time = now
                        if not debug:
                            self.status_changed.emit("캡처 복구 중...")
                        self._capture._recreate()

                        new_w = self._capture.screen_w
                        new_h = self._capture.screen_h
                        if (new_w > 0 and new_h > 0
                                and (new_w != self._active_w or new_h != self._active_h)):
                            self._display_change_flag.set()

                if stale_duration > _STALE_LED_OFF_THRESHOLD and not led_turned_off:
                    try:
                        self._device.turn_off()
                        led_turned_off = True
                        if not debug:
                            self.status_changed.emit("캡처 없음 — LED 대기 중")
                    except HW_ERRORS:
                        pass

                if stop_wait(timeout=0.01): break
                continue

            last_good_frame_time = time.monotonic()
            if led_turned_off:
                led_turned_off = False
                if not debug:
                    self.status_changed.emit("미러링 실행 중")

            # 해상도 변경 감지
            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                continue

            if not _NATIVE_CAPTURE:
                if current_h != self._active_h or current_w != self._active_w:
                    self._active_w, self._active_h = current_w, current_h
                    self._capture.screen_w = current_w
                    self._capture.screen_h = current_h
                    new_gc, new_gr = self._resolve_grid_size(current_w, current_h)
                    if new_gc != self._active_grid_cols or new_gr != self._active_grid_rows:
                        self._display_change_flag.set()
                        continue
                    try:
                        self._weight_matrix = self._build_layout(current_w, current_h)
                        self._rebuild_pipeline()
                        pipeline = self._pipeline
                        prev_colors = None
                    except (ValueError, IndexError, np.linalg.LinAlgError):
                        pass
            else:
                cap_w, cap_h = self._capture.screen_w, self._capture.screen_h
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    continue

            # ── 색상 연산 ──
            if debug: t1 = _timer()

            if self._mirror_zone_map is not None:
                # ★ N구역 모드
                try:
                    grb_data, raw_preview = self._compute_mirror_zone_colors(frame)
                    prev_colors = None
                    if frame_count % 5 == 0:
                        self.screen_colors_updated.emit(raw_preview.tolist())
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue
            else:
                # per-LED 모드 (기본)
                try:
                    grb_data, rgb_colors = pipeline.process(frame, prev_colors)
                    prev_colors = rgb_colors
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue

                # ★ 프리뷰: weight_matrix로 raw RGB 계산 (보정 전)
                if frame_count % 5 == 0:
                    try:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        raw_rgb = self._weight_matrix @ grid_flat
                        self.screen_colors_updated.emit(raw_rgb.tolist())
                    except Exception:
                        pass

            if debug: profiler.add_color(_timer() - t1)
            if debug: t2 = _timer()

            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 대기 중...")
                if stop_wait(timeout=1.0): break
                continue

            if debug: profiler.add_usb(_timer() - t2)

            frame_count += 1
            now = time.monotonic()
            if now - fps_display_time >= 1.0:
                elapsed = now - fps_start_time
                fps = frame_count / elapsed if elapsed > 0 else 0
                self.fps_updated.emit(fps)
                fps_display_time = now

            if debug:
                profiler.add_total(_timer() - loop_start)
                profiler.maybe_log(frame_count, fps)

            elapsed = _timer() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time): break

    # ══════════════════════════════════════════════════════════════
    #  정리
    # ══════════════════════════════════════════════════════════════

    def _cleanup(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        try:
            if self._device:
                self._device.turn_off()
                self._device.disconnect()
        except HW_ERRORS:
            pass
        if self._capture:
            self._capture.stop()
        self.status_changed.emit("엔진 중지됨")
