"""오디오 렌더링 — pulse/spectrum/wave/dynamic 벡터화 렌더러

engine_utils.py에서 분리. 순수 numpy 모듈.

포함:
  build_base_color_array      — LED 기본 색상 배열 생성
  band_color_vectorized       — 무지개 키포인트 → RGB
  vectorized_render_pulse     — Pulse 모드 렌더링
  vectorized_render_spectrum  — Spectrum 모드 렌더링
  vectorized_render_wave      — Wave 모드 렌더링
  vectorized_render_dynamic   — Dynamic 모드 렌더링
  WavePulse, wave_tick_pulses — Wave 상태 관리
  DynamicRipple, dynamic_tick_ripples — Dynamic 상태 관리

[min_brightness_mode 추가]
  4개 렌더러에 min_brightness_mode 파라미터 추가:
  - "floor": 기존 방식. max(min_b, energy) — min_b 이하 클램핑
  - "remap": min_b + energy * (1.0 - min_b) — 전 구간 선형 리매핑
"""

import numpy as np

# ══════════════════════════════════════════════════════════════════
#  무지개 키포인트 + 색상 배열 생성
# ══════════════════════════════════════════════════════════════════

RAINBOW_KEYPOINTS = np.array([
    [0.000, 255,   0,   0],
    [0.130, 255, 127,   0],
    [0.260, 255, 255,   0],
    [0.400,   0, 255,   0],
    [0.540,   0, 180, 255],
    [0.680,   0,  50, 255],
    [0.820,  80,   0, 255],
    [1.000, 160,   0, 220],
], dtype=np.float32)


def band_color_vectorized(t_array):
    """밴드 위치 배열 (n,) → RGB 배열 (n, 3)."""
    t = np.clip(t_array, 0.0, 1.0)
    n = len(t)
    result = np.zeros((n, 3), dtype=np.float32)

    kp = RAINBOW_KEYPOINTS
    for seg_idx in range(len(kp) - 1):
        t0 = kp[seg_idx, 0]
        t1 = kp[seg_idx + 1, 0]
        rgb0 = kp[seg_idx, 1:4]
        rgb1 = kp[seg_idx + 1, 1:4]

        mask = (t >= t0) & (t <= t1)
        if not mask.any():
            continue

        frac = np.where(
            t1 > t0,
            (t[mask] - t0) / (t1 - t0),
            np.float32(0.0)
        )
        result[mask] = rgb0 + frac[:, np.newaxis] * (rgb1 - rgb0)

    return result


def build_base_color_array(led_band_indices, n_bands, rainbow=True,
                           solid_color=None, screen_colors=None):
    """전체 LED의 기본 색상 배열을 한 번에 생성."""
    n_leds = len(led_band_indices)

    if screen_colors is not None:
        return screen_colors.copy()

    if rainbow:
        t = led_band_indices / max(1, n_bands - 1)
        return band_color_vectorized(t)

    if solid_color is not None:
        return np.broadcast_to(solid_color, (n_leds, 3)).copy()

    return np.full((n_leds, 3), 255.0, dtype=np.float32)


# ══════════════════════════════════════════════════════════════════
#  min_brightness 적용 헬퍼
# ══════════════════════════════════════════════════════════════════

def _apply_min_brightness_scalar(min_brightness, raw_energy, mode="floor"):
    """스칼라 에너지 값에 min_brightness 적용.

    floor:  max(min_b, raw) — min_b 이하 클램핑
    remap:  min_b + raw * (1.0 - min_b) — 전 구간 선형 리매핑
    """
    if mode == "remap":
        return min_brightness + raw_energy * (1.0 - min_brightness)
    else:
        return max(min_brightness, raw_energy)


def _apply_min_brightness_array(min_brightness, raw_energy, mode="floor"):
    """배열 에너지 값에 min_brightness 적용.

    floor:  np.maximum(min_b, raw) — min_b 이하 클램핑
    remap:  min_b + raw * (1.0 - min_b) — 전 구간 선형 리매핑
    """
    if mode == "remap":
        return min_brightness + raw_energy * (1.0 - min_brightness)
    else:
        return np.maximum(min_brightness, raw_energy)


# ══════════════════════════════════════════════════════════════════
#  Pulse 렌더링
# ══════════════════════════════════════════════════════════════════

def vectorized_render_pulse(base_colors, bass, mid, high,
                            min_brightness, audio_brightness,
                            min_brightness_mode="floor"):
    """벡터화된 Pulse 렌더링.

    min_brightness_mode:
        "floor": intensity = max(min_b, bass) * brightness
        "remap": intensity = (min_b + bass * (1 - min_b)) * brightness
    """
    intensity = _apply_min_brightness_scalar(
        min_brightness, bass, min_brightness_mode
    ) * audio_brightness

    saturation_boost = 1.0 + mid * 0.3
    leds = np.clip(base_colors * saturation_boost, 0, 255)

    white_mix = high * 0.15
    leds = leds * (1.0 - white_mix) + 255.0 * white_mix

    leds *= intensity
    return leds


# ══════════════════════════════════════════════════════════════════
#  Spectrum 렌더링
# ══════════════════════════════════════════════════════════════════

def vectorized_render_spectrum(base_colors, led_band_indices, spectrum,
                               min_brightness, audio_brightness,
                               min_brightness_mode="floor"):
    """벡터화된 Spectrum 렌더링.

    min_brightness_mode:
        "floor": intensity = max(min_b, energy) * brightness
        "remap": intensity = (min_b + energy * (1 - min_b)) * brightness
    """
    n_bands = len(spectrum)

    band_lo = np.clip(np.floor(led_band_indices).astype(np.int32), 0, n_bands - 1)
    band_hi = np.clip(band_lo + 1, 0, n_bands - 1)
    frac = led_band_indices - np.floor(led_band_indices)

    energy = spectrum[band_lo] * (1.0 - frac) + spectrum[band_hi] * frac
    intensity = _apply_min_brightness_array(
        min_brightness, energy, min_brightness_mode
    ) * audio_brightness

    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)


# ══════════════════════════════════════════════════════════════════
#  Wave 모드 — 펄스 큐 + 렌더링
# ══════════════════════════════════════════════════════════════════

# Wave 파라미터 상수
WAVE_SPEED_DEFAULT = 1.4
WAVE_SPEED_MIN = 0.4
WAVE_SPEED_MAX = 8.0
WAVE_WIDTH_FRONT = 0.07
WAVE_WIDTH_TRAIL = 0.15
WAVE_DECAY = 0.45
WAVE_COOLDOWN = 0.06
WAVE_MAX_PULSES = 8
WAVE_ENERGY_BOOST = 1.8
WAVE_TILT_COMP = 0.5
WAVE_ONSET_DELTA = 0.02


def wave_speed_from_slider(slider_pct):
    """슬라이더 값(0~100) → 실제 wave speed 변환."""
    t = slider_pct / 100.0
    return WAVE_SPEED_MIN + t * (WAVE_SPEED_MAX - WAVE_SPEED_MIN)


class WavePulse:
    """하나의 Wave 펄스 상태."""
    __slots__ = ("position", "energy", "age", "color_t")

    def __init__(self, energy, color_t=0.0):
        self.position = 0.0
        self.energy = min(energy * WAVE_ENERGY_BOOST, 2.5)
        self.age = 0.0
        self.color_t = color_t


def wave_tick_pulses(pulses, dt, bass, prev_bass, last_spawn_time,
                     current_time, speed=WAVE_SPEED_DEFAULT):
    """펄스 큐 업데이트."""
    for p in pulses:
        p.position += speed * dt
        p.age += dt
        p.energy *= (1.0 - WAVE_DECAY * dt)

    pulses[:] = [p for p in pulses
                 if p.position < 1.6 and p.energy > 0.02]

    bass_delta = bass - prev_bass

    if (bass_delta > WAVE_ONSET_DELTA
            and bass > 0.02
            and (current_time - last_spawn_time) > WAVE_COOLDOWN
            and len(pulses) < WAVE_MAX_PULSES):
        color_t = (current_time * 0.15) % 1.0
        pulses.append(WavePulse(energy=bass, color_t=color_t))
        last_spawn_time = current_time

    return last_spawn_time


def vectorized_render_wave(base_colors, led_norm_y, pulses,
                           min_brightness, audio_brightness,
                           speed=WAVE_SPEED_DEFAULT,
                           min_brightness_mode="floor"):
    """Wave 모드 렌더링 — 비대칭 가우시안 + additive blending.

    min_brightness_mode:
        "floor": 바닥이 min_b, 펄스가 그 위에 additive
        "remap": 펄스 합산(0~1+) → min_b + raw * (1 - min_b) 리매핑
    """
    n_leds = len(led_norm_y)

    # ── raw intensity: 펄스 기여만 누적 (바닥 0) ──
    raw_intensity = np.zeros(n_leds, dtype=np.float64)

    speed_ratio = max(speed / WAVE_SPEED_DEFAULT, 1.0)
    width_scale = speed_ratio ** 0.7

    sf = WAVE_WIDTH_FRONT * width_scale
    st = WAVE_WIDTH_TRAIL * width_scale
    two_sf_sq = 2.0 * sf * sf
    two_st_sq = 2.0 * st * st

    for p in pulses:
        dist = led_norm_y - p.position
        two_sigma_sq = np.where(dist >= 0, two_sf_sq, two_st_sq)
        contribution = p.energy * np.exp(-(dist * dist) / two_sigma_sq)
        raw_intensity += contribution

    tilt_factor = 1.0 + led_norm_y * WAVE_TILT_COMP
    raw_intensity *= tilt_factor

    # ── min_brightness 적용 ──
    intensity = _apply_min_brightness_array(
        min_brightness, raw_intensity, min_brightness_mode
    )

    intensity *= audio_brightness
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)


# ══════════════════════════════════════════════════════════════════
#  Dynamic 모드 — Slot + Envelope Follower
# ══════════════════════════════════════════════════════════════════

DYN_ONSET_DELTA_BASE = 0.012
DYN_ONSET_BASS_MIN = 0.12
DYN_REFRACTORY = 0.10
DYN_COOLDOWN = 0.03
DYN_MAX_SLOTS = 8
DYN_MIN_DISTANCE = 0.18

DYN_RIPPLE_WIDTH_MIN = 0.03
DYN_RIPPLE_WIDTH_MAX = 0.06
DYN_RIPPLE_HALO_MIN = 0.08
DYN_RIPPLE_HALO_MAX = 0.12

DYN_RIPPLE_SPEED = 0.2
DYN_RIPPLE_EXPAND_CORE = 0.02
DYN_RIPPLE_EXPAND_HALO = 0.04

DYN_ENV_ATTACK_MIN = 3.0
DYN_ENV_ATTACK_MAX = 25.0
DYN_ENV_RELEASE_MIN = 0.5
DYN_ENV_RELEASE_MAX = 4.0

DYN_ENERGY_BOOST = 1.4
DYN_SPARKLE_PROB = 0.10
DYN_SLOT_DEATH_THRESHOLD = 0.015


class DynamicRipple:
    """하나의 Dynamic slot."""
    __slots__ = ("center_t", "radius", "target_energy", "envelope",
                 "age", "last_onset_time", "color_offset",
                 "width_core", "width_halo")

    def __init__(self, center_t, energy, current_time, color_offset=0.0):
        self.center_t = center_t
        self.radius = 0.0
        self.target_energy = min(energy * DYN_ENERGY_BOOST, 1.8)
        self.envelope = 0.0
        self.age = 0.0
        self.last_onset_time = current_time
        self.color_offset = color_offset

        e_frac = min(energy, 1.0)
        self.width_core = DYN_RIPPLE_WIDTH_MIN + e_frac * (DYN_RIPPLE_WIDTH_MAX - DYN_RIPPLE_WIDTH_MIN)
        self.width_halo = DYN_RIPPLE_HALO_MIN + e_frac * (DYN_RIPPLE_HALO_MAX - DYN_RIPPLE_HALO_MIN)

    def boost(self, energy, current_time):
        new_target = min(energy * DYN_ENERGY_BOOST, 1.8)
        if new_target > self.target_energy:
            self.target_energy = new_target
            e_frac = min(energy, 1.0)
            self.width_core = max(self.width_core,
                                  DYN_RIPPLE_WIDTH_MIN + e_frac * (DYN_RIPPLE_WIDTH_MAX - DYN_RIPPLE_WIDTH_MIN))
            self.width_halo = max(self.width_halo,
                                  DYN_RIPPLE_HALO_MIN + e_frac * (DYN_RIPPLE_HALO_MAX - DYN_RIPPLE_HALO_MIN))
        self.last_onset_time = current_time


def dynamic_tick_ripples(ripples, dt, bass, mid, high,
                         perimeter_t, last_spawn_time, current_time,
                         prev_bass=0.0, side_t_ranges=None,
                         attack=0.5, release=0.5, sensitivity=1.0,
                         raw_bass=None, prev_raw_bass=0.0):
    """Dynamic Slot + Envelope Follower 업데이트."""
    env_attack = DYN_ENV_ATTACK_MIN + attack * (DYN_ENV_ATTACK_MAX - DYN_ENV_ATTACK_MIN)
    env_release = DYN_ENV_RELEASE_MAX - release * (DYN_ENV_RELEASE_MAX - DYN_ENV_RELEASE_MIN)

    for r in ripples:
        r.radius += DYN_RIPPLE_SPEED * dt
        r.age += dt
        r.target_energy *= (1.0 - env_release * dt)
        if r.envelope < r.target_energy:
            r.envelope += env_attack * dt
            r.envelope = min(r.envelope, r.target_energy)
        else:
            r.envelope *= (1.0 - env_release * dt)

    ripples[:] = [r for r in ripples if r.envelope > DYN_SLOT_DEATH_THRESHOLD]

    onset_bass = raw_bass if raw_bass is not None else bass
    onset_prev = prev_raw_bass if raw_bass is not None else prev_bass

    onset_threshold = DYN_ONSET_DELTA_BASE / max(sensitivity, 0.1)
    bass_delta = onset_bass - onset_prev

    if (bass_delta > onset_threshold
            and onset_bass > DYN_ONSET_BASS_MIN):

        energy = max(bass, onset_bass * 0.7)

        most_recent = None
        if ripples:
            most_recent = max(ripples, key=lambda r: r.last_onset_time)

        if (most_recent is not None
                and (current_time - most_recent.last_onset_time) < DYN_REFRACTORY):
            most_recent.boost(energy, current_time)
        elif (current_time - last_spawn_time) > DYN_COOLDOWN:
            center = _pick_position_with_spacing(side_t_ranges, ripples)
            if center is not None and len(ripples) < DYN_MAX_SLOTS:
                color_off = (current_time * 0.2) % 1.0
                ripples.append(DynamicRipple(center, energy, current_time, color_off))
                last_spawn_time = current_time
            elif center is None and len(ripples) < DYN_MAX_SLOTS:
                fallback = _pick_position_proportional(side_t_ranges)
                color_off = (current_time * 0.2) % 1.0
                ripples.append(DynamicRipple(fallback, energy, current_time, color_off))
                last_spawn_time = current_time
            elif ripples:
                weakest = min(ripples, key=lambda r: r.envelope)
                if energy * DYN_ENERGY_BOOST > weakest.envelope * 2.0:
                    weakest.boost(energy, current_time)
                    last_spawn_time = current_time

    return last_spawn_time


def _circular_distance(a, b):
    d = abs(a - b)
    return min(d, 1.0 - d)


def _pick_position_with_spacing(side_t_ranges, ripples, max_attempts=5):
    if side_t_ranges is None:
        return np.random.random()
    valid = []
    weights = []
    for side, (t_min, t_max) in side_t_ranges.items():
        length = t_max - t_min
        if length > 0:
            valid.append((t_min, t_max))
            weights.append(length)
    if not valid:
        return np.random.random()
    total = sum(weights)
    probs = [w / total for w in weights]
    for _ in range(max_attempts):
        idx = np.random.choice(len(valid), p=probs)
        t_min, t_max = valid[idx]
        candidate = np.random.uniform(t_min, t_max)
        too_close = False
        for r in ripples:
            if _circular_distance(candidate, r.center_t) < DYN_MIN_DISTANCE:
                too_close = True
                break
        if not too_close:
            return candidate
    return None


def _pick_position_proportional(side_t_ranges):
    if side_t_ranges is None:
        return np.random.random()
    valid = []
    weights = []
    for side, (t_min, t_max) in side_t_ranges.items():
        length = t_max - t_min
        if length > 0:
            valid.append((t_min, t_max))
            weights.append(length)
    if not valid:
        return np.random.random()
    total = sum(weights)
    probs = [w / total for w in weights]
    idx = np.random.choice(len(valid), p=probs)
    t_min, t_max = valid[idx]
    return np.random.uniform(t_min, t_max)


def vectorized_render_dynamic(base_colors, perimeter_t, ripples,
                              high, min_brightness, audio_brightness,
                              min_brightness_mode="floor"):
    """Dynamic envelope follower 렌더링.

    min_brightness_mode:
        "floor": 바닥이 min_b, ripple 기여가 그 위에 additive
        "remap": ripple 합산(0~1) → min_b + raw * (1 - min_b) 리매핑
    """
    n_leds = len(perimeter_t)

    # ── raw intensity: ripple 기여만 누적 (바닥 0) ──
    raw_intensity = np.zeros(n_leds, dtype=np.float64)

    for r in ripples:
        delta = np.abs(perimeter_t - r.center_t)
        delta = np.minimum(delta, 1.0 - delta)

        esc = r.width_core + r.radius * DYN_RIPPLE_EXPAND_CORE
        esh = r.width_halo + r.radius * DYN_RIPPLE_EXPAND_HALO
        esc_sq = 2.0 * esc * esc
        esh_sq = 2.0 * esh * esh

        e = r.envelope
        core = e * np.exp(-(delta * delta) / esc_sq)
        halo = e * 0.35 * np.exp(-(delta * delta) / esh_sq)
        raw_intensity += core + halo

    np.minimum(raw_intensity, 1.0, out=raw_intensity)

    # ── min_brightness 적용 ──
    intensity = _apply_min_brightness_array(
        min_brightness, raw_intensity, min_brightness_mode
    )

    intensity *= audio_brightness
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)
