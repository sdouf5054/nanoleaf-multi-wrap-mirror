"""native_capture.py — fast_capture.dll Python 래퍼

dxcam을 대체하는 네이티브 DXGI 캡처 모듈.
기존 ScreenCapture와 호환되는 인터페이스를 제공합니다.

fallback 체인: DLL(DXGI) → dxcam(DXGI) → mss(GDI)
DLL과 dxcam은 모두 DXGI Desktop Duplication에 의존하므로,
미지원 GPU에서는 둘 다 실패하고 mss로 최종 fallback.
"""

import os
import sys
import ctypes
import numpy as np
import time

from core.capture_base import StaleDetectionMixin
from core.capture_log import clog

_ACCESS_LOST_REINIT_THRESHOLD = 5


def _find_dll():
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
    """네이티브 DXGI 캡처 — DLL 내부에서 서브샘플링."""

    def __init__(self, monitor_index=0, out_width=64, out_height=32):
        self.monitor_index = monitor_index
        self.out_width = out_width
        self.out_height = out_height
        self._closed = False
        self._access_lost_count = 0

        dll_path = _find_dll()
        clog("[native] dll=%s", dll_path)
        self._dll = ctypes.CDLL(dll_path)

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
        clog("[native] init: monitor=%d, out=%dx%d → %d",
             monitor_index, out_width, out_height, result)
        if result != 0:
            error_map = {
                -1: "D3D11 디바이스 생성 실패",
                -2: "DXGI 디바이스 쿼리 실패",
                -3: "어댑터 획득 실패",
                -4: f"모니터 {monitor_index}을 찾을 수 없음",
                -5: "DXGI Output1 인터페이스 실패",
                -6: "Desktop Duplication 시작 실패",
                -7: "축소용 텍스처 생성 실패",
                -8: "STAGING 텍스처 생성 실패",
            }
            raise RuntimeError(
                f"네이티브 캡처 초기화 실패 (코드 {result}): "
                f"{error_map.get(result, '알 수 없는 오류')}"
            )

        self._buf_size = out_width * out_height * 4
        self._buffer = ctypes.create_string_buffer(self._buf_size)
        self.screen_w = self._dll.capture_get_width()
        self.screen_h = self._dll.capture_get_height()
        clog("[native] OK: screen=%dx%d", self.screen_w, self.screen_h)

    def grab(self):
        if self._closed:
            return None
        result = self._dll.capture_grab(self._buffer, self._buf_size)
        if result == 1:
            self._access_lost_count = 0
            return np.frombuffer(self._buffer, dtype=np.uint8).reshape(
                self.out_height, self.out_width, 4)
        elif result == 0:
            self._access_lost_count = 0
            return None
        elif result == -2:
            self._access_lost_count += 1
            clog("[native] access lost (%d)", self._access_lost_count)
            if self._access_lost_count <= _ACCESS_LOST_REINIT_THRESHOLD:
                self._dll.capture_reset()
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
            else:
                try:
                    self._dll.capture_cleanup()
                    time.sleep(0.5)
                    if self._dll.capture_init(
                        self.monitor_index, self.out_width, self.out_height) == 0:
                        self.screen_w = self._dll.capture_get_width()
                        self.screen_h = self._dll.capture_get_height()
                        self._access_lost_count = 0
                except Exception:
                    pass
            return None
        return None

    def grab_rgb(self):
        bgra = self.grab()
        return bgra[:, :, [2, 1, 0]] if bgra is not None else None

    def reset(self):
        if not self._closed:
            self._dll.capture_reset()
            self.screen_w = self._dll.capture_get_width()
            self.screen_h = self._dll.capture_get_height()
            self._access_lost_count = 0

    def full_reinit(self):
        if self._closed:
            return False
        try:
            self._dll.capture_cleanup()
            time.sleep(0.3)
            if self._dll.capture_init(
                self.monitor_index, self.out_width, self.out_height) == 0:
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
                self._access_lost_count = 0
                return True
        except Exception:
            pass
        return False

    def close(self):
        if not self._closed:
            self._dll.capture_cleanup()
            self._closed = True

    def __del__(self):
        self.close()


class NativeScreenCapture(StaleDetectionMixin):
    """ScreenCapture 호환 래퍼. fallback 체인: DLL → ScreenCapture(dxcam+mss)."""

    def __init__(self, monitor_index=0, grid_cols=64, grid_rows=32):
        self._cap = None
        self._fallback = None
        self.monitor_index = monitor_index
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        self.screen_w = 0
        self.screen_h = 0
        self._init_stale_detection()

    def start(self, max_wait=30, target_fps=60):
        try:
            self._cap = FastCapture(
                monitor_index=self.monitor_index,
                out_width=self.grid_cols,
                out_height=self.grid_rows,
            )
            self.screen_w = self._cap.screen_w
            self.screen_h = self._cap.screen_h

            deadline = time.time() + max_wait
            while time.time() < deadline:
                frame = self._cap.grab_rgb()
                if frame is not None:
                    self.last_frame = frame
                    self._consecutive_nones = 0
                    clog("[NativeSC] DLL 성공: %dx%d", self.screen_w, self.screen_h)
                    return True
                time.sleep(0.1)

            clog("[NativeSC] DLL 타임아웃 → fallback")
            return self._fallback_start(max_wait, target_fps)

        except Exception as e:
            clog("[NativeSC] DLL 실패: %s → fallback", e)
            return self._fallback_start(max_wait, target_fps)

    def _fallback_start(self, max_wait, target_fps):
        from core.capture import ScreenCapture
        self._fallback = ScreenCapture(self.monitor_index)
        self._fallback.set_grid_size(self.grid_cols, self.grid_rows)
        result = self._fallback.start(max_wait, target_fps)
        self.screen_w = self._fallback.screen_w
        self.screen_h = self._fallback.screen_h
        clog("[NativeSC] fallback: %s, %dx%d, mode=%s",
             result, self.screen_w, self.screen_h,
             getattr(self._fallback, '_mode', '?'))
        return result

    # ── StaleDetectionMixin ──

    def _do_grab(self):
        return self._cap.grab_rgb() if self._cap else None

    def _do_recreate(self):
        if self._cap and self._cap.full_reinit():
            self.screen_w = self._cap.screen_w
            self.screen_h = self._cap.screen_h

    # ── 공개 API ──

    def grab(self):
        with self._lock:
            if self._fallback is not None:
                return self._fallback.grab()
            return self._grab_with_stale_detection()

    def _recreate(self):
        if self._fallback is not None:
            self._fallback._recreate()
            self.screen_w = self._fallback.screen_w
            self.screen_h = self._fallback.screen_h
        elif self._cap:
            if self._cap.full_reinit():
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h
                self._consecutive_nones = 0
            else:
                self._cap.reset()
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h

    def stop(self):
        with self._lock:
            if self._fallback is not None:
                self._fallback.stop()
                self._fallback = None
            elif self._cap:
                self._cap.close()
                self._cap = None
