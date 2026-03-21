"""아이콘 없는 커스텀 다이얼로그 유틸리티.

QMessageBox/QInputDialog 대신 QDialog를 직접 구성하여
아이콘 공간, 여백, 창 폭을 완전히 제어합니다.

사용법:
    from ui.dialogs import msg_info, msg_warning, msg_question, input_text
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit,
)
from PySide6.QtCore import Qt


def _make_dialog(parent, title, text, buttons, min_width=200):
    """공통 다이얼로그 생성.

    Args:
        buttons: [("label", role), ...] — role: "accept" 또는 "reject"
        min_width: 최소 폭 (콘텐츠가 더 넓으면 자동 확장)
    Returns:
        (QDialog, {label: QPushButton}) 튜플
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)

    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)
    lay.setContentsMargins(16, 12, 16, 10)

    lbl = QLabel(text)
    lbl.setWordWrap(False)
    lay.addWidget(lbl)

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    btn_map = {}
    for label, role in buttons:
        btn = QPushButton(label)
        btn.setMinimumWidth(72)
        if role == "accept":
            btn.setDefault(True)
            btn.clicked.connect(dlg.accept)
        else:
            btn.clicked.connect(dlg.reject)
        btn_row.addWidget(btn)
        btn_map[label] = btn
    lay.addLayout(btn_row)

    # 콘텐츠에 맞게 크기 산정 후, 최소 폭 보장
    dlg.adjustSize()
    if dlg.width() < min_width:
        dlg.resize(min_width, dlg.height())

    return dlg, btn_map


def msg_info(parent, title, text):
    """정보 메시지 팝업."""
    dlg, _ = _make_dialog(parent, title, text, [("OK", "accept")])
    dlg.exec()


def msg_warning(parent, title, text):
    """경고 메시지 팝업."""
    dlg, _ = _make_dialog(parent, title, text, [("OK", "accept")])
    dlg.exec()


def msg_question(parent, title, text):
    """Yes/No 질문 팝업. True=Yes."""
    dlg, _ = _make_dialog(
        parent, title, text,
        [("Yes", "accept"), ("No", "reject")],
    )
    return dlg.exec() == QDialog.DialogCode.Accepted


def input_text(parent, title, label, min_width=400):
    """텍스트 입력 팝업."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(min_width)

    lay = QVBoxLayout(dlg)
    lay.setSpacing(10)
    lay.setContentsMargins(20, 16, 20, 14)

    lbl = QLabel(label)
    lay.addWidget(lbl)

    edit = QLineEdit()
    lay.addWidget(edit)

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    btn_ok = QPushButton("OK")
    btn_ok.setDefault(True)
    btn_ok.setMinimumWidth(72)
    btn_ok.clicked.connect(dlg.accept)
    btn_row.addWidget(btn_ok)
    btn_cancel = QPushButton("Cancel")
    btn_cancel.setMinimumWidth(72)
    btn_cancel.clicked.connect(dlg.reject)
    btn_row.addWidget(btn_cancel)
    lay.addLayout(btn_row)

    edit.setFocus()

    if dlg.exec() == QDialog.DialogCode.Accepted:
        return edit.text(), True
    return "", False