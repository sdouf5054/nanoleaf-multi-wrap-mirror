"""미러링 루프 — QThread 기반 (디스플레이 변경·분리 대응 강화)

[변경 사항 v6 — CPU 최적화]
- ColorPipeline 캐싱: 매 프레임 dict 참조·LUT 생성 오버헤드 제거
- 디버그 프로파일링: _debug_profile 분기를 루프 밖으로 이동
  → 비디버그 시 perf_counter() 호출 0회
- sleep: Event.wait() 단일 호출로 통합
- 밝기 변경: LUT 스칼라 비율 조정 (전체 재빌드 불필요)
"""

import time
import os
import copy
import ctypes
import logging
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.capture import ScreenCapture
from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.color import ColorPipeline


class MirrorThread(QThread):
    """백그라운드 미러링 스레드.

    Signals:
        fps_updated(float): 1초마다 현재 fps 전달
        error(str, str): (메시지, 심각도) — "critical"=팝업, "warning"=상태바
        status_changed(str): 상태 변경 알림
    """

    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        # ★ 스레드 안전성: config를 deep copy하여 메인 스레드와 참조 분리
        self.config = copy.deepcopy(config)
        self._stop_event = threading.Event()
        self._paused = False

        # 외부에서 실시간 변경 가능한 값 (메인 스레드에서 직접 변경)
        self.brightness = config["mirror"]["brightness"]
        self.smoothing_enabled = True
        self.smoothing_factor = config["mirror"]["smoothing_factor"]

        # ★ 밝기 변경 감지용
        self._last_brightness = self.brightness

        # ★ 레이아웃 재계산 플래그 + 락
        self._layout_dirty = False
        self._layout_lock = threading.Lock()

        # _init_resources()에서 초기화되는 멤버
        self._capture = None
        self._device = None
        self._pipeline = None           # ★ ColorPipeline
        self._weight_matrix = None
        self._active_w = 0
        self._active_h = 0
        self._logger = None
        self._debug_profile = False
        self._expected_monitors = 0
        self._monitor_disconnected = False

    @property
    def _running(self):
        return not self._stop_event.is_set()

    # ── 실시간 파라미터 업데이트 (메인 스레드에서 호출) ────────────────

    def update_layout_params(self, decay_radius=None, parallel_penalty=None,
                             decay_per_side=None, penalty_per_side=None):
        """레이아웃 파라미터를 실시간으로 변경합니다."""
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

    # ── 모니터 연결 감지 ─────────────────────────────────────────────

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
                self._capture = ScreenCapture(mirror_cfg["monitor_index"])
                self._capture.start(target_fps=mirror_cfg["target_fps"])
                self._active_w = self._capture.screen_w
                self._active_h = self._capture.screen_h
                self._weight_matrix = self._build_layout(
                    self._active_w, self._active_h
                )
                self._rebuild_pipeline()
                self._monitor_disconnected = False
                self.status_changed.emit("미러링 실행 중")
            except Exception:
                pass

        if not self._stop_event.is_set():
            timer = threading.Timer(1.0, self._monitor_watcher_tick)
            timer.daemon = True
            timer.start()

    # ── 레이아웃 계산 ────────────────────────────────────────────────

    def _build_layout(self, w, h):
        """LED 위치 + 가중치 행렬을 (w, h) 해상도 기준으로 재계산."""
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

    # ── 초기화 ───────────────────────────────────────────────────────

    def _init_resources(self):
        cfg = self.config
        dev_cfg = cfg["device"]
        mirror_cfg = cfg["mirror"]

        led_count = dev_cfg["led_count"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)
        target_fps = mirror_cfg["target_fps"]

        # 디버그 프로파일 설정
        self._debug_profile = cfg.get("options", {}).get("debug_profile", False)
        log_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log"
        )
        self._logger = logging.getLogger("nanoleaf.mirror")
        if self._debug_profile:
            self._logger.setLevel(logging.DEBUG)
            if not self._logger.handlers:
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self._logger.addHandler(fh)
            self._logger.propagate = False

        try:
            # 화면 캡처
            self.status_changed.emit("화면 캡처 초기화...")
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
            self._weight_matrix = self._build_layout(self._active_w, self._active_h)

            # ★ ColorPipeline 생성
            self._rebuild_pipeline()

            # Nanoleaf 장치
            self.status_changed.emit("Nanoleaf 연결 중...")
            self._device = NanoleafDevice(vendor_id, product_id, led_count)
            self._device.connect()

            self._expected_monitors = self._get_monitor_count()
            return True

        except (OSError, IOError, ValueError, ConnectionError) as e:
            self.error.emit(str(e), "critical")
            if self._capture:
                self._capture.stop()
            return False

    # ── 미러링 루프 ──────────────────────────────────────────────────

    def _run_loop(self):
        """메인 미러링 루프 — 프레임 획득→색상 연산→USB 전송 반복."""
        mirror_cfg = self.config["mirror"]
        target_fps = mirror_cfg["target_fps"]
        frame_interval = 1.0 / target_fps

        prev_colors = None
        frame_count = 0
        fps_start_time = time.monotonic()
        fps_display_time = fps_start_time

        # 프레임 실패 감시용
        last_good_frame_time = time.monotonic()
        STALE_THRESHOLD = 3.0

        pipeline = self._pipeline

        self.status_changed.emit("미러링 실행 중")
        self._start_monitor_watcher()

        # ★ 디버그/비디버그 루프 분리 — 비디버그 시 perf_counter 호출 0회
        if self._debug_profile:
            self._run_loop_debug(pipeline, prev_colors, frame_count,
                                 fps_start_time, fps_display_time,
                                 last_good_frame_time, frame_interval,
                                 STALE_THRESHOLD, mirror_cfg)
        else:
            self._run_loop_fast(pipeline, prev_colors, frame_count,
                                fps_start_time, fps_display_time,
                                last_good_frame_time, frame_interval,
                                STALE_THRESHOLD, mirror_cfg)

    def _run_loop_fast(self, pipeline, prev_colors, frame_count,
                       fps_start_time, fps_display_time,
                       last_good_frame_time, frame_interval,
                       STALE_THRESHOLD, mirror_cfg):
        """★ 비디버그 고속 루프 — perf_counter/프로파일링 코드 완전 제거."""

        stop_wait = self._stop_event.wait  # ★ 메서드 룩업 캐싱

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

            # ── 밝기 실시간 반영 (LUT 스칼라 조정) ──
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
                if now - last_good_frame_time > STALE_THRESHOLD:
                    self._capture._recreate()
                    last_good_frame_time = now

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

                if stop_wait(timeout=0.01):
                    break
                continue

            last_good_frame_time = time.monotonic()

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

            # ── 색상 연산 (★ ColorPipeline) ──
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

    def _run_loop_debug(self, pipeline, prev_colors, frame_count,
                        fps_start_time, fps_display_time,
                        last_good_frame_time, frame_interval,
                        STALE_THRESHOLD, mirror_cfg):
        """디버그 프로파일링 루프 — 기존 로직 유지."""
        PROFILE_INTERVAL = 60
        t_capture_acc = 0.0
        t_color_acc = 0.0
        t_usb_acc = 0.0
        t_total_acc = 0.0
        fps = 0.0

        stop_wait = self._stop_event.wait

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
                if now_mono - last_good_frame_time > STALE_THRESHOLD:
                    self._capture._recreate()
                    last_good_frame_time = now_mono
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
                if stop_wait(timeout=0.01):
                    break
                continue

            last_good_frame_time = time.monotonic()

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

    # ── 정리 ─────────────────────────────────────────────────────────

    def _cleanup(self):
        try:
            if self._device:
                self._device.turn_off()
                self._device.disconnect()
        except (OSError, IOError, ValueError):
            pass
        if self._capture:
            self._capture.stop()
        self.status_changed.emit("미러링 중지됨")

    # ── QThread 진입점 ───────────────────────────────────────────────

    def run(self):
        if not self._init_resources():
            return

        try:
            self._run_loop()
        except Exception as e:
            self.error.emit(f"미러링 오류: {e}", "warning")
        finally:
            self._cleanup()

    # ── 외부 제어 ────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        self.status_changed.emit("일시정지")

    def resume(self):
        self._paused = False
        self.status_changed.emit("미러링 실행 중")

    def toggle_pause(self):
        if self._paused:
            self.resume()
        else:
            self.pause()

    def stop_mirror(self):
        self._stop_event.set()
