"""Phase 3 UI Shell Unit Tests

PySide6 MainWindow/Tray 구조 테스트.
QT_QPA_PLATFORM=offscreen 환경에서 실행.
"""

import copy
import unittest
import sys
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget
from PySide6.QtCore import QObject

_app = QApplication.instance() or QApplication(sys.argv)

from core.config import DEFAULT_CONFIG


# ══════════════════════════════════════════════════════════════════
#  MainWindow 구조
# ══════════════════════════════════════════════════════════════════

class TestMainWindowStructure(unittest.TestCase):
    def setUp(self):
        from ui.main_window import MainWindow
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.win = MainWindow(self.config)

    def tearDown(self):
        self.win.tray.cleanup()
        self.win.tray.hide()
        self.win.close()

    def test_is_qmainwindow(self):
        self.assertIsInstance(self.win, QMainWindow)

    def test_has_tab_widget(self):
        central = self.win.centralWidget()
        self.assertIsInstance(central, QTabWidget)

    def test_has_four_tabs(self):
        self.assertEqual(self.win.tabs.count(), 4)

    def test_tab_names(self):
        names = [self.win.tabs.tabText(i) for i in range(4)]
        self.assertIn("컨트롤", names)
        self.assertIn("색상 보정", names)
        self.assertIn("LED 설정", names)
        self.assertIn("옵션", names)

    def test_window_title(self):
        self.assertEqual(self.win.windowTitle(), "Nanoleaf Screen Mirror")

    def test_has_engine_controller(self):
        from core.engine_controller import EngineController
        self.assertIsInstance(self.win.engine_ctrl, EngineController)

    def test_has_device_manager(self):
        from core.device_manager import DeviceManager
        self.assertIsInstance(self.win.device_manager, DeviceManager)

    def test_has_tray(self):
        from ui.tray import SystemTray
        self.assertIsInstance(self.win.tray, SystemTray)

    def test_has_status_bar(self):
        self.assertIsNotNone(self.win.statusBar())


# ══════════════════════════════════════════════════════════════════
#  SystemTray 구조
# ══════════════════════════════════════════════════════════════════

class TestSystemTray(unittest.TestCase):
    def setUp(self):
        from ui.tray import SystemTray
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.tray = SystemTray(self.config)

    def tearDown(self):
        self.tray.cleanup()
        self.tray.hide()

    def test_has_required_signals(self):
        for sig in ("toggle_requested", "brightness_delta",
                     "brightness_set", "show_window_requested",
                     "quit_requested"):
            self.assertTrue(hasattr(self.tray, sig))

    def test_has_context_menu(self):
        self.assertIsNotNone(self.tray.contextMenu())

    def test_update_status(self):
        self.tray.update_status("테스트 상태")
        self.assertEqual(self.tray.status_action.text(), "테스트 상태")

    def test_set_engine_running(self):
        self.tray.set_engine_running(True)
        self.assertIn("중지", self.tray.onoff_action.text())
        self.tray.set_engine_running(False)
        self.assertIn("시작", self.tray.onoff_action.text())

    def test_cleanup_no_error(self):
        self.tray.cleanup()  # 에러 없이 완료


# ══════════════════════════════════════════════════════════════════
#  ADR-039: 트레이 밝기 시그널 분리
# ══════════════════════════════════════════════════════════════════

class TestTraySignalDecoupling(unittest.TestCase):
    """트레이가 MainWindow 내부 위젯에 직접 접근하지 않는지 확인."""

    def test_tray_has_no_main_window_reference(self):
        """SystemTray가 main_window 속성을 갖지 않음."""
        from ui.tray import SystemTray
        config = copy.deepcopy(DEFAULT_CONFIG)
        tray = SystemTray(config)
        self.assertFalse(hasattr(tray, 'main_window'))
        tray.cleanup()

    def test_brightness_signals_exist(self):
        from ui.tray import SystemTray
        config = copy.deepcopy(DEFAULT_CONFIG)
        tray = SystemTray(config)
        # 시그널 연결 테스트
        received = []
        tray.brightness_delta.connect(lambda v: received.append(('delta', v)))
        tray.brightness_set.connect(lambda v: received.append(('set', v)))
        tray.cleanup()


# ══════════════════════════════════════════════════════════════════
#  ADR-029: DPI 수동 코드 제거 확인
# ══════════════════════════════════════════════════════════════════

class TestDPISimplification(unittest.TestCase):
    """ADR-029: MainWindow에 수동 DPI 조정 코드가 없는지 확인."""

    def test_no_manual_dpi_attributes(self):
        from ui.main_window import MainWindow
        config = copy.deepcopy(DEFAULT_CONFIG)
        win = MainWindow(config)
        # 원본에 있던 수동 DPI 속성들이 없어야 함
        self.assertFalse(hasattr(win, '_base_font_size'))
        self.assertFalse(hasattr(win, '_current_dpi_ratio'))
        self.assertFalse(hasattr(win, '_dpi_connected'))
        self.assertFalse(hasattr(win, '_apply_dpi_adjustment'))
        win.tray.cleanup()
        win.close()


# ══════════════════════════════════════════════════════════════════
#  ADR-030/031: 세션 이벤트 + 디스플레이 변경 구조
# ══════════════════════════════════════════════════════════════════

class TestSessionAndDisplay(unittest.TestCase):
    def setUp(self):
        from ui.main_window import MainWindow
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.win = MainWindow(self.config)

    def tearDown(self):
        self.win.tray.cleanup()
        self.win.tray.hide()
        self.win.close()

    def test_has_display_change_timer(self):
        self.assertIsNotNone(self.win._display_change_timer)
        self.assertTrue(self.win._display_change_timer.isSingleShot())
        self.assertEqual(self.win._display_change_timer.interval(), 1500)

    def test_lock_state_initial(self):
        self.assertFalse(self.win._was_running_before_lock)
        self.assertIsNone(self.win._lock_restart_mode)

    def test_session_filter_exists(self):
        self.assertIsNotNone(self.win._session_filter)


# ══════════════════════════════════════════════════════════════════
#  엔진 제어 통합 (EngineController 경유)
# ══════════════════════════════════════════════════════════════════

class TestMainWindowEngineControl(unittest.TestCase):
    def setUp(self):
        from ui.main_window import MainWindow
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.win = MainWindow(self.config)

    def tearDown(self):
        self.win.tray.cleanup()
        self.win.tray.hide()
        self.win.close()

    def test_start_engine_method_exists(self):
        self.assertTrue(callable(getattr(self.win, 'start_engine', None)))

    def test_stop_engine_method_exists(self):
        self.assertTrue(callable(getattr(self.win, 'stop_engine', None)))

    def test_stop_engine_when_not_running(self):
        """엔진 미실행 시 stop 호출해도 에러 없음."""
        self.win.stop_engine()  # 에러 없이 완료

    def test_engine_controller_initial_state(self):
        self.assertFalse(self.win.engine_ctrl.is_running)


if __name__ == "__main__":
    unittest.main()
