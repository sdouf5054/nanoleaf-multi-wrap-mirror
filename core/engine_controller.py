"""EngineController — UI↔엔진 중재자 (Phase 5: EngineParams 지원)

[Phase 5 변경]
- set_params(EngineParams) 신규 메서드 → engine.update_params() 직접 전달
- set_mirror_params / set_audio_params: 호환 유지 (내부에서 변환)
- start_engine: initial_params (EngineParams) 지원
"""

import copy
from PySide6.QtCore import QObject, Signal, QTimer

from core.engine_params import EngineParams, MirrorParams, AudioParams
from core.engine_mirror import MirrorEngine
from core.engine_audio_mode import AudioModeEngine
from core.engine_hybrid_mode import HybridEngine
from core.engine_utils import MODE_MIRROR, MODE_AUDIO, MODE_HYBRID

_ENGINE_CLASSES = {
    MODE_MIRROR: MirrorEngine,
    MODE_AUDIO: AudioModeEngine,
    MODE_HYBRID: HybridEngine,
}


class EngineController(QObject):
    """엔진 수명주기 관리 + 파라미터 전달 중재자."""

    fps_updated = Signal(float)
    status_changed = Signal(str)
    error = Signal(str, str)
    energy_updated = Signal(float, float, float)
    spectrum_updated = Signal(object)
    screen_colors_updated = Signal(object)

    engine_started = Signal(str)
    engine_stopped = Signal()
    running_changed = Signal(bool)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._engine = None
        self._current_mode = MODE_MIRROR
        self._audio_device_index = None

    @property
    def is_running(self) -> bool:
        return self._engine is not None and self._engine.isRunning()

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @property
    def engine(self):
        return self._engine

    # ══════════════════════════════════════════════════════════════
    #  엔진 수명주기
    # ══════════════════════════════════════════════════════════════

    def set_audio_device_index(self, index):
        self._audio_device_index = index

    def start_engine(self, mode=None, initial_mirror_params=None,
                     initial_audio_params=None, initial_params=None):
        """엔진 시작.

        Args:
            mode: 엔진 모드 문자열
            initial_mirror_params: MirrorParams (호환)
            initial_audio_params: AudioParams (호환)
            initial_params: EngineParams (Phase 5 — 직접 전달)
        """
        self.stop_engine_sync()

        if mode is not None:
            self._current_mode = mode

        engine_cls = _ENGINE_CLASSES.get(self._current_mode)
        if engine_cls is None:
            self.error.emit(f"알 수 없는 모드: {self._current_mode}", "critical")
            return

        engine = engine_cls(
            self.config,
            audio_device_index=self._audio_device_index,
        )

        # Phase 5: EngineParams 직접 전달 우선
        if initial_params is not None:
            engine.update_params(initial_params)
        else:
            # 호환: 기존 MirrorParams/AudioParams
            if initial_mirror_params is not None:
                engine.update_mirror_params(initial_mirror_params)
            if initial_audio_params is not None:
                engine.update_audio_params(initial_audio_params)

        self._engine = engine
        self._connect_signals(engine)
        engine.start()
        self.running_changed.emit(True)
        self.engine_started.emit(self._current_mode)

    def stop_engine(self):
        if self._engine and self._engine.isRunning():
            self._engine.stop_engine()
            self.status_changed.emit("중지 중...")

    def stop_engine_sync(self):
        if self._engine is None:
            return
        old = self._engine
        self._engine = None
        self._disconnect_signals(old)
        if old.isRunning():
            old.stop_engine()
            old.wait(3000)
        self.running_changed.emit(False)

    def switch_mode(self, new_mode):
        self._current_mode = new_mode
        if self.is_running:
            self.start_engine(new_mode)

    def toggle_pause(self):
        if self._engine and self._engine.isRunning():
            self._engine.toggle_pause()
            return self._engine._paused
        return False

    def on_display_changed(self):
        if self._engine and self._engine.isRunning():
            self._engine.on_display_changed()

    def on_session_resume(self):
        if self._engine and self._engine.isRunning():
            self._engine.on_session_resume()

    # ══════════════════════════════════════════════════════════════
    #  파라미터 전달
    # ══════════════════════════════════════════════════════════════

    def set_params(self, params: EngineParams):
        """Phase 5: 통합 EngineParams 직접 전달."""
        if self._engine:
            self._engine.update_params(params)

    def set_mirror_params(self, params: MirrorParams):
        """[호환 유지] 미러링 파라미터 전달."""
        if self._engine:
            self._engine.update_mirror_params(params)

    def set_audio_params(self, params: AudioParams):
        """[호환 유지] 오디오/하이브리드 파라미터 전달."""
        if self._engine:
            self._engine.update_audio_params(params)

    def update_layout_params(self, **kwargs):
        if self._engine:
            self._engine.update_layout_params(**kwargs)

    # ══════════════════════════════════════════════════════════════
    #  시그널 연결
    # ══════════════════════════════════════════════════════════════

    def _connect_signals(self, engine):
        engine.fps_updated.connect(self.fps_updated)
        engine.status_changed.connect(self.status_changed)
        engine.error.connect(self.error)
        engine.energy_updated.connect(self.energy_updated)
        engine.spectrum_updated.connect(self.spectrum_updated)
        engine.screen_colors_updated.connect(self.screen_colors_updated)
        engine.finished.connect(self._on_engine_finished)

    def _disconnect_signals(self, engine):
        for sig_name in ("fps_updated", "status_changed", "error",
                         "energy_updated", "spectrum_updated",
                         "screen_colors_updated", "finished"):
            try:
                getattr(engine, sig_name).disconnect()
            except (TypeError, RuntimeError):
                pass

    def _on_engine_finished(self):
        self._engine = None
        self.running_changed.emit(False)
        self.engine_stopped.emit()
        self.status_changed.emit("대기 중")

    def cleanup(self):
        self.stop_engine_sync()
