"""화면 캡처 — dxcam + mss GDI fallback (DXGI 미지원 GPU 대응)

StaleDetectionMixin을 사용하여 연속 None 감지 + recreate 쿨다운을 처리합니다.

캡처 백엔드 우선순위:
  1. dxcam (DXGI Desktop Duplication) — 고성능, D3D11+ GPU 필요
  2. mss (GDI) — 저성능이지만 모든 GPU에서 동작

mss fallback이 발동하는 조건:
  - dxcam.create()에서 DXGI_ERROR_UNSUPPORTED 예외 발생
  - GPU가 D3D Feature Level 11.0 미만 또는 WDDM 1.2 미만
"""

import time
import numpy as np

from core.capture_base import StaleDetectionMixin
from core.capture_log import clog

try:
    import dxcam
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


class ScreenCapture(StaleDetectionMixin):
    def __init__(self, monitor_index=0):
        self.camera = None
        self._sct = None
        self.monitor_index = monitor_index
        self.screen_w = 0
        self.screen_h = 0
        self._mode = None           # "dxcam" | "mss" | None
        self._mss_monitor = None
        self._mss_grid_cols = 64
        self._mss_grid_rows = 32
        self._init_stale_detection()
        clog("[capture] __init__: monitor_index=%d, HAS_DXCAM=%s, HAS_MSS=%s",
             monitor_index, HAS_DXCAM, HAS_MSS)

    def set_grid_size(self, cols, rows):
        """mss resize 대상 크기 설정."""
        self._mss_grid_cols = cols
        self._mss_grid_rows = rows

    # ══════════════════════════════════════════════════════════════
    #  초기화
    # ══════════════════════════════════════════════════════════════

    def start(self, max_wait=30, target_fps=60):
        """캡처 초기화 — dxcam 시도 → 실패 시 mss fallback."""
        clog("[capture] start: max_wait=%d, target_fps=%d", max_wait, target_fps)

        if HAS_DXCAM:
            if self._try_dxcam(max_wait=min(max_wait, 10)):
                clog("[capture] dxcam 모드 성공: %dx%d", self.screen_w, self.screen_h)
                return True
            clog("[capture] dxcam 실패 → mss fallback")

        if HAS_MSS:
            if self._try_mss():
                clog("[capture] mss 모드 성공: %dx%d", self.screen_w, self.screen_h)
                return True

        clog("[capture] 모든 캡처 백엔드 실패")
        return False

    def _try_dxcam(self, max_wait=10):
        """dxcam 캡처 시도."""
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

                f = self.camera.grab()
                if f is not None and f.mean() > 0:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    self._mode = "dxcam"
                    clog("[dxcam] 성공: attempt=%d, %dx%d", attempt, self.screen_w, self.screen_h)
                    return True

                if attempt <= 5 or attempt % 10 == 0:
                    clog("[dxcam] attempt=%d: grab=%s", attempt,
                         "None" if f is None else f"mean={f.mean():.1f}")
                time.sleep(0.5)
            except Exception as e:
                clog("[dxcam] attempt=%d 예외: %s", attempt, e)
                self._destroy_dxcam()
                if "지원되지 않습니다" in str(e) or "UNSUPPORTED" in str(e).upper():
                    clog("[dxcam] DXGI 미지원 GPU — dxcam 포기")
                    return False
                time.sleep(1.0)

        self._destroy_dxcam()
        return False

    def _try_mss(self):
        """mss GDI 캡처 시도 — 주 모니터(좌표 원점) 자동 탐색."""
        try:
            self._sct = mss.mss()
            monitors = self._sct.monitors
            clog("[mss] monitors: %s", monitors)

            # 주 모니터 = left=0, top=0 (Windows 좌표 원점)
            target_idx = None
            for i in range(1, len(monitors)):
                if monitors[i]['left'] == 0 and monitors[i]['top'] == 0:
                    target_idx = i
                    break

            if target_idx is None:
                target_idx = self.monitor_index + 1
                if target_idx >= len(monitors):
                    target_idx = 1
                clog("[mss] 주 모니터(origin) 못 찾음 → idx=%d", target_idx)

            self._mss_monitor = monitors[target_idx]
            self.screen_w = self._mss_monitor["width"]
            self.screen_h = self._mss_monitor["height"]
            clog("[mss] target: idx=%d, %dx%d",
                 target_idx, self.screen_w, self.screen_h)

            f = self._grab_mss()
            if f is not None and f.mean() > 0:
                self.last_frame = f
                self._consecutive_nones = 0
                self._mode = "mss"
                clog("[mss] 첫 프레임 OK: shape=%s", f.shape)
                return True

            return False
        except Exception as e:
            clog("[mss] 초기화 예외: %s", e)
            self._sct = None
            return False

    def _grab_mss(self):
        """mss 프레임 캡처 → RGB → grid 크기 resize."""
        if self._sct is None or self._mss_monitor is None:
            return None
        try:
            shot = self._sct.grab(self._mss_monitor)
            frame = np.array(shot, dtype=np.uint8)
            rgb = frame[:, :, [2, 1, 0]]  # BGRA → RGB

            if HAS_CV2:
                h, w = rgb.shape[:2]
                th, tw = self._mss_grid_rows, self._mss_grid_cols
                if h != th or w != tw:
                    rgb = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_LINEAR)

            return rgb
        except Exception as e:
            clog("[mss] grab 예외: %s", e)
            return None

    # ══════════════════════════════════════════════════════════════
    #  StaleDetectionMixin 구현
    # ══════════════════════════════════════════════════════════════

    def _do_grab(self):
        if self._mode == "mss":
            return self._grab_mss()
        if self._mode is None:
            return None
        if self.camera is None:
            self._do_recreate()
            return None
        return self.camera.grab()

    def _do_recreate(self):
        if self._mode is None:
            return

        if self._mode == "mss":
            clog("[mss] recreate")
            try:
                if self._sct:
                    self._sct.close()
            except Exception:
                pass
            try:
                self._sct = mss.mss()
                monitors = self._sct.monitors
                target_idx = None
                for i in range(1, len(monitors)):
                    if monitors[i]['left'] == 0 and monitors[i]['top'] == 0:
                        target_idx = i
                        break
                if target_idx is None:
                    target_idx = self.monitor_index + 1
                    if target_idx >= len(monitors):
                        target_idx = 1
                self._mss_monitor = monitors[target_idx]
                self.screen_w = self._mss_monitor["width"]
                self.screen_h = self._mss_monitor["height"]
            except Exception as e:
                clog("[mss] recreate 예외: %s", e)
            return

        clog("[dxcam] recreate")
        self._destroy_dxcam()
        time.sleep(0.3)
        try:
            self.camera = dxcam.create(
                device_idx=0, output_idx=self.monitor_index, max_buffer_len=2,
            )
            for _ in range(5):
                f = self.camera.grab()
                if f is not None:
                    self.screen_h, self.screen_w = f.shape[:2]
                    self.last_frame = f
                    self._consecutive_nones = 0
                    break
                time.sleep(0.2)
        except Exception as e:
            clog("[dxcam] recreate 예외: %s", e)
            self.camera = None

    # ══════════════════════════════════════════════════════════════
    #  공개 API
    # ══════════════════════════════════════════════════════════════

    def grab(self):
        with self._lock:
            return self._grab_with_stale_detection()

    def _recreate(self):
        clog("[capture] recreate (외부)")
        self._do_recreate()

    # ══════════════════════════════════════════════════════════════
    #  정리
    # ══════════════════════════════════════════════════════════════

    def _destroy_dxcam(self):
        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception:
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

    def stop(self):
        clog("[capture] stop (mode=%s)", self._mode)
        with self._lock:
            if self._mode == "mss":
                try:
                    if self._sct:
                        self._sct.close()
                        self._sct = None
                except Exception:
                    pass
            else:
                self._destroy_dxcam()
