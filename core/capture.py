"""화면 캡처 — dxcam 래퍼 (부팅 시 지연 대응)"""

import dxcam
import time
import numpy as np


class ScreenCapture:
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self.last_frame = None

    def start(self, max_wait=30):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기.

        부팅 직후 화면이 아직 준비 안 된 경우 최대 max_wait초 대기.
        dxcam이 죽으면 재생성 시도.
        """
        deadline = time.time() + max_wait
        attempt = 0

        while time.time() < deadline:
            try:
                if self.camera is None:
                    self.camera = dxcam.create(device_idx=0, output_idx=self.monitor_index)
                    attempt += 1

                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    return True

                time.sleep(0.5)

            except Exception:
                # dxcam 에러 시 재생성
                self._destroy_camera()
                time.sleep(1.0)

        # 최후 수단: 마지막 시도에서 프레임이 잡혔지만 검은색이면 해상도라도 사용
        if self.camera is not None:
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    return True
                time.sleep(0.2)

        # fallback
        self.screen_w, self.screen_h = 2560, 1440
        return True

    def grab(self):
        """최신 프레임 반환. 캡처 실패 시 dxcam 재생성 시도."""
        try:
            frame = self.camera.grab()
            if frame is not None:
                self.last_frame = frame
                return frame
            return self.last_frame
        except Exception:
            # dxcam이 죽었으면 재생성
            self._recreate()
            return self.last_frame

    def _recreate(self):
        """dxcam 재생성"""
        self._destroy_camera()
        try:
            self.camera = dxcam.create(device_idx=0, output_idx=self.monitor_index)
        except Exception:
            pass

    def _destroy_camera(self):
        if self.camera is not None:
            try:
                del self.camera
            except Exception:
                pass
            self.camera = None

    def stop(self):
        self._destroy_camera()
