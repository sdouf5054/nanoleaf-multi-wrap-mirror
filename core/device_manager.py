"""DeviceManager — Nanoleaf USB 장치의 공유 연결 관리

PySide6 포팅: pyqtSignal → Signal, QObject → QObject
로직은 원본과 동일 (ADR-034 KEEP).
"""

from PySide6.QtCore import QObject, Signal
from core.device import NanoleafDevice


class DeviceManager(QObject):
    """앱 전체에서 공유되는 Nanoleaf USB 장치 관리자.

    Signals:
        connection_changed(bool, str): (연결 여부, 소유자 이름)
        force_released(str): 강제 해제 시 이전 소유자 이름
    """

    connection_changed = Signal(bool, str)
    force_released = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._device: NanoleafDevice | None = None
        self._owner: str = ""

    @property
    def device(self) -> NanoleafDevice | None:
        return self._device

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def is_connected(self) -> bool:
        return self._device is not None and self._device.connected

    def acquire(self, owner: str) -> bool:
        if self.is_connected and self._owner == owner:
            return True
        if self.is_connected:
            self._disconnect_device()

        dev_cfg = self.config["device"]
        device = NanoleafDevice(
            int(dev_cfg["vendor_id"], 16),
            int(dev_cfg["product_id"], 16),
            dev_cfg["led_count"],
        )
        device.connect()

        self._device = device
        self._owner = owner
        self.connection_changed.emit(True, owner)
        return True

    def release(self, owner: str):
        if self._owner == owner:
            self._disconnect_device()
            self.connection_changed.emit(False, "")

    def force_release(self):
        if not self.is_connected:
            return
        prev_owner = self._owner
        self._disconnect_device()
        self.connection_changed.emit(False, "")
        if prev_owner:
            self.force_released.emit(prev_owner)

    def _disconnect_device(self):
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
        self._disconnect_device()
