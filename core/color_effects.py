"""색상 효과 — 시간 기반 그라데이션/무지개 + GradientPhase 누적 위상

engine_utils.py에서 분리. hsv_utils + audio_render 의존.

포함:
  GradientPhase                      — 누적 위상 관리
  gradient_speed_from_slider         — 슬라이더 → 속도 변환
  build_base_color_array_animated    — 시간 기반 색상 효과
  apply_mirror_gradient_modulation   — 미러링 HSV 변조
  _has_mirror_gradient_effect        — 효과 활성 판단
"""

import numpy as np

from core.hsv_utils import (
    rgb_to_hsv as _rgb_to_hsv_single,
    hsv_to_rgb as _hsv_to_rgb_single,
    hsv_to_rgb_array as _hsv_to_rgb_array,
    rgb_array_to_hsv as _rgb_array_to_hsv,
)
from core.audio_render import build_base_color_array, band_color_vectorized

# ── 효과 상수 (engine_utils에서도 re-export) ──
COLOR_EFFECT_STATIC = "static"
COLOR_EFFECT_GRADIENT_CW = "gradient_cw"
COLOR_EFFECT_GRADIENT_CCW = "gradient_ccw"
COLOR_EFFECT_RAINBOW_TIME = "rainbow_time"

# 그라데이션 물결 파라미터
_GRADIENT_V_PHASE_OFFSET = np.pi / 3
_GRADIENT_HUE_FREQ = 0.7

# 무지개 시간 순회 기본 속도
_RAINBOW_TIME_SPEED = 0.08

# 무지개+그라데이션에서 무지개 회전 속도
_RAINBOW_ROTATION_SPEED = 0.03


# ══════════════════════════════════════════════════════════════════
#  GradientPhase 누적 위상 관리
# ══════════════════════════════════════════════════════════════════

class GradientPhase:
    """그라데이션/무지개 효과의 누적 위상 관리.

    기존 문제: phase = current_time × speed → speed 변경 시 phase 점프
    해결: 매 프레임 dt × speed를 누적 → speed 변경해도 현재 위치에서 부드럽게 이어짐
    """
    __slots__ = ("phase", "hue_phase")

    def __init__(self):
        self.phase = 0.0
        self.hue_phase = 0.0

    def tick(self, dt, speed=1.0):
        self.phase += dt * speed
        self.hue_phase += dt * speed * 0.5

    def reset(self):
        self.phase = 0.0
        self.hue_phase = 0.0


# ══════════════════════════════════════════════════════════════════
#  속도 변환
# ══════════════════════════════════════════════════════════════════

def gradient_speed_from_slider(slider_pct):
    """효과 속도 슬라이더 값(0~100) → 실제 speed 변환.

    0% → 0.3, 50% → 1.0, 100% → 3.0
    """
    t = slider_pct / 100.0
    return 0.3 + t * 2.7


# ══════════════════════════════════════════════════════════════════
#  시간 기반 색상 효과
# ══════════════════════════════════════════════════════════════════

def build_base_color_array_animated(
    led_band_indices, n_bands, clockwise_t, current_time,
    color_effect="static",
    rainbow=True, solid_color=None, screen_colors=None,
    gradient_speed=1.0, gradient_hue_range=0.08, gradient_sv_range=0.5,
    gradient_phase=None,
):
    """시간 기반 색상 효과가 적용된 base_colors 생성.

    color_effect가 "static"이면 기존 build_base_color_array()와 동일.
    """
    if screen_colors is not None:
        return screen_colors.copy()

    if color_effect == COLOR_EFFECT_STATIC:
        return build_base_color_array(
            led_band_indices, n_bands,
            rainbow=rainbow, solid_color=solid_color,
        )

    n_leds = len(led_band_indices)
    ct = np.asarray(clockwise_t, dtype=np.float64)

    if gradient_phase is not None:
        main_phase = gradient_phase.phase
        hue_phase_val = gradient_phase.hue_phase
    else:
        main_phase = current_time * gradient_speed
        hue_phase_val = current_time * gradient_speed * 0.5

    if color_effect == COLOR_EFFECT_RAINBOW_TIME:
        hue = (main_phase * _RAINBOW_TIME_SPEED) % 1.0
        rgb = _hsv_to_rgb_single(hue, 1.0, 1.0)
        return np.broadcast_to(rgb, (n_leds, 3)).copy()

    if color_effect in (COLOR_EFFECT_GRADIENT_CW, COLOR_EFFECT_GRADIENT_CCW):
        direction = 1.0 if color_effect == COLOR_EFFECT_GRADIENT_CW else -1.0

        sv = max(0.0, min(1.0, gradient_sv_range))
        s_min = 1.0 - sv * 0.8
        s_range = sv * 0.8
        v_min = 1.0 - sv * 0.7
        v_range = sv * 0.7

        hue_range = max(0.0, min(0.25, gradient_hue_range))

        phase = ct * 2.0 * np.pi + main_phase * direction
        hue_p = ct * 2.0 * np.pi * _GRADIENT_HUE_FREQ + hue_phase_val * direction

        if rainbow:
            rainbow_offset = main_phase * _RAINBOW_ROTATION_SPEED * direction
            t = (led_band_indices / max(1, n_bands - 1) + rainbow_offset) % 1.0
            base_rgb = band_color_vectorized(t)
            h, s, v = _rgb_array_to_hsv(base_rgb)

            s_mod = s_min + s_range * (0.5 + 0.5 * np.sin(phase))
            v_mod = v_min + v_range * (0.5 + 0.5 * np.sin(phase + _GRADIENT_V_PHASE_OFFSET))
            s = np.clip(s * s_mod, 0, 1)
            v = np.clip(v * v_mod, 0, 1)
            return _hsv_to_rgb_array(h, s, v)
        else:
            if solid_color is None:
                solid_color = np.array([255, 0, 80], dtype=np.float32)
            base_h, base_s, base_v = _rgb_to_hsv_single(solid_color)

            s_mod = s_min + s_range * (0.5 + 0.5 * np.sin(phase))
            v_mod = v_min + v_range * (0.5 + 0.5 * np.sin(phase + _GRADIENT_V_PHASE_OFFSET))

            h_shift = hue_range * np.sin(hue_p)

            h = (base_h + h_shift) % 1.0
            s = np.clip(base_s * s_mod, 0, 1)
            v = np.clip(base_v * v_mod, 0, 1)
            return _hsv_to_rgb_array(h, s, v)

    # fallback
    return build_base_color_array(
        led_band_indices, n_bands,
        rainbow=rainbow, solid_color=solid_color,
    )


# ══════════════════════════════════════════════════════════════════
#  미러링 그라데이션 변조
# ══════════════════════════════════════════════════════════════════

def _has_mirror_gradient_effect(color_effect, gradient_sv_range, gradient_hue_range):
    """미러링 그라데이션 효과가 실질적으로 활성화되어 있는지 판단."""
    if color_effect == COLOR_EFFECT_STATIC:
        return False
    if color_effect not in (COLOR_EFFECT_GRADIENT_CW, COLOR_EFFECT_GRADIENT_CCW):
        return False
    if gradient_sv_range < 0.001 and gradient_hue_range < 0.001:
        return False
    return True


def apply_mirror_gradient_modulation(
    per_led_rgb, clockwise_t, current_time,
    color_effect="static",
    gradient_speed=1.0, gradient_hue_range=0.08, gradient_sv_range=0.5,
    gradient_phase=None,
):
    """미러링 전용 모드에서 화면 색상에 그라데이션 S/V/H 물결 변조 적용."""
    if not _has_mirror_gradient_effect(color_effect, gradient_sv_range, gradient_hue_range):
        return per_led_rgb

    ct = np.asarray(clockwise_t, dtype=np.float64)
    direction = 1.0 if color_effect == COLOR_EFFECT_GRADIENT_CW else -1.0

    sv = max(0.0, min(1.0, gradient_sv_range))
    s_min = 1.0 - sv * 0.8
    s_range = sv * 0.8
    v_min = 1.0 - sv * 0.7
    v_range = sv * 0.7

    hue_range = max(0.0, min(0.25, gradient_hue_range))

    if gradient_phase is not None:
        main_phase = gradient_phase.phase
        hue_phase_val = gradient_phase.hue_phase
    else:
        main_phase = current_time * gradient_speed
        hue_phase_val = current_time * gradient_speed * 0.5

    h, s, v = _rgb_array_to_hsv(per_led_rgb)

    phase = ct * 2.0 * np.pi + main_phase * direction

    s_mod = s_min + s_range * (0.5 + 0.5 * np.sin(phase))
    s = np.clip(s * s_mod, 0, 1)

    v_mod = v_min + v_range * (0.5 + 0.5 * np.sin(phase + _GRADIENT_V_PHASE_OFFSET))
    v = np.clip(v * v_mod, 0, 1)

    if hue_range > 0.001:
        hue_p = (ct * 2.0 * np.pi * _GRADIENT_HUE_FREQ
                 + hue_phase_val * direction)
        h_shift = hue_range * np.sin(hue_p)
        h = (h + h_shift) % 1.0

    return _hsv_to_rgb_array(h, s, v)
