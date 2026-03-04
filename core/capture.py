"""화면 캡처 — dxcam 래퍼 (디스플레이 변경·회전·분리 대응 강화)

[변경 사항 v2]
- dxcam 연속 캡처 모드(start)의 내부 스레드가 해상도 변경·모니터 분리 시
  ValueError / OSError로 죽는 문제에 대응.
- grab()에서 dxcam 스레드 생존 여부를 확인하고, 죽었으면 자동 재생성.
- get_latest_frame() 대신 타임아웃 가드가 있는 안전한 래퍼 사용.
- _recreate() 시 연속 캡처를 안전하게 중단한 뒤 재시작.
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
        self._started = False
        self._target_fps = 60
        self._lock = threading.Lock()  # 재생성 중 동시 접근 방지

    # ── 초기화 ───────────────────────────────────────────────────────

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화. 유효한 프레임이 잡힐 때까지 대기."""
        self._target_fps = target_fps
        deadline = time.time() + max_wait
        attempt = 0

        while time.time() < deadline:
            try:
                if self.camera is None:
                    self.camera = dxcam.create(
                        device_idx=0,
                        output_idx=self.monitor_index,
                        max_buffer_len=2,
                    )
                    attempt += 1

                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._start_continuous(target_fps)
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
                    self._start_continuous(target_fps)
                    return True
                time.sleep(0.2)

        self.screen_w, self.screen_h = 2560, 1440
        return True

    # ── 연속 캡처 ────────────────────────────────────────────────────

    def _start_continuous(self, target_fps):
        """dxcam 연속 캡처 모드 시작"""
        if not self._started:
            try:
                self.camera.start(target_fps=target_fps, video_mode=False)
                self._started = True
            except Exception:
                self._started = False

    def _is_continuous_alive(self):
        """dxcam 내부 캡처 스레드가 살아있는지 확인.

        dxcam의 __thread 속성이 존재하고 is_alive()가 True이면 정상.
        해상도 변경·모니터 분리 시 내부 스레드가 ValueError로 죽으면 False 반환.
        """
        if not self._started or self.camera is None:
            return False
        try:
            # dxcam 내부 속성: _DXCamera__thread (name-mangled)
            t = getattr(self.camera, '_DXCamera__thread', None)
            if t is None:
                return False
            return t.is_alive()
        except Exception:
            return False

    # ── 프레임 획득 ──────────────────────────────────────────────────

    def grab(self):
        """최신 프레임 반환 — 장애 시 자동 재생성.

        1. 연속 캡처 모드가 살아있으면 get_latest_frame()
        2. 스레드가 죽었으면 → 재생성 후 grab() 폴백
        3. 모든 예외 → 재생성
        """
        with self._lock:
            return self._grab_inner()

    def _grab_inner(self):
        try:
            if self._started:
                # ★ 스레드 죽었으면 즉시 재생성 경로로
                if not self._is_continuous_alive():
                    self._recreate()
                    return self.last_frame

                frame = self.camera.get_latest_frame()
            else:
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
        """dxcam 카메라 안전하게 재생성.

        모니터 분리·해상도 변경·회전 등으로 내부 스레드가 죽은 경우 호출.
        새 카메라로 해상도 정보를 갱신하고 연속 캡처 모드를 재시작한다.
        """
        self._destroy_camera()

        # 모니터가 물리적으로 재연결될 시간 확보
        time.sleep(0.3)

        try:
            self.camera = dxcam.create(
                device_idx=0,
                output_idx=self.monitor_index,
                max_buffer_len=2,
            )
            # 재생성 직후 grab()으로 새 해상도 감지
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    break
                time.sleep(0.2)

            # 연속 캡처 모드 재시작
            self._start_continuous(self._target_fps)
        except Exception:
            # 모니터가 아직 준비 안 된 경우 — 다음 grab() 때 재시도
            self.camera = None
            self._started = False

    def _destroy_camera(self):
        """카메라 안전하게 파괴 — dxcam 싱글턴 캐시까지 정리

        ★ dxcam은 DXFactory._camera_instances (WeakValueDictionary)에
        (device_idx, output_idx) 키로 인스턴스를 캐싱합니다.
        del camera만으로는 내부 스레드가 참조를 잡고 있어 GC가 안 되므로,
        stop() 후 캐시에서 직접 삭제해야 dxcam.create()가 새 인스턴스를 생성합니다.
        """
        if self.camera is not None:
            # 1. 연속 캡처 중지
            if self._started:
                try:
                    self.camera.stop()
                except (RuntimeError, OSError, Exception):
                    pass

            # 2. ★ dxcam 팩토리 캐시에서 제거
            try:
                key = (0, self.monitor_index)
                cache = dxcam.DXFactory._camera_instances
                if key in cache:
                    del cache[key]
            except Exception:
                pass

            # 3. 로컬 참조 해제
            try:
                del self.camera
            except Exception:
                pass
            self.camera = None
            self._started = False

    # ── 종료 ─────────────────────────────────────────────────────────

    def stop(self):
        with self._lock:
            self._destroy_camera()
