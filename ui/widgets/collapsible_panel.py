"""접기/펼치기 패널 — max-height 애니메이션 컨테이너

토글 상태에 따라 부드럽게 펼쳐지거나 접히는 위젯입니다.

사용법:
    panel = CollapsiblePanel()
    layout = QVBoxLayout()
    layout.addWidget(some_widget)
    panel.set_content_layout(layout)
    panel.set_expanded(True)   # 펼치기
    panel.set_expanded(False)  # 접기
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import QPropertyAnimation, QEasingCurve


class CollapsiblePanel(QWidget):
    """max-height 애니메이션으로 부드럽게 펼치고 접는 패널."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False

        # 내부 컨테이너
        self._container = QWidget()
        self._container.setMaximumHeight(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._container)

        # 애니메이션
        self._anim = QPropertyAnimation(self._container, b"maximumHeight")
        self._anim.setDuration(250)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def set_content_layout(self, layout):
        """내부 레이아웃 설정."""
        self._container.setLayout(layout)

    @property
    def container(self):
        """내부 위젯 — 직접 레이아웃을 설정할 때 사용."""
        return self._container

    def set_expanded(self, expanded, animate=True):
        """펼침/접힘 상태 설정."""
        if self._expanded == expanded:
            return
        self._expanded = expanded

        if expanded:
            # 펼치기: 보이게 한 뒤 높이 측정
            self._container.setVisible(True)
            self._container.setMaximumHeight(0)
            self._container.adjustSize()
            target = self._container.sizeHint().height()
            if target < 10:
                target = 2000  # fallback
        else:
            target = 0
            # 접기: 애니메이션 시작 전에 즉시 숨김 → QPainter 경고 방지
            self._container.setVisible(False)

        if animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(self._container.maximumHeight())
            self._anim.setEndValue(target)
            if expanded:
                self._anim.finished.connect(self._unlock_height)
            self._anim.start()
        else:
            if expanded:
                self._container.setMaximumHeight(16777215)
            else:
                self._container.setMaximumHeight(0)

    def _unlock_height(self):
        """펼침 애니메이션 완료 후 max-height 제한 해제."""
        if self._expanded:
            self._container.setMaximumHeight(16777215)
        try:
            self._anim.finished.disconnect(self._unlock_height)
        except (TypeError, RuntimeError):
            pass

    @property
    def is_expanded(self):
        return self._expanded
