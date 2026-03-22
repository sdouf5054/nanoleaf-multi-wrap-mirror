"""화면 캡처 — dxcam 래퍼 (디스플레이 변경·회전·분리 대응 강화)

StaleDetectionMixin을 사용하여 연속 None 감지 + recreate 쿨다운을 처리합니다.

[디버그 로깅 추가] 로직 변경 없음 — clog() 호출만 삽입
"""

import dxcam
import time
import numpy as np

from core.capture_base import StaleDetectionMixin
from core.capture_log import clog


class ScreenCapture(StaleDetectionMixin):
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self._init_stale_detection()  # last_frame, _lock, _consecutive_nones 등
        clog("[dxcam] __init__: monitor_index=%d", monitor_index)

    # ── 초기화 ───────────────────────────────────────────────────

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기."""
        clog("[dxcam] start: max_wait=%d, target_fps=%d", max_wait, target_fps)
        deadline = time.time() + max_wait
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            try:
                if self.camera is None:
                    self.camera = dxcam.create(
                        device_idx=0,
                        output_idx=self.monitor_index,
                        max_buffer_len=2,
                    )
                    clog("[dxcam] create OK: device_idx=0, output_idx=%d, camera=%s",
                         self.monitor_index, self.camera)
                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    clog("[dxcam] start 성공: attempt=%d, screen=%dx%d, shape=%s, mean=%.1f",
                         attempt, self.screen_w, self.screen_h, f.shape, f.mean())
                    return True
                if attempt <= 5 or attempt % 10 == 0:
                    clog("[dxcam] start attempt=%d: grab=%s",
                         attempt, "None" if f is None else f"shape={f.shape},mean={f.mean():.1f}")
                time.sleep(0.5)
            except Exception as e:
                clog("[dxcam] start attempt=%d 예외: %s", attempt, e)
                self._destroy_camera()
                time.sleep(1.0)

        # 최후 수단
        clog("[dxcam] start: 메인 루프 타임아웃 (%d초), 최후 시도...", max_wait)
        if self.camera is not None:
            for i in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    clog("[dxcam] start 최후 시도 성공: i=%d, screen=%dx%d", i, self.screen_w, self.screen_h)
                    return True
                time.sleep(0.2)

        clog("[dxcam] start 실패: 모든 시도 실패, 가짜 해상도 2560x1440 설정")
        self.screen_w, self.screen_h = 2560, 1440
        return True

    # ── StaleDetectionMixin 구현 ─────────────────────────────────

    def _do_grab(self):
        """한 프레임 획득 시도."""
        if self.camera is None:
            clog("[dxcam] _do_grab: camera is None → recreate")
            self._do_recreate()
            return None
        return self.camera.grab()

    def _do_recreate(self):
        """dxcam 카메라 안전하게 재생성."""
        clog("[dxcam] _do_recreate 시작")
        self._destroy_camera()
        time.sleep(0.3)
        try:
            self.camera = dxcam.create(
                device_idx=0,
                output_idx=self.monitor_index,
                max_buffer_len=2,
            )
            clog("[dxcam] _do_recreate: create OK")
            for i in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    clog("[dxcam] _do_recreate 성공: i=%d, screen=%dx%d", i, self.screen_w, self.screen_h)
                    break
                time.sleep(0.2)
            else:
                clog("[dxcam] _do_recreate: 5번 grab 전부 None")
        except Exception as e:
            clog("[dxcam] _do_recreate 예외: %s", e)
            self.camera = None

    # ── 프레임 획득 (공개 API) ───────────────────────────────────

    def grab(self):
        """최신 프레임 반환 — stale detection 적용."""
        with self._lock:
            return self._grab_with_stale_detection()

    # ── 외부에서 호출하는 recreate (모니터 변경 등) ───────────────

    def _recreate(self):
        """모니터 변경 시 외부에서 호출하는 재초기화."""
        clog("[dxcam] _recreate (외부 호출)")
        self._do_recreate()

    # ── 내부 헬퍼 ────────────────────────────────────────────────

    def _destroy_camera(self):
        """카메라 안전하게 파괴 — dxcam 싱글턴 캐시까지 정리."""
        if self.camera is not None:
            clog("[dxcam] _destroy_camera")
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
        clog("[dxcam] stop")
        with self._lock:
            self._destroy_camera()
