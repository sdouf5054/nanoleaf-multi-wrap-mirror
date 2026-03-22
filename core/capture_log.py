"""캡처 디버그 로거 — capture_debug.log 자동 생성

exe 옆(또는 프로젝트 루트)에 capture_debug.log를 생성하여
캡처 관련 디버그 정보를 기록합니다.

debug_profile 옵션과 무관하게 항상 활성.
문제 해결 후 이 모듈을 제거하거나 import를 주석 처리하면 됩니다.

사용법:
    from core.capture_log import clog
    clog("메시지")
    clog("값: %s", some_value)
"""

import logging
import os
import sys
import time

_logger = logging.getLogger("nanoleaf.capture_debug")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

if not _logger.handlers:
    if getattr(sys, 'frozen', False):
        _log_dir = os.path.dirname(sys.executable)
    else:
        _log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    _log_path = os.path.join(_log_dir, "capture_debug.log")

    try:
        _fh = logging.FileHandler(_log_path, encoding="utf-8", mode="w")
        _fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s",
                                           datefmt="%H:%M:%S"))
        _logger.addHandler(_fh)
    except OSError:
        pass


def clog(msg, *args):
    """캡처 디버그 로그 기록."""
    try:
        _logger.debug(msg, *args)
    except Exception:
        pass
