"""엔진 유틸리티 — 공용 상수 + GRB 변환 + re-export hub

[Refactor] 1100줄 → 4개 모듈로 분리 후, 기존 import 경로 호환을 위한 re-export.

분리된 모듈:
  core.led_mapping    — LED 둘레 좌표, 밴드 매핑, 구역 매핑
  core.audio_render   — pulse/spectrum/wave/dynamic 렌더링
  core.color_effects  — gradient/rainbow 효과, GradientPhase
  core.hsv_utils      — HSV↔RGB 변환 (1단계에서 분리)

이 파일의 역할:
  1. 공용 상수 정의 (모드 이름, 기본값 등)
  2. leds_to_grb() — GRB 바이트 변환
  3. 분리된 모듈의 모든 공개 이름을 re-export
     → 기존 `from core.engine_utils import X` 가 그대로 동작

순수 numpy 모듈. Qt 의존성 없음.
"""

import numpy as np

# ══════════════════════════════════════════════════════════════════
#  공용 상수
# ══════════════════════════════════════════════════════════════════

MODE_MIRROR = "mirror"
MODE_AUDIO = "audio"
MODE_HYBRID = "hybrid"

AUDIO_PULSE = "pulse"
AUDIO_SPECTRUM = "spectrum"
AUDIO_BASS_DETAIL = "bass_detail"
AUDIO_WAVE = "wave"
AUDIO_DYNAMIC = "dynamic"
AUDIO_FLOWING = "flowing"

COLOR_SOURCE_SOLID = "solid"
COLOR_SOURCE_SCREEN = "screen"

N_ZONES_PER_LED = -1

SCREEN_UPDATE_INTERVAL = 3

_STALE_RECREATE_COOLDOWN = 3.0
_STALE_LED_OFF_THRESHOLD = 10.0

DEFAULT_FPS = 60
MIN_BRIGHTNESS = 0.02
DEFAULT_ZONE_WEIGHTS = (33, 33, 34)

BASS_DETAIL_FREQ_MIN = 20
BASS_DETAIL_FREQ_MAX = 500
BASS_DETAIL_N_BANDS = 16


# ══════════════════════════════════════════════════════════════════
#  GRB 변환 (이 파일에 유지 — 모든 모듈에서 사용)
# ══════════════════════════════════════════════════════════════════

def leds_to_grb(leds):
    """LED RGB 배열을 GRB bytes로 변환.

    Args:
        leds: (n_leds, 3) float32, 0~255

    Returns:
        bytes — GRB 순서
    """
    np.clip(leds, 0, 255, out=leds)
    u8 = leds.astype(np.uint8)
    grb = np.empty_like(u8)
    grb[:, 0] = u8[:, 1]  # G
    grb[:, 1] = u8[:, 0]  # R
    grb[:, 2] = u8[:, 2]  # B
    return grb.tobytes()


# ══════════════════════════════════════════════════════════════════
#  Re-export: core.led_mapping
# ══════════════════════════════════════════════════════════════════

from core.led_mapping import (  # noqa: E402, F401
    _remap_t,
    _compute_led_perimeter_t,
    _compute_led_clockwise_t,
    _compute_led_band_mapping,
    compute_led_normalized_y,
    _build_led_order_from_segments,
    _build_led_zone_map_by_side,
    per_led_to_zone_colors,
    compute_side_t_ranges,
)

# ══════════════════════════════════════════════════════════════════
#  Re-export: core.audio_render
# ══════════════════════════════════════════════════════════════════

from core.audio_render import (  # noqa: E402, F401
    RAINBOW_KEYPOINTS,
    band_color_vectorized,
    build_base_color_array,
    vectorized_render_pulse,
    vectorized_render_spectrum,
    vectorized_render_wave,
    vectorized_render_dynamic,
    wave_speed_from_slider,
    WavePulse,
    wave_tick_pulses,
    DynamicRipple,
    dynamic_tick_ripples,
    WAVE_SPEED_DEFAULT,
)

# ══════════════════════════════════════════════════════════════════
#  Re-export: core.color_effects
# ══════════════════════════════════════════════════════════════════

from core.color_effects import (  # noqa: E402, F401
    COLOR_EFFECT_STATIC,
    COLOR_EFFECT_GRADIENT_CW,
    COLOR_EFFECT_GRADIENT_CCW,
    COLOR_EFFECT_RAINBOW_TIME,
    COLOR_EFFECT_FLOWING,          # ★ 미러링 전용 flowing
    GradientPhase,
    gradient_speed_from_slider,
    build_base_color_array_animated,
    apply_mirror_gradient_modulation,
    _has_mirror_gradient_effect,
)

# ══════════════════════════════════════════════════════════════════
#  Re-export: core.hsv_utils (engine_utils에서 사용되던 private alias)
# ══════════════════════════════════════════════════════════════════

from core.hsv_utils import (  # noqa: E402, F401
    rgb_to_hsv as _rgb_to_hsv_single,
    hsv_to_rgb as _hsv_to_rgb_single,
    hsv_to_rgb_array as _hsv_to_rgb_array,
    rgb_array_to_hsv as _rgb_array_to_hsv,
)
