"""플랫폼 추상화 — Windows API 호출 격리

base_engine.py에 인라인되어 있던 ctypes.windll 호출을 분리합니다.
비-Windows 환경(Linux CI 등)에서는 안전한 폴백을 반환하여
테스트와 import가 실패하지 않습니다.

격리 대상:
  get_monitor_count()      — 연결된 모니터 수
  get_primary_resolution() — 주 모니터 해상도 (w, h)

사용법:
  from core.platform import get_monitor_count, get_primary_resolution

  count = get_monitor_count()          # int, 실패 시 -1
  w, h = get_primary_resolution()      # (int, int), 실패 시 (0, 0)
"""

import sys

_IS_WINDOWS = sys.platform == "win32"


def get_monitor_count():
    """연결된 모니터 수를 반환.

    Windows: GetSystemMetrics(SM_CMONITORS = 80)
    비-Windows: -1 (알 수 없음)

    Returns:
        int — 모니터 수. 실패 시 -1.
    """
    if not _IS_WINDOWS:
        return -1
    try:
        import ctypes
        return ctypes.windll.user32.GetSystemMetrics(80)
    except Exception:
        return -1


def get_primary_resolution():
    """주 모니터 해상도를 반환.

    Windows: GetSystemMetrics(SM_CXSCREEN = 0), GetSystemMetrics(SM_CYSCREEN = 1)
    비-Windows: (0, 0)

    Returns:
        (width, height) — 해상도. 실패 시 (0, 0).
    """
    if not _IS_WINDOWS:
        return (0, 0)
    try:
        import ctypes
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return (w, h)
    except Exception:
        return (0, 0)
