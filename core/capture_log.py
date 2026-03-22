"""캡처 디버그 로거 — debug_profile 옵션 연동

config.json의 options.debug_profile = true 일 때만 활성화.
비활성 상태에서 clog()는 no-op (오버헤드 없음).

사용법:
    from core.capture_log import clog, enable_capture_log

    # 엔진 초기화 시 (debug_profile 확인 후):
    enable_capture_log()

    # 이후 어디서든:
    clog("메시지: %s", value)
"""

import logging
import os
import sys

_enabled = False
_logger = None


def enable_capture_log():
    """캡처 디버그 로깅 활성화 — capture_debug.log 파일 생성."""
    global _enabled, _logger

    if _enabled:
        return

    _logger = logging.getLogger("nanoleaf.capture_debug")
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False

    if not _logger.handlers:
        if getattr(sys, 'frozen', False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        log_path = os.path.join(log_dir, "capture_debug.log")

        try:
            fh = logging.FileHandler(log_path, encoding="utf-8", mode="w")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"
            ))
            _logger.addHandler(fh)
            _enabled = True
        except OSError:
            pass


def clog(msg, *args):
    """캡처 디버그 로그 기록. 비활성 시 no-op."""
    if _enabled and _logger is not None:
        try:
            _logger.debug(msg, *args)
        except Exception:
            pass
