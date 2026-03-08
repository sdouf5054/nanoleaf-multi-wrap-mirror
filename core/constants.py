"""공용 상수 — 하드웨어 에러 튜플, 캡처 stale detection 임계값

여러 모듈에서 반복 사용되는 상수를 한 곳에서 관리합니다.
"""

# ── 하드웨어/통신 예외 튜플 ───────────────────────────────────────
# USB HID, 캡처, 디바이스 통신에서 발생할 수 있는 예외만 포착합니다.
# NameError, TypeError 등 코드 버그는 의도적으로 포착하지 않습니다.
HW_ERRORS = (OSError, IOError, ValueError)

# 연결 시도 시 발생할 수 있는 예외 (HW_ERRORS + ConnectionError)
HW_CONNECT_ERRORS = (OSError, IOError, ValueError, ConnectionError)

# ── 캡처 세션 사망 감지 ──────────────────────────────────────────
# grab()이 연속으로 None을 반환한 횟수가 이 값을 초과하면
# 캡처 세션이 사망한 것으로 판단하고 recreate를 시도합니다.
STALE_NONE_THRESHOLD = 60

# recreate 후 연속 실패 시 재시도 간격을 늘리기 위한 쿨다운 (초)
RECREATE_COOLDOWN = 2.0
