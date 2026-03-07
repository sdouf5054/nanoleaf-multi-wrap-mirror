"""native_capture.py — fast_capture.dll Python 래퍼

dxcam을 대체하는 네이티브 DXGI 캡처 모듈.
기존 ScreenCapture와 호환되는 인터페이스를 제공합니다.

[변경 사항 v2 — 캡처 세션 사망 감지]
- ★ FastCapture: access lost (-2) 시 연속 실패 카운터 추적
  단순 reset으로 복구 안 되면 _access_lost_count 증가
  임계값 초과 시 full cleanup + reinit 시도
- ★ NativeScreenCapture: capture.py와 동일한 consecutive-None 감지
  _STALE_NONE_THRESHOLD 초과 시 None 반환 (last_frame 대신)
  → mirror.py의 stale detection이 정상 작동

[사용법]
    from native_capture import FastCapture

    cap = FastCapture(monitor_index=0, out_width=64, out_height=32)
    bgra_frame = cap.grab()    # numpy (32, 64, 4) BGRA uint8 또는 None
    rgb_frame  = cap.grab_rgb() # numpy (32, 64, 3) RGB uint8 또는 None
    cap.close()

[기존 ScreenCapture 대체]
    from native_capture import NativeScreenCapture as ScreenCapture
    # 나머지 코드는 동일하게 사용 가능
"""

import os
import sys
import ctypes
import numpy as np
import threading
import time


# ── 캡처 세션 사망 감지 임계값 ────────────────────────────────────
_STALE_NONE_THRESHOLD = 60   # 연속 None 허용 횟수
_RECREATE_COOLDOWN = 2.0     # recreate 재시도 최소 간격 (초)
_ACCESS_LOST_REINIT_THRESHOLD = 5  # access lost 연속 실패 시 full reinit


def _find_dll():
    """fast_capture.dll 경로를 찾습니다.

    검색 순서:
    1. 이 파일과 같은 디렉토리
    2. 프로젝트 루트/native/
    3. exe 빌드 시 _MEIPASS
    """
    candidates = []

    # 이 파일과 같은 디렉토리
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "fast_capture.dll"))

    # 프로젝트 루트/native/
    project_root = os.path.dirname(here)
    candidates.append(os.path.join(project_root, "native", "fast_capture.dll"))
    candidates.append(os.path.join(project_root, "fast_capture.dll"))

    # PyInstaller
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, "fast_capture.dll"))

    for path in candidates:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "fast_capture.dll을 찾을 수 없습니다.\n"
        "빌드 후 다음 위치 중 하나에 배치하세요:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


class FastCapture:
    """네이티브 DXGI 캡처 — GPU→CPU 풀 복사 후 DLL 내부에서 서브샘플링.

    [변경 v2]
    - ★ access lost (-2) 연속 실패 추적
      단순 reset으로 복구 안 되면 full cleanup + reinit 시도
    """

    def __init__(self, monitor_index=0, out_width=64, out_height=32):
        self.monitor_index = monitor_index
        self.out_width = out_width
        self.out_height = out_height
        self._closed = False

        # ★ access lost 추적
        self._access_lost_count = 0

        # DLL 로드
        dll_path = _find_dll()
        self._dll = ctypes.CDLL(dll_path)
        self._dll_path = dll_path

        # 함수 시그니처 선언
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

        # 초기화
        result = self._dll.capture_init(monitor_index, out_width, out_height)
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
            raise RuntimeError(
                f"네이티브 캡처 초기화 실패 (코드 {result}): "
                f"{error_map.get(result, '알 수 없는 오류')}"
            )

        # 출력 버퍼 사전 할당
        self._buf_size = out_width * out_height * 4
        self._buffer = ctypes.create_string_buffer(self._buf_size)

        # 화면 해상도
        self.screen_w = self._dll.capture_get_width()
        self.screen_h = self._dll.capture_get_height()

    def grab(self):
        """BGRA 프레임 캡처. 새 프레임이면 numpy 배열, 없으면 None.

        ★ v2: access lost 연속 실패 시 full reinit 시도
        """
        if self._closed:
            return None

        result = self._dll.capture_grab(self._buffer, self._buf_size)

        if result == 1:
            # 새 프레임 — 성공
            self._access_lost_count = 0  # ★ 리셋
            arr = np.frombuffer(self._buffer, dtype=np.uint8)
            return arr.reshape(self.out_height, self.out_width, 4)
        elif result == 0:
            # 화면 변경 없음 — 정상
            self._access_lost_count = 0  # ★ grab 자체는 성공
            return None
        elif result == -2:
            # ★ access lost — 재초기화 시도
            self._access_lost_count += 1

            if self._access_lost_count <= _ACCESS_LOST_REINIT_THRESHOLD:
                # 단순 reset 시도
                self._dll.capture_reset()
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
            else:
                # ★ 단순 reset으로 복구 안 됨 → full cleanup + reinit
                try:
                    self._dll.capture_cleanup()
                    time.sleep(0.5)
                    init_result = self._dll.capture_init(
                        self.monitor_index, self.out_width, self.out_height
                    )
                    if init_result == 0:
                        self.screen_w = self._dll.capture_get_width()
                        self.screen_h = self._dll.capture_get_height()
                        self._access_lost_count = 0
                except Exception:
                    pass

            return None
        else:
            return None

    def grab_rgb(self):
        """RGB 프레임 캡처 (기존 dxcam/ScreenCapture 호환).

        BGRA → RGB 변환 (알파 채널 제거 + B/R 스왑).
        """
        bgra = self.grab()
        if bgra is None:
            return None
        # BGRA → RGB: [:,:,[2,1,0]] 슬라이싱
        return bgra[:, :, [2, 1, 0]]

    def reset(self):
        """모니터 변경/해상도 변경 시 재초기화."""
        if not self._closed:
            self._dll.capture_reset()
            self.screen_w = self._dll.capture_get_width()
            self.screen_h = self._dll.capture_get_height()
            self._access_lost_count = 0

    def full_reinit(self):
        """★ 완전 재초기화 — cleanup 후 다시 init."""
        if self._closed:
            return False
        try:
            self._dll.capture_cleanup()
            time.sleep(0.3)
            result = self._dll.capture_init(
                self.monitor_index, self.out_width, self.out_height
            )
            if result == 0:
                self.screen_w = self._dll.capture_get_width()
                self.screen_h = self._dll.capture_get_height()
                self._access_lost_count = 0
                return True
            return False
        except Exception:
            return False

    def close(self):
        """리소스 해제."""
        if not self._closed:
            self._dll.capture_cleanup()
            self._closed = True

    def __del__(self):
        self.close()


class NativeScreenCapture:
    """기존 core/capture.py의 ScreenCapture와 호환되는 래퍼.

    [변경 v2]
    - ★ consecutive-None 감지: capture.py와 동일한 패턴
      _STALE_NONE_THRESHOLD 초과 시 None 반환 (last_frame 대신)
      → mirror.py의 stale detection이 정상 작동
    - ★ recreate 강화: full_reinit 사용

    drop-in 교체 가능:
        # 기존
        from core.capture import ScreenCapture
        # 변경
        from native_capture import NativeScreenCapture as ScreenCapture
    """

    def __init__(self, monitor_index=0, grid_cols=64, grid_rows=32):
        self._cap = None
        self.monitor_index = monitor_index
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        self.screen_w = 0
        self.screen_h = 0
        self.last_frame = None
        self._lock = threading.Lock()

        # ★ 캡처 세션 사망 감지
        self._consecutive_nones = 0
        self._last_recreate_time = 0.0

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화.

        target_fps는 호환성을 위해 받지만,
        네이티브 캡처는 on-demand grab이므로 사용하지 않습니다.
        """
        try:
            self._cap = FastCapture(
                monitor_index=self.monitor_index,
                out_width=self.grid_cols,
                out_height=self.grid_rows,
            )
            self.screen_w = self._cap.screen_w
            self.screen_h = self._cap.screen_h

            # 첫 프레임 대기
            deadline = time.time() + max_wait
            while time.time() < deadline:
                frame = self._cap.grab_rgb()
                if frame is not None:
                    self.last_frame = frame
                    self._consecutive_nones = 0
                    return True
                time.sleep(0.1)

            return True  # 타임아웃이어도 True 반환 (기존 동작 호환)

        except Exception as e:
            print(f"[NativeScreenCapture] 초기화 실패: {e}")
            print("[NativeScreenCapture] dxcam 폴백으로 전환합니다.")
            return self._fallback_start(max_wait, target_fps)

    def _fallback_start(self, max_wait, target_fps):
        """DLL 로드 실패 시 기존 dxcam으로 폴백."""
        from core.capture import ScreenCapture as DxcamCapture
        self._dxcam_fallback = DxcamCapture(self.monitor_index)
        result = self._dxcam_fallback.start(max_wait, target_fps)
        self.screen_w = self._dxcam_fallback.screen_w
        self.screen_h = self._dxcam_fallback.screen_h
        return result

    def grab(self):
        """프레임 반환.

        ★ v2: consecutive-None 감지
        - 정상 범위 내: last_frame 반환 (기존 동작)
        - 임계값 초과: None 반환 → mirror.py stale detection 발동
        """
        with self._lock:
            # dxcam 폴백 모드 — 폴백 capture.py가 자체 감지 로직을 가짐
            if hasattr(self, '_dxcam_fallback'):
                return self._dxcam_fallback.grab()

            if self._cap is None:
                return self.last_frame

            try:
                frame = self._cap.grab_rgb()

                if frame is not None:
                    self.last_frame = frame
                    self._consecutive_nones = 0  # ★ 리셋
                    return frame

                # ── frame is None ──────────────────────────────
                self._consecutive_nones += 1

                if self._consecutive_nones <= _STALE_NONE_THRESHOLD:
                    # 정상 범위: 화면 정지 또는 일시적 지연
                    return self.last_frame
                else:
                    # ★ 임계값 초과: 캡처 세션 사망 추정
                    now = time.monotonic()
                    if now - self._last_recreate_time >= _RECREATE_COOLDOWN:
                        self._last_recreate_time = now
                        self._try_full_reinit()
                        self._consecutive_nones = 0

                    # None 반환 → mirror.py stale detection
                    return None

            except Exception:
                self._consecutive_nones += 1

                if self._consecutive_nones <= _STALE_NONE_THRESHOLD:
                    return self.last_frame
                else:
                    now = time.monotonic()
                    if now - self._last_recreate_time >= _RECREATE_COOLDOWN:
                        self._last_recreate_time = now
                        self._try_full_reinit()
                        self._consecutive_nones = 0
                    return None

    def _try_full_reinit(self):
        """★ FastCapture의 full reinit을 시도."""
        if self._cap is not None:
            try:
                if self._cap.full_reinit():
                    self.screen_w = self._cap.screen_w
                    self.screen_h = self._cap.screen_h
            except Exception:
                pass

    def _recreate(self):
        """모니터 변경 시 재초기화."""
        if hasattr(self, '_dxcam_fallback'):
            self._dxcam_fallback._recreate()
            return

        if self._cap:
            # ★ 단순 reset 대신 full reinit 시도
            if self._cap.full_reinit():
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h
                self._consecutive_nones = 0
            else:
                # full reinit 실패 시 기존 reset 폴백
                self._cap.reset()
                self.screen_w = self._cap.screen_w
                self.screen_h = self._cap.screen_h

    def stop(self):
        """캡처 종료."""
        with self._lock:
            if hasattr(self, '_dxcam_fallback'):
                self._dxcam_fallback.stop()
                return

            if self._cap:
                self._cap.close()
                self._cap = None
