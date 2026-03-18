"""엔진 파라미터 스냅샷 — EngineParams 단일 dataclass (Phase 7)

[Phase 7 변경]
- MirrorParams / AudioParams 완전 제거
- EngineParams만 유지
- 호환 속성(brightness, smoothing_enabled, color_source, n_zones) 유지
  — UnifiedEngine 내부에서 직접 참조하는 편의 속성

[미디어 연동 추가]
- media_color_enabled: bool = False
  → display_enabled=True일 때만 유효
  → True이면 화면 캡처 대신 앨범 아트 이미지를 파이프라인에 투입
  → 기존 미러링 옵션(구역, 추출, 스무딩, 색상효과)이 그대로 적용됨

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
    - 토글: display_enabled, audio_enabled, media_color_enabled
    - 공용: master_brightness
    - 디스플레이 계열: smoothing, 구역, 추출, 감쇠/페널티
    - 색상 (디스플레이 OFF 시): rainbow, base_color, color_effect 등
    - 오디오 계열: audio_mode, 감도, attack/release, 모드별 파라미터
    - flowing 전용: flowing_interval, flowing_speed
    """

    # ── 토글 상태 ──
    display_enabled: bool = False
    audio_enabled: bool = False
    media_color_enabled: bool = False  # ★ 미디어 연동 (display_enabled=True 시에만 유효)

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
    #  편의 속성
    # ══════════════════════════════════════════════════════════════

    @property
    def brightness(self) -> float:
        """master_brightness 별칭."""
        return self.master_brightness

    @property
    def smoothing_enabled(self) -> bool:
        """smoothing_factor > 0이면 활성."""
        return self.smoothing_factor > 0.0

    @property
    def color_source(self) -> str:
        """디스플레이 소스 결정.

        display_enabled=True + media_color_enabled=True → "media"
        display_enabled=True → "screen"
        display_enabled=False → "solid"
        """
        if self.display_enabled:
            return "media" if self.media_color_enabled else "screen"
        return "solid"

    @property
    def n_zones(self) -> int:
        """mirror_n_zones 별칭."""
        return self.mirror_n_zones

    @property
    def use_media_frame(self) -> bool:
        """미디어 프레임을 사용해야 하는지 여부.

        display_enabled=True이고 media_color_enabled=True일 때만 True.
        """
        return self.display_enabled and self.media_color_enabled


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