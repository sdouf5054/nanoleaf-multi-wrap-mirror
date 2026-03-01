"""미러링 루프 — QThread 기반"""

import time
import os
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
        self._running = False
        self._paused = False

        # 외부에서 실시간 변경 가능한 값
        self.brightness = config["mirror"]["brightness"]
        self.smoothing_enabled = True

    def run(self):
        cfg = self.config
        dev_cfg = cfg["device"]
        layout_cfg = cfg["layout"]
        color_cfg = cfg["color"]
        mirror_cfg = cfg["mirror"]

        led_count = dev_cfg["led_count"]
        vendor_id = int(dev_cfg["vendor_id"], 16)
        product_id = int(dev_cfg["product_id"], 16)

        # --- 초기화 ---
        try:
            self.status_changed.emit("화면 캡처 초기화...")
            capture = ScreenCapture(mirror_cfg["monitor_index"])
            capture.start()

            # 디버그 로그
            import logging
            logging.basicConfig(
                filename=os.path.join(os.path.dirname(os.path.dirname(__file__)), "mirror_debug.log"),
                level=logging.DEBUG, format="%(asctime)s %(message)s"
            )
            logging.debug(f"screen: {capture.screen_w}x{capture.screen_h}")
            test_frame = capture.grab()
            if test_frame is not None:
                logging.debug(f"frame shape: {test_frame.shape}, mean: {test_frame.mean():.1f}")
            else:
                logging.debug("grab() returned None")

            self.status_changed.emit("가중치 행렬 생성...")
            led_positions, led_sides = get_led_positions(
                capture.screen_w, capture.screen_h,
                layout_cfg["segments"], led_count,
                orientation=mirror_cfg.get("orientation", "auto"),
                portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
            )
            weight_matrix = build_weight_matrix(
                capture.screen_w, capture.screen_h,
                led_positions, led_sides,
                mirror_cfg["grid_cols"], mirror_cfg["grid_rows"],
                mirror_cfg["decay_radius"], mirror_cfg["parallel_penalty"]
            )
            logging.debug(f"weight_matrix: {weight_matrix.shape}, sum[0]: {weight_matrix[0].sum():.3f}")

            self.status_changed.emit("Nanoleaf 연결 중...")
            device = NanoleafDevice(vendor_id, product_id, led_count)
            device.connect()
            logging.debug("device connected")

        except Exception as e:
            self.error.emit(str(e))
            return

        # --- 미러링 루프 ---
        self._running = True
        self.status_changed.emit("미러링 실행 중")

        prev_colors = None
        frame_count = 0
        start_time = time.time()
        fps_display_time = start_time
        frame_interval = 1.0 / mirror_cfg["target_fps"]

        try:
            while self._running:
                loop_start = time.perf_counter()

                if self._paused:
                    time.sleep(0.05)
                    continue

                frame = capture.grab()
                if frame is None:
                    time.sleep(0.005)
                    continue

                # 실시간 값 반영
                mirror_cfg_live = dict(mirror_cfg)
                mirror_cfg_live["brightness"] = self.brightness
                mirror_cfg_live["smoothing_factor"] = (
                    mirror_cfg["smoothing_factor"] if self.smoothing_enabled else 0.0
                )

                grb_data, rgb_colors = compute_led_colors(
                    frame, weight_matrix, color_cfg, mirror_cfg_live, prev_colors
                )
                prev_colors = rgb_colors

                device.send_rgb(grb_data)
                frame_count += 1

                now = time.time()
                if now - fps_display_time >= 1.0:
                    fps = frame_count / (now - start_time)
                    self.fps_updated.emit(fps)
                    fps_display_time = now

                elapsed = time.perf_counter() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

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
        self._running = False
