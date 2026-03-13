"""Phase 2 Engine Layer Unit Tests

PySide6 QThread 기반 엔진 구조 테스트.
실제 USB/캡처/오디오 디바이스 없이 구조와 인터페이스를 검증합니다.
"""

import copy
import unittest
import sys

import numpy as np

# PySide6 QApplication은 테스트에서 한 번만 생성
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread

_app = QApplication.instance() or QApplication(sys.argv)


# ══════════════════════════════════════════════════════════════════
#  engine_params (ADR-003)
# ══════════════════════════════════════════════════════════════════

class TestEngineParams(unittest.TestCase):
    def test_mirror_params_frozen(self):
        from core.engine_params import MirrorParams
        mp = MirrorParams(brightness=0.8, smoothing_factor=0.3)
        self.assertEqual(mp.brightness, 0.8)
        with self.assertRaises(AttributeError):
            mp.brightness = 0.5  # frozen=True

    def test_audio_params_frozen(self):
        from core.engine_params import AudioParams
        ap = AudioParams(brightness=0.6, rainbow=False, base_color=(0, 255, 0))
        self.assertEqual(ap.brightness, 0.6)
        self.assertFalse(ap.rainbow)
        with self.assertRaises(AttributeError):
            ap.rainbow = True

    def test_layout_params_mutable(self):
        from core.engine_params import LayoutParams
        lp = LayoutParams()
        lp.dirty = True
        self.assertTrue(lp.dirty)
        lp.decay_radius = 0.5
        self.assertEqual(lp.decay_radius, 0.5)

    def test_mirror_params_defaults(self):
        from core.engine_params import MirrorParams
        mp = MirrorParams()
        self.assertEqual(mp.brightness, 1.0)
        self.assertTrue(mp.smoothing_enabled)
        self.assertEqual(mp.mirror_n_zones, -1)

    def test_audio_params_defaults(self):
        from core.engine_params import AudioParams
        ap = AudioParams()
        self.assertEqual(ap.audio_mode, "pulse")
        self.assertTrue(ap.rainbow)
        self.assertEqual(ap.zone_weights, (33, 33, 34))


# ══════════════════════════════════════════════════════════════════
#  base_engine 구조
# ══════════════════════════════════════════════════════════════════

class TestBaseEngineStructure(unittest.TestCase):
    def test_base_engine_is_qthread(self):
        from core.base_engine import BaseEngine
        self.assertTrue(issubclass(BaseEngine, QThread))

    def test_base_engine_has_signals(self):
        from core.base_engine import BaseEngine
        # Signal 속성이 존재하는지 확인
        for sig in ("fps_updated", "error", "status_changed",
                     "energy_updated", "spectrum_updated",
                     "screen_colors_updated"):
            self.assertTrue(hasattr(BaseEngine, sig))

    def test_base_engine_abstract_methods(self):
        """서브클래스가 필수 메서드를 구현하지 않으면 NotImplementedError."""
        from core.base_engine import BaseEngine
        from core.config import DEFAULT_CONFIG

        class IncompleteEngine(BaseEngine):
            mode = "test"

        engine = IncompleteEngine(DEFAULT_CONFIG)
        with self.assertRaises(NotImplementedError):
            engine._init_mode_resources()
        with self.assertRaises(NotImplementedError):
            engine._run_loop()


# ══════════════════════════════════════════════════════════════════
#  ADR-003: 파라미터 스냅샷 교체
# ══════════════════════════════════════════════════════════════════

class TestParamSwap(unittest.TestCase):
    def test_swap_mirror_params(self):
        from core.base_engine import BaseEngine
        from core.engine_params import MirrorParams
        from core.config import DEFAULT_CONFIG

        class DummyEngine(BaseEngine):
            mode = "test"
            def _init_mode_resources(self): pass
            def _run_loop(self): pass

        engine = DummyEngine(DEFAULT_CONFIG)
        self.assertEqual(engine._current_mirror_params.brightness, 1.0)

        new_params = MirrorParams(brightness=0.42)
        engine.update_mirror_params(new_params)
        # pending에 있지만 아직 current에 반영 안 됨
        self.assertEqual(engine._current_mirror_params.brightness, 1.0)

        engine._swap_params()
        self.assertAlmostEqual(engine._current_mirror_params.brightness, 0.42)
        # pending은 소비됨
        self.assertIsNone(engine._pending_mirror_params)

    def test_swap_audio_params(self):
        from core.base_engine import BaseEngine
        from core.engine_params import AudioParams
        from core.config import DEFAULT_CONFIG

        class DummyEngine(BaseEngine):
            mode = "test"
            def _init_mode_resources(self): pass
            def _run_loop(self): pass

        engine = DummyEngine(DEFAULT_CONFIG)
        new_params = AudioParams(brightness=0.7, rainbow=False,
                                 base_color=(0, 255, 0))
        engine.update_audio_params(new_params)
        engine._swap_params()
        self.assertAlmostEqual(engine._current_audio_params.brightness, 0.7)
        self.assertFalse(engine._current_audio_params.rainbow)

    def test_swap_without_pending_is_noop(self):
        from core.base_engine import BaseEngine
        from core.config import DEFAULT_CONFIG

        class DummyEngine(BaseEngine):
            mode = "test"
            def _init_mode_resources(self): pass
            def _run_loop(self): pass

        engine = DummyEngine(DEFAULT_CONFIG)
        original = engine._current_mirror_params
        engine._swap_params()
        self.assertIs(engine._current_mirror_params, original)


# ══════════════════════════════════════════════════════════════════
#  ADR-004: Layout dirty flag
# ══════════════════════════════════════════════════════════════════

class TestLayoutDirty(unittest.TestCase):
    def test_update_layout_sets_dirty(self):
        from core.base_engine import BaseEngine
        from core.config import DEFAULT_CONFIG

        class DummyEngine(BaseEngine):
            mode = "test"
            def _init_mode_resources(self): pass
            def _run_loop(self): pass

        engine = DummyEngine(DEFAULT_CONFIG)
        self.assertFalse(engine._layout_params.dirty)

        engine.update_layout_params(decay_radius=0.5)
        self.assertTrue(engine._layout_params.dirty)
        self.assertEqual(engine._layout_params.decay_radius, 0.5)


# ══════════════════════════════════════════════════════════════════
#  엔진 서브클래스 구조
# ══════════════════════════════════════════════════════════════════

class TestEngineSubclasses(unittest.TestCase):
    def test_mirror_engine_mode(self):
        from core.engine_mirror import MirrorEngine
        from core.base_engine import BaseEngine
        self.assertTrue(issubclass(MirrorEngine, BaseEngine))
        self.assertEqual(MirrorEngine.mode, "mirror")

    def test_audio_engine_mode(self):
        from core.engine_audio_mode import AudioModeEngine
        from core.base_engine import BaseEngine
        self.assertTrue(issubclass(AudioModeEngine, BaseEngine))
        self.assertEqual(AudioModeEngine.mode, "audio")

    def test_hybrid_engine_mode(self):
        from core.engine_hybrid_mode import HybridEngine
        from core.base_engine import BaseEngine
        self.assertTrue(issubclass(HybridEngine, BaseEngine))
        self.assertEqual(HybridEngine.mode, "hybrid")

    def test_all_subclasses_have_required_methods(self):
        from core.engine_mirror import MirrorEngine
        from core.engine_audio_mode import AudioModeEngine
        from core.engine_hybrid_mode import HybridEngine
        for cls in (MirrorEngine, AudioModeEngine, HybridEngine):
            self.assertTrue(hasattr(cls, '_init_mode_resources'))
            self.assertTrue(hasattr(cls, '_run_loop'))
            self.assertTrue(hasattr(cls, '_cleanup_mode'))


# ══════════════════════════════════════════════════════════════════
#  device_manager (PySide6 포팅)
# ══════════════════════════════════════════════════════════════════

class TestDeviceManager(unittest.TestCase):
    def test_device_manager_is_qobject(self):
        from core.device_manager import DeviceManager
        from PySide6.QtCore import QObject
        self.assertTrue(issubclass(DeviceManager, QObject))

    def test_device_manager_has_signals(self):
        from core.device_manager import DeviceManager
        self.assertTrue(hasattr(DeviceManager, 'connection_changed'))
        self.assertTrue(hasattr(DeviceManager, 'force_released'))

    def test_initial_state(self):
        from core.device_manager import DeviceManager
        from core.config import DEFAULT_CONFIG
        dm = DeviceManager(DEFAULT_CONFIG)
        self.assertFalse(dm.is_connected)
        self.assertEqual(dm.owner, "")
        self.assertIsNone(dm.device)


# ══════════════════════════════════════════════════════════════════
#  engine_controller (ADR-019)
# ══════════════════════════════════════════════════════════════════

class TestEngineController(unittest.TestCase):
    def test_controller_has_proxy_signals(self):
        from core.engine_controller import EngineController
        for sig in ("fps_updated", "status_changed", "error",
                     "energy_updated", "spectrum_updated",
                     "screen_colors_updated",
                     "engine_started", "engine_stopped", "running_changed"):
            self.assertTrue(hasattr(EngineController, sig))

    def test_initial_state(self):
        from core.engine_controller import EngineController
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        self.assertFalse(ctrl.is_running)
        self.assertEqual(ctrl.current_mode, "mirror")

    def test_engine_class_mapping(self):
        from core.engine_controller import _ENGINE_CLASSES
        from core.engine_mirror import MirrorEngine
        from core.engine_audio_mode import AudioModeEngine
        from core.engine_hybrid_mode import HybridEngine
        self.assertIs(_ENGINE_CLASSES["mirror"], MirrorEngine)
        self.assertIs(_ENGINE_CLASSES["audio"], AudioModeEngine)
        self.assertIs(_ENGINE_CLASSES["hybrid"], HybridEngine)

    def test_set_audio_device_index(self):
        from core.engine_controller import EngineController
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        ctrl.set_audio_device_index(3)
        self.assertEqual(ctrl._audio_device_index, 3)

    def test_stop_engine_sync_when_no_engine(self):
        """엔진 없을 때 stop_engine_sync는 에러 없이 완료."""
        from core.engine_controller import EngineController
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        ctrl.stop_engine_sync()  # 에러 없이 완료되어야 함
        self.assertFalse(ctrl.is_running)

    def test_cleanup(self):
        from core.engine_controller import EngineController
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        ctrl.cleanup()
        self.assertIsNone(ctrl._engine)

    def test_switch_mode_updates_current_mode(self):
        from core.engine_controller import EngineController
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        ctrl.switch_mode("audio")
        self.assertEqual(ctrl.current_mode, "audio")

    def test_param_passthrough_without_engine(self):
        """엔진 없을 때 파라미터 전달은 에러 없이 무시."""
        from core.engine_controller import EngineController
        from core.engine_params import MirrorParams, AudioParams
        from core.config import DEFAULT_CONFIG
        ctrl = EngineController(DEFAULT_CONFIG)
        ctrl.set_mirror_params(MirrorParams(brightness=0.5))
        ctrl.set_audio_params(AudioParams(brightness=0.3))
        ctrl.update_layout_params(decay_radius=0.4)
        # 에러 없이 완료


# ══════════════════════════════════════════════════════════════════
#  ADR-005: 모니터 워처 구조
# ══════════════════════════════════════════════════════════════════

class TestMonitorWatcher(unittest.TestCase):
    def test_monitor_watcher_stop_event(self):
        from core.base_engine import BaseEngine
        from core.config import DEFAULT_CONFIG

        class DummyEngine(BaseEngine):
            mode = "test"
            def _init_mode_resources(self): pass
            def _run_loop(self): pass

        engine = DummyEngine(DEFAULT_CONFIG)
        # stop 이벤트가 있고 초기 상태 확인
        self.assertFalse(engine._monitor_watcher_stop.is_set())
        engine.stop_engine()
        self.assertTrue(engine._stop_event.is_set())
        self.assertTrue(engine._monitor_watcher_stop.is_set())


if __name__ == "__main__":
    unittest.main()
