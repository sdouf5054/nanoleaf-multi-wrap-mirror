"""ScreenSampler — 화면 가장자리 N구역 대표색 추출기

[목적]
오디오 비주얼라이저의 색상 소스로 사용.
LED 둘레 위치에 대응하는 화면 가장자리 영역의 평균색을 제공합니다.

[설계 원칙]
- DLL 네이티브 캡처 우선, 실패 시 dxcam 자동 폴백
- thread-safe: update()와 get_*()을 다른 스레드에서 호출 가능
- update()는 호출부가 주기를 제어 (push 방식, 내부 타이머 없음)
- 64×32 다운샘플 프레임 기준으로 구역 추출 — 추가 축소 불필요

[구역 정의]
LED 스트립은 모니터 뒷면 둘레를 감싸므로,
화면 가장자리의 색상이 LED 색상과 대응합니다.

구역 순서는 LED 물리적 둘레 순서와 일치:
  1구역:  화면 전체 평균 (가장자리가 아닌 전체 픽셀)
  2구역:  top → bottom (상단/하단 절반)
  4구역:  top → right → bottom → left
  8구역:  top → top-right → right → bottom-right
          → bottom → bottom-left → left → top-left
  16/32:  둘레를 균등 분할 (top 중앙부터 시계방향)

[사용법]
    sampler = ScreenSampler(n_zones=4)
    sampler.start(monitor_index=0)

    # 메인 루프에서 주기적으로:
    sampler.update()                    # 최신 프레임에서 색상 갱신
    colors = sampler.get_zone_colors()  # (n_zones, 3) float32 RGB
    avg = sampler.get_global_color()    # (3,) float32 RGB

    sampler.stop()
"""

import threading
import numpy as np

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False


# ── 구역 수 선택지 ─────────────────────────────────────────────────
VALID_ZONE_COUNTS = (1, 2, 4, 8, 16, 32)

# ── 다운샘플 목표 크기 (네이티브 DLL과 동일) ─────────────────────────
_GRID_COLS = 64
_GRID_ROWS = 32

# ── 가장자리 두께 비율 ──────────────────────────────────────────────
# 64×32 프레임에서 가장자리로 사용할 픽셀 행/열 수
# 너무 얇으면 노이즈, 너무 두꺼우면 화면 중앙 색이 섞임
_EDGE_ROWS = 6   # 32의 ~19% — 상/하 가장자리 높이
_EDGE_COLS = 10   # 64의 ~16% — 좌/우 가장자리 너비


class ScreenSampler:
    """화면 가장자리에서 N구역 대표색을 추출.

    Attributes:
        n_zones: 구역 수 (4, 8, 16, 32)
        screen_w: 원본 화면 너비 (캡처 모듈 보고값)
        screen_h: 원본 화면 높이
    """

    def __init__(self, n_zones=4, grid_cols=_GRID_COLS, grid_rows=_GRID_ROWS):
        if n_zones not in VALID_ZONE_COUNTS:
            raise ValueError(
                f"n_zones must be one of {VALID_ZONE_COUNTS}, got {n_zones}"
            )

        self.n_zones = n_zones
        self._grid_cols = grid_cols
        self._grid_rows = grid_rows

        self._capture = None
        self._is_native = False
        self._started = False

        # thread-safe 출력
        self._lock = threading.Lock()
        self._zone_colors = np.zeros((n_zones, 3), dtype=np.float32)
        self._global_color = np.zeros(3, dtype=np.float32)
        self._last_frame = None  # ★ per-LED 모드용 raw 프레임
        self._has_data = False

        # 원본 해상도 (캡처 모듈에서 보고)
        self.screen_w = 0
        self.screen_h = 0

    # ── 초기화 / 종료 ──────────────────────────────────────────────

    def start(self, monitor_index=0, max_wait=10):
        """캡처 초기화 — 네이티브 DLL 우선, 실패 시 dxcam 폴백.

        Args:
            monitor_index: 모니터 번호 (0=주 모니터)
            max_wait: 첫 프레임 대기 타임아웃 (초)

        Returns:
            True: 초기화 성공 (첫 프레임 획득 또는 타임아웃)
            False: 캡처 모듈 로드 자체가 실패

        Raises:
            RuntimeError: DLL과 dxcam 모두 사용 불가
        """
        if self._started:
            return True

        # 1차: 네이티브 DLL
        if self._try_native(monitor_index, max_wait):
            self._started = True
            return True

        # 2차: dxcam 폴백
        if self._try_dxcam(monitor_index, max_wait):
            self._started = True
            return True

        raise RuntimeError(
            "화면 캡처 모듈을 초기화할 수 없습니다.\n"
            "native_capture (fast_capture.dll) 또는 dxcam이 필요합니다."
        )

    def _try_native(self, monitor_index, max_wait):
        """네이티브 DLL 캡처 시도."""
        try:
            from native_capture import NativeScreenCapture
            cap = NativeScreenCapture(
                monitor_index=monitor_index,
                grid_cols=self._grid_cols,
                grid_rows=self._grid_rows,
            )
            ok = cap.start(max_wait=max_wait)
            if ok:
                self._capture = cap
                self._is_native = True
                self.screen_w = cap.screen_w
                self.screen_h = cap.screen_h
                return True
            else:
                cap.stop()
                return False
        except (ImportError, RuntimeError, OSError):
            return False

    def _try_dxcam(self, monitor_index, max_wait):
        """dxcam 폴백 캡처 시도."""
        try:
            from core.capture import ScreenCapture
            cap = ScreenCapture(monitor_index)
            ok = cap.start(max_wait=max_wait)
            if ok:
                self._capture = cap
                self._is_native = False
                self.screen_w = cap.screen_w
                self.screen_h = cap.screen_h
                return True
            else:
                cap.stop()
                return False
        except (ImportError, RuntimeError, OSError):
            return False

    def stop(self):
        """캡처 종료 및 리소스 해제."""
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        self._started = False

    # ── 프레임 갱신 ────────────────────────────────────────────────

    def update(self):
        """최신 프레임에서 구역별 색상을 갱신합니다.

        호출부가 주기를 제어합니다 (예: 오디오 루프에서 매 3프레임).
        새 프레임이 없으면 (화면 변화 없음) 이전 값을 유지합니다.

        Returns:
            True: 새 프레임으로 갱신됨
            False: 새 프레임 없음 (이전 값 유지)
        """
        if self._capture is None:
            return False

        frame = self._capture.grab()
        if frame is None:
            return False

        # dxcam 폴백: 풀프레임 → grid 크기로 다운샘플
        if not self._is_native:
            if not _HAS_CV2:
                return False
            h, w = frame.shape[:2]
            if h != self._grid_rows or w != self._grid_cols:
                frame = cv2.resize(
                    frame,
                    (self._grid_cols, self._grid_rows),
                    interpolation=cv2.INTER_LINEAR,
                )

        # frame: (grid_rows, grid_cols, 3) RGB uint8
        h, w = frame.shape[:2]
        if h == 0 or w == 0:
            return False

        # 가장자리 두께 — 프레임 크기에 비례하되 최소 1px
        er = max(1, min(_EDGE_ROWS, h // 3))
        ec = max(1, min(_EDGE_COLS, w // 3))

        zone_colors = self._extract_zones(frame, h, w, er, ec)
        global_color = frame.reshape(-1, 3).mean(axis=0).astype(np.float32)

        with self._lock:
            self._zone_colors = zone_colors
            self._global_color = global_color
            self._last_frame = frame  # ★ per-LED 모드용
            self._has_data = True

        return True

    # ── 구역 추출 ──────────────────────────────────────────────────

    def _extract_zones(self, frame, h, w, er, ec):
        """프레임 가장자리에서 n_zones개 구역의 평균 RGB를 추출.

        Args:
            frame: (h, w, 3) uint8 RGB
            h, w: 프레임 크기
            er: 가장자리 행 수 (상/하)
            ec: 가장자리 열 수 (좌/우)

        Returns:
            (n_zones, 3) float32 — 각 구역의 평균 RGB
        """
        n = self.n_zones

        if n == 1:
            regions = self._regions_1(frame, h, w)
        elif n == 2:
            regions = self._regions_2(frame, h, w)
        elif n == 4:
            regions = self._regions_4(frame, h, w, er, ec)
        elif n == 8:
            regions = self._regions_8(frame, h, w, er, ec)
        else:
            regions = self._regions_perimeter(frame, h, w, er, ec, n)

        colors = np.zeros((n, 3), dtype=np.float32)
        for i, region in enumerate(regions):
            if region.size >= 3:  # 최소 1 픽셀
                colors[i] = region.reshape(-1, 3).mean(axis=0)

        return colors

    def _regions_1(self, frame, h, w):
        """1구역: 화면 전체 평균.

        가장자리가 아닌 프레임 전체 픽셀을 사용합니다.
        화면 중심의 메인 오브젝트 색상도 반영됩니다.
        """
        return [frame]

    def _regions_2(self, frame, h, w):
        """2구역: top, bottom (상단/하단 절반).

        LED 배치 기준:
          zone 0 (top)   : 화면 상단 절반 — 상단 LED에 매핑
          zone 1 (bottom): 화면 하단 절반 — 하단 LED에 매핑
        """
        mid_h = h // 2
        return [
            frame[:mid_h, :],     # top half
            frame[mid_h:, :],     # bottom half
        ]

    def _regions_4(self, frame, h, w, er, ec):
        """4구역: top, right, bottom, left

        각 영역은 모서리 중복 없이 분리:
          top    : 상단 er행, 좌우 ec열 제외한 중앙
          right  : 우측 ec열, 상하 er행 제외한 중앙
          bottom : 하단 er행, 좌우 ec열 제외한 중앙
          left   : 좌측 ec열, 상하 er행 제외한 중앙
        """
        return [
            frame[:er, ec:w - ec],         # top
            frame[er:h - er, w - ec:],     # right
            frame[h - er:, ec:w - ec],     # bottom
            frame[er:h - er, :ec],         # left
        ]

    def _regions_8(self, frame, h, w, er, ec):
        """8구역: 4변 중앙 + 4모서리 (시계방향, top부터)

        top, top-right, right, bottom-right,
        bottom, bottom-left, left, top-left
        """
        return [
            frame[:er, ec:w - ec],         # top center
            frame[:er, w - ec:],           # top-right corner
            frame[er:h - er, w - ec:],     # right center
            frame[h - er:, w - ec:],       # bottom-right corner
            frame[h - er:, ec:w - ec],     # bottom center
            frame[h - er:, :ec],           # bottom-left corner
            frame[er:h - er, :ec],         # left center
            frame[:er, :ec],              # top-left corner
        ]

    def _regions_perimeter(self, frame, h, w, er, ec, n):
        """16/32구역: 둘레 픽셀을 시계방향으로 수집 후 N등분.

        수집 순서 (top 중앙부터 시계방향):
          top 행 (좌→우) → right 열 (상→하)
          → bottom 행 (우→좌) → left 열 (하→상)

        이렇게 하면 LED 둘레 순서와 자연스럽게 대응합니다.
        """
        # 각 변의 가장자리 픽셀을 1D 배열로 펼침
        parts = []

        # top: er행 전체, 좌→우
        parts.append(frame[:er, :].reshape(-1, 3))

        # right: ec열 전체, 상→하 (top과 겹치는 모서리 포함 — 간결함 우선)
        parts.append(frame[:, w - ec:].reshape(-1, 3))

        # bottom: er행 전체, 우→좌 (뒤집기)
        parts.append(frame[h - er:, ::-1].reshape(-1, 3))

        # left: ec열 전체, 하→상 (뒤집기)
        parts.append(frame[::-1, :ec].reshape(-1, 3))

        all_pixels = np.concatenate(parts, axis=0)
        total = len(all_pixels)

        # N등분
        chunk_size = max(1, total // n)
        regions = []
        for i in range(n):
            start = i * chunk_size
            # 마지막 구역은 나머지 픽셀 전부 포함
            end = start + chunk_size if i < n - 1 else total
            regions.append(all_pixels[start:end])

        return regions

    # ── 읽기 (thread-safe) ─────────────────────────────────────────

    def get_zone_colors(self):
        """구역별 평균 RGB. Returns (n_zones, 3) float32, 0~255.

        update() 호출 전이면 모두 0입니다.
        """
        with self._lock:
            return self._zone_colors.copy()

    def get_global_color(self):
        """화면 전체 평균 RGB. Returns (3,) float32, 0~255."""
        with self._lock:
            return self._global_color.copy()

    def get_last_frame(self):
        """최신 다운샘플 프레임. Returns (grid_rows, grid_cols, 3) uint8 or None.

        per-LED 미러링 모드에서 weight matrix를 적용하기 위해 사용.
        """
        with self._lock:
            if self._last_frame is not None:
                return self._last_frame.copy()
            return None

    @property
    def has_data(self):
        """최소 1회 이상 update()로 데이터가 채워졌는지."""
        with self._lock:
            return self._has_data

    def set_n_zones(self, n_zones):
        """구역 수 변경 — 다음 update() 호출 시 반영.

        실행 중에도 안전하게 변경 가능합니다.
        """
        if n_zones not in VALID_ZONE_COUNTS:
            raise ValueError(
                f"n_zones must be one of {VALID_ZONE_COUNTS}, got {n_zones}"
            )
        with self._lock:
            if n_zones != self.n_zones:
                self.n_zones = n_zones
                self._zone_colors = np.zeros((n_zones, 3), dtype=np.float32)
                self._has_data = False

    # ── 재초기화 (모니터 변경 등) ────────────────────────────────────

    def recreate(self):
        """캡처 모듈 재초기화 — 모니터 연결/해제 시 호출."""
        if self._capture is not None:
            try:
                self._capture._recreate()
                self.screen_w = self._capture.screen_w
                self.screen_h = self._capture.screen_h
            except Exception:
                pass
