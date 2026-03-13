"""엔진 파라미터 스냅샷 — ADR-003 적용

[ADR-003] GIL 의존 atomic scalar 대신, UI가 빌드한 스냅샷 객체를
엔진이 프레임 시작 시 한 번 atomic swap하는 패턴.

UI 스레드: snapshot 객체를 생성하여 engine._pending_params에 대입
엔진 스레드: 매 프레임 시작 시 _pending_params를 읽어 현재 파라미터로 교체

CPython GIL 하에서 객체 참조 대입은 atomic이므로,
별도 lock 없이 안전하게 스냅샷을 교환할 수 있습니다.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class MirrorParams:
    """미러링 모드 파라미터 스냅샷."""
    brightness: float = 1.0
    smoothing_enabled: bool = True
    smoothing_factor: float = 0.5
    mirror_n_zones: int = -1  # N_ZONES_PER_LED


@dataclass(frozen=True)
class AudioParams:
    """오디오/하이브리드 공통 파라미터 스냅샷."""
    audio_mode: str = "pulse"
    brightness: float = 1.0
    min_brightness: float = 0.02
    bass_sensitivity: float = 1.0
    mid_sensitivity: float = 1.0
    high_sensitivity: float = 1.0
    attack: float = 0.5
    release: float = 0.1
    input_smoothing: float = 0.3
    zone_weights: Tuple[int, int, int] = (33, 33, 34)

    # 색상
    rainbow: bool = True
    base_color: Tuple[int, int, int] = (255, 0, 80)

    # 하이브리드 전용
    color_source: str = "screen"
    n_zones: int = 4


@dataclass
class LayoutParams:
    """레이아웃 파라미터 — dirty flag 포함.

    frozen이 아닌 이유: dirty flag를 엔진이 리셋해야 하므로.
    이 객체는 _layout_lock 아래에서만 접근합니다.
    """
    decay_radius: float = 0.3
    parallel_penalty: float = 5.0
    decay_per_side: dict = field(default_factory=dict)
    penalty_per_side: dict = field(default_factory=dict)
    dirty: bool = False
