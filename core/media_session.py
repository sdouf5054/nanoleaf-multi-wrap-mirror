"""미디어 세션 연동 — SMTC 앨범 아트 이미지 프레임 제공 (Windows)

현재 OS에서 재생 중인 미디어(음악, 영상)의 앨범 커버/썸네일을
**이미지 프레임**(numpy 배열)으로 제공한다.

[설계 — v2 리팩터]
기존(v1): 앨범아트 → K-means 색상 추출 → 5색 팔레트 → LED 분배
문제: 기존 미러링 파이프라인(weight_matrix, 구역, 스무딩 등)을 무시
      → LED 색상이 블록 단위로 부자연스럽게 배치됨

변경(v2): 앨범아트 → grid 크기(cols×rows)로 리사이즈 → numpy 프레임 반환
          → 엔진이 화면 캡처 프레임 대신 이 프레임을 기존 파이프라인에 투입
          → weight_matrix, 구역 수, 추출 방식, 스무딩, 색상 효과가 전부 그대로 작동

[흐름]
  기존 미러링: 화면 캡처 → 64×32 grid → weight_matrix → color pipeline → LED
  미디어 연동: 앨범 아트 → cols×rows resize → (동일 파이프라인) → LED

[의존성]
winrt 패키지가 없으면 HAS_MEDIA_SESSION = False로 설정되며,
모든 기능이 graceful하게 비활성화된다.

    pip install winrt-runtime winrt-Windows.Media.Control \
                winrt-Windows.Storage.Streams winrt-Windows.Foundation

[사용법]
    from core.media_session import MediaFrameProvider, HAS_MEDIA_SESSION

    if HAS_MEDIA_SESSION:
        provider = MediaFrameProvider(grid_cols=64, grid_rows=32)
        provider.start()
        # ...
        frame = provider.get_frame()      # (32, 64, 3) uint8 RGB 또는 None
        info = provider.get_media_info()  # {"title":..., "artist":...} 또는 None
        # ...
        provider.stop()
"""

import io
import asyncio
import threading
import time
import numpy as np
from typing import Optional

# ── optional import: winrt (Windows Media Control) ────────────────
HAS_MEDIA_SESSION = False
try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSession,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )
    from winrt.windows.storage.streams import (
        Buffer, DataReader, InputStreamOptions,
    )
    HAS_MEDIA_SESSION = True
except ImportError:
    pass

# PlaybackType은 별도 패키지(winrt-Windows.Media)가 필요할 수 있음 — optional
_MediaPlaybackType = None
try:
    from winrt.windows.media import MediaPlaybackType as _MediaPlaybackType
except ImportError:
    pass

# ── optional import: PIL (썸네일 디코딩) ──────────────────────────
HAS_PIL = False
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    pass

# ── 상수 ──────────────────────────────────────────────────────────
POLL_INTERVAL = 2.5            # 폴링 주기 (초)
THUMBNAIL_READ_SIZE = 1048576  # 썸네일 최대 읽기 크기 (1MB)


# ══════════════════════════════════════════════════════════════════
#  내부 async 헬퍼 (SMTC API 호출)
# ══════════════════════════════════════════════════════════════════

async def _get_current_session():
    """SMTC에서 현재 활성 미디어 세션을 가져온다.

    Returns:
        GlobalSystemMediaTransportControlsSession 또는 None
    """
    if not HAS_MEDIA_SESSION:
        return None
    try:
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = manager.get_current_session()
        return session
    except Exception:
        return None


async def _get_media_properties(session):
    """미디어 세션에서 제목, 아티스트, 썸네일 참조를 추출.

    Returns:
        (title, artist, thumbnail_ref) 또는 (None, None, None)
    """
    if session is None:
        return None, None, None
    try:
        props = await session.try_get_media_properties_async()
        title = props.title or ""
        artist = props.artist or ""
        thumbnail_ref = props.thumbnail  # IRandomAccessStreamReference or None
        return title, artist, thumbnail_ref
    except Exception:
        return None, None, None


def _get_playback_type(session):
    """SMTC 세션의 PlaybackType을 가져온다.

    Returns:
        str — "music", "video", "image", "unknown"
    """
    if session is None or _MediaPlaybackType is None:
        return "unknown"
    try:
        info = session.get_playback_info()
        if info is None or info.playback_type is None:
            return "unknown"
        pt = info.playback_type.value
        # MediaPlaybackType: Unknown=0, Music=1, Video=2, Image=3
        return {1: "music", 2: "video", 3: "image"}.get(pt, "unknown")
    except Exception:
        return "unknown"


async def _read_thumbnail_bytes(thumbnail_ref):
    """IRandomAccessStreamReference → raw bytes.

    Args:
        thumbnail_ref: IRandomAccessStreamReference

    Returns:
        bytes 또는 None
    """
    if thumbnail_ref is None:
        return None
    try:
        stream = await thumbnail_ref.open_read_async()
        size = min(stream.size, THUMBNAIL_READ_SIZE)
        if size == 0:
            return None

        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)

        # Buffer → bytes
        reader = DataReader.from_buffer(buf)
        data = bytearray(reader.unconsumed_buffer_length)
        reader.read_bytes(data)
        return bytes(data)
    except Exception:
        return None


def _thumbnail_bytes_to_frame(raw_bytes, grid_cols, grid_rows):
    """raw 이미지 바이트 → grid 크기의 numpy 프레임.

    화면 캡처 프레임과 동일한 형식으로 반환하여
    기존 미러링 파이프라인에 직접 투입 가능.

    Args:
        raw_bytes: bytes — JPEG/PNG 등 이미지 데이터
        grid_cols: int — 가로 픽셀 수 (기본 64)
        grid_rows: int — 세로 픽셀 수 (기본 32)

    Returns:
        (grid_rows, grid_cols, 3) uint8 RGB 또는 None
    """
    if not HAS_PIL or raw_bytes is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        img = img.resize((grid_cols, grid_rows), Image.LANCZOS)
        return np.array(img, dtype=np.uint8)
    except Exception:
        return None


def _thumbnail_bytes_to_square(raw_bytes, size=128):
    """raw 이미지 바이트 → 정사각형 썸네일 (원본 비율 유지, 패딩 없음).

    UI 표시용. LED 파이프라인과 무관하게 원본 비율을 유지한 채
    size×size 안에 맞춰 리사이즈.

    Args:
        raw_bytes: bytes — JPEG/PNG 등 이미지 데이터
        size: int — 최대 변 길이 (기본 128)

    Returns:
        (h, w, 3) uint8 RGB (h, w ≤ size) 또는 None
    """
    if not HAS_PIL or raw_bytes is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        return np.array(img, dtype=np.uint8)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  MediaFrameProvider
# ══════════════════════════════════════════════════════════════════

class MediaFrameProvider:
    """백그라운드에서 현재 재생 중인 미디어의 앨범 아트를 이미지 프레임으로 제공.

    엔진 루프와 독립적으로 동작 — 곡 변경 시에만 썸네일을 재추출.
    get_frame() / get_media_info()는 스레드 세이프.

    [핵심 설계]
    화면 캡처 프레임과 동일한 형식 (grid_rows, grid_cols, 3) uint8을 반환.
    엔진은 캡처 소스만 교체하면 기존 파이프라인이 그대로 동작:
      weight_matrix → 구역 분할 → 색상 보정 → 스무딩 → GRB 변환

    Attributes:
        grid_cols: int — 프레임 가로 크기 (캡처 grid와 동일)
        grid_rows: int — 프레임 세로 크기 (캡처 grid와 동일)
    """

    def __init__(self, grid_cols=64, grid_rows=32):
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows

        # ── 캐시 (lock 보호) ──
        self._lock = threading.Lock()
        self._cached_frame: Optional[np.ndarray] = None  # (rows, cols, 3) uint8
        self._cached_thumbnail: Optional[np.ndarray] = None  # ★ 원본 비율 썸네일 (UI용)
        self._cached_title: str = ""
        self._cached_artist: str = ""
        self._cached_playback_type: str = "unknown"  # ★ music/video/image/unknown
        self._media_hash: int = 0  # hash(title + artist) — 변경 감지용

        # ── 폴링 스레드 ──
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ══════════════════════════════════════════════════════════════
    #  공개 API
    # ══════════════════════════════════════════════════════════════

    def start(self):
        """백그라운드 폴링 시작."""
        if self._running:
            return
        if not HAS_MEDIA_SESSION:
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="media-frame-poll"
        )
        self._thread.start()

    def stop(self):
        """폴링 중지 + 리소스 정리."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._lock:
            self._cached_frame = None
            self._cached_thumbnail = None
            self._cached_title = ""
            self._cached_artist = ""
            self._cached_playback_type = "unknown"
            self._media_hash = 0

    def get_frame(self) -> Optional[np.ndarray]:
        """최신 앨범 아트 프레임 반환.

        화면 캡처 프레임과 동일한 형식이므로
        기존 파이프라인에 직접 투입 가능.

        Returns:
            (grid_rows, grid_cols, 3) uint8 RGB 또는 None (미디어 없음)
        """
        with self._lock:
            if self._cached_frame is None:
                return None
            return self._cached_frame.copy()

    def get_media_info(self) -> Optional[dict]:
        """현재 곡 정보 반환 (UI 표시용).

        Returns:
            {"title": str, "artist": str, "playback_type": str} 또는 None
        """
        with self._lock:
            if not self._cached_title and not self._cached_artist:
                return None
            return {
                "title": self._cached_title,
                "artist": self._cached_artist,
                "playback_type": self._cached_playback_type,
            }

    def get_thumbnail(self, size: int = 128) -> Optional[np.ndarray]:
        """원본 비율 정사각형 썸네일 반환 (UI 표시용).

        get_frame()은 LED 파이프라인용 (grid_rows × grid_cols)으로
        비율이 왜곡되므로, UI 썸네일에는 이 메서드를 사용.

        Returns:
            (size, size, 3) uint8 RGB 또는 None
        """
        with self._lock:
            if self._cached_thumbnail is None:
                return None
            return self._cached_thumbnail.copy()

    def update_grid_size(self, grid_cols, grid_rows):
        """grid 크기 변경 시 호출 — 캐시된 프레임을 새 크기로 리사이즈.

        디스플레이 변경(해상도, portrait 전환)에 대응.
        """
        if grid_cols == self.grid_cols and grid_rows == self.grid_rows:
            return
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        with self._lock:
            if self._cached_frame is not None and HAS_PIL:
                try:
                    img = Image.fromarray(self._cached_frame)
                    img = img.resize((grid_cols, grid_rows), Image.LANCZOS)
                    self._cached_frame = np.array(img, dtype=np.uint8)
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════
    #  폴링 스레드
    # ══════════════════════════════════════════════════════════════

    def _poll_loop(self):
        """백그라운드 폴링 — 독립 asyncio event loop 사용."""
        while not self._stop_event.is_set():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._poll_once())
                finally:
                    loop.close()
            except Exception:
                pass

            self._stop_event.wait(timeout=POLL_INTERVAL)

    async def _poll_once(self):
        """한 번의 폴링 — 세션 확인 + 변경 감지 + 프레임 추출."""
        session = await _get_current_session()
        if session is None:
            self._clear_cache()
            return

        # ★ 재생 상태 확인 — 정지/닫힘이면 캐시 클리어
        try:
            playback_info = session.get_playback_info()
            if playback_info is not None:
                status = playback_info.playback_status
                # Closed=0, Opened=1, Changing=2, Stopped=3, Playing=4, Paused=5
                if status is not None and status.value in (0, 3):  # Closed or Stopped
                    self._clear_cache()
                    return
        except Exception:
            pass

        title, artist, thumbnail_ref = await _get_media_properties(session)
        if title is None:
            self._clear_cache()
            return

        # ★ PlaybackType 캐시 (매 폴링마다 갱신 — 세션 중간에 바뀔 수 있음)
        playback_type = _get_playback_type(session)
        with self._lock:
            self._cached_playback_type = playback_type

        # ── 변경 감지: hash(title + artist) 비교 ──
        new_hash = hash((title, artist))
        if new_hash == self._media_hash:
            # 동일 곡 — 캐시 유지, 재추출 안 함
            return

        # ── 새 곡 — 썸네일을 grid 크기 프레임으로 변환 ──
        raw_bytes = await _read_thumbnail_bytes(thumbnail_ref)
        frame = _thumbnail_bytes_to_frame(
            raw_bytes, self.grid_cols, self.grid_rows
        )
        # ★ UI용 정사각형 썸네일 (원본 비율 유지)
        thumbnail = _thumbnail_bytes_to_square(raw_bytes, 128)

        with self._lock:
            self._cached_title = title
            self._cached_artist = artist
            self._media_hash = new_hash
            if frame is not None:
                self._cached_frame = frame
            if thumbnail is not None:
                self._cached_thumbnail = thumbnail
            # frame이 None이면 기존 캐시 유지 (이전 곡 프레임)
            # → 썸네일 실패 시 깜빡임 방지

    def _clear_cache(self):
        """미디어 세션 없음 — 캐시 클리어."""
        with self._lock:
            self._cached_title = ""
            self._cached_artist = ""
            self._cached_playback_type = "unknown"
            self._media_hash = 0
            self._cached_frame = None
            self._cached_thumbnail = None