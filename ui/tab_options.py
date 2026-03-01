"""옵션 탭 — 시스템 트레이, 글로벌 핫키, 시작프로그램 설정"""

import os
import sys
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QCheckBox, QLabel,
    QPushButton, QMessageBox, QHBoxLayout
)
from PyQt5.QtCore import Qt

from core.config import save_config


def _startup_shortcut_path():
    """Windows 시작프로그램 폴더의 바로가기 경로"""
    startup = os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows",
        "Start Menu", "Programs", "Startup"
    )
    return os.path.join(startup, "NanoleafMirror.lnk")


def _is_startup_registered():
    return os.path.exists(_startup_shortcut_path())


def _register_startup():
    """Windows 시작프로그램에 바로가기 생성 (pythonw, PowerShell 사용)"""
    import shutil
    import subprocess

    shortcut_path = _startup_shortcut_path()
    main_py = os.path.abspath("main.py")
    workdir = os.path.abspath(".")

    # python.exe 사용 (pythonw는 dxcam 호환 문제)
    # 콘솔은 main.py에서 ctypes로 숨김
    pythonw = shutil.which("python")
    if not pythonw:
        pythonw = sys.executable

    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{shortcut_path}"); '
        f'$sc.TargetPath = "{pythonw}"; '
        f'$sc.Arguments = \'"{main_py}"\'; '
        f'$sc.WorkingDirectory = "{workdir}"; '
        f'$sc.Description = "Nanoleaf Screen Mirror"; '
        f'$sc.Save()'
    )

    try:
        subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, timeout=10
        )
        return os.path.exists(shortcut_path)
    except Exception:
        return False


def _unregister_startup():
    path = _startup_shortcut_path()
    if os.path.exists(path):
        os.remove(path)


class OptionsTab(QWidget):
    def __init__(self, config, main_window=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = main_window

        # config에 options 섹션 없으면 생성
        if "options" not in self.config:
            self.config["options"] = {
                "tray_enabled": True,
                "hotkey_enabled": True,
                "minimize_to_tray": True,
            }
        self.opt = self.config["options"]

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # === 시스템 트레이 ===
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

        # === 글로벌 핫키 ===
        hotkey_group = QGroupBox("글로벌 핫키")
        hotkey_layout = QVBoxLayout(hotkey_group)

        self.chk_hotkey = QCheckBox("글로벌 핫키 사용")
        self.chk_hotkey.setChecked(self.opt.get("hotkey_enabled", True))
        hotkey_layout.addWidget(self.chk_hotkey)

        hotkey_desc = QLabel(
            "• Ctrl+Shift+O — 미러링 On/Off\n"
            "• Ctrl+Shift+↑ — 밝기 +10%\n"
            "• Ctrl+Shift+↓ — 밝기 -10%"
        )
        hotkey_layout.addWidget(hotkey_desc)

        hotkey_note = QLabel("※ keyboard 패키지 필요 (pip install keyboard)")
        hotkey_note.setStyleSheet("color: #888;")
        hotkey_layout.addWidget(hotkey_note)

        layout.addWidget(hotkey_group)

        # === 시작프로그램 ===
        startup_group = QGroupBox("Windows 시작프로그램")
        startup_layout = QVBoxLayout(startup_group)

        self.chk_startup = QCheckBox("Windows 시작 시 자동 실행")
        self.chk_startup.setChecked(_is_startup_registered())
        startup_layout.addWidget(self.chk_startup)

        startup_note = QLabel("※ 시작프로그램 폴더에 바로가기를 생성합니다.")
        startup_note.setStyleSheet("color: #888;")
        startup_layout.addWidget(startup_note)

        layout.addWidget(startup_group)

        # === 저장 ===
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("💾 옵션 저장")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    def _on_tray_changed(self, state):
        """트레이 비활성화 시 최소화 옵션도 비활성화"""
        self.chk_minimize.setEnabled(bool(state))

    def _save(self):
        # config 반영
        self.opt["tray_enabled"] = self.chk_tray.isChecked()
        self.opt["hotkey_enabled"] = self.chk_hotkey.isChecked()
        self.opt["minimize_to_tray"] = self.chk_minimize.isChecked()
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

        # 핫키 활성/비활성
        if self.main_window and hasattr(self.main_window, "tray"):
            tray = self.main_window.tray
            if self.opt["hotkey_enabled"]:
                tray._setup_hotkey()
            else:
                tray.cleanup()

        QMessageBox.information(self, "저장", "옵션이 저장되었습니다.")
