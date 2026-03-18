"""스크롤 방지 이벤트 필터 — QComboBox/QSpinBox/QSlider 대상

폼 안의 위젯에서 의도치 않은 휠 스크롤을 방지합니다.
스크롤 영역 안에 슬라이더/콤보가 있을 때 유용합니다.

사용법:
    filter = NoScrollFilter(parent)
    for widget in container.findChildren(QWidget):
        if isinstance(widget, NoScrollFilter.FILTERED_TYPES):
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            widget.installEventFilter(filter)
"""

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QComboBox, QSpinBox, QDoubleSpinBox, QSlider


class NoScrollFilter(QObject):
    """QComboBox, QSpinBox, QSlider 등에서 마우스 휠 스크롤을 무시하는 필터.

    포커스가 없는 위젯의 휠 이벤트를 무시하여 스크롤 영역이 대신 스크롤됩니다.
    """

    FILTERED_TYPES = (QComboBox, QSpinBox, QDoubleSpinBox, QSlider)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, self.FILTERED_TYPES):
            event.ignore()
            return True
        return False
