"""엔진 파라미터 스냅샷 — 통합 EngineParams (Phase 0)

[변경 이력]
- 기존 MirrorParams + AudioParams → EngineParams 단일 dataclass로 통합
- display_enabled / audio_enabled 토글 플래그 추가
- master_brightness: 모든 모드 공용 최대 밝기
- MirrorParams / AudioParams: 호환 팩토리 함수로 유지 (Phase 4~5 완료 시 제거)

[설계 원칙]
- frozen=True: UI가 빌드한 스냅샷을 엔진이 atomic swap
- 모든 필드에 안전한 기본값 → UI에서 부분 갱신 시에도 동작
- LayoutParams는 별도 유지 (dirty flag 때문에 frozen 불가)
"""

from dataclasses import dataclass, field
from typing import Tuple


# ══════════════════════════════════════════════════════════════════
#  EngineParams — 통합 파라미터 스냅샷
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EngineParams:
    """모든 엔진 모드의 통합 파라미터 스냅샷.

    UI 스레드에서 빌드 → engine._pending_params에 atomic 대입 →
    엔진 스레드가 프레임 시작 시 _swap_params()로 교체.

    필드 그룹:
    - 토글: display_enabled, audio_enabled
    - 공용: master_brightness
    - 디스플레이 계열: smoothing, 구역, 추출, 감쇠/페널티
    - 색상 (디스플레이 OFF 시): rainbow, base_color, color_effect 등
    - 오디오 계열: audio_mode, 감도, attack/release, 모드별 파라미터
    - flowing 전용: flowing_interval, flowing_speed
    """

    # ── 토글 상태 ──
    display_enabled: bool = False
    audio_enabled: bool = False

    # ── 공용 ──
    master_brightness: float = 1.0    # 0~1. 모든 모드의 최대 밝기.

    # ── 디스플레이 계열 ──
    smoothing_factor: float = 0.5     # 0이면 스무딩 off
    mirror_n_zones: int = -1          # -1 = per-LED
    color_extract_mode: str = "average"  # "average" | "distinctive"

    # ── 색상 (디스플레이 OFF 또는 오디오-only 모드) ──
    rainbow: bool = True
    base_color: Tuple[int, int, int] = (255, 0, 80)
    color_effect: str = "static"      # static, gradient_cw, gradient_ccw, rainbow_time
    gradient_speed: float = 1.0       # 효과 속도 배수
    gradient_hue_range: float = 0.08  # hue shift 범위 (0~0.20)
    gradient_sv_range: float = 0.5    # S/V 변동 강도 (0~1)

    # ── 오디오 계열 ──
    audio_mode: str = "pulse"         # pulse, spectrum, bass_detail, wave, dynamic, flowing
    min_brightness: float = 0.02      # 오디오 최소 밝기
    bass_sensitivity: float = 1.0
    mid_sensitivity: float = 1.0
    high_sensitivity: float = 1.0
    attack: float = 0.5
    release: float = 0.1
    input_smoothing: float = 0.3
    zone_weights: Tuple[int, int, int] = (33, 33, 34)

    # ── Wave 전용 ──
    wave_speed: float = 1.4

    # ── Flowing 전용 (디스플레이+오디오 ON) ──
    flowing_interval: float = 3.0     # palette 갱신 주기 (초)
    flowing_speed: float = 0.08       # 기본 회전 속도

    # ══════════════════════════════════════════════════════════════
    #  호환 속성: 기존 엔진 코드에서 참조하는 이름들
    # ══════════════════════════════════════════════════════════════

    @property
    def brightness(self) -> float:
        """기존 AudioParams.brightness 호환.

        오디오 모드에서 brightness는 master_brightness로 대체.
        기존 코드: ap.brightness → 이제 ep.brightness = ep.master_brightness
        """
        return self.master_brightness

    @property
    def smoothing_enabled(self) -> bool:
        """기존 MirrorParams.smoothing_enabled 호환.

        smoothing_factor > 0이면 활성.
        """
        return self.smoothing_factor > 0.0

    @property
    def color_source(self) -> str:
        """기존 AudioParams.color_source 호환.

        디스플레이 ON → "screen", OFF → "solid".
        """
        return "screen" if self.display_enabled else "solid"

    @property
    def n_zones(self) -> int:
        """기존 AudioParams.n_zones 호환 (하이브리드 구역 수)."""
        return self.mirror_n_zones


# ══════════════════════════════════════════════════════════════════
#  호환 팩토리: 기존 코드가 MirrorParams/AudioParams를 생성하는 곳에서
#  EngineParams로 전환하기 전까지 사용
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MirrorParams:
    """[호환 유지] 미러링 전용 파라미터 — Phase 5 완료 시 제거."""
    brightness: float = 1.0
    smoothing_enabled: bool = True
    smoothing_factor: float = 0.5
    mirror_n_zones: int = -1

    def to_engine_params(self, **overrides) -> EngineParams:
        """MirrorParams → EngineParams 변환."""
        base = {
            "display_enabled": True,
            "audio_enabled": False,
            "master_brightness": self.brightness,
            "smoothing_factor": self.smoothing_factor if self.smoothing_enabled else 0.0,
            "mirror_n_zones": self.mirror_n_zones,
        }
        base.update(overrides)
        return EngineParams(**base)


@dataclass(frozen=True)
class AudioParams:
    """[호환 유지] 오디오/하이브리드 파라미터 — Phase 5 완료 시 제거."""
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
    rainbow: bool = True
    base_color: Tuple[int, int, int] = (255, 0, 80)
    color_effect: str = "static"
    gradient_speed: float = 1.0
    gradient_hue_range: float = 0.08
    gradient_sv_range: float = 0.5
    color_source: str = "screen"
    n_zones: int = 4
    color_extract_mode: str = "average"
    wave_speed: float = 1.4
    flowing_interval: float = 3.0
    flowing_speed: float = 0.08

    def to_engine_params(self, display_enabled=False, **overrides) -> EngineParams:
        """AudioParams → EngineParams 변환."""
        base = {
            "display_enabled": display_enabled,
            "audio_enabled": True,
            "master_brightness": self.brightness,
            "min_brightness": self.min_brightness,
            "bass_sensitivity": self.bass_sensitivity,
            "mid_sensitivity": self.mid_sensitivity,
            "high_sensitivity": self.high_sensitivity,
            "attack": self.attack,
            "release": self.release,
            "input_smoothing": self.input_smoothing,
            "zone_weights": self.zone_weights,
            "rainbow": self.rainbow,
            "base_color": self.base_color,
            "color_effect": self.color_effect,
            "gradient_speed": self.gradient_speed,
            "gradient_hue_range": self.gradient_hue_range,
            "gradient_sv_range": self.gradient_sv_range,
            "audio_mode": self.audio_mode,
            "mirror_n_zones": self.n_zones,
            "color_extract_mode": self.color_extract_mode,
            "wave_speed": self.wave_speed,
            "flowing_interval": self.flowing_interval,
            "flowing_speed": self.flowing_speed,
        }
        base.update(overrides)
        return EngineParams(**base)


# ══════════════════════════════════════════════════════════════════
#  LayoutParams — 별도 유지 (dirty flag 필요)
# ══════════════════════════════════════════════════════════════════

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