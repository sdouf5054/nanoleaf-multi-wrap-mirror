"""BaseEngine — 공통 엔진 수명주기 (ADR-015 Strategy Pattern)

[ADR-015] 단일 UnifiedEngine 대신 BaseEngine + 모드별 서브클래스:
  - MirrorEngine(base_engine.py 에서 import)
  - AudioEngine(engine_audio_mode.py)
  - HybridEngine(engine_hybrid_mode.py)

[ADR-002] threading.Event.wait(timeout)를 sleep 대신 사용 — stop 즉시 응답
[ADR-005] 모니터 워처를 persistent daemon thread로 변경
[ADR-003] _pending_params 스냅샷 패턴으로 파라미터 전달

공유 리소스(USB device, config, signals)는 BaseEngine에,
모드별 로직(_init_mode_resources, _run_loop, _cleanup_mode)은 서브클래스에.

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

from PySide6.QtCore import QThread, Signal

from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.color import ColorPipeline
from core.color_correction import ColorCorrection
from core.constants import HW_ERRORS, HW_CONNECT_ERRORS
from core.engine_params import MirrorParams, AudioParams, LayoutParams
from core.engine_utils import (
    MODE_MIRROR, MODE_AUDIO, MODE_HYBRID,
    N_ZONES_PER_LED,
    _build_led_zone_map_by_side,
)


class BaseEngine(QThread):
    """모든 엔진 모드의 공통 베이스 클래스.

    서브클래스는 다음을 구현해야 합니다:
        mode: str              — "mirror", "audio", "hybrid"
        _init_mode_resources() — 모드별 리소스 초기화
        _run_loop()            — 메인 루프
        _cleanup_mode()        — 모드별 리소스 정리
    """

    # ── Qt Signals ──────────────────────────────────────────────
    fps_updated = Signal(float)
    error = Signal(str, str)
    status_changed = Signal(str)
    energy_updated = Signal(float, float, float)
    spectrum_updated = Signal(object)
    screen_colors_updated = Signal(object)

    # ── 서브클래스가 설정해야 하는 속성 ──────────────────────────
    mode: str = ""  # 서브클래스에서 오버라이드

    def __init__(self, config, audio_device_index=None):
        super().__init__()
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()

        # ── ADR-003: 파라미터 스냅샷 ──
        self._pending_mirror_params: MirrorParams | None = None
        self._pending_audio_params: AudioParams | None = None
        self._current_mirror_params = MirrorParams(
            brightness=config["mirror"]["brightness"],
            smoothing_enabled=True,
            smoothing_factor=config["mirror"]["smoothing_factor"],
            mirror_n_zones=config["mirror"].get("zone_count", N_ZONES_PER_LED),
        )
        self._current_audio_params = AudioParams()

        # ── ADR-004: 레이아웃 dirty flag + lock ──
        self._layout_params = LayoutParams(
            decay_radius=config["mirror"]["decay_radius"],
            parallel_penalty=config["mirror"]["parallel_penalty"],
            decay_per_side=config["mirror"].get("decay_radius_per_side", {}),
            penalty_per_side=config["mirror"].get("parallel_penalty_per_side", {}),
        )
        self._layout_lock = threading.Lock()

        # ── 디스플레이 변경 ──
        self._display_change_flag = threading.Event()

        # ── 공유 리소스 (서브클래스에서도 사용) ──
        self._device: NanoleafDevice | None = None
        self._capture = None
        self._pipeline: ColorPipeline | None = None
        self._weight_matrix = None

        self._active_w = 0
        self._active_h = 0
        self._active_grid_cols = config["mirror"].get("grid_cols", 64)
        self._active_grid_rows = config["mirror"].get("grid_rows", 32)

        self._led_count = config["device"]["led_count"]
        self._audio_device_index = audio_device_index
        self._paused = False

        # ── 모니터 워처 상태 ──
        self._expected_monitors = 0
        self._expected_resolution = (0, 0)
        self._monitor_disconnected = False
        self._monitor_watcher_stop = threading.Event()

        # ── 프로파일링 ──
        self._logger = None
        self._debug_profile = False

    # ══════════════════════════════════════════════════════════════
    #  ADR-003: 파라미터 스냅샷 API (UI 스레드에서 호출)
    # ══════════════════════════════════════════════════════════════

    def update_mirror_params(self, params: MirrorParams):
        """UI → 엔진 미러링 파라미터 전달 (atomic 참조 대입)."""
        self._pending_mirror_params = params

    def update_audio_params(self, params: AudioParams):
        """UI → 엔진 오디오 파라미터 전달 (atomic 참조 대입)."""
        self._pending_audio_params = params

    def update_layout_params(self, decay_radius=None, parallel_penalty=None,
                             decay_per_side=None, penalty_per_side=None):
        """UI → 엔진 레이아웃 파라미터 (lock 보호)."""
        with self._layout_lock:
            lp = self._layout_params
            if decay_radius is not None:
                lp.decay_radius = decay_radius
            if parallel_penalty is not None:
                lp.parallel_penalty = parallel_penalty
            if decay_per_side is not None:
                lp.decay_per_side = decay_per_side
            if penalty_per_side is not None:
                lp.penalty_per_side = penalty_per_side
            lp.dirty = True

    def _swap_params(self):
        """프레임 시작 시 pending 스냅샷을 current로 교체."""
        p = self._pending_mirror_params
        if p is not None:
            self._pending_mirror_params = None
            self._current_mirror_params = p

        a = self._pending_audio_params
        if a is not None:
            self._pending_audio_params = None
            self._current_audio_params = a

    # ══════════════════════════════════════════════════════════════
    #  일시정지 / 중지
    # ══════════════════════════════════════════════════════════════

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
        self._monitor_watcher_stop.set()

    def on_display_changed(self):
        self._display_change_flag.set()

    # ══════════════════════════════════════════════════════════════
    #  Grid 크기 결정 — 세로 모드에서 swap
    # ══════════════════════════════════════════════════════════════

    def _resolve_grid_size(self, screen_w, screen_h):
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
    #  캡처 초기화 (미러링 + 하이브리드 공유)
    # ══════════════════════════════════════════════════════════════

    def _init_capture(self):
        """화면 캡처 + 가중치 행렬 초기화."""
        mirror_cfg = self.config["mirror"]
        target_fps = mirror_cfg["target_fps"]

        init_res = self._get_primary_resolution()
        if init_res[0] > 0 and init_res[1] > 0:
            grid_cols, grid_rows = self._resolve_grid_size(init_res[0], init_res[1])
        else:
            grid_cols = mirror_cfg.get("grid_cols", 64)
            grid_rows = mirror_cfg.get("grid_rows", 32)

        self._active_grid_cols = grid_cols
        self._active_grid_rows = grid_rows

        self.status_changed.emit("화면 캡처 초기화...")

        try:
            from native_capture import NativeScreenCapture as ScreenCapture
            self._native_capture = True
            self._capture = ScreenCapture(
                monitor_index=mirror_cfg["monitor_index"],
                grid_cols=grid_cols, grid_rows=grid_rows,
            )
        except ImportError:
            from core.capture import ScreenCapture
            self._native_capture = False
            self._capture = ScreenCapture(mirror_cfg["monitor_index"])

        self._capture.start(target_fps=target_fps)

        self.status_changed.emit("가중치 행렬 생성...")
        self._active_w = self._capture.screen_w
        self._active_h = self._capture.screen_h
        self._weight_matrix = self._build_layout(self._active_w, self._active_h)

    def _init_usb(self):
        """USB 디바이스 연결."""
        dev_cfg = self.config["device"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)

        self.status_changed.emit("Nanoleaf 연결 중...")
        self._device = NanoleafDevice(vendor_id, product_id, self._led_count)
        self._device.connect()

    # ══════════════════════════════════════════════════════════════
    #  레이아웃 계산
    # ══════════════════════════════════════════════════════════════

    def _build_layout(self, w, h):
        mirror_cfg = self.config["mirror"]
        layout_cfg = self.config["layout"]
        led_count = self._led_count

        with self._layout_lock:
            lp = self._layout_params
            base_decay = lp.decay_radius
            per_decay = lp.decay_per_side
            base_penalty = lp.parallel_penalty
            per_penalty = lp.penalty_per_side

        decay_param = (
            {s: per_decay.get(s, base_decay) for s in ("top", "bottom", "left", "right")}
            if per_decay else base_decay
        )
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
        mp = self._current_mirror_params
        mirror_cfg_copy = {
            "grid_cols": self._active_grid_cols,
            "grid_rows": self._active_grid_rows,
            "brightness": mp.brightness,
            "smoothing_factor": mp.smoothing_factor,
        }
        self._pipeline = ColorPipeline(self._weight_matrix, color_cfg, mirror_cfg_copy)

    # ══════════════════════════════════════════════════════════════
    #  ADR-005: 모니터 워처 — persistent daemon thread
    # ══════════════════════════════════════════════════════════════

    def _start_monitor_watcher(self):
        self._expected_resolution = self._get_primary_resolution()
        self._expected_monitors = self._get_monitor_count()
        t = threading.Thread(target=self._monitor_watcher_loop, daemon=True)
        t.start()

    def _monitor_watcher_loop(self):
        """[ADR-005] 단일 persistent thread — 1초 간격 polling."""
        while not self._monitor_watcher_stop.wait(timeout=1.0):
            self._monitor_watcher_tick()

    def _monitor_watcher_tick(self):
        if self._stop_event.is_set():
            return

        current_res = self._get_primary_resolution()
        if (current_res[0] > 0 and current_res[1] > 0
                and self._expected_resolution[0] > 0
                and current_res != self._expected_resolution):
            if not self._display_change_flag.is_set():
                self._display_change_flag.set()
            self._expected_resolution = current_res

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
            self._monitor_disconnected = False
            self._display_change_flag.set()

    # ══════════════════════════════════════════════════════════════
    #  디스플레이 변경 처리
    # ══════════════════════════════════════════════════════════════

    def _handle_display_change(self):
        self._display_change_flag.clear()
        self.status_changed.emit("디스플레이 변경 — 재초기화 중...")

        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None

        new_res = self._get_primary_resolution()
        if new_res[0] <= 0 or new_res[1] <= 0:
            self.status_changed.emit("디스플레이 변경 — 해상도 조회 실패")
            return

        new_w, new_h = new_res
        new_grid_cols, new_grid_rows = self._resolve_grid_size(new_w, new_h)
        mirror_cfg = self.config["mirror"]

        try:
            try:
                from native_capture import NativeScreenCapture as ScreenCapture
                self._capture = ScreenCapture(
                    monitor_index=mirror_cfg["monitor_index"],
                    grid_cols=new_grid_cols, grid_rows=new_grid_rows,
                )
            except ImportError:
                from core.capture import ScreenCapture
                self._capture = ScreenCapture(mirror_cfg["monitor_index"])

            self._capture.start(max_wait=10,
                                target_fps=mirror_cfg.get("target_fps", 60))
        except Exception as e:
            self.status_changed.emit(f"캡처 재생성 실패: {e}")
            return

        self._active_w = self._capture.screen_w if self._capture.screen_w > 0 else new_w
        self._active_h = self._capture.screen_h if self._capture.screen_h > 0 else new_h
        self._active_grid_cols = new_grid_cols
        self._active_grid_rows = new_grid_rows

        try:
            self._weight_matrix = self._build_layout(self._active_w, self._active_h)
            self._rebuild_pipeline()
            self.status_changed.emit(
                f"디스플레이 변경 반영 완료 "
                f"({self._active_w}×{self._active_h}, "
                f"grid {new_grid_cols}×{new_grid_rows})"
            )
        except (ValueError, IndexError, np.linalg.LinAlgError) as e:
            self.status_changed.emit(f"layout 재빌드 실패: {e}")

        self._expected_resolution = new_res

    # ══════════════════════════════════════════════════════════════
    #  Windows API 헬퍼
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

    # ══════════════════════════════════════════════════════════════
    #  QThread 진입점
    # ══════════════════════════════════════════════════════════════

    def run(self):
        self._init_logging()
        try:
            self._init_mode_resources()
            self._init_usb()
            self._expected_monitors = self._get_monitor_count()
            self._expected_resolution = self._get_primary_resolution()
        except HW_CONNECT_ERRORS as e:
            self.error.emit(str(e), "critical")
            self._cleanup_partial()
            return
        except Exception as e:
            self.error.emit(f"초기화 오류: {e}", "critical")
            self._cleanup_partial()
            return

        try:
            self._run_loop()
        except Exception as e:
            self.error.emit(f"엔진 오류: {e}", "warning")
        finally:
            self._cleanup()

    # ══════════════════════════════════════════════════════════════
    #  서브클래스 인터페이스 (오버라이드 필수)
    # ══════════════════════════════════════════════════════════════

    def _init_mode_resources(self):
        """모드별 리소스 초기화. 서브클래스에서 오버라이드."""
        raise NotImplementedError

    def _run_loop(self):
        """메인 루프. 서브클래스에서 오버라이드."""
        raise NotImplementedError

    def _cleanup_mode(self):
        """모드별 리소스 정리. 서브클래스에서 오버라이드."""
        pass

    # ══════════════════════════════════════════════════════════════
    #  정리
    # ══════════════════════════════════════════════════════════════

    def _cleanup_partial(self):
        """초기화 실패 시 부분 정리."""
        self._cleanup_mode()
        if self._capture:
            self._capture.stop()
            self._capture = None

    def _cleanup(self):
        self._monitor_watcher_stop.set()
        self._cleanup_mode()
        try:
            if self._device:
                self._device.turn_off()
                self._device.disconnect()
        except HW_ERRORS:
            pass
        if self._capture:
            self._capture.stop()
        self.status_changed.emit("엔진 중지됨")

    def _init_logging(self):
        self._debug_profile = self.config.get("options", {}).get("debug_profile", False)
        self._logger = logging.getLogger("nanoleaf.engine")
        if self._debug_profile:
            self._logger.setLevel(logging.DEBUG)
            log_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log"
            )
            if not self._logger.handlers:
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self._logger.addHandler(fh)
            self._logger.propagate = False
