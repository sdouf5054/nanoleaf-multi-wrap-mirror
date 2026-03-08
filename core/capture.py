"""화면 캡처 — dxcam 래퍼 (디스플레이 변경·회전·분리 대응 강화)

StaleDetectionMixin을 사용하여 연속 None 감지 + recreate 쿨다운을 처리합니다.
"""

import dxcam
import time
import numpy as np

from core.capture_base import StaleDetectionMixin


class ScreenCapture(StaleDetectionMixin):
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self._init_stale_detection()  # last_frame, _lock, _consecutive_nones 등

    # ── 초기화 ───────────────────────────────────────────────────

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기."""
        deadline = time.time() + max_wait

        while time.time() < deadline:
            try:
                if self.camera is None:
                    self.camera = dxcam.create(
                        device_idx=0,
                        output_idx=self.monitor_index,
                        max_buffer_len=2,
                    )
                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    return True
                time.sleep(0.5)
            except Exception:
                self._destroy_camera()
                time.sleep(1.0)

        # 최후 수단
        if self.camera is not None:
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    return True
                time.sleep(0.2)

        self.screen_w, self.screen_h = 2560, 1440
        return True

    # ── StaleDetectionMixin 구현 ─────────────────────────────────

    def _do_grab(self):
        """한 프레임 획득 시도."""
        if self.camera is None:
            self._do_recreate()
            return None
        return self.camera.grab()

    def _do_recreate(self):
        """dxcam 카메라 안전하게 재생성."""
        self._destroy_camera()
        time.sleep(0.3)
        try:
            self.camera = dxcam.create(
                device_idx=0,
                output_idx=self.monitor_index,
                max_buffer_len=2,
            )
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    break
                time.sleep(0.2)
        except Exception:
            self.camera = None

    # ── 프레임 획득 (공개 API) ───────────────────────────────────

    def grab(self):
        """최신 프레임 반환 — stale detection 적용."""
        with self._lock:
            return self._grab_with_stale_detection()

    # ── 외부에서 호출하는 recreate (모니터 변경 등) ───────────────

    def _recreate(self):
        """모니터 변경 시 외부에서 호출하는 재초기화."""
        self._do_recreate()

    # ── 내부 헬퍼 ────────────────────────────────────────────────

    def _destroy_camera(self):
        """카메라 안전하게 파괴 — dxcam 싱글턴 캐시까지 정리."""
        if self.camera is not None:
            try:
                self.camera.stop()
            except (RuntimeError, OSError, Exception):
                pass
            try:
                key = (0, self.monitor_index)
                cache = dxcam.DXFactory._camera_instances
                if key in cache:
                    del cache[key]
            except Exception:
                pass
            try:
                del self.camera
            except Exception:
                pass
            self.camera = None

    # ── 종료 ─────────────────────────────────────────────────────

    def stop(self):
        with self._lock:
            self._destroy_camera()
