"""MirrorEngine — 미러링 모드 엔진 (ADR-015 서브클래스)

BaseEngine을 상속하여 미러링 전용 메인 루프와 리소스를 구현합니다.

[변경] 절전 복귀 대응:
- _run_loop에서 _check_and_handle_session_resume() 호출
- USB 끊김 시 재연결 시도 후 continue (기존: 1초 대기만)

프레임별 처리:
1. 파라미터 스냅샷 교체 (ADR-003)
2. 세션 복귀 / 디스플레이 변경 / 레이아웃 dirty 처리
3. 화면 캡처
4. 색상 파이프라인 (per-LED 또는 N구역)
5. USB 전송
6. 프리뷰 시그널 (5프레임마다)
"""

import time
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.constants import HW_ERRORS
from core.engine_utils import (
    MODE_MIRROR, N_ZONES_PER_LED,
    _STALE_RECREATE_COOLDOWN, _STALE_LED_OFF_THRESHOLD,
    _build_led_zone_map_by_side, per_led_to_zone_colors,
    leds_to_grb,
)
from core.color_extract import extract_zone_dominant  # ★ Phase 3

class _MirrorProfiler:
    PROFILE_INTERVAL = 60

    def __init__(self, logger):
        self._logger = logger
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


class MirrorEngine(BaseEngine):
    """미러링 모드 엔진."""

    mode = MODE_MIRROR

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self._mirror_zone_map = None
        self._mirror_cc = None
        self._last_brightness = self._current_mirror_params.brightness

    # ── 서브클래스 인터페이스 구현 ────────────────────────────────

    def _init_mode_resources(self):
        self._init_capture()

        # N구역 모드 준비
        mp = self._current_mirror_params
        if mp.mirror_n_zones != N_ZONES_PER_LED:
            self._mirror_zone_map = _build_led_zone_map_by_side(
                self.config, mp.mirror_n_zones
            )
            self._mirror_cc = ColorCorrection(self.config.get("color", {}))

        self._rebuild_pipeline()

    def _cleanup_mode(self):
        pass  # 미러링 전용 리소스 없음 (capture/device는 BaseEngine이 정리)

    # ── 메인 루프 ────────────────────────────────────────────────

    def _run_loop(self):
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
        stop_wait = self._stop_event.wait
        last_recreate_time = 0.0
        led_turned_off = False
        debug = self._debug_profile
        _timer = time.perf_counter if debug else time.monotonic
        profiler = _MirrorProfiler(self._logger) if debug else None
        fps = 0.0

        self.status_changed.emit("미러링 실행 중")
        self._start_monitor_watcher()

        while not self._stop_event.is_set():
            loop_start = _timer()

            # ── ADR-003: 파라미터 스냅샷 교체 ──
            self._swap_params()
            mp = self._current_mirror_params

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            if self._monitor_disconnected:
                if stop_wait(timeout=0.5):
                    break
                continue

            # ── ★ 세션 복귀 (절전모드) ──
            self._check_and_handle_session_resume()

            # ── 디스플레이 변경 ──
            if self._display_change_flag.is_set():
                self._handle_display_change()
                pipeline = self._pipeline
                prev_colors = None
                last_good_frame_time = time.monotonic()
                led_turned_off = False

            # ── 레이아웃 dirty ──
            with self._layout_lock:
                layout_dirty = self._layout_params.dirty
                if layout_dirty:
                    self._layout_params.dirty = False

            if layout_dirty:
                try:
                    self._weight_matrix = self._build_layout(
                        self._active_w, self._active_h
                    )
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass

            # ── 밝기 / 스무딩 반영 ──
            if mp.brightness != self._last_brightness:
                pipeline.update_brightness(mp.brightness)
                self._last_brightness = mp.brightness
            pipeline.smoothing = mp.smoothing_factor
            pipeline.smoothing_enabled = mp.smoothing_enabled

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
                        if not debug:
                            self.status_changed.emit("캡처 복구 중...")
                        self._capture._recreate()

                        new_w = self._capture.screen_w
                        new_h = self._capture.screen_h
                        if (new_w > 0 and new_h > 0
                                and (new_w != self._active_w
                                     or new_h != self._active_h)):
                            self._display_change_flag.set()

                if (stale_duration > _STALE_LED_OFF_THRESHOLD
                        and not led_turned_off):
                    try:
                        self._device.turn_off()
                        led_turned_off = True
                        if not debug:
                            self.status_changed.emit("캡처 없음 — LED 대기 중")
                    except HW_ERRORS:
                        pass

                if stop_wait(timeout=0.01):
                    break
                continue

            last_good_frame_time = time.monotonic()
            if led_turned_off:
                led_turned_off = False
                if not debug:
                    self.status_changed.emit("미러링 실행 중")

            # ── 해상도 변경 감지 ──
            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                continue

            if not getattr(self, '_native_capture', False):
                if current_h != self._active_h or current_w != self._active_w:
                    self._active_w, self._active_h = current_w, current_h
                    self._capture.screen_w = current_w
                    self._capture.screen_h = current_h
                    new_gc, new_gr = self._resolve_grid_size(current_w, current_h)
                    if (new_gc != self._active_grid_cols
                            or new_gr != self._active_grid_rows):
                        self._display_change_flag.set()
                        continue
                    try:
                        self._weight_matrix = self._build_layout(
                            current_w, current_h
                        )
                        self._rebuild_pipeline()
                        pipeline = self._pipeline
                        prev_colors = None
                    except (ValueError, IndexError, np.linalg.LinAlgError):
                        pass
            else:
                cap_w = self._capture.screen_w
                cap_h = self._capture.screen_h
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w
                             or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    continue

            # ── 색상 연산 ──
            if debug:
                t1 = _timer()

            if self._mirror_zone_map is not None:
                try:
                    grb_data, raw_preview = self._compute_zone_colors(frame, mp)
                    prev_colors = None
                    if frame_count % 5 == 0:
                        self.screen_colors_updated.emit(raw_preview.tolist())
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue
            else:
                try:
                    grb_data, rgb_colors = pipeline.process(frame, prev_colors)
                    prev_colors = rgb_colors
                except (ValueError, IndexError, FloatingPointError):
                    prev_colors = None
                    continue

                if frame_count % 5 == 0:
                    try:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        raw_rgb = self._weight_matrix @ grid_flat
                        self.screen_colors_updated.emit(raw_rgb.tolist())
                    except Exception:
                        pass

            if debug:
                profiler.add_color(_timer() - t1)
                t2 = _timer()

            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 시도 중...")
                # ★ 즉시 재연결 시도 (기존: 1초 대기만 하고 넘어감)
                self._device.force_reconnect()
                if self._device.connected:
                    self.status_changed.emit("USB 재연결 성공 — 미러링 실행 중")
                else:
                    if stop_wait(timeout=2.0):
                        break
                continue

            if debug:
                profiler.add_usb(_timer() - t2)

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
                if stop_wait(timeout=sleep_time):
                    break

    # ── N구역 미러링 ─────────────────────────────────────────────

    def _compute_zone_colors(self, frame, mp):
        grid_flat = frame.reshape(-1, 3).astype(np.float32)
        per_led_raw = self._weight_matrix @ grid_flat
 
        # ★ Phase 3: distinctive 모드 분기
        extract_mode = self.config.get("mirror", {}).get("color_extract_mode", "average")
        if extract_mode == "distinctive":
            zone_colors = extract_zone_dominant(
                per_led_raw, self._mirror_zone_map, mp.mirror_n_zones
            )
        else:
            zone_colors = per_led_to_zone_colors(
                per_led_raw, self._mirror_zone_map, mp.mirror_n_zones
            )
        '''
        참고: MirrorEngine은 MirrorParams를 사용하고, MirrorParams에는
        color_extract_mode 필드가 없음. 대신 config["mirror"]에서 직접 읽음.
        이는 mirror_panel.apply_to_config()가 config["mirror"]["color_extract_mode"]에 저장하기 때문.
        UI에서 추출 모드를 변경하면 zone_count_changed 시그널이 발생하고,
        ControlTab._on_zone_count에서 엔진을 재시작하여 config가 반영됨.
        '''
        leds = zone_colors[self._mirror_zone_map]

        raw_preview = leds.copy()
        raw_preview *= mp.brightness

        leds *= mp.brightness
        self._mirror_cc.apply(leds)

        return leds_to_grb(leds), raw_preview
