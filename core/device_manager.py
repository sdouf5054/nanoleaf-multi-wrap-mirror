"""DeviceManager — Nanoleaf USB 장치의 공유 연결 관리

[목적]
tab_setup, tab_color, main_window에서 각각 NanoleafDevice를 생성·관리하던
중복 로직을 한 곳으로 통합합니다.

[설계 원칙]
- 단일 인스턴스: 앱 전체에서 하나의 DeviceManager가 장치를 소유
- 소유권 추적: 현재 어떤 모듈이 장치를 사용 중인지 owner로 관리
- 미러링 우선: 미러링 시작 시 다른 소유자의 연결을 자동 해제
- 시그널 기반: 연결 상태 변경을 구독자에게 알림

[사용 패턴]
    manager = DeviceManager(config)
    success = manager.acquire("color_tab")   # 연결 획득
    device = manager.device                   # NanoleafDevice 접근
    manager.release("color_tab")              # 연결 해제
    manager.force_release()                   # 미러링 시작 시 강제 해제
"""

from PyQt5.QtCore import QObject, pyqtSignal
from core.device import NanoleafDevice


class DeviceManager(QObject):
    """앱 전체에서 공유되는 Nanoleaf USB 장치 관리자.

    Signals:
        connection_changed(bool, str):
            (연결 여부, 소유자 이름) — UI 업데이트용
        force_released(str):
            강제 해제 시 이전 소유자 이름 전달 — 해당 탭이 UI를 리셋하도록
    """

    connection_changed = pyqtSignal(bool, str)  # (connected, owner)
    force_released = pyqtSignal(str)            # previous_owner

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._device: NanoleafDevice | None = None
        self._owner: str = ""  # 현재 장치를 소유한 모듈 이름

    @property
    def device(self) -> NanoleafDevice | None:
        """현재 연결된 NanoleafDevice 인스턴스 (없으면 None)"""
        return self._device

    @property
    def owner(self) -> str:
        """현재 장치를 소유한 모듈 이름"""
        return self._owner

    @property
    def is_connected(self) -> bool:
        return self._device is not None and self._device.connected

    def acquire(self, owner: str) -> bool:
        """장치 연결을 획득합니다.

        이미 같은 owner가 소유 중이면 True 반환 (재연결 불필요).
        다른 owner가 소유 중이면 먼저 해제한 뒤 새로 연결합니다.

        Args:
            owner: 소유자 식별 문자열 (예: "setup_tab", "color_tab")

        Returns:
            True: 연결 성공
            False: 연결 실패

        Raises:
            ConnectionError: USB 연결 실패 시 (호출부에서 처리)
        """
        # 이미 같은 owner가 연결 중
        if self.is_connected and self._owner == owner:
            return True

        # 다른 owner가 소유 중이면 해제
        if self.is_connected:
            self._disconnect_device()

        # 새로 연결
        dev_cfg = self.config["device"]
        device = NanoleafDevice(
            int(dev_cfg["vendor_id"], 16),
            int(dev_cfg["product_id"], 16),
            dev_cfg["led_count"],
        )
        device.connect()  # ConnectionError는 호출부로 전파

        self._device = device
        self._owner = owner
        self.connection_changed.emit(True, owner)
        return True

    def release(self, owner: str):
        """지정 owner가 소유 중일 때만 연결을 해제합니다.

        Args:
            owner: 해제를 요청하는 소유자 이름
        """
        if self._owner == owner:
            self._disconnect_device()
            self.connection_changed.emit(False, "")

    def force_release(self):
        """소유자와 무관하게 강제 해제합니다.

        미러링 시작 시 호출하여, setup/color 탭이 장치를 잡고 있어도
        즉시 반환받습니다. 이전 소유자에게 force_released 시그널로 알립니다.
        """
        if not self.is_connected:
            return

        prev_owner = self._owner
        self._disconnect_device()
        self.connection_changed.emit(False, "")

        if prev_owner:
            self.force_released.emit(prev_owner)

    def _disconnect_device(self):
        """내부 — 장치 안전하게 끄고 연결 해제"""
        if self._device is not None:
            if self._device.connected:
                try:
                    self._device.turn_off()
                except Exception:
                    pass
                self._device.disconnect()
            self._device = None
        self._owner = ""

    def cleanup(self):
        """앱 종료 시 호출 — 장치 정리"""
        self._disconnect_device()
