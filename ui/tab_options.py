"""옵션 탭 — 시스템 트레이, 글로벌 핫키, 시작프로그램 설정 (PySide6)

[ADR-032] PowerShell .lnk 대신 winreg Registry Run key 사용 (RETHINK→C)

[Phase 8 변경]
- "미러링" 표현 → 모드 중립적 표현으로 전면 교체
- auto_start_mirror → auto_start_engine 키 이름 변경

[★ 오디오 모드 순환 핫키 추가]
- hotkey_audio_cycle: 편집 행 추가
- 기본값 없음 (사용자가 명시적으로 설정)
- 동작 설명 라벨 추가

[QSS 테마] 인라인 setStyleSheet → palette 참조 + property 기반으로 전환.
  - HotkeyEdit: idle/listening 동적 스타일은 인라인 유지 (palette 참조)
  - 힌트 라벨: setProperty("role", "hint")
"""

import os
import sys
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QCheckBox, QLabel,
    QPushButton, QMessageBox, QHBoxLayout, QLineEdit, QFormLayout, QFrame,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence

from core.config import save_config
from styles.palette import DARK as _PAL

# ── F13~F24 가상 키 매핑 ──
_FKEY_MAP = {getattr(Qt.Key, f"Key_F{n}", None): f"f{n}" for n in range(13, 25)}
_MOD_MAP = {Qt.Key.Key_Control: "ctrl", Qt.Key.Key_Shift: "shift", Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "windows"}
_NAMED_KEYS = {
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter", Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Tab: "tab", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end", Qt.Key.Key_PageUp: "page up", Qt.Key.Key_PageDown: "page down",
}


def _qt_key_to_str(event):
    key = event.key()
    mods = event.modifiers()
    if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta): return None
    parts = []
    if mods & Qt.KeyboardModifier.ControlModifier: parts.append("ctrl")
    if mods & Qt.KeyboardModifier.ShiftModifier: parts.append("shift")
    if mods & Qt.KeyboardModifier.AltModifier: parts.append("alt")
    if mods & Qt.KeyboardModifier.MetaModifier: parts.append("windows")
    if key in _FKEY_MAP: parts.append(_FKEY_MAP[key])
    elif key in _NAMED_KEYS: parts.append(_NAMED_KEYS[key])
    else:
        ch = chr(key).lower() if 32 <= key < 127 else None
        if ch is None: return None
        parts.append(ch)
    return "+".join(parts)


class HotkeyEdit(QLineEdit):
    def __init__(self, placeholder="예: ctrl+shift+o  또는  f13", parent=None):
        super().__init__(parent)
        self._listening = False; self._original_text = ""
        self.setPlaceholderText(placeholder); self.setReadOnly(True); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_idle_style()

    def _set_idle_style(self):
        # ★ palette 참조로 하드코딩 제거
        self.setStyleSheet(
            f"QLineEdit{{background:{_PAL['bg_tertiary']};color:{_PAL['text_primary']};"
            f"border:1px solid {_PAL['border']};border-radius:4px;padding:3px 6px;}}"
            f"QLineEdit:hover{{border-color:{_PAL['border_hover']};}}"
        )

    def _set_listening_style(self):
        self.setStyleSheet(
            f"QLineEdit{{background:{_PAL['hotkey_listen_bg']};color:{_PAL['hotkey_listen_text']};"
            f"border:2px solid {_PAL['hotkey_listen_border']};border-radius:4px;"
            "padding:3px 6px;font-style:italic;}}"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton: self._start_listening()
        super().mousePressEvent(event)

    def _start_listening(self):
        self._original_text = self.text(); self._listening = True; self.setText("키를 누르세요…"); self._set_listening_style(); self.setFocus()

    def keyPressEvent(self, event):
        if not self._listening: return
        if event.key() == Qt.Key.Key_Escape:
            self.setText(self._original_text); self._listening = False; self._set_idle_style(); return
        hotkey = _qt_key_to_str(event)
        if hotkey: self.setText(hotkey); self._listening = False; self._set_idle_style()

    def focusOutEvent(self, event):
        if self._listening: self.setText(self._original_text); self._listening = False; self._set_idle_style()
        super().focusOutEvent(event)


# ── ADR-032: Registry Run key ──

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "NanoleafMirror"


def _is_startup_registered():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _REG_NAME); winreg.CloseKey(key); return True
    except Exception: return False


def _register_startup():
    try:
        import winreg
        if getattr(sys, 'frozen', False):
            cmd = f'"{sys.executable}" --startup'
        else:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            main_py = os.path.join(project_root, "main.py")
            cmd = f'"{pythonw}" "{main_py}" --startup'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _unregister_startup():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, _REG_NAME); winreg.CloseKey(key)
    except Exception: pass


class OptionsTab(QWidget):
    def __init__(self, config, main_window=None, parent=None):
        super().__init__(parent)
        self.config = config; self.main_window = main_window
        if "options" not in self.config: self.config["options"] = {}
        self.opt = self.config["options"]
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self); layout.setSpacing(14)

        # 시스템 트레이
        tray_group = QGroupBox("시스템 트레이"); tray_layout = QVBoxLayout(tray_group)
        self.chk_tray = QCheckBox("시스템 트레이 아이콘 표시"); self.chk_tray.setChecked(self.opt.get("tray_enabled", True))
        self.chk_tray.stateChanged.connect(self._on_tray_changed); tray_layout.addWidget(self.chk_tray)
        self.chk_minimize = QCheckBox("실행 중 창 닫기 시 트레이로 최소화"); self.chk_minimize.setChecked(self.opt.get("minimize_to_tray", True))
        tray_layout.addWidget(self.chk_minimize); layout.addWidget(tray_group)

        # 글로벌 핫키
        hotkey_group = QGroupBox("글로벌 핫키"); hotkey_layout = QVBoxLayout(hotkey_group)
        self.chk_hotkey = QCheckBox("글로벌 핫키 사용"); self.chk_hotkey.setChecked(self.opt.get("hotkey_enabled", True))
        self.chk_hotkey.stateChanged.connect(self._on_hotkey_enabled_changed); hotkey_layout.addWidget(self.chk_hotkey)
        hint = QLabel("버튼을 클릭한 뒤 원하는 키를 누르면 자동으로 입력됩니다.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        hotkey_layout.addWidget(hint)
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine); line.setFrameShadow(QFrame.Shadow.Sunken); hotkey_layout.addWidget(line)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight); form.setSpacing(8)
        self.edit_toggle = HotkeyEdit(); self.edit_toggle.setText(self.opt.get("hotkey_toggle", "ctrl+shift+o")); form.addRow("엔진 On/Off :", self.edit_toggle)
        self.edit_bright_up = HotkeyEdit(); self.edit_bright_up.setText(self.opt.get("hotkey_bright_up", "ctrl+shift+up")); form.addRow("밝기 +10% :", self.edit_bright_up)
        self.edit_bright_down = HotkeyEdit(); self.edit_bright_down.setText(self.opt.get("hotkey_bright_down", "ctrl+shift+down")); form.addRow("밝기 -10% :", self.edit_bright_down)
        self.edit_audio_cycle = HotkeyEdit()
        self.edit_audio_cycle.setText(self.opt.get("hotkey_audio_cycle", ""))
        form.addRow("오디오 모드 순환 :", self.edit_audio_cycle)
        hotkey_layout.addLayout(form)

        audio_cycle_hint = QLabel(
            "OFF → 기본 모드로 ON · ON → 다음 모드 순환 · 한 바퀴 후 OFF\n"
            "Pulse부터 시작하여 전체를 한 바퀴 돕니다"
        )
        audio_cycle_hint.setProperty("role", "hint")
        audio_cycle_hint.setWordWrap(True)
        hotkey_layout.addWidget(audio_cycle_hint)

        btn_reset_hk = QPushButton("↩ 핫키 기본값 복원"); btn_reset_hk.setFixedWidth(160); btn_reset_hk.clicked.connect(self._reset_hotkeys); hotkey_layout.addWidget(btn_reset_hk)
        layout.addWidget(hotkey_group)

        # 잠금 화면
        lock_group = QGroupBox("잠금 화면 동작"); lock_layout = QVBoxLayout(lock_group)
        self.chk_lock_stop = QCheckBox("잠금 화면(Win+L) 시 엔진 자동 중지 및 LED 소등"); self.chk_lock_stop.setChecked(self.opt.get("turn_off_on_lock", True))
        lock_layout.addWidget(self.chk_lock_stop)
        lock_note = QLabel("• 체크 시: 잠금 화면 진입 시 LED가 꺼지고, 잠금 해제 후 자동으로 재시작됩니다.")
        lock_note.setProperty("role", "hint")
        lock_note.setWordWrap(True)
        lock_layout.addWidget(lock_note); layout.addWidget(lock_group)

        # 시작프로그램
        startup_group = QGroupBox("Windows 시작프로그램"); startup_layout = QVBoxLayout(startup_group)
        self.chk_startup = QCheckBox("Windows 시작 시 자동 실행"); self.chk_startup.setChecked(_is_startup_registered())
        startup_layout.addWidget(self.chk_startup)
        self.chk_auto_engine = QCheckBox("실행 시 기본값 설정으로 엔진 자동 시작")
        self.chk_auto_engine.setChecked(self.opt.get("auto_start_engine", self.opt.get("auto_start_mirror", False)))
        startup_layout.addWidget(self.chk_auto_engine)
        startup_note = QLabel(
            "※ 시작프로그램 등록 시 창 없이 트레이로 바로 실행됩니다.\n"
            "※ 엔진 자동 시작 시 컨트롤 탭의 기본값 토글 설정을 사용합니다."
        )
        startup_note.setProperty("role", "hint")
        startup_note.setWordWrap(True)
        startup_layout.addWidget(startup_note); layout.addWidget(startup_group)

        # 저장
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("옵션 저장"); btn_save.clicked.connect(self._save); btn_layout.addWidget(btn_save); btn_layout.addStretch()
        layout.addLayout(btn_layout); layout.addStretch()
        self._on_hotkey_enabled_changed(self.chk_hotkey.checkState())

    def _on_tray_changed(self, state): self.chk_minimize.setEnabled(bool(state))
    def _on_hotkey_enabled_changed(self, state):
        enabled = bool(state)
        self.edit_toggle.setEnabled(enabled); self.edit_bright_up.setEnabled(enabled); self.edit_bright_down.setEnabled(enabled)
        self.edit_audio_cycle.setEnabled(enabled)
    def _reset_hotkeys(self):
        self.edit_toggle.setText("ctrl+shift+o"); self.edit_bright_up.setText("ctrl+shift+up"); self.edit_bright_down.setText("ctrl+shift+down")
        self.edit_audio_cycle.setText("")

    def _save(self):
        self.opt["tray_enabled"] = self.chk_tray.isChecked()
        self.opt["hotkey_enabled"] = self.chk_hotkey.isChecked()
        self.opt["minimize_to_tray"] = self.chk_minimize.isChecked()
        self.opt["auto_start_engine"] = self.chk_auto_engine.isChecked()
        self.opt["turn_off_on_lock"] = self.chk_lock_stop.isChecked()
        self.opt["hotkey_toggle"] = self.edit_toggle.text().strip()
        self.opt["hotkey_bright_up"] = self.edit_bright_up.text().strip()
        self.opt["hotkey_bright_down"] = self.edit_bright_down.text().strip()
        self.opt["hotkey_audio_cycle"] = self.edit_audio_cycle.text().strip()

        self.opt.pop("auto_start_mirror", None)

        save_config(self.config)

        if self.chk_startup.isChecked():
            if not _is_startup_registered():
                if not _register_startup(): QMessageBox.warning(self, "오류", "시작프로그램 등록에 실패했습니다.")
        else:
            if _is_startup_registered(): _unregister_startup()

        if self.main_window and hasattr(self.main_window, "tray"):
            if self.opt["tray_enabled"]: self.main_window.tray.show()
            else: self.main_window.tray.hide()
            if self.opt["hotkey_enabled"]: self.main_window.tray.setup_hotkeys()
            else: self.main_window.tray.cleanup()

        QMessageBox.information(self, "저장", "옵션이 저장되었습니다.")