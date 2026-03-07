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

[확장 포인트 — Step 4-5에서 구현]
- MODE_AUDIO: AudioEngine 소스 추가
- MODE_HYBRID: 화면 + 오디오 믹싱

Signals:
    fps_updated(float): 1초마다 현재 FPS
    error(str, str): (메시지, 심각도) — "critical"=팝업, "warning"=상태바
    status_changed(str): 상태 변경 알림
    energy_updated(float, float, float): bass, mid, high (오디오 모드용)
    spectrum_updated(object): 16밴드 스펙트럼 (오디오 모드용)
    screen_colors_updated(object): 구역별 색상 (프리뷰용)
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

# ── 엔진 모드 상수 ────────────────────────────────────────────────
MODE_MIRROR = "mirror"
MODE_AUDIO = "audio"          # Step 4에서 구현
MODE_HYBRID = "hybrid"        # Step 5에서 구현

# ── stale 복구 관련 상수 ──────────────────────────────────────────
_STALE_RECREATE_COOLDOWN = 3.0   # recreate 재시도 최소 간격 (초)
_STALE_LED_OFF_THRESHOLD = 10.0  # 이 시간 동안 프레임 없으면 LED 끄기 (초)


class UnifiedEngine(QThread):
    """단일 LED 엔진 — 모드에 따라 화면/오디오/하이브리드 소스 사용.

    Step 3: mirror 모드 완전 구현.
    MirrorThread와 100% 동일한 동작을 보장합니다.
    """

    # ── 시그널 ─────────────────────────────────────────────────────
    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)

    # 오디오 모드용 시그널 (Step 4에서 활성화)
    energy_updated = pyqtSignal(float, float, float)
    spectrum_updated = pyqtSignal(object)
    screen_colors_updated = pyqtSignal(object)

    def __init__(self, config):
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
    #  모니터 연결 감지
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
    #  레이아웃 계산
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
    #  리소스 초기화
    # ══════════════════════════════════════════════════════════════

    def _init_resources(self):
        """모드에 따라 필요한 리소스를 초기화."""
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

            # ── Nanoleaf 장치 ──
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

    # ══════════════════════════════════════════════════════════════
    #  QThread 진입점
    # ══════════════════════════════════════════════════════════════

    def run(self):
        if not self._init_resources():
            return

        try:
            if self.mode == MODE_MIRROR:
                self._run_mirror()
            # Step 4: elif self.mode == MODE_AUDIO: self._run_audio()
            # Step 5: elif self.mode == MODE_HYBRID: self._run_hybrid()
            else:
                self.error.emit(f"알 수 없는 모드: {self.mode}", "critical")
        except Exception as e:
            self.error.emit(f"엔진 오류: {e}", "warning")
        finally:
            self._cleanup()

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
        try:
            if self._device:
                self._device.turn_off()
                self._device.disconnect()
        except (OSError, IOError, ValueError):
            pass

        if self._capture:
            self._capture.stop()

        self.status_changed.emit("엔진 중지됨")
