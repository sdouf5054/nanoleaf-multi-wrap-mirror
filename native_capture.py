"""native_capture.py — fast_capture.dll Python 래퍼

dxcam을 대체하는 네이티브 DXGI 캡처 모듈.
기존 ScreenCapture와 호환되는 인터페이스를 제공합니다.

NativeScreenCapture는 StaleDetectionMixin을 사용하여
capture.py와 동일한 stale detection 로직을 공유합니다.

[디버그 로깅 추가] 로직 변경 없음 — clog() 호출만 삽입
"""

import os
import sys
import ctypes
import numpy as np
import time

from core.capture_base import StaleDetectionMixin
from core.constants import RECREATE_COOLDOWN
from core.capture_log import clog

# ── FastCapture 전용 상수 ─────────────────────────────────────────
_ACCESS_LOST_REINIT_THRESHOLD = 5


def _find_dll():
    """fast_capture.dll 경로를 찾습니다."""
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "fast_capture.dll"))
    project_root = os.path.dirname(here)
    candidates.append(os.path.join(project_root, "native", "fast_capture.dll"))
    candidates.append(os.path.join(project_root, "fast_capture.dll"))
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, "fast_capture.dll"))
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "fast_capture.dll을 찾을 수 없습니다.\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


class FastCapture:
    """네이티브 DXGI 캡처 — GPU→CPU 풀 복사 후 DLL 내부에서 서브샘플링."""

    def __init__(self, monitor_index=0, out_width=64, out_height=32):
        self.monitor_index = monitor_index
        self.out_width = out_width
        self.out_height = out_height
        self._closed = False
        self._access_lost_count = 0

        dll_path = _find_dll()
        clog("[native] FastCapture: dll=%s", dll_path)
        self._dll = ctypes.CDLL(dll_path)
        self._dll_path = dll_path

        self._dll.capture_init.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self._dll.capture_init.restype = ctypes.c_int
        self._dll.capture_grab.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self._dll.capture_grab.restype = ctypes.c_int
        self._dll.capture_get_width.argtypes = []
        self._dll.capture_get_width.restype = ctypes.c_int
        self._dll.capture_get_height.argtypes = []
        self._dll.capture_get_height.restype = ctypes.c_int
        self._dll.capture_reset.argtypes = []
        self._dll.capture_reset.restype = None
        self._dll.capture_cleanup.argtypes = []
        self._dll.capture_cleanup.restype = None

        result = self._dll.capture_init(monitor_index, out_width, out_height)
        clog("[native] capture_init: monitor=%d, out=%dx%d → result=%d",
             monitor_index, out_width, out_height, result)
        if result != 0:
            error_map = {
                -1: "D3D11 디바이스 생성 실패",
                -2: "DXGI 디바이스 쿼리 실패",
                -3: "어댑터 획득 실패",
                -4: f"모니터 {monitor_index}을 찾을 수 없음",
                -5: "DXGI Output1 인터페이스 실패",
                -6: "Desktop Duplication 시작 실패 (다른 앱이 점유 중?)",
                -7: "축소용 텍스처 생성 실패",
                -8: "STAGING 텍스처 생성 실패",
            }
            msg = error_map.get(result, '알 수 없는 오류')
            clog("[native] capture_init 실패: %s", msg)
            raise RuntimeError(
                f"네이티브 캡처 초기화 실패 (코드 {result}): {msg}"
            )

        self._buf_size = out_width * out_height * 4
        self._buffer = ctypes.create_string_buffer(self._buf_size)
        self.screen_w = self._dll.capture_get_width()
        self.screen_h = self._dll.capture_get_height()
        clog("[native] init OK: screen=%dx%d", self.screen_w, self.screen_h)

    def grab(self):
        """BGRA 프레임 캡처. 새 프레임이면 numpy 배열, 없으면 None."""
        if self._closed:
            return None

        result = self._dll.capture_grab(self._buffer, self._buf_size)

        if result == 1:
            self._access_lost_count = 0
            arr = np.frombuffer(self._buffer, dtype=np.uint8)
            return arr.reshape(self.out_height, self.out_width, 4)
        elif result == 0:
            self._access_lost_count = 0
            return None
        elif result == -2:
            self._access_lost_count += 1
            clog("[native] grab: access lost (count=%d)", self._access_lost_count)
            if self._access_lost_count <= _ACCESS_LOST_REINIT_THRESHOLD:
                self._dll.capture_reset()
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
            else:
                clog("[native] grab: access lost 초과 → full reinit")
                try:
                    self._dll.capture_cleanup()
                    time.sleep(0.5)
                    init_result = self._dll.capture_init(
                        self.monitor_index, self.out_width, self.out_height
                    )
                    clog("[native] full reinit result=%d", init_result)
                    if init_result == 0:
                        self.screen_w = self._dll.capture_get_width()
                        self.screen_h = self._dll.capture_get_height()
                        self._access_lost_count = 0
                except Exception as e:
                    clog("[native] full reinit 예외: %s", e)
            return None
        else:
            clog("[native] grab: unexpected result=%d", result)
            return None

    def grab_rgb(self):
        """RGB 프레임 캡처 (BGRA → RGB 변환)."""
        bgra = self.grab()
        if bgra is None:
            return None
        return bgra[:, :, [2, 1, 0]]

    def reset(self):
        """모니터 변경/해상도 변경 시 재초기화."""
        if not self._closed:
            clog("[native] reset")
            self._dll.capture_reset()
            self.screen_w = self._dll.capture_get_width()
            self.screen_h = self._dll.capture_get_height()
            self._access_lost_count = 0

    def full_reinit(self):
        """완전 재초기화 — cleanup 후 다시 init."""
        if self._closed:
            return False
        try:
            clog("[native] full_reinit")
            self._dll.capture_cleanup()
            time.sleep(0.3)
            result = self._dll.capture_init(
                self.monitor_index, self.out_width, self.out_height
            )
            clog("[native] full_reinit result=%d", result)
            if result == 0:
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
                self._access_lost_count = 0
                return True
            return False
        except Exception as e:
            clog("[native] full_reinit 예외: %s", e)
            return False

    def close(self):
        """리소스 해제."""
        if not self._closed:
            self._dll.capture_cleanup()
            self._closed = True

    def __del__(self):
        self.close()


class NativeScreenCapture(StaleDetectionMixin):
    """기존 core/capture.py의 ScreenCapture와 호환되는 래퍼.

    StaleDetectionMixin을 사용하여 capture.py와 동일한
    stale detection 로직을 공유합니다.
    """

    def __init__(self, monitor_index=0, grid_cols=64, grid_rows=32):
        self._cap = None
        self.monitor_index = monitor_index
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        self.screen_w = 0
        self.screen_h = 0
        self._init_stale_detection()  # last_frame, _lock, _consecutive_nones 등
        clog("[NativeSC] __init__: monitor=%d, grid=%dx%d", monitor_index, grid_cols, grid_rows)

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화."""
        clog("[NativeSC] start: max_wait=%d, target_fps=%d", max_wait, target_fps)
        try:
            self._cap = FastCapture(
                monitor_index=self.monitor_index,
                out_width=self.grid_cols,
                out_height=self.grid_rows,
            )
            self.screen_w = self._cap.screen_w
            self.screen_h = self._cap.screen_h
            clog("[NativeSC] FastCapture 생성 OK: screen=%dx%d", self.screen_w, self.screen_h)

            deadline = time.time() + max_wait
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                frame = self._cap.grab_rgb()
                if frame is not None:
                    self.last_frame = frame
                    self._consecutive_nones = 0
                    clog("[NativeSC] start 성공: attempt=%d, shape=%s, mean=%.1f",
                         attempt, frame.shape, frame.mean())
                    return True
                if attempt <= 5 or attempt % 20 == 0:
                    clog("[NativeSC] start attempt=%d: grab_rgb=None", attempt)
                time.sleep(0.1)

            clog("[NativeSC] start: 타임아웃 (%d초), 프레임 못 잡음", max_wait)
            return True

        except Exception as e:
            clog("[NativeSC] start 예외: %s → dxcam fallback", e)
            print(f"[NativeScreenCapture] 초기화 실패: {e}")
            print("[NativeScreenCapture] dxcam 폴백으로 전환합니다.")
            return self._fallback_start(max_wait, target_fps)

    def _fallback_start(self, max_wait, target_fps):
        """DLL 로드 실패 시 기존 dxcam으로 폴백."""
        clog("[NativeSC] fallback → dxcam")
        from core.capture import ScreenCapture as DxcamCapture
        self._dxcam_fallback = DxcamCapture(self.monitor_index)
        result = self._dxcam_fallback.start(max_wait, target_fps)
        self.screen_w = self._dxcam_fallback.screen_w
        self.screen_h = self._dxcam_fallback.screen_h
        clog("[NativeSC] fallback result=%s, screen=%dx%d", result, self.screen_w, self.screen_h)
        return result

    # ── StaleDetectionMixin 구현 ─────────────────────────────────

    def _do_grab(self):
        """한 프레임 획득 시도."""
        if self._cap is None:
            return None
        return self._cap.grab_rgb()

    def _do_recreate(self):
        """FastCapture의 full reinit을 시도."""
        clog("[NativeSC] _do_recreate")
        if self._cap is not None:
            if self._cap.full_reinit():
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h
                clog("[NativeSC] _do_recreate 성공: screen=%dx%d", self.screen_w, self.screen_h)

    # ── 프레임 획득 (공개 API) ───────────────────────────────────

    def grab(self):
        """프레임 반환 — stale detection 적용."""
        with self._lock:
            # dxcam 폴백 모드 — 폴백 capture.py가 자체 감지 로직을 가짐
            if hasattr(self, '_dxcam_fallback'):
                return self._dxcam_fallback.grab()
            return self._grab_with_stale_detection()

    # ── 외부에서 호출하는 recreate ───────────────────────────────

    def _recreate(self):
        """모니터 변경 시 외부에서 호출하는 재초기화."""
        if hasattr(self, '_dxcam_fallback'):
            self._dxcam_fallback._recreate()
            return
        if self._cap:
            if self._cap.full_reinit():
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h
                self._consecutive_nones = 0
            else:
                self._cap.reset()
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h

    # ── 종료 ─────────────────────────────────────────────────────

    def stop(self):
        """캡처 종료."""
        clog("[NativeSC] stop")
        with self._lock:
            if hasattr(self, '_dxcam_fallback'):
                self._dxcam_fallback.stop()
                return
            if self._cap:
                self._cap.close()
                self._cap = None
