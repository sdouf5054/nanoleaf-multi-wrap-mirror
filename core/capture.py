"""화면 캡처 — dxcam 래퍼 (디스플레이 변경·회전·분리 대응 강화)

[변경 사항 v4 — 캡처 세션 사망 감지]
- ★ 연속 grab() None 카운터 (`_consecutive_nones`) 추가
  정상 정지 화면: 가끔 None (1~수 회) → last_frame 반환 (기존 동작)
  캡처 세션 사망: 연속 수십~수백 회 None → 임계값 초과 시 자동 recreate
  + None 반환 → mirror.py의 stale detection이 정상 발동
- ★ _STALE_NONE_THRESHOLD: 연속 None 허용 횟수 (기본 60회)
  60fps 기준 ~1초, 30fps 기준 ~2초 동안 새 프레임이 없으면 캡처 사망으로 판단
- ★ _grab_inner()에서 연속 None 초과 시:
  1) _recreate() 시도
  2) None 반환 (last_frame 대신)
  → mirror.py의 STALE_THRESHOLD (3초) 타이머가 정상 작동

[변경 사항 v3 — CPU 최적화]
- ★ 연속 캡처 모드(start) 제거 → on-demand grab() 전용
- 재생성 시 dxcam 싱글턴 캐시 정리 유지 (v2와 동일)
"""

import dxcam
import time
import threading
import numpy as np

# ── 캡처 세션 사망 감지 임계값 ────────────────────────────────────
# grab()이 연속으로 None을 반환한 횟수가 이 값을 초과하면
# 캡처 세션이 사망한 것으로 판단하고 recreate를 시도합니다.
#
# 정상 정지 화면에서는 dxcam이 가끔 None을 반환하지만 연속으로
# 수십 회 이상 None을 반환하지는 않습니다.
# 반면 캡처 세션 사망 시에는 영구적으로 None만 반환됩니다.
#
# 60fps 루프에서 60회 = ~1초, 30fps 루프에서 60회 = ~2초
_STALE_NONE_THRESHOLD = 60

# recreate 후 연속 실패 시 재시도 간격을 늘리기 위한 쿨다운
_RECREATE_COOLDOWN = 2.0  # 초


class ScreenCapture:
    def __init__(self, monitor_index=0):
        self.camera = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self.last_frame = None
        self._lock = threading.Lock()

        # ★ 캡처 세션 사망 감지
        self._consecutive_nones = 0
        self._last_recreate_time = 0.0

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

    # ── 프레임 획득 ──────────────────────────────────────────────────

    def grab(self):
        """최신 프레임 반환 — on-demand 캡처.

        ★ v4: 연속 None 카운터로 캡처 세션 사망 감지
        - 정상 정지 화면: None 수 회 → last_frame 반환 (기존 동작)
        - 캡처 세션 사망: 연속 None 임계값 초과 → recreate 시도 + None 반환
          → mirror.py의 stale detection이 정상 작동하여 복구 가능
        """
        with self._lock:
            return self._grab_inner()

    def _grab_inner(self):
        try:
            if self.camera is None:
                self._recreate()
                # recreate 직후에는 last_frame 반환 (한 번은 허용)
                return self.last_frame

            frame = self.camera.grab()

            if frame is not None:
                self.last_frame = frame
                self._consecutive_nones = 0  # ★ 리셋
                return frame

            # ── frame is None ──────────────────────────────────
            self._consecutive_nones += 1

            if self._consecutive_nones <= _STALE_NONE_THRESHOLD:
                # 정상 범위: 화면이 안 변했거나 일시적 지연
                # → 기존 동작 유지 (last_frame 반환)
                return self.last_frame
            else:
                # ★ 임계값 초과: 캡처 세션 사망으로 판단
                # recreate 쿨다운 체크 (너무 빈번한 재생성 방지)
                now = time.monotonic()
                if now - self._last_recreate_time >= _RECREATE_COOLDOWN:
                    self._last_recreate_time = now
                    self._recreate()
                    self._consecutive_nones = 0

                # ★ None 반환 → mirror.py의 stale detection 발동
                return None

        except Exception:
            # 예외 발생 시에도 동일한 로직 적용
            self._consecutive_nones += 1

            if self._consecutive_nones <= _STALE_NONE_THRESHOLD:
                # 일시적 예외: last_frame으로 버팀
                return self.last_frame
            else:
                # 반복 예외: recreate 시도 + None 반환
                now = time.monotonic()
                if now - self._last_recreate_time >= _RECREATE_COOLDOWN:
                    self._last_recreate_time = now
                    self._recreate()
                    self._consecutive_nones = 0
                return None

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
                    self._consecutive_nones = 0  # ★ 성공하면 리셋
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
