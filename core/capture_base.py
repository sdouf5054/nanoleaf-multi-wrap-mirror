"""캡처 Stale Detection Mixin — 연속 None 감지 + recreate 쿨다운

ScreenCapture(dxcam)와 NativeScreenCapture가 동일하게 사용하던
consecutive-None 감지 + recreate 쿨다운 로직을 한 곳으로 통합합니다.

[사용법]
    class MyCapture(StaleDetectionMixin):
        def _do_grab(self):
            '''한 프레임 획득 시도. 성공 시 frame, 실패 시 None 반환.'''
            ...

        def _do_recreate(self):
            '''캡처 세션 재초기화.'''
            ...

        def grab(self):
            with self._lock:
                return self._grab_with_stale_detection()
"""

import time
import threading

from core.constants import STALE_NONE_THRESHOLD, RECREATE_COOLDOWN


class StaleDetectionMixin:
    """연속 None 감지 + recreate 쿨다운 공통 로직.

    서브클래스는 다음을 구현해야 합니다:
        _do_grab() -> frame or None: 한 프레임 획득 시도
        _do_recreate(): 캡처 세션 재초기화

    서브클래스의 __init__에서 _init_stale_detection()을 호출해야 합니다.
    """

    def _init_stale_detection(self):
        """stale detection 상태 초기화 — 서브클래스 __init__에서 호출."""
        self.last_frame = None
        self._lock = threading.Lock()
        self._consecutive_nones = 0
        self._last_recreate_time = 0.0

    def _grab_with_stale_detection(self):
        """stale detection이 적용된 프레임 획득.

        _lock을 잡은 상태에서 호출해야 합니다.

        동작:
        1. _do_grab() 호출
        2. 프레임 수신 → last_frame 갱신, 카운터 리셋, 프레임 반환
        3. None 수신 (연속 카운터 ≤ 임계값) → last_frame 반환 (정상 범위)
        4. None 수신 (연속 카운터 > 임계값) → recreate 시도 + None 반환
           → 상위의 stale detection 타이머가 정상 작동
        """
        try:
            frame = self._do_grab()
        except Exception:
            frame = None

        if frame is not None:
            self.last_frame = frame
            self._consecutive_nones = 0
            return frame

        # ── frame is None ──
        self._consecutive_nones += 1

        if self._consecutive_nones <= STALE_NONE_THRESHOLD:
            # 정상 범위: 화면 정지 또는 일시적 지연
            return self.last_frame

        # 임계값 초과: 캡처 세션 사망 추정
        now = time.monotonic()
        if now - self._last_recreate_time >= RECREATE_COOLDOWN:
            self._last_recreate_time = now
            try:
                self._do_recreate()
            except Exception:
                pass
            self._consecutive_nones = 0

        # None 반환 → mirror.py의 stale detection 발동
        return None
