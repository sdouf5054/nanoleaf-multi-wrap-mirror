"""디바이스 소유자 Mixin — tab_color / tab_setup 공통 USB 연결 패턴

두 탭에서 거의 동일하게 반복되던 패턴을 통합:
  - _toggle_connection(): 연결/해제 토글
  - _set_connected_ui() / _set_disconnected_ui(): UI 상태 갱신
  - _on_force_released(): DeviceManager에 의한 강제 해제 처리
  - force_disconnect(): 외부에서 연결 해제 요청
  - cleanup(): 종료 시 소유권 해제

사용법:
    class MyTab(DeviceOwnerMixin, QWidget):
        _DEVICE_OWNER = "my_tab"

        def __init__(self, config, device_manager):
            QWidget.__init__(self)
            self._init_device_owner(device_manager)

    이후 self._toggle_connection() 등 사용.

서브클래스 요구사항:
    - _DEVICE_OWNER: str — 소유자 식별 문자열
    - self.btn_connect: QPushButton — 연결 버튼
    - self.conn_label: QLabel — 상태 라벨
    - self.request_mirror_stop: Signal — 엔진 중지 요청 시그널
"""

from PySide6.QtWidgets import QMessageBox


class DeviceOwnerMixin:
    """USB 디바이스 소유권 관리 Mixin.

    서브클래스는 _DEVICE_OWNER, btn_connect, conn_label,
    request_mirror_stop을 제공해야 합니다.
    """

    _DEVICE_OWNER: str = ""

    def _init_device_owner(self, device_manager):
        """Mixin 초기화 — __init__에서 호출."""
        self.dm = device_manager
        if self.dm:
            self.dm.force_released.connect(self._on_force_released)

    def _set_connected_ui(self):
        """연결 상태 UI 갱신."""
        self.conn_label.setText("연결됨")
        self.conn_label.setStyleSheet("color: #2d8c46;")
        self.btn_connect.setText("연결 해제")

    def _set_disconnected_ui(self):
        """비연결 상태 UI 갱신."""
        self.conn_label.setText("연결 안 됨")
        self.conn_label.setStyleSheet("color: #c0392b;")
        self.btn_connect.setText("LED 연결")

    def _on_force_released(self, prev_owner):
        """DeviceManager에 의한 강제 해제 처리."""
        if prev_owner == self._DEVICE_OWNER:
            self._on_device_force_released()
            self._set_disconnected_ui()

    def _on_device_force_released(self):
        """서브클래스에서 오버라이드 가능 — 강제 해제 시 추가 처리.

        예: SetupTab에서 스캔 중지.
        """
        pass

    def force_disconnect(self):
        """외부에서 연결 해제 요청."""
        if self.dm:
            self.dm.release(self._DEVICE_OWNER)
            self._set_disconnected_ui()

    def _toggle_connection(self):
        """연결/해제 토글."""
        if not self.dm:
            return
        if self.dm.is_connected and self.dm.owner == self._DEVICE_OWNER:
            self.dm.release(self._DEVICE_OWNER)
            self._set_disconnected_ui()
        else:
            self.request_mirror_stop.emit()
            try:
                self.dm.acquire(self._DEVICE_OWNER)
                self._set_connected_ui()
            except Exception as e:
                QMessageBox.warning(self, "연결 실패", str(e))

    def _device_cleanup(self):
        """종료 시 소유권 해제."""
        if self.dm:
            self.dm.release(self._DEVICE_OWNER)
