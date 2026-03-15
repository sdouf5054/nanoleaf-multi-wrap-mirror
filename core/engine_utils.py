"""엔진 유틸리티 — LED 둘레 매핑, 밴드 계산, 구역 매핑, 공용 상수

[ADR-014 적용] 벡터화된 오디오 렌더링 헬퍼 함수 추가
- build_base_color_array(): 전체 LED 색상 배열을 한 번에 생성
- vectorized_render_pulse(): Python 루프 없이 펄스 렌더링
- vectorized_render_spectrum(): Python 루프 없이 스펙트럼 렌더링

순수 numpy 모듈. Qt 의존성 없음.
"""

import numpy as np
from core.layout import get_led_positions

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
#  유틸리티 함수
# ══════════════════════════════════════════════════════════════════

def _remap_t(t, zone_weights):
    """균등 둘레 비율 t(0~1)를 대역 비율에 맞게 색상/밴드 t로 변환."""
    b_pct = zone_weights[0] / 100.0
    m_pct = zone_weights[1] / 100.0
    h_pct = zone_weights[2] / 100.0

    t_bound1 = b_pct
    t_bound2 = b_pct + m_pct

    c0, c1 = 0.0, 1.0 / 3.0
    c2, c3 = 1.0 / 3.0, 2.0 / 3.0
    c4, c5 = 2.0 / 3.0, 1.0

    t = max(0.0, min(1.0, t))

    if t <= t_bound1 and b_pct > 0:
        frac = t / b_pct
        return c0 + frac * (c1 - c0)
    elif t <= t_bound2 and m_pct > 0:
        frac = (t - t_bound1) / m_pct
        return c2 + frac * (c3 - c2)
    elif h_pct > 0:
        frac = (t - t_bound2) / h_pct
        return c4 + frac * (c5 - c4)
    else:
        return t


def _compute_led_perimeter_t(config):
    """각 LED의 균등 둘레 비율 t(0~1)를 계산."""
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    cx = screen_w / 2.0
    half_bottom = screen_w / 2.0
    side_height = screen_h
    half_top = screen_w / 2.0
    half_perimeter = half_bottom + side_height + half_top

    perimeter_t = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        x, y = positions[i, 0], positions[i, 1]
        side = sides[i]

        if side == "bottom":
            dist = abs(x - cx)
        elif side == "left" or side == "right":
            dist = half_bottom + (screen_h - y)
        elif side == "top":
            dist_to_center = abs(x - cx)
            dist = half_bottom + side_height + (half_top - dist_to_center)
        else:
            dist = 0.0

        t = dist / half_perimeter if half_perimeter > 0 else 0.5
        perimeter_t[i] = max(0.0, min(1.0, t))

    return perimeter_t


def _compute_led_band_mapping(perimeter_t, n_bands, zone_weights):
    """둘레 비율 + 대역 비율 → 각 LED의 밴드 인덱스."""
    led_count = len(perimeter_t)
    band_indices = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        remapped = _remap_t(perimeter_t[i], zone_weights)
        band_indices[i] = remapped * (n_bands - 1)

    return band_indices


def _build_led_order_from_segments(segments, led_count):
    """세그먼트 순서대로 LED 인덱스의 물리적 순서를 반환."""
    order = []
    for seg in segments:
        start, end = seg["start"], seg["end"]
        if start > end:
            indices = list(range(start, end, -1))
        elif start < end:
            indices = list(range(start, end))
        else:
            continue
        for idx in indices:
            if idx not in order and 0 <= idx < led_count:
                order.append(idx)
    for i in range(led_count):
        if i not in order:
            order.append(i)
    return order


def _build_led_zone_map_by_side(config, n_zones):
    """각 LED가 어느 screen zone에 매핑되는지 계산."""
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    mapping = np.zeros(led_count, dtype=np.int32)

    if n_zones == 1:
        pass

    elif n_zones == 2:
        cy = screen_h / 2.0
        for i in range(led_count):
            side = sides[i]
            if side == "top":
                mapping[i] = 0
            elif side == "bottom":
                mapping[i] = 1
            elif side in ("left", "right"):
                y = positions[i, 1]
                mapping[i] = 0 if y <= cy else 1
            else:
                mapping[i] = 0

    elif n_zones == 4:
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            if x <= cx:
                mapping[i] = 0 if y <= cy else 3  # 좌상=0, 좌하=3
            else:
                mapping[i] = 1 if y <= cy else 2  # 우상=1, 우하=2

    elif n_zones == 8:
        cx, cy = screen_w / 2.0, screen_h / 2.0
        for i in range(led_count):
            x, y = positions[i]
            side = sides[i]
            if side == "top":
                mapping[i] = 0 if x <= cx else 1
            elif side == "right":
                mapping[i] = 2 if y <= cy else 3
            elif side == "bottom":
                mapping[i] = 4 if x >= cx else 5
            elif side == "left":
                mapping[i] = 6 if y >= cy else 7
            else:
                mapping[i] = 0

    else:
        for i in range(led_count):
            side = sides[i]
            x, y = positions[i]

            if side == "top":
                progress = x / screen_w if screen_w > 0 else 0.5
                cw_t = 0.00 + progress * 0.25
            elif side == "right":
                progress = y / screen_h if screen_h > 0 else 0.5
                cw_t = 0.25 + progress * 0.25
            elif side == "bottom":
                progress = 1.0 - (x / screen_w if screen_w > 0 else 0.5)
                cw_t = 0.50 + progress * 0.25
            elif side == "left":
                progress = 1.0 - (y / screen_h if screen_h > 0 else 0.5)
                cw_t = 0.75 + progress * 0.25
            else:
                cw_t = 0.0

            cw_t = max(0.0, min(cw_t, 0.9999))
            mapping[i] = int(cw_t * n_zones)

    return mapping


def per_led_to_zone_colors(per_led_colors, zone_map, n_zones):
    """per-LED 색상 배열에서 구역별 평균 색상을 계산."""
    zone_colors = np.zeros((n_zones, 3), dtype=np.float32)
    zone_counts = np.zeros(n_zones, dtype=np.int32)

    for i in range(len(per_led_colors)):
        zi = zone_map[i]
        if 0 <= zi < n_zones:
            zone_colors[zi] += per_led_colors[i]
            zone_counts[zi] += 1

    for zi in range(n_zones):
        if zone_counts[zi] > 0:
            zone_colors[zi] /= zone_counts[zi]

    return zone_colors


# ══════════════════════════════════════════════════════════════════
#  [ADR-014] 벡터화된 오디오 렌더링 헬퍼
# ══════════════════════════════════════════════════════════════════

# 무지개 키포인트 (AudioEngineMixin._band_color과 동일)
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
    """밴드 위치 배열 (n,) → RGB 배열 (n, 3).

    Python 루프 대신 numpy 벡터 연산으로 전체 LED의 무지개 색상을 한 번에 계산.
    """
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
        # (k, 1) * (3,) broadcast
        result[mask] = rgb0 + frac[:, np.newaxis] * (rgb1 - rgb0)

    return result


def build_base_color_array(led_band_indices, n_bands, rainbow=True,
                           solid_color=None, screen_colors=None):
    """전체 LED의 기본 색상 배열을 한 번에 생성.

    [ADR-014] 매 프레임 Python 루프를 제거. 색상/모드 변경 시에만 재빌드.

    Args:
        led_band_indices: (n_leds,) float64 — LED별 밴드 인덱스
        n_bands: 총 밴드 수
        rainbow: 무지개 모드 여부
        solid_color: (3,) float32 — 단색 RGB (rainbow=False일 때)
        screen_colors: (n_leds, 3) float32 — 화면 색상 (하이브리드)

    Returns:
        (n_leds, 3) float32 — LED별 기본 RGB 색상
    """
    n_leds = len(led_band_indices)

    if screen_colors is not None:
        return screen_colors.copy()

    if rainbow:
        t = led_band_indices / max(1, n_bands - 1)
        return band_color_vectorized(t)

    # 단색
    if solid_color is not None:
        return np.broadcast_to(solid_color, (n_leds, 3)).copy()

    return np.full((n_leds, 3), 255.0, dtype=np.float32)


def vectorized_render_pulse(base_colors, bass, mid, high,
                            min_brightness, audio_brightness):
    """[ADR-014] 벡터화된 Pulse 렌더링.

    밝기 모델:
    - bass → 전체 밝기 (intensity)
    - mid → 채도 부스트 (1.0~1.3배, 중역 강할수록 색이 선명)
    - high → 화이트 틴트 (고역 강할수록 밝은 반짝임)
    - min_brightness=1.0이면 미러링 brightness=100%와 동일한 밝기

    Args:
        base_colors: (n_leds, 3) float32
        bass, mid, high: float 0~1
        min_brightness, audio_brightness: float

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB
    """
    intensity = max(min_brightness, bass) * audio_brightness

    # mid → 채도 부스트: 보컬/기타가 들릴 때 색이 풍부해짐
    saturation_boost = 1.0 + mid * 0.3
    leds = np.clip(base_colors * saturation_boost, 0, 255)

    # high → 화이트 틴트: 하이햇/심벌에 반짝임
    white_mix = high * 0.15
    leds = leds * (1.0 - white_mix) + 255.0 * white_mix

    leds *= intensity
    return leds


def vectorized_render_spectrum(base_colors, led_band_indices, spectrum,
                               min_brightness, audio_brightness):
    """[ADR-014] 벡터화된 Spectrum 렌더링.

    Python 루프 없이 전체 LED를 한 번에 계산.

    Args:
        base_colors: (n_leds, 3) float32
        led_band_indices: (n_leds,) float64
        spectrum: (n_bands,) float64
        min_brightness, audio_brightness: float

    Returns:
        (n_leds, 3) float32 — 보정 전 raw RGB
    """
    n_bands = len(spectrum)

    # 밴드 인덱스에서 에너지를 보간하여 추출
    band_lo = np.clip(np.floor(led_band_indices).astype(np.int32), 0, n_bands - 1)
    band_hi = np.clip(band_lo + 1, 0, n_bands - 1)
    frac = led_band_indices - np.floor(led_band_indices)

    energy = spectrum[band_lo] * (1.0 - frac) + spectrum[band_hi] * frac
    intensity = np.maximum(min_brightness, energy) * audio_brightness

    # (n_leds, 3) * (n_leds, 1) broadcast
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)


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
#  LED 정규화 y좌표 (Wave/Dynamic용)
# ══════════════════════════════════════════════════════════════════

def compute_led_normalized_y(config):
    """Wave 모드용 LED 전파 위치 — 둘레 경로 기반.

    하단 중앙(0.0) → 좌측은 시계방향 / 우측은 반시계방향으로
    모니터 둘레를 타고 올라가서 → 상단 중앙(1.0)에서 합류.

    Returns:
        (n_leds,) float64 — 0(하단 중앙) ~ 1(상단 중앙)
    """
    return _compute_led_perimeter_t(config)


# ══════════════════════════════════════════════════════════════════
#  [Wave 모드] 펄스 큐 + 렌더링
# ══════════════════════════════════════════════════════════════════

# Wave 파라미터 상수
WAVE_SPEED_DEFAULT = 1.4   # 기본 속도 (UI 슬라이더 50%)
WAVE_SPEED_MIN = 0.4       # 슬라이더 0%
WAVE_SPEED_MAX = 8.0       # 슬라이더 100%
WAVE_WIDTH_FRONT = 0.07    # 펄스 앞쪽 폭
WAVE_WIDTH_TRAIL = 0.15    # 펄스 뒤쪽 잔상 폭
WAVE_DECAY = 0.45          # 초당 밝기 감쇠
WAVE_COOLDOWN = 0.06       # 펄스 간 최소 간격 — 짧게 (Pulse 반응성 매칭)
WAVE_MAX_PULSES = 8        # 동시 최대 펄스 수
WAVE_ENERGY_BOOST = 1.8    # 초기 에너지 배수
WAVE_TILT_COMP = 0.5       # 상단 밝기 보정
WAVE_ONSET_DELTA = 0.02    # bass 상승 감지 최소 변화량 — 거의 모든 비트에 반응


def wave_speed_from_slider(slider_pct):
    """슬라이더 값(0~100) → 실제 wave speed 변환.

    0%   → 0.4  (느림)
    50%  → 1.4  (기본)
    100% → 3.0  (빠름)
    """
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
    """펄스 큐 업데이트 — Pulse 모드와 동일한 반응성.

    Pulse 모드: intensity = max(min_brightness, bass) → bass가 곧 밝기.
    Wave 모드: bass가 상승하는 순간(onset) 펄스 생성, bass 값이 곧 에너지.

    onset 감지: 현재 bass > 이전 bass + WAVE_ONSET_DELTA
    → Pulse 모드에서 밝아지는 모든 순간에 Wave 펄스도 생성됨.
    threshold를 제거하고 bass 값 자체가 에너지이므로,
    bass=0.1이면 어두운 펄스, bass=0.9이면 밝은 펄스.

    Args:
        pulses: list[WavePulse] — in-place 변경
        dt: 프레임 시간 (초)
        bass: 현재 bass 에너지 (0~1, Attack/Release 스무딩 적용 후)
        prev_bass: 이전 프레임의 bass 값 (onset 감지용)
        last_spawn_time: 마지막 펄스 생성 시각
        current_time: 현재 monotonic 시각
        speed: 펄스 이동 속도

    Returns:
        last_spawn_time (갱신된)
    """
    # 기존 펄스 진행
    for p in pulses:
        p.position += speed * dt
        p.age += dt
        p.energy *= (1.0 - WAVE_DECAY * dt)

    pulses[:] = [p for p in pulses
                 if p.position < 1.6 and p.energy > 0.02]

    # onset 감지: bass가 상승 중이면 펄스 생성
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
                           speed=WAVE_SPEED_DEFAULT):
    """[Wave 모드] 비대칭 가우시안 + additive blending + 기울기 보정.

    속도 적응형 펄스 폭: speed가 높을수록 σ를 넓혀서
    빠른 이동 시 motion blur 효과를 내고 점멸을 방지.

    Args:
        base_colors: (n_leds, 3) float32
        led_norm_y: (n_leds,) float64 — 0(하단 중앙)~1(상단 중앙), 둘레 경로
        pulses: list[WavePulse]
        min_brightness, audio_brightness: float
        speed: 현재 wave 속도 (폭 스케일링에 사용)

    Returns:
        (n_leds, 3) float32
    """
    n_leds = len(led_norm_y)
    intensity = np.full(n_leds, min_brightness, dtype=np.float64)

    # 속도 적응형 폭: 기준 속도(1.4) 대비 비율로 σ를 스케일
    # speed=1.4 → 1.0배, speed=3.0 → ~1.6배, speed=5.0 → ~2.3배
    speed_ratio = max(speed / WAVE_SPEED_DEFAULT, 1.0)
    width_scale = speed_ratio ** 0.7  # 제곱근 비례 — 너무 급격히 넓어지지 않게

    sf = WAVE_WIDTH_FRONT * width_scale
    st = WAVE_WIDTH_TRAIL * width_scale
    two_sf_sq = 2.0 * sf * sf
    two_st_sq = 2.0 * st * st

    for p in pulses:
        dist = led_norm_y - p.position
        # 비대칭: 앞쪽(위) 좁고 뒤쪽(아래) trail
        two_sigma_sq = np.where(dist >= 0, two_sf_sq, two_st_sq)
        contribution = p.energy * np.exp(-(dist * dist) / two_sigma_sq)
        intensity += contribution  # additive

    # 기울기 보정
    tilt_factor = 1.0 + led_norm_y * WAVE_TILT_COMP
    intensity *= tilt_factor

    intensity *= audio_brightness
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)


# ══════════════════════════════════════════════════════════════════
#  [Dynamic 모드] v4 — Slot + Envelope Follower
#
#  핵심 컨셉 변경:
#  - 기존: onset → spawn ripple → ripple 자체가 attack/release로 밝아졌다 꺼짐
#  - 신규: onset → slot(위치)을 할당 → slot의 밝기는 매 프레임 bass envelope을 따라감
#
#  Pulse 모드와의 유사성:
#  - Pulse: 매 프레임 intensity = bass → 전체 LED가 bass를 따라감
#  - Dynamic v4: 매 프레임 각 slot의 intensity가 bass를 따라감, 위치만 onset 시 결정
#
#  같은 비트의 연속 onset:
#  - 새 ripple을 만들지 않고, 가장 최근 slot의 energy를 boost
#  - "refractory period" — onset 후 일정 시간은 같은 slot에 축적
#
#  자연스러운 소멸:
#  - slot의 envelope이 threshold 이하로 내려가면 자연 소멸
#  - 교체/즉사 없음 → 시각적 불연속 제거
# ══════════════════════════════════════════════════════════════════

# ── Dynamic 파라미터 상수 ──
DYN_ONSET_DELTA_BASE = 0.012   # 기본 onset 감지 최소 변화량
DYN_ONSET_BASS_MIN = 0.12     # ★ onset_bass 절대값 최소 (AGC 노이즈 필터)
DYN_REFRACTORY = 0.10          # onset 후 이 시간 내 재onset → 기존 slot boost (초)
DYN_COOLDOWN = 0.03            # 새 slot 생성 최소 간격 (boost는 이 제한 없음)
DYN_MAX_SLOTS = 8              # 동시 최대 slot 수
DYN_MIN_DISTANCE = 0.18        # 새 slot과 기존 slot 간 최소 거리

# 파원 크기 — 에너지에 따라 스케일링
DYN_RIPPLE_WIDTH_MIN = 0.03    # 약한 비트: core σ  LED
DYN_RIPPLE_WIDTH_MAX = 0.06    # 강한 비트: core σ  LED
DYN_RIPPLE_HALO_MIN = 0.08     # 약한 비트: halo σ  LED
DYN_RIPPLE_HALO_MAX = 0.12     # 강한 비트: halo σ  LED

DYN_RIPPLE_SPEED = 0.2         # 미세 확산 (제자리 느낌)
DYN_RIPPLE_EXPAND_CORE = 0.02
DYN_RIPPLE_EXPAND_HALO = 0.04

# Envelope follower — asymmetric IIR (fast attack / slow release)
# UI attack/release 슬라이더가 이 값을 스케일링
DYN_ENV_ATTACK_MIN = 3.0       # attack=0: 보통 속도로 밝아짐
DYN_ENV_ATTACK_MAX = 25.0      # attack=1: 즉시 밝아짐
DYN_ENV_RELEASE_MIN = 0.5      # release=0: 빨리 꺼짐 (초당 감쇠율)
DYN_ENV_RELEASE_MAX = 4.0      # release=1: 아주 천천히 꺼짐

DYN_ENERGY_BOOST = 1.4         # 초기 에너지 배수
DYN_SPARKLE_PROB = 0.10        # sparkle 확률
DYN_SLOT_DEATH_THRESHOLD = 0.015  # 이 이하면 slot 소멸

_SIDES = ("bottom", "left", "top", "right")


def _compute_led_clockwise_t(config):
    """[Dynamic 전용] 시계방향 한 바퀴 둘레 좌표 — 모든 LED가 고유 위치.

    상단 좌측 코너(0.0) → 상단(→) → 우측(↓) → 하단(←) → 좌측(↑) → (1.0)
    """
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    full_perimeter = 2.0 * (screen_w + screen_h)
    if full_perimeter <= 0:
        return np.linspace(0, 1, led_count)

    clockwise_t = np.zeros(led_count, dtype=np.float64)

    for i in range(led_count):
        x, y = positions[i, 0], positions[i, 1]
        side = sides[i]

        if side == "top":
            dist = x
        elif side == "right":
            dist = screen_w + y
        elif side == "bottom":
            dist = screen_w + screen_h + (screen_w - x)
        elif side == "left":
            dist = 2.0 * screen_w + screen_h + (screen_h - y)
        else:
            dist = 0.0

        clockwise_t[i] = max(0.0, min(dist / full_perimeter, 0.9999))

    return clockwise_t


def compute_side_t_ranges(config):
    """각 면의 clockwise_t 범위를 계산."""
    layout_cfg = config["layout"]
    mirror_cfg = config.get("mirror", {})
    dev_cfg = config["device"]
    led_count = dev_cfg["led_count"]

    screen_w = mirror_cfg.get("grid_cols", 64) * 40
    screen_h = mirror_cfg.get("grid_rows", 32) * 40

    positions, sides = get_led_positions(
        screen_w, screen_h,
        layout_cfg["segments"], led_count,
        orientation=mirror_cfg.get("orientation", "auto"),
        portrait_rotation=mirror_cfg.get("portrait_rotation", "cw"),
    )

    cw_t = _compute_led_clockwise_t(config)

    side_ranges = {}
    for side in _SIDES:
        t_vals = [cw_t[i] for i in range(led_count) if sides[i] == side]
        if t_vals:
            side_ranges[side] = (min(t_vals), max(t_vals))
        else:
            side_ranges[side] = (0.0, 0.0)

    return side_ranges


class DynamicRipple:
    """하나의 Dynamic slot.

    v4: envelope follower 패턴.
    - target_energy: onset 시 설정되는 "이 slot이 도달해야 할 밝기"
    - envelope: 매 프레임 attack/release로 target을 추적하는 현재 밝기
    - onset이 반복되면 target이 갱신(boost)될 뿐, slot이 새로 생기지 않음
    """
    __slots__ = ("center_t", "radius", "target_energy", "envelope",
                 "age", "last_onset_time", "color_offset",
                 "width_core", "width_halo")

    def __init__(self, center_t, energy, current_time, color_offset=0.0):
        self.center_t = center_t
        self.radius = 0.0
        self.target_energy = min(energy * DYN_ENERGY_BOOST, 1.8)
        self.envelope = 0.0  # envelope follower — attack rate로 target 추적
        self.age = 0.0
        self.last_onset_time = current_time
        self.color_offset = color_offset

        # 에너지 비례 파원 크기
        e_frac = min(energy, 1.0)
        self.width_core = DYN_RIPPLE_WIDTH_MIN + e_frac * (DYN_RIPPLE_WIDTH_MAX - DYN_RIPPLE_WIDTH_MIN)
        self.width_halo = DYN_RIPPLE_HALO_MIN + e_frac * (DYN_RIPPLE_HALO_MAX - DYN_RIPPLE_HALO_MIN)

    def boost(self, energy, current_time):
        """같은 비트의 연속 onset — 기존 slot의 target을 갱신."""
        new_target = min(energy * DYN_ENERGY_BOOST, 1.8)
        # target은 현재보다 높을 때만 갱신 (이미 밝으면 유지)
        if new_target > self.target_energy:
            self.target_energy = new_target
            # 크기도 갱신 (더 강한 비트 → 더 큰 파원)
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
    """v4: Slot + Envelope Follower.

    매 프레임:
    1. 모든 slot의 envelope을 bass 기반으로 업데이트 (attack/release)
    2. onset 감지 시:
       a. refractory 기간 내 → 가장 최근 slot을 boost
       b. refractory 지남 → 새 slot 생성 (spacing 적용)
    3. envelope이 threshold 이하인 slot은 자연 소멸
    """
    # attack/release → envelope follower rate
    env_attack = DYN_ENV_ATTACK_MIN + attack * (DYN_ENV_ATTACK_MAX - DYN_ENV_ATTACK_MIN)
    env_release = DYN_ENV_RELEASE_MAX - release * (DYN_ENV_RELEASE_MAX - DYN_ENV_RELEASE_MIN)
    # release=0 → 4.0 (빨리 꺼짐), release=1 → 0.5 (천천히 꺼짐)

    # ── 1. 모든 slot의 envelope 업데이트 ──
    for r in ripples:
        r.radius += DYN_RIPPLE_SPEED * dt
        r.age += dt

        # target은 release rate로 자연 감쇠
        r.target_energy *= (1.0 - env_release * dt)

        # envelope은 target을 향해 asymmetric하게 추적
        if r.envelope < r.target_energy:
            # attack: 빠르게 따라 올라감
            r.envelope += env_attack * dt
            r.envelope = min(r.envelope, r.target_energy)
        else:
            # release: 천천히 따라 내려감
            r.envelope *= (1.0 - env_release * dt)

    # ── 2. 자연 소멸 ──
    ripples[:] = [r for r in ripples if r.envelope > DYN_SLOT_DEATH_THRESHOLD]

    # ── 3. onset 감지 ──
    onset_bass = raw_bass if raw_bass is not None else bass
    onset_prev = prev_raw_bass if raw_bass is not None else prev_bass

    onset_threshold = DYN_ONSET_DELTA_BASE / max(sensitivity, 0.1)
    bass_delta = onset_bass - onset_prev

    # ★ onset 조건:
    # 1. delta가 threshold 초과 — sensitivity가 threshold만 조절
    # 2. onset_bass가 고정 최소값(0.20) 이상 — AGC 노이즈 필터
    if (bass_delta > onset_threshold
            and onset_bass > DYN_ONSET_BASS_MIN):

        energy = max(bass, onset_bass * 0.7)

        # refractory 확인: 최근 onset이 있었으면 그 slot을 boost
        most_recent = None
        if ripples:
            most_recent = max(ripples, key=lambda r: r.last_onset_time)

        if (most_recent is not None
                and (current_time - most_recent.last_onset_time) < DYN_REFRACTORY):
            # ★ 같은 비트 → 기존 slot boost (새 ripple 안 만듦)
            most_recent.boost(energy, current_time)
        elif (current_time - last_spawn_time) > DYN_COOLDOWN:
            # 새 slot 생성
            center = _pick_position_with_spacing(side_t_ranges, ripples)
            if center is not None and len(ripples) < DYN_MAX_SLOTS:
                color_off = (current_time * 0.2) % 1.0
                ripples.append(DynamicRipple(center, energy, current_time, color_off))
                last_spawn_time = current_time
            elif center is None and len(ripples) < DYN_MAX_SLOTS:
                # spacing 실패 → 랜덤 위치로 완화 (비트를 놓치지 않기 위해)
                fallback = _pick_position_proportional(side_t_ranges)
                color_off = (current_time * 0.2) % 1.0
                ripples.append(DynamicRipple(fallback, energy, current_time, color_off))
                last_spawn_time = current_time
            elif ripples:
                # slot 꽉 참 → 가장 어두운 slot을 boost (교체 아님!)
                weakest = min(ripples, key=lambda r: r.envelope)
                if energy * DYN_ENERGY_BOOST > weakest.envelope * 2.0:
                    weakest.boost(energy, current_time)
                    last_spawn_time = current_time

    return last_spawn_time


def _circular_distance(a, b):
    """둘레 좌표 0~1에서의 circular 거리 (최대 0.5)."""
    d = abs(a - b)
    return min(d, 1.0 - d)


def _pick_position_with_spacing(side_t_ranges, ripples, max_attempts=5):
    """면 길이 비례 위치 + 기존 slot과 최소 거리 유지."""
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
    """면 길이 비례 위치 (spacing 없이)."""
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
                              high, min_brightness, audio_brightness):
    """v4: envelope follower 기반 렌더링.

    각 slot의 envelope 값이 곧 밝기 — Pulse처럼 부드러운 밝기 변화.
    """
    n_leds = len(perimeter_t)
    intensity = np.full(n_leds, min_brightness, dtype=np.float64)

    for r in ripples:
        delta = np.abs(perimeter_t - r.center_t)
        delta = np.minimum(delta, 1.0 - delta)

        esc = r.width_core + r.radius * DYN_RIPPLE_EXPAND_CORE
        esh = r.width_halo + r.radius * DYN_RIPPLE_EXPAND_HALO
        esc_sq = 2.0 * esc * esc
        esh_sq = 2.0 * esh * esh

        # ★ envelope 사용 — attack/release가 반영된 부드러운 밝기
        e = r.envelope
        core = e * np.exp(-(delta * delta) / esc_sq)
        halo = e * 0.35 * np.exp(-(delta * delta) / esh_sq)
        intensity += core + halo

    # per-LED soft clamp
    np.minimum(intensity, 1.0, out=intensity)

    intensity *= audio_brightness
    leds = base_colors * intensity[:, np.newaxis]

    return leds.astype(np.float32)