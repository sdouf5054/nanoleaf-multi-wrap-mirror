"""EngineController — UI↔엔진 중재자 (ADR-019)

[ADR-019] MainWindow의 13+ 릴레이 슬롯을 제거하고,
UI와 엔진 사이의 파라미터 전달을 단일 객체로 집중합니다.

사용 패턴:
    controller = EngineController(config)

    # UI에서:
    controller.start_engine("mirror")
    controller.set_mirror_brightness(0.8)
    controller.set_audio_params(AudioParams(...))

    # UI 시그널 연결:
    controller.fps_updated.connect(tab.update_fps)
    controller.status_changed.connect(tab.update_status)

    # 종료:
    controller.stop_engine()
    controller.cleanup()
"""

import copy
from PySide6.QtCore import QObject, Signal, QTimer

from core.engine_params import MirrorParams, AudioParams
from core.engine_mirror import MirrorEngine
from core.engine_audio_mode import AudioModeEngine
from core.engine_hybrid_mode import HybridEngine
from core.engine_utils import MODE_MIRROR, MODE_AUDIO, MODE_HYBRID


# 모드 → 엔진 클래스 매핑
_ENGINE_CLASSES = {
    MODE_MIRROR: MirrorEngine,
    MODE_AUDIO: AudioModeEngine,
    MODE_HYBRID: HybridEngine,
}


class EngineController(QObject):
    """엔진 수명주기 관리 + 파라미터 전달 중재자.

    Signals (엔진 → UI 전달):
        fps_updated(float)
        status_changed(str)
        error(str, str)
        energy_updated(float, float, float)
        spectrum_updated(object)
        screen_colors_updated(object)
        engine_started(str)         — 모드 문자열
        engine_stopped()
        running_changed(bool)       — 실행 상태 변경
    """

    # 엔진 → UI (프록시)
    fps_updated = Signal(float)
    status_changed = Signal(str)
    error = Signal(str, str)
    energy_updated = Signal(float, float, float)
    spectrum_updated = Signal(object)
    screen_colors_updated = Signal(object)

    # 컨트롤러 자체 시그널
    engine_started = Signal(str)
    engine_stopped = Signal()
    running_changed = Signal(bool)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._engine = None
        self._current_mode = MODE_MIRROR
        self._audio_device_index = None

    # ══════════════════════════════════════════════════════════════
    #  속성
    # ══════════════════════════════════════════════════════════════

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

    def start_engine(self, mode=None, initial_audio_params=None,
                     initial_mirror_params=None):
        """엔진 시작 (기존 엔진이 있으면 먼저 정리).

        initial_audio_params/initial_mirror_params: UI에서 수집한
        초기 파라미터. 엔진 시작 전에 pending으로 설정되어
        첫 프레임부터 적용됩니다.
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

        # 초기 파라미터 적용 (첫 프레임의 _swap_params에서 반영됨)
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
        """엔진 중지 요청 (비동기 — finished 시그널로 완료 통보)."""
        if self._engine and self._engine.isRunning():
            self._engine.stop_engine()
            self.status_changed.emit("중지 중...")

    def stop_engine_sync(self):
        """엔진 완전 정지 (동기 — 최대 3초 대기)."""
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
        """모드 전환 — 엔진 재시작."""
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

    # ══════════════════════════════════════════════════════════════
    #  파라미터 전달 (ADR-003 + ADR-019)
    # ══════════════════════════════════════════════════════════════

    def set_mirror_params(self, params: MirrorParams):
        """미러링 파라미터 스냅샷 전달."""
        if self._engine:
            self._engine.update_mirror_params(params)

    def set_audio_params(self, params: AudioParams):
        """오디오/하이브리드 파라미터 스냅샷 전달."""
        if self._engine:
            self._engine.update_audio_params(params)

    def update_layout_params(self, **kwargs):
        """레이아웃 파라미터 (decay/penalty) 전달."""
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

    # ══════════════════════════════════════════════════════════════
    #  정리
    # ══════════════════════════════════════════════════════════════

    def cleanup(self):
        """앱 종료 시 호출."""
        self.stop_engine_sync()
