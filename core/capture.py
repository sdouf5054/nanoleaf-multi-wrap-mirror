"""화면 캡처 — dxcam 래퍼 (디스플레이 변경·회전·분리 대응 강화)

[변경 사항 v3 — CPU 최적화]
- ★ 연속 캡처 모드(start) 제거 → on-demand grab() 전용
  - dxcam.start()는 내부에서 별도 스레드를 생성하여 target_fps로
    화면을 캡처하고 링 버퍼에 저장합니다.
  - 이 내부 스레드가 CPU의 주요 소비원 (30fps 설정 시 ~1-2% CPU)
  - grab()은 호출 시점에 한 번만 캡처하므로 idle 시 CPU 0%
  - 미러링 루프에서 frame_interval 대기 후 grab()을 호출하면
    연속 캡처와 동일한 fps를 달성하면서 CPU를 절약합니다.

- 재생성 시 dxcam 싱글턴 캐시 정리 유지 (v2와 동일)
- _started 플래그 및 연속 캡처 관련 코드 완전 제거
"""

import dxcam
import time
import threading
import numpy as np


class ScreenCapture:
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self.last_frame = None
        self._lock = threading.Lock()

    # ── 초기화 ───────────────────────────────────────────────────────

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기.

        ★ target_fps는 호환성을 위해 파라미터로 남기지만,
        연속 캡처 모드를 사용하지 않으므로 내부에서는 사용하지 않습니다.
        미러링 루프의 frame_interval이 실질적 fps를 제어합니다.
        """
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
                    return True
                time.sleep(0.2)

        self.screen_w, self.screen_h = 2560, 1440
        return True

    # ── 프레임 획득 ──────────────────────────────────────────────────

    def grab(self):
        """최신 프레임 반환 — on-demand 캡처.

        ★ 연속 캡처 모드 대신 매 호출 시 grab()으로 한 장 캡처.
        dxcam.grab()은 내부적으로 DXGI Desktop Duplication API를 사용하며,
        화면이 변경되지 않았으면 None을 반환합니다 (CPU 거의 0).
        화면이 변경되었으면 GPU→CPU 복사 1회만 수행합니다.
        """
        with self._lock:
            return self._grab_inner()

    def _grab_inner(self):
        try:
            if self.camera is None:
                self._recreate()
                return self.last_frame

            frame = self.camera.grab()

            if frame is not None:
                self.last_frame = frame
                return frame
            return self.last_frame

        except Exception:
            self._recreate()
            return self.last_frame

    # ── 재생성 ───────────────────────────────────────────────────────

    def _recreate(self):
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
                    break
                time.sleep(0.2)
        except Exception:
            self.camera = None

    def _destroy_camera(self):
        """카메라 안전하게 파괴 — dxcam 싱글턴 캐시까지 정리"""
        if self.camera is not None:
            # ★ 연속 캡처 모드를 사용하지 않으므로 stop() 호출 불필요
            # 하지만 혹시 외부에서 start()를 호출했을 경우를 대비
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

    # ── 종료 ─────────────────────────────────────────────────────────

    def stop(self):
        with self._lock:
            self._destroy_camera()
