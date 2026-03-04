"""미러링 루프 — QThread 기반 (디스플레이 변경·분리 대응 강화)

[변경 사항 v2]
- stop 시그널을 threading.Event로 변경 → wait()로 즉시 응답 가능
- 프레임 획득 실패 시 자동 재생성 + 일정 시간 실패 지속 시 강제 재생성
- 해상도/회전 변경 감지 로직을 안전한 try/except로 래핑
- device.send_rgb() 실패 시에도 루프가 계속 동작
"""

import time
import os
import threading
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from core.capture import ScreenCapture
from core.device import NanoleafDevice
from core.layout import get_led_positions, build_weight_matrix
from core.color import compute_led_colors


class MirrorThread(QThread):
    """백그라운드 미러링 스레드.

    Signals:
        fps_updated(float): 1초마다 현재 fps 전달
        error(str): 에러 발생 시 메시지 전달
        status_changed(str): 상태 변경 알림
    """

    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._stop_event = threading.Event()  # ★ bool 대신 Event 사용
        self._paused = False

        # 외부에서 실시간 변경 가능한 값
        self.brightness = config["mirror"]["brightness"]
        self.smoothing_enabled = True

    @property
    def _running(self):
        return not self._stop_event.is_set()

    def run(self):
        cfg = self.config
        dev_cfg = cfg["device"]
        layout_cfg = cfg["layout"]
        color_cfg = cfg["color"]
        mirror_cfg = cfg["mirror"]

        led_count = dev_cfg["led_count"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)
        target_fps = mirror_cfg["target_fps"]

        capture = None
        device = None

        # --- 초기화 ---
        try:
            self.status_changed.emit("화면 캡처 초기화...")
            capture = ScreenCapture(mirror_cfg["monitor_index"])
            capture.start(target_fps=target_fps)

            import logging
            debug_profile = cfg.get("options", {}).get("debug_profile", False)
            log_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log"
            )
            logger = logging.getLogger("nanoleaf.mirror")
            if debug_profile:
                logger.setLevel(logging.DEBUG)
                if not logger.handlers:
                    fh = logging.FileHandler(log_path, encoding="utf-8")
                    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                    logger.addHandler(fh)
                logger.propagate = False
                logger.debug(f"screen: {capture.screen_w}x{capture.screen_h}")
                logger.debug(f"capture target_fps: {target_fps}")

            test_frame = capture.grab()
            if debug_profile and test_frame is not None:
                logger.debug(
                    f"frame shape: {test_frame.shape}, mean: {test_frame.mean():.1f}"
                )

            self.status_changed.emit("가중치 행렬 생성...")

            # ── 레이아웃 계산 헬퍼 ──────────────────────────────────
            def _build_layout(w, h):
                """LED 위치 + 가중치 행렬을 (w, h) 해상도 기준으로 재계산."""
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
            # ────────────────────────────────────────────────────────

            weight_matrix = _build_layout(capture.screen_w, capture.screen_h)

            if debug_profile:
                logger.debug(
                    f"weight_matrix: {weight_matrix.shape}, "
                    f"sum[0]: {weight_matrix[0].sum():.3f}"
                )

            self.status_changed.emit("Nanoleaf 연결 중...")
            device = NanoleafDevice(vendor_id, product_id, led_count)
            device.connect()
            if debug_profile:
                logger.debug("device connected")

        except Exception as e:
            self.error.emit(str(e))
            if capture:
                capture.stop()
            return

        # --- 미러링 루프 ---
        self.status_changed.emit("미러링 실행 중")

        prev_colors = None
        frame_count = 0
        start_time = time.time()
        fps_display_time = start_time
        frame_interval = 1.0 / target_fps

        # ★ 프레임 실패 감시용
        last_good_frame_time = time.time()
        STALE_THRESHOLD = 3.0  # 3초간 새 프레임 없으면 강제 재생성

        PROFILE_INTERVAL = 60
        t_capture_acc = 0.0
        t_color_acc = 0.0
        t_usb_acc = 0.0
        t_total_acc = 0.0

        fps = 0.0

        # 현재 작업 해상도 추적
        active_w, active_h = capture.screen_w, capture.screen_h

        try:
            while not self._stop_event.is_set():
                loop_start = time.perf_counter()

                if self._paused:
                    # ★ Event.wait()로 즉시 중지 가능
                    if self._stop_event.wait(timeout=0.05):
                        break
                    continue

                # ── 캡처 ────────────────────────────────────────────
                if debug_profile:
                    t0 = time.perf_counter()

                frame = capture.grab()

                if debug_profile:
                    t_capture_acc += time.perf_counter() - t0

                if frame is None:
                    # ★ 오래 실패하면 강제 재생성
                    if time.time() - last_good_frame_time > STALE_THRESHOLD:
                        self.status_changed.emit("프레임 없음 — 캡처 재초기화...")
                        capture._recreate()
                        last_good_frame_time = time.time()
                        # 재생성 후 해상도 변경 가능
                        if capture.screen_w > 0 and capture.screen_h > 0:
                            if (capture.screen_w != active_w
                                    or capture.screen_h != active_h):
                                active_w, active_h = (
                                    capture.screen_w, capture.screen_h
                                )
                                try:
                                    weight_matrix = _build_layout(
                                        active_w, active_h
                                    )
                                    prev_colors = None
                                except Exception:
                                    pass
                        self.status_changed.emit("미러링 실행 중")

                    if self._stop_event.wait(timeout=0.01):
                        break
                    continue

                last_good_frame_time = time.time()

                # ── 해상도/회전 변경 감지 ───────────────────────────
                try:
                    current_h, current_w = frame.shape[:2]
                except Exception:
                    continue

                if current_h != active_h or current_w != active_w:
                    self.status_changed.emit(
                        f"해상도 변경 감지 ({current_w}×{current_h})"
                        " — 레이아웃 재계산 중..."
                    )
                    active_w, active_h = current_w, current_h
                    capture.screen_w = current_w
                    capture.screen_h = current_h

                    try:
                        weight_matrix = _build_layout(current_w, current_h)
                        prev_colors = None
                        if debug_profile:
                            logger.debug(
                                f"layout rebuilt: {current_w}x{current_h}, "
                                f"wmat={weight_matrix.shape}"
                            )
                    except Exception as layout_err:
                        if debug_profile:
                            logger.debug(f"layout rebuild error: {layout_err}")
                    else:
                        self.status_changed.emit("미러링 실행 중")

                # ── 실시간 값 반영 ──────────────────────────────────
                mirror_cfg_live = dict(mirror_cfg)
                mirror_cfg_live["brightness"] = self.brightness
                mirror_cfg_live["smoothing_factor"] = (
                    mirror_cfg["smoothing_factor"]
                    if self.smoothing_enabled else 0.0
                )

                # ── 색상 연산 ───────────────────────────────────────
                if debug_profile:
                    t1 = time.perf_counter()

                try:
                    grb_data, rgb_colors = compute_led_colors(
                        frame, weight_matrix, color_cfg, mirror_cfg_live,
                        prev_colors,
                    )
                    prev_colors = rgb_colors
                except Exception:
                    # weight_matrix와 frame 크기 불일치 등
                    prev_colors = None
                    continue

                if debug_profile:
                    t_color_acc += time.perf_counter() - t1

                # ── USB 전송 ────────────────────────────────────────
                if debug_profile:
                    t2 = time.perf_counter()

                try:
                    device.send_rgb(grb_data)
                except Exception:
                    # USB 일시 장애 — 스킵하고 계속
                    pass

                if debug_profile:
                    t_usb_acc += time.perf_counter() - t2

                frame_count += 1
                if debug_profile:
                    t_total_acc += time.perf_counter() - loop_start

                now = time.time()
                if now - fps_display_time >= 1.0:
                    fps = frame_count / (now - start_time)
                    self.fps_updated.emit(fps)
                    fps_display_time = now

                if debug_profile and frame_count % PROFILE_INTERVAL == 0:
                    n = PROFILE_INTERVAL
                    avg_cap = t_capture_acc / n * 1000
                    avg_color = t_color_acc / n * 1000
                    avg_usb = t_usb_acc / n * 1000
                    avg_total = t_total_acc / n * 1000
                    avg_sleep = max(
                        0, frame_interval - avg_total / 1000
                    ) * 1000
                    logger.debug(
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
                    # ★ Event.wait()로 sleep 대체 → stop 즉시 반응
                    if self._stop_event.wait(timeout=sleep_time):
                        break

        except Exception as e:
            self.error.emit(f"미러링 오류: {e}")
        finally:
            try:
                device.turn_off()
                device.disconnect()
            except Exception:
                pass
            capture.stop()
            self.status_changed.emit("미러링 중지됨")

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
        """★ Event.set()으로 즉시 루프 탈출 — sleep/wait 중이라도 바로 반응"""
        self._stop_event.set()
