"""옵션 탭 — 시스템 트레이, 글로벌 핫키, 시작프로그램 설정"""

import os
import sys
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QCheckBox, QLabel,
    QPushButton, QMessageBox, QHBoxLayout, QLineEdit,
    QFormLayout, QFrame
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence

from core.config import save_config


# ── F13~F24 가상 키 Qt→keyboard 문자열 매핑 ────────────────────────────────
_FKEY_MAP = {getattr(Qt, f"Key_F{n}", None): f"f{n}" for n in range(13, 25)}

# 수식어 키 Qt→keyboard 문자열
_MOD_MAP = {
    Qt.Key_Control: "ctrl",
    Qt.Key_Shift:   "shift",
    Qt.Key_Alt:     "alt",
    Qt.Key_Meta:    "windows",
}

# keyboard 라이브러리가 인식하는 일반 키 이름 (Qt Key → 문자열)
_NAMED_KEYS = {
    Qt.Key_Up:     "up",
    Qt.Key_Down:   "down",
    Qt.Key_Left:   "left",
    Qt.Key_Right:  "right",
    Qt.Key_Space:  "space",
    Qt.Key_Return: "enter",
    Qt.Key_Escape: "esc",
    Qt.Key_Tab:    "tab",
    Qt.Key_Delete: "delete",
    Qt.Key_Home:   "home",
    Qt.Key_End:    "end",
    Qt.Key_PageUp: "page up",
    Qt.Key_PageDown: "page down",
}


def _qt_key_to_str(event):
    """QKeyEvent → keyboard 라이브러리 핫키 문자열 변환."""
    key = event.key()
    mods = event.modifiers()

    if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
        return None

    parts = []
    if mods & Qt.ControlModifier:
        parts.append("ctrl")
    if mods & Qt.ShiftModifier:
        parts.append("shift")
    if mods & Qt.AltModifier:
        parts.append("alt")
    if mods & Qt.MetaModifier:
        parts.append("windows")

    if key in _FKEY_MAP:
        parts.append(_FKEY_MAP[key])
    elif key in _NAMED_KEYS:
        parts.append(_NAMED_KEYS[key])
    else:
        ch = chr(key).lower() if 32 <= key < 127 else None
        if ch is None:
            return None
        parts.append(ch)

    return "+".join(parts)


class HotkeyEdit(QLineEdit):
    """클릭 후 키 입력을 받아 핫키 문자열을 자동 설정하는 위젯."""

    def __init__(self, placeholder="예: ctrl+shift+o  또는  f13", parent=None):
        super().__init__(parent)
        self._listening = False
        self._original_text = ""
        self.setPlaceholderText(placeholder)
        self.setReadOnly(True)
        self.setCursor(Qt.PointingHandCursor)
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet(
            "QLineEdit { background: #2b2b2b; color: #ddd; border: 1px solid #555;"
            " border-radius: 4px; padding: 3px 6px; }"
            "QLineEdit:hover { border-color: #888; }"
        )

    def _set_listening_style(self):
        self.setStyleSheet(
            "QLineEdit { background: #1a3a5c; color: #7ec8e3; border: 2px solid #3a8fc7;"
            " border-radius: 4px; padding: 3px 6px; font-style: italic; }"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start_listening()
        super().mousePressEvent(event)

    def _start_listening(self):
        self._original_text = self.text()
        self._listening = True
        self.setText("🎹 키를 누르세요…")
        self._set_listening_style()
        self.setFocus()

    def keyPressEvent(self, event):
        if not self._listening:
            return

        if event.key() == Qt.Key_Escape:
            self.setText(self._original_text)
            self._listening = False
            self._set_idle_style()
            return

        hotkey = _qt_key_to_str(event)
        if hotkey:
            self.setText(hotkey)
            self._listening = False
            self._set_idle_style()

    def focusOutEvent(self, event):
        if self._listening:
            self.setText(self._original_text)
            self._listening = False
            self._set_idle_style()
        super().focusOutEvent(event)


# ── 시작프로그램 헬퍼 ────────────────────────────────────────────────────────

def _startup_shortcut_path():
    startup = os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows",
        "Start Menu", "Programs", "Startup"
    )
    return os.path.join(startup, "NanoleafMirror.lnk")


def _is_startup_registered():
    return os.path.exists(_startup_shortcut_path())


def _register_startup():
    """★ 시작프로그램 바로가기에 --startup 인자를 추가하여
    Windows 시작 시 트레이로 바로 실행되도록 합니다."""
    import subprocess
    shortcut_path = _startup_shortcut_path()
    main_py = os.path.abspath("main.py")
    workdir = os.path.abspath(".")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    # ★ --startup 인자 추가: 시작 시 창 없이 트레이로 바로 실행
    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{shortcut_path}"); '
        f'$sc.TargetPath = "{pythonw}"; '
        f'$sc.Arguments = \'"{main_py}" --startup\'; '
        f'$sc.WorkingDirectory = "{workdir}"; '
        f'$sc.Description = "Nanoleaf Screen Mirror"; '
        f'$sc.Save()'
    )
    try:
        subprocess.run(["powershell", "-Command", ps_script],
                       capture_output=True, timeout=10)
        return os.path.exists(shortcut_path)
    except Exception:
        return False


def _unregister_startup():
    path = _startup_shortcut_path()
    if os.path.exists(path):
        os.remove(path)


# ── 옵션 탭 ─────────────────────────────────────────────────────────────────

class OptionsTab(QWidget):
    def __init__(self, config, main_window=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = main_window

        if "options" not in self.config:
            self.config["options"] = {}
        self.opt = self.config["options"]

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # ── 시스템 트레이 ───────────────────────────────────────────
        tray_group = QGroupBox("시스템 트레이")
        tray_layout = QVBoxLayout(tray_group)

        self.chk_tray = QCheckBox("시스템 트레이 아이콘 표시")
        self.chk_tray.setChecked(self.opt.get("tray_enabled", True))
        self.chk_tray.stateChanged.connect(self._on_tray_changed)
        tray_layout.addWidget(self.chk_tray)

        self.chk_minimize = QCheckBox("미러링 중 창 닫기 시 트레이로 최소화")
        self.chk_minimize.setChecked(self.opt.get("minimize_to_tray", True))
        tray_layout.addWidget(self.chk_minimize)

        layout.addWidget(tray_group)

        # ── 글로벌 핫키 ─────────────────────────────────────────────
        hotkey_group = QGroupBox("글로벌 핫키")
        hotkey_layout = QVBoxLayout(hotkey_group)

        self.chk_hotkey = QCheckBox("글로벌 핫키 사용")
        self.chk_hotkey.setChecked(self.opt.get("hotkey_enabled", True))
        self.chk_hotkey.stateChanged.connect(self._on_hotkey_enabled_changed)
        hotkey_layout.addWidget(self.chk_hotkey)

        hint = QLabel(
            "버튼을 클릭한 뒤 원하는 키를 누르면 자동으로 입력됩니다.\n"
            "단일 키(F13~F24) 또는 조합 키(Ctrl+Shift+O 등) 모두 지원합니다."
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        hotkey_layout.addWidget(hint)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        hotkey_layout.addWidget(line)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)

        self.edit_toggle = HotkeyEdit()
        self.edit_toggle.setText(self.opt.get("hotkey_toggle", "ctrl+shift+o"))
        form.addRow("미러링 On/Off :", self.edit_toggle)

        self.edit_bright_up = HotkeyEdit()
        self.edit_bright_up.setText(self.opt.get("hotkey_bright_up", "ctrl+shift+up"))
        form.addRow("밝기 +10% :", self.edit_bright_up)

        self.edit_bright_down = HotkeyEdit()
        self.edit_bright_down.setText(self.opt.get("hotkey_bright_down", "ctrl+shift+down"))
        form.addRow("밝기 -10% :", self.edit_bright_down)

        hotkey_layout.addLayout(form)

        btn_reset_hk = QPushButton("↩ 핫키 기본값 복원")
        btn_reset_hk.setFixedWidth(160)
        btn_reset_hk.clicked.connect(self._reset_hotkeys)
        hotkey_layout.addWidget(btn_reset_hk)

        layout.addWidget(hotkey_group)

        # ── 잠금 화면 동작 ───────────────────────────────────────────
        lock_group = QGroupBox("잠금 화면 동작")
        lock_layout = QVBoxLayout(lock_group)

        self.chk_lock_stop = QCheckBox("잠금 화면(Win+L) 시 미러링 자동 중지 및 LED 소등")
        self.chk_lock_stop.setChecked(self.opt.get("turn_off_on_lock", True))
        lock_layout.addWidget(self.chk_lock_stop)

        lock_note = QLabel(
            "• 체크 시: 잠금 화면 진입 시 LED가 꺼지고, 잠금 해제 후 자동으로 재시작됩니다.\n"
            "• 해제 시: 잠금 화면에서도 미러링이 계속 실행됩니다."
        )
        lock_note.setStyleSheet("color: #888;")
        lock_note.setWordWrap(True)
        lock_layout.addWidget(lock_note)

        layout.addWidget(lock_group)

        # ── 시작프로그램 ─────────────────────────────────────────────
        startup_group = QGroupBox("Windows 시작프로그램")
        startup_layout = QVBoxLayout(startup_group)

        self.chk_startup = QCheckBox("Windows 시작 시 자동 실행")
        self.chk_startup.setChecked(_is_startup_registered())
        startup_layout.addWidget(self.chk_startup)

        self.chk_auto_mirror = QCheckBox("실행 시 미러링 자동 시작")
        self.chk_auto_mirror.setChecked(self.opt.get("auto_start_mirror", False))
        startup_layout.addWidget(self.chk_auto_mirror)

        startup_note = QLabel(
            "※ 시작프로그램 등록 시 창 없이 트레이로 바로 실행됩니다.\n"
            "   트레이 아이콘을 더블클릭하면 설정 창을 열 수 있습니다."
        )
        startup_note.setStyleSheet("color: #888;")
        startup_note.setWordWrap(True)
        startup_layout.addWidget(startup_note)

        layout.addWidget(startup_group)

        # ── 저장 ────────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("💾 옵션 저장")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

        self._on_hotkey_enabled_changed(self.chk_hotkey.checkState())

    # ── 내부 슬롯 ───────────────────────────────────────────────────

    def _on_tray_changed(self, state):
        self.chk_minimize.setEnabled(bool(state))

    def _on_hotkey_enabled_changed(self, state):
        enabled = bool(state)
        self.edit_toggle.setEnabled(enabled)
        self.edit_bright_up.setEnabled(enabled)
        self.edit_bright_down.setEnabled(enabled)

    def _reset_hotkeys(self):
        self.edit_toggle.setText("ctrl+shift+o")
        self.edit_bright_up.setText("ctrl+shift+up")
        self.edit_bright_down.setText("ctrl+shift+down")

    def _save(self):
        self.opt["tray_enabled"]      = self.chk_tray.isChecked()
        self.opt["hotkey_enabled"]    = self.chk_hotkey.isChecked()
        self.opt["minimize_to_tray"]  = self.chk_minimize.isChecked()
        self.opt["auto_start_mirror"] = self.chk_auto_mirror.isChecked()
        self.opt["turn_off_on_lock"]  = self.chk_lock_stop.isChecked()

        self.opt["hotkey_toggle"]      = self.edit_toggle.text().strip()
        self.opt["hotkey_bright_up"]   = self.edit_bright_up.text().strip()
        self.opt["hotkey_bright_down"] = self.edit_bright_down.text().strip()

        save_config(self.config)

        # 시작프로그램 등록/해제
        if self.chk_startup.isChecked():
            if not _is_startup_registered():
                ok = _register_startup()
                if not ok:
                    QMessageBox.warning(self, "오류", "시작프로그램 등록에 실패했습니다.")
        else:
            if _is_startup_registered():
                _unregister_startup()

        # 트레이 표시/숨기기
        if self.main_window and hasattr(self.main_window, "tray"):
            if self.opt["tray_enabled"]:
                self.main_window.tray.show()
            else:
                self.main_window.tray.hide()

        # 핫키 재등록
        if self.main_window and hasattr(self.main_window, "tray"):
            tray = self.main_window.tray
            if self.opt["hotkey_enabled"]:
                tray._setup_hotkey()
            else:
                tray.cleanup()

        QMessageBox.information(self, "저장", "옵션이 저장되었습니다.")
