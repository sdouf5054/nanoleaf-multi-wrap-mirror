"""UnifiedEngine — 4가지 토글 조합을 단일 엔진으로 처리 (Phase 7)

display_enabled / audio_enabled 플래그로 모든 모드를 통합.

[동작 모드]
  D=OFF, A=OFF  → 정적/애니메이션 색상 출력 (plain LED)
  D=ON,  A=OFF  → 미러링 전용 (화면 색 → LED)
  D=OFF, A=ON   → 오디오 비주얼라이저 (사용자 색상 + 오디오 반응)
  D=ON,  A=ON   → 하이브리드 (화면 색 + 오디오 반응)

[미디어 연동 v2 — 캡처 소스 교체 방식]
  media_color_enabled = True (display_enabled=True 필수):
  - 화면 캡처 프레임 대신 앨범 아트 이미지 프레임을 파이프라인에 투입
  - _grab_frame(ep)로 소스 선택 → 이후 파이프라인 완전 동일
  - 구역 수, 추출 방식, 스무딩, 색상 효과가 전부 그대로 작동
  - 하이브리드(D+A ON)에서도 동일: flowing 포함 모든 오디오 모드 적용

[미디어 소스 자동판별 v6 — 2-phase + audio idle]
  (생략 — 기존과 동일)

[★ Mirror Flowing 추가]
  D=ON, A=OFF, color_effect="flowing" 일 때:
  - FlowPalette를 사용하여 화면 색이 LED 둘레를 회전
  - bass=0, mid=0, high=0으로 호출 → 오디오 반응 없이 일정한 밝기
  - 화면 색 갱신은 기존 weight_matrix 경로 재사용
  - _frame_mirror_only()에서 flowing 분기로 진입

[Refactor] _run_loop() 분해 + MirrorFrameResult 구조화
(생략 — 기존과 동일)
"""

import time
from dataclasses import dataclass
from typing import Optional
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.color import ColorPipeline
from core.audio_engine import AudioEngine as AudioCapture, _build_log_bands
from core.constants import HW_ERRORS
from core.platform import get_primary_resolution
from core.engine_utils import (
    N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    COLOR_EFFECT_STATIC,
    COLOR_EFFECT_FLOWING,      # ★MIRROR-FLOWING
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _compute_led_clockwise_t,
    _build_led_order_from_segments, _build_led_zone_map_by_side,
    per_led_to_zone_colors, compute_side_t_ranges,
    build_base_color_array, build_base_color_array_animated,
    vectorized_render_pulse, vectorized_render_spectrum,
    vectorized_render_wave, vectorized_render_dynamic,
    leds_to_grb, compute_led_normalized_y,
    WavePulse, wave_tick_pulses,
    DynamicRipple, dynamic_tick_ripples,
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    AUDIO_WAVE, AUDIO_DYNAMIC, AUDIO_FLOWING,
    apply_mirror_gradient_modulation,
    _has_mirror_gradient_effect,
    GradientPhase,
)
from core.color_extract import extract_zone_dominant
from core.vivid_extract import (
    build_led_region_masks,
    boost_per_led_vivid,
    smooth_per_led,
)
from core.flowing import FlowPalette, render_flowing

# ★ 미디어 연동 import (v2: 프레임 제공 방식)
from core.media_session import MediaFrameProvider, HAS_MEDIA_SESSION

# ★ dxcam fallback용 cv2 import
import cv2

# ── stale detection 상수 ──
_STALE_THRESHOLD = 3.0
_STALE_RECREATE_COOLDOWN = 3.0
_STALE_LED_OFF_THRESHOLD = 10.0

# ── ★ 미디어 소스 자동판별 상수 (v6: 2-phase + audio idle) ──
MEDIA_DETECT_INTERVAL = 0.1

MEDIA_DETECT_DURATION = 3.0
MEDIA_DETECT_PHASE1_MSE_THRESHOLD = 8.0
MEDIA_DETECT_PHASE1_DYNAMIC_COUNT = 8

MEDIA_DETECT_PHASE2_MSE_THRESHOLD = 50.0
MEDIA_DETECT_PHASE2_DYNAMIC_COUNT = 20
MEDIA_DETECT_PHASE2_STATIC_COUNT  = 25

MEDIA_AUDIO_IDLE_THRESHOLD = 0.02
MEDIA_AUDIO_IDLE_COUNT = 25
MEDIA_AUDIO_RESUME_THRESHOLD = 0.05
MEDIA_AUDIO_RESUME_COUNT = 32

# ── ★MIRROR-FLOWING: 미러링 flowing 화면 색 갱신 주기 (프레임 수) ──
_MIRROR_FLOW_SCREEN_UPDATE_INTERVAL = 3


# ══════════════════════════════════════════════════════════════════
#  MirrorFrameResult
# ══════════════════════════════════════════════════════════════════

@dataclass
class MirrorFrameResult:
    """미러링 전용 프레임 처리 결과."""
    raw_preview: np.ndarray
    grb_data: bytes
    prev_colors: Optional[np.ndarray]
    last_good_frame_time: float
    led_turned_off: bool


def _ar(current, target, attack_rate, release_rate):
    """Attack/Release 스무딩."""
    if target > current:
        return current + (target - current) * attack_rate
    else:
        return current + (target - current) * release_rate


class UnifiedEngine(BaseEngine):
    """통합 엔진 — display_enabled/audio_enabled/media_color_enabled 조합 처리."""

    mode = "unified"

    _MEDIA_DEBUG = False

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)

        # ── 오디오 리소스 ──
        self._audio_engine: AudioCapture | None = None
        self._cc: ColorCorrection | None = None

        # ── 상태 메시지 추적 ──
        self._last_status_key = None

        # ── ★ 미디어 전용 경량 오디오 모니터 플래그 ──
        self._audio_monitor_only = False

        # ── 둘레 좌표 ──
        self._perimeter_t = None
        self._clockwise_t = None

        # ── 밴드 매핑 ──
        self._led_band_indices = None
        self._led_order = []

        # ── 오디오 스무딩 상태 ──
        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

        # ── Bass Detail ──
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # ── 화면 색상 (디스플레이 ON) ──
        self._per_led_colors = None
        self._zone_map = None
        self._zone_colors = None
        self._prev_zone_dominant = None

        # ── 미러링 전용 ──
        self._mirror_cc = None
        self._last_brightness = -1.0

        # ── 채도 우선순위 v2 ──
        self._vivid_region_masks = None
        self._prev_ambient_color = None
        self._prev_per_led_vivid = None

        # ── 오디오 base colors 캐시 ──
        self._cached_base_colors = None
        self._cached_rainbow = None
        self._cached_base_color_tuple = None
        self._cached_color_effect = COLOR_EFFECT_STATIC

        # ── Wave 모드 상태 ──
        self._wave_pulses = []
        self._wave_last_spawn = 0.0
        self._wave_prev_bass = 0.0
        self._led_norm_y = None

        # ── Dynamic 모드 상태 ──
        self._dyn_ripples = []
        self._dyn_last_spawn = 0.0
        self._dyn_prev_bass = 0.0
        self._dyn_prev_raw_bass = 0.0
        self._dyn_side_t_ranges = None
        self._dyn_clockwise_t = None

        # ── Flowing 모드 (하이브리드 오디오 flowing) ──
        self._flow_palette = None
        self._flow_last_update = 0.0
        self._flow_palette_colors = None
        self._flow_palette_ratios = None

        # ── ★MIRROR-FLOWING: 미러링 전용 flowing 상태 ──
        self._mirror_flow_palette: Optional[FlowPalette] = None
        self._mirror_flow_last_update = 0.0

        # ── 정적 모드 색상 캐시 ──
        self._static_dirty = True

        # ── 그라데이션 누적 위상 ──
        self._gradient_phase = GradientPhase()

        # ── ★ 미디어 연동 리소스 (v2: 프레임 제공) ──
        self._media_provider: Optional[MediaFrameProvider] = None
        self._prev_media_enabled: bool = False

        # ── ★ 미디어 소스 자동판별 상태 (v6) ──
        self._media_detect_state = "idle"
        self._media_detect_decision = "media"
        self._media_detect_start_time = 0.0
        self._media_detect_last_check = 0.0
        self._media_detect_phase1_dynamic_hits = 0
        self._media_detect_phase2_dynamic_hits = 0
        self._media_detect_phase2_static_hits  = 0
        self._media_detect_audio_idle_hits = 0
        self._media_detect_audio_resume_hits = 0
        self._media_detect_prev_frame: Optional[np.ndarray] = None
        self._media_detect_last_hash = 0
        self._media_detect_last_confirmed = None
        self._prev_media_toggle_count = 0

    # ══════════════════════════════════════════════════════════════
    #  ★ 디버그 메시지 헬퍼 (v6)
    # ══════════════════════════════════════════════════════════════

    def _media_debug(self, msg):
        if self._MEDIA_DEBUG:
            self.status_changed.emit(msg)

    # ══════════════════════════════════════════════════════════════
    #  ★ dxcam fallback 다운샘플 게이트
    # ══════════════════════════════════════════════════════════════

    def _downsample_if_needed(self, frame):
        if frame is None:
            return None
        if getattr(self, '_native_capture', False):
            return frame
        try:
            h, w = frame.shape[:2]
        except (AttributeError, ValueError):
            return frame
        if h == self._active_grid_rows and w == self._active_grid_cols:
            return frame
        return cv2.resize(
            frame,
            (self._active_grid_cols, self._active_grid_rows),
            interpolation=cv2.INTER_LINEAR,
        )

    # ══════════════════════════════════════════════════════════════
    #  ★ 캡처 소스 선택 (v6)
    # ══════════════════════════════════════════════════════════════

    def _grab_frame(self, ep):
        if ep.use_media_frame and self._media_provider is not None:
            media_frame = self._media_provider.get_frame()

            if media_frame is not None:
                override = ep.media_source_override
                if override == "media":
                    return media_frame
                elif override == "mirror":
                    return self._downsample_if_needed(
                        self._capture.grab()
                    ) if self._capture else None

                source = self._resolve_media_source(media_frame)
                if source == "media":
                    return media_frame
                else:
                    return self._downsample_if_needed(
                        self._capture.grab()
                    ) if self._capture else None

        if self._capture is not None:
            return self._downsample_if_needed(self._capture.grab())
        return None

    def _get_audio_energy_total(self):
        if self._audio_engine is None:
            return None
        if self._audio_monitor_only:
            bands = self._audio_engine.get_band_energies()
            return bands["bass"] + bands["mid"] + bands["high"]
        else:
            return self._smooth_bass + self._smooth_mid + self._smooth_high

    def _resolve_media_source(self, media_frame):
        """미디어 소스 자동판별 — 2-phase + audio idle (v6)."""
        now = time.monotonic()

        info = self._media_provider.get_media_info() if self._media_provider else None
        current_hash = hash((info.get("title", ""), info.get("artist", ""))) if info else 0

        if current_hash != self._media_detect_last_hash and current_hash != 0:
            self._media_detect_last_hash = current_hash
            self._media_detect_state = "phase1"
            self._media_detect_decision = (
                self._media_detect_last_confirmed
                if self._media_detect_last_confirmed is not None
                else "media"
            )
            self._media_detect_start_time = now
            self._media_detect_last_check = 0.0
            self._media_detect_phase1_dynamic_hits = 0
            self._media_detect_phase2_dynamic_hits = 0
            self._media_detect_phase2_static_hits  = 0
            self._media_detect_audio_idle_hits = 0
            self._media_detect_prev_frame = None
            self._media_debug(
                f"[미디어] 새 미디어 감지 — Phase 1 판별 시작 ({MEDIA_DETECT_DURATION:.0f}초)..."
            )

        if self._media_detect_state == "idle":
            return "media"

        # ── audio_idle ──
        if self._media_detect_state == "audio_idle":
            total_energy = self._get_audio_energy_total()
            if total_energy is not None and total_energy >= MEDIA_AUDIO_RESUME_THRESHOLD:
                self._media_detect_audio_resume_hits += 1
                if self._media_detect_audio_resume_hits >= MEDIA_AUDIO_RESUME_COUNT:
                    self._media_detect_audio_resume_hits = 0
                    self._media_detect_state = "phase1"
                    self._media_detect_decision = (
                        self._media_detect_last_confirmed
                        if self._media_detect_last_confirmed is not None
                        else "media"
                    )
                    self._media_detect_start_time = now
                    self._media_detect_last_check = 0.0
                    self._media_detect_phase1_dynamic_hits = 0
                    self._media_detect_phase2_dynamic_hits = 0
                    self._media_detect_phase2_static_hits  = 0
                    self._media_detect_audio_idle_hits = 0
                    self._media_detect_prev_frame = None
                    self._media_debug("[미디어] 오디오 복귀 감지 → Phase 1 재진입")
            else:
                self._media_detect_audio_resume_hits = 0
            return self._media_detect_decision

        if now - self._media_detect_last_check < MEDIA_DETECT_INTERVAL:
            return self._media_detect_decision

        self._media_detect_last_check = now

        if self._capture is None:
            return self._media_detect_decision

        current_capture = self._downsample_if_needed(self._capture.grab())
        if current_capture is None:
            return self._media_detect_decision

        if self._media_detect_prev_frame is None:
            self._media_detect_prev_frame = current_capture.copy()
            return self._media_detect_decision

        try:
            prev = self._media_detect_prev_frame.astype(np.float32)
            curr = current_capture.astype(np.float32)
            if prev.shape != curr.shape:
                self._media_detect_prev_frame = current_capture.copy()
                return self._media_detect_decision
            mse = float(np.mean((curr - prev) ** 2))
        except Exception:
            self._media_detect_prev_frame = current_capture.copy()
            return self._media_detect_decision

        self._media_detect_prev_frame = current_capture.copy()

        # ── Holding ──
        if self._media_detect_state == "holding":
            elapsed = now - self._media_detect_start_time
            if elapsed >= MEDIA_DETECT_DURATION:
                self._media_detect_state = "phase2"
                self._media_detect_phase2_dynamic_hits = 0
                self._media_detect_phase2_static_hits  = 0
                self._media_detect_audio_idle_hits = 0
                lbl = "앨범아트" if self._media_detect_decision == "media" else "미러링"
                self._media_debug(f"[미디어] 유지 완료 → Phase 2 진입 ({lbl})")
            return self._media_detect_decision

        # ── Phase 1 ──
        if self._media_detect_state == "phase1":
            elapsed = now - self._media_detect_start_time

            if mse >= MEDIA_DETECT_PHASE1_MSE_THRESHOLD:
                self._media_detect_phase1_dynamic_hits += 1
                self._media_debug(
                    f"[미디어] Phase 1: MSE={mse:.1f} "
                    f"(dynamic {self._media_detect_phase1_dynamic_hits}/{MEDIA_DETECT_PHASE1_DYNAMIC_COUNT}) "
                    f"[{elapsed:.1f}/{MEDIA_DETECT_DURATION:.0f}s]"
                )
                if self._media_detect_phase1_dynamic_hits >= MEDIA_DETECT_PHASE1_DYNAMIC_COUNT:
                    self._media_detect_decision = "mirror"
                    self._media_detect_last_confirmed = "mirror"
                    self._media_detect_state = "phase2"
                    self._media_detect_phase2_dynamic_hits = 0
                    self._media_detect_phase2_static_hits  = 0
                    self._media_detect_audio_idle_hits = 0
                    self._media_debug("[미디어] Phase 1 완료 → 미러링 확정, Phase 2 진입")
            else:
                self._media_detect_phase1_dynamic_hits = 0

            if self._media_detect_state == "phase1" and elapsed >= MEDIA_DETECT_DURATION:
                self._media_detect_decision = "media"
                self._media_detect_last_confirmed = "media"
                self._media_detect_state = "phase2"
                self._media_detect_phase2_dynamic_hits = 0
                self._media_detect_phase2_static_hits  = 0
                self._media_detect_audio_idle_hits = 0
                self._media_debug("[미디어] Phase 1 완료 → 앨범아트 확정, Phase 2 진입")

            return self._media_detect_decision

        # ── Phase 2 ──
        if self._media_detect_state == "phase2":
            if self._media_detect_decision == "media":
                if mse >= MEDIA_DETECT_PHASE2_MSE_THRESHOLD:
                    self._media_detect_phase2_dynamic_hits += 1
                    self._media_detect_phase2_static_hits  = 0
                    self._media_debug(
                        f"[미디어] Phase 2 (앨범아트): MSE={mse:.1f} "
                        f"(dynamic {self._media_detect_phase2_dynamic_hits}/{MEDIA_DETECT_PHASE2_DYNAMIC_COUNT})"
                    )
                    if self._media_detect_phase2_dynamic_hits >= MEDIA_DETECT_PHASE2_DYNAMIC_COUNT:
                        self._media_detect_decision = "mirror"
                        self._media_detect_last_confirmed = "mirror"
                        self._media_detect_phase2_dynamic_hits = 0
                        self._media_detect_phase2_static_hits  = 0
                        self._media_detect_audio_idle_hits = 0
                        self._media_debug("[미디어] Phase 2 → 화면 활동 감지, 미러링으로 전환")
                else:
                    self._media_detect_phase2_dynamic_hits = 0

                total_energy = self._get_audio_energy_total()
                if total_energy is not None:
                    if total_energy < MEDIA_AUDIO_IDLE_THRESHOLD:
                        self._media_detect_audio_idle_hits += 1
                        if self._media_detect_audio_idle_hits % 20 == 0:
                            self._media_debug(
                                f"[미디어] Phase 2: 오디오 idle "
                                f"({self._media_detect_audio_idle_hits}/{MEDIA_AUDIO_IDLE_COUNT})"
                            )
                        if self._media_detect_audio_idle_hits >= MEDIA_AUDIO_IDLE_COUNT:
                            self._media_detect_decision = "mirror"
                            self._media_detect_last_confirmed = "mirror"
                            self._media_detect_state = "audio_idle"
                            self._media_debug(
                                "[미디어] 오디오 무음 감지 → 미러링으로 복귀 (audio_idle)"
                            )
                    else:
                        self._media_detect_audio_idle_hits = 0

            else:  # "mirror"
                if mse < MEDIA_DETECT_PHASE2_MSE_THRESHOLD:
                    self._media_detect_phase2_static_hits  += 1
                    self._media_detect_phase2_dynamic_hits = 0
                    self._media_debug(
                        f"[미디어] Phase 2 (미러링): MSE={mse:.1f} "
                        f"(static {self._media_detect_phase2_static_hits}/{MEDIA_DETECT_PHASE2_STATIC_COUNT})"
                    )
                    if self._media_detect_phase2_static_hits >= MEDIA_DETECT_PHASE2_STATIC_COUNT:
                        total_energy = self._get_audio_energy_total()
                        if total_energy is not None and total_energy < MEDIA_AUDIO_IDLE_THRESHOLD:
                            self._media_detect_phase2_static_hits = 0
                            self._media_debug("[미디어] Phase 2 → 정적 + 무음, 미러링 유지")
                        else:
                            self._media_detect_decision = "media"
                            self._media_detect_last_confirmed = "media"
                            self._media_detect_phase2_dynamic_hits = 0
                            self._media_detect_phase2_static_hits  = 0
                            self._media_detect_audio_idle_hits = 0
                            self._media_debug("[미디어] Phase 2 → 화면 정적 감지, 앨범아트로 전환")
                else:
                    self._media_detect_phase2_static_hits = 0

        return self._media_detect_decision

    # ══════════════════════════════════════════════════════════════
    #  초기화
    # ══════════════════════════════════════════════════════════════

    def _init_mode_resources(self):
        ep = self._current_params

        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._clockwise_t = _compute_led_clockwise_t(self.config)
        self._led_norm_y = compute_led_normalized_y(self.config)
        self._dyn_clockwise_t = self._clockwise_t
        self._dyn_side_t_ranges = compute_side_t_ranges(self.config)

        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        self._cc = ColorCorrection(self.config.get("color", {}))

        if ep.display_enabled:
            self._init_display_resources(ep)
        if ep.audio_enabled:
            self._init_audio_resources(ep)
        elif ep.use_media_frame:
            self._init_audio_monitor()
        if not ep.display_enabled:
            self._rebuild_base_colors_for_color_panel(ep)

        # ★ 미디어 연동 초기화
        self._prev_media_enabled = ep.media_color_enabled
        if ep.use_media_frame and HAS_MEDIA_SESSION:
            self._media_provider = MediaFrameProvider(
                grid_cols=self._active_grid_cols,
                grid_rows=self._active_grid_rows,
            )
            self._media_provider.start()

    def _init_display_resources(self, ep):
        self._init_capture()
        if self._weight_matrix is not None:
            self._vivid_region_masks = build_led_region_masks(
                self._weight_matrix, top_pct=0.10
            )
        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)
        if ep.mirror_n_zones != N_ZONES_PER_LED:
            self._zone_map = _build_led_zone_map_by_side(self.config, ep.mirror_n_zones)
        if not ep.audio_enabled:
            self._rebuild_pipeline()
            self._mirror_cc = ColorCorrection(self.config.get("color", {}))
        self._last_brightness = ep.master_brightness

        # ★MIRROR-FLOWING: 미러링 전용 FlowPalette 초기화
        self._mirror_flow_palette = FlowPalette(n_colors=5)
        self._mirror_flow_last_update = 0.0

    def _init_audio_resources(self, ep):
        self.status_changed.emit("오디오 캡처 초기화...")
        self._audio_monitor_only = False
        self._audio_engine = AudioCapture(
            device_index=self._audio_device_index,
            sensitivity=1.0, smoothing=0.15,
        )
        self._audio_engine.bass_sensitivity = ep.bass_sensitivity
        self._audio_engine.mid_sensitivity = ep.mid_sensitivity
        self._audio_engine.high_sensitivity = ep.high_sensitivity
        self._audio_engine.start()
        self._init_band_mapping(ep)
        if ep.display_enabled:
            self._flow_palette = FlowPalette(n_colors=5)
            self._flow_last_update = 0.0

    def _init_audio_monitor(self):
        self._audio_monitor_only = True
        try:
            self._audio_engine = AudioCapture(
                device_index=self._audio_device_index,
                sensitivity=1.0, smoothing=0.15,
            )
            self._audio_engine.start()
        except Exception:
            self._audio_engine = None
            self._audio_monitor_only = False

    def _init_band_mapping(self, ep):
        n_bands = self._audio_engine.n_bands
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, ep.zone_weights
        )
        self._smooth_bass = self._smooth_mid = self._smooth_high = 0.0
        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)
        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN,
            BASS_DETAIL_FREQ_MAX, fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)
        self._rebuild_base_colors_for_audio(ep)

    # ══════════════════════════════════════════════════════════════
    #  색상 배열 관리
    # ══════════════════════════════════════════════════════════════

    def _rebuild_base_colors_for_color_panel(self, ep):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        if self._led_band_indices is None:
            self._led_band_indices = _compute_led_band_mapping(
                self._perimeter_t, n_bands, ep.zone_weights
            )
        self._cached_base_colors = build_base_color_array(
            self._led_band_indices, n_bands,
            rainbow=ep.rainbow,
            solid_color=np.array(ep.base_color, dtype=np.float32),
        )
        self._cached_rainbow = ep.rainbow
        self._cached_base_color_tuple = ep.base_color
        self._cached_color_effect = ep.color_effect
        self._static_dirty = False

    def _rebuild_base_colors_for_audio(self, ep):
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16
        if ep.audio_mode == AUDIO_FLOWING:
            return
        if (ep.display_enabled
                and self._per_led_colors is not None
                and self._per_led_colors.sum() > 0):
            if (ep.mirror_n_zones != N_ZONES_PER_LED
                    and self._zone_map is not None):
                if ep.color_extract_mode == "distinctive":
                    zone_colors = extract_zone_dominant(
                        self._per_led_colors, self._zone_map, ep.mirror_n_zones,
                        prev_zone_colors=self._prev_zone_dominant,
                        smoothing=0.4, saturation_boost=0.3,
                    )
                    self._prev_zone_dominant = zone_colors.copy()
                else:
                    zone_colors = per_led_to_zone_colors(
                        self._per_led_colors, self._zone_map, ep.mirror_n_zones
                    )
                self._cached_base_colors = zone_colors[self._zone_map]
            else:
                self._cached_base_colors = self._per_led_colors.copy()
        else:
            self._cached_base_colors = build_base_color_array(
                self._led_band_indices, n_bands,
                rainbow=ep.rainbow,
                solid_color=np.array(ep.base_color, dtype=np.float32),
            )
        self._cached_rainbow = ep.rainbow
        self._cached_base_color_tuple = ep.base_color
        self._cached_color_effect = ep.color_effect

    def _maybe_rebuild_base_colors(self, ep):
        changed = (
            ep.rainbow != self._cached_rainbow
            or ep.base_color != self._cached_base_color_tuple
            or ep.color_effect != self._cached_color_effect
        )
        if changed:
            if ep.audio_enabled:
                self._rebuild_base_colors_for_audio(ep)
            else:
                self._rebuild_base_colors_for_color_panel(ep)
                self._static_dirty = False

    # ══════════════════════════════════════════════════════════════
    #  메인 루프 — 오케스트레이터
    # ══════════════════════════════════════════════════════════════

    def _run_loop(self):
        ep = self._current_params
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        prev_zone_weights = ep.zone_weights
        prev_n_zones = ep.mirror_n_zones

        prev_colors = None
        last_good_frame_time = time.monotonic()
        last_recreate_time = 0.0
        led_turned_off = False

        prev_loop_time = time.monotonic()

        if ep.display_enabled:
            self._start_monitor_watcher()

        self._emit_status_message(ep)

        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            dt = loop_start - prev_loop_time
            prev_loop_time = loop_start

            self._swap_params()
            ep = self._current_params

            self._emit_status_message(ep)

            if ep.color_effect != COLOR_EFFECT_STATIC:
                self._gradient_phase.tick(dt, ep.gradient_speed)

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            if ep.display_enabled:
                if self._monitor_disconnected:
                    if stop_wait(timeout=0.5):
                        break
                    continue
                self._check_and_handle_session_resume()
                if self._display_change_flag.is_set():
                    self._handle_display_change()
                    if self._media_provider is not None:
                        self._media_provider.update_grid_size(
                            self._active_grid_cols, self._active_grid_rows
                        )
                    if not ep.audio_enabled:
                        prev_colors = None
                    last_good_frame_time = time.monotonic()
                    led_turned_off = False

            prev_n_zones, prev_zone_weights = self._handle_runtime_param_changes(
                ep, prev_n_zones, prev_zone_weights, prev_colors,
            )

            render_result = self._render_frame(
                ep, prev_colors,
                last_good_frame_time, last_recreate_time, led_turned_off,
                frame_count, loop_start, stop_wait,
            )

            if render_result is None:
                if self._stop_event.is_set():
                    break
                continue

            if isinstance(render_result, MirrorFrameResult):
                raw_rgb = render_result.raw_preview
                grb_data = render_result.grb_data
                prev_colors = render_result.prev_colors
                last_good_frame_time = render_result.last_good_frame_time
                led_turned_off = render_result.led_turned_off
            else:
                raw_rgb, grb_data = render_result
                if ep.display_enabled and ep.audio_enabled:
                    last_good_frame_time = time.monotonic()
                    led_turned_off = False

            should_break = self._send_frame_and_emit_signals(
                ep, grb_data, raw_rgb, frame_count, stop_wait,
            )
            if should_break:
                break

            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  런타임 파라미터 변경 처리
    # ══════════════════════════════════════════════════════════════

    def _handle_runtime_param_changes(self, ep, prev_n_zones, prev_zone_weights,
                                       prev_colors):
        # ── ★ 미디어 연동 토글 런타임 변경 ──
        current_media = ep.use_media_frame
        if current_media != self._prev_media_enabled:
            self._prev_media_enabled = current_media
            if current_media and HAS_MEDIA_SESSION:
                if self._media_provider is None:
                    self._media_provider = MediaFrameProvider(
                        grid_cols=self._active_grid_cols,
                        grid_rows=self._active_grid_rows,
                    )
                    self._media_provider.start()
                if not ep.audio_enabled and self._audio_engine is None:
                    self._init_audio_monitor()
                self._media_detect_state = "idle"
                self._media_detect_last_hash = 0
            else:
                if self._media_provider is not None:
                    self._media_provider.stop()
                    self._media_provider = None
                if self._audio_monitor_only and self._audio_engine is not None:
                    self._audio_engine.stop()
                    self._audio_engine = None
                    self._audio_monitor_only = False
                self._media_detect_state = "idle"
                self._media_detect_last_hash = 0

        # ── ★ 자동 판별 결과 수동 반전 처리 ──
        if (ep.media_source_override == "auto"
                and ep.media_decision_toggle_count != self._prev_media_toggle_count):
            self._prev_media_toggle_count = ep.media_decision_toggle_count
            if self._media_detect_decision == "media":
                self._media_detect_decision = "mirror"
            else:
                self._media_detect_decision = "media"
            self._media_detect_state = "holding"
            self._media_detect_start_time = time.monotonic()
            self._media_detect_phase1_dynamic_hits = 0
            self._media_detect_phase2_dynamic_hits = 0
            self._media_detect_phase2_static_hits  = 0
            self._media_detect_audio_idle_hits = 0
            self._media_detect_prev_frame = None
            lbl = "앨범아트" if self._media_detect_decision == "media" else "미러링"
            self._media_debug(
                f"[미디어] 수동 전환 → {lbl} ({MEDIA_DETECT_DURATION:.0f}초 유지 후 Phase 2 진입)"
            )

        # ── 레이아웃 dirty ──
        if ep.display_enabled:
            with self._layout_lock:
                layout_dirty = self._layout_params.dirty
                if layout_dirty:
                    self._layout_params.dirty = False
            if layout_dirty:
                try:
                    self._weight_matrix = self._build_layout(
                        self._active_w, self._active_h
                    )
                    if not ep.audio_enabled:
                        self._rebuild_pipeline()
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass

        # ── n_zones 변경 ──
        if ep.mirror_n_zones != prev_n_zones:
            prev_n_zones = ep.mirror_n_zones
            if ep.mirror_n_zones != N_ZONES_PER_LED:
                self._zone_map = _build_led_zone_map_by_side(
                    self.config, ep.mirror_n_zones
                )
            else:
                self._zone_map = None
            self._prev_zone_dominant = None
            if ep.audio_enabled:
                self._rebuild_base_colors_for_audio(ep)

        # ── 대역 비율 변경 ──
        if ep.audio_enabled and ep.zone_weights != prev_zone_weights:
            n_bands = self._audio_engine.n_bands
            self._led_band_indices = _compute_led_band_mapping(
                self._perimeter_t, n_bands, ep.zone_weights
            )
            prev_zone_weights = ep.zone_weights
            self._rebuild_base_colors_for_audio(ep)

        # ── 색상 변경 감지 ──
        self._maybe_rebuild_base_colors(ep)

        return prev_n_zones, prev_zone_weights

    # ══════════════════════════════════════════════════════════════
    #  렌더링 경로 분기
    # ══════════════════════════════════════════════════════════════

    def _render_frame(self, ep, prev_colors,
                      last_good_frame_time, last_recreate_time, led_turned_off,
                      frame_count, loop_start, stop_wait):
        if ep.display_enabled and not ep.audio_enabled:
            # ── 미러링 전용 ──
            result = self._frame_mirror_only(
                ep, prev_colors,
                last_good_frame_time, last_recreate_time, led_turned_off,
                frame_count,
            )
            if result is None:
                lgt, lrt, lto = self._handle_stale_frame(
                    last_good_frame_time, last_recreate_time,
                    led_turned_off, stop_wait,
                )
                self._stale_state = (lgt, lrt, lto)
                return None
            return result

        elif ep.audio_enabled:
            raw_rgb, grb_data = self._frame_audio(
                ep, loop_start, frame_count,
                last_good_frame_time, last_recreate_time, led_turned_off,
            )
            return raw_rgb, grb_data

        else:
            raw_rgb, grb_data = self._frame_static(ep, loop_start)
            return raw_rgb, grb_data

    # ══════════════════════════════════════════════════════════════
    #  USB 전송 + 시그널 emit
    # ══════════════════════════════════════════════════════════════

    def _send_frame_and_emit_signals(self, ep, grb_data, raw_rgb,
                                      frame_count, stop_wait):
        try:
            self._device.send_rgb(grb_data)
        except HW_ERRORS:
            pass

        if not self._device.connected:
            self.status_changed.emit("USB 연결 끊김 — 재연결 시도 중...")
            self._device.force_reconnect()
            if self._device.connected:
                self._emit_status_message(ep)
            else:
                if stop_wait(timeout=2.0):
                    return True
                return False

        if frame_count % 3 == 0:
            if ep.audio_enabled:
                self.energy_updated.emit(
                    self._smooth_bass, self._smooth_mid, self._smooth_high
                )
                if (ep.audio_mode == AUDIO_BASS_DETAIL
                        and self._bd_smooth is not None):
                    self.spectrum_updated.emit(self._bd_smooth.copy())
                elif self._smooth_spectrum is not None:
                    self.spectrum_updated.emit(self._smooth_spectrum.copy())

                if (ep.audio_mode == AUDIO_FLOWING
                        and self._flow_palette_colors is not None):
                    self.spectrum_updated.emit(
                        {"type": "flow_palette",
                         "colors": self._flow_palette_colors,
                         "ratios": self._flow_palette_ratios}
                    )

            if raw_rgb is not None:
                self.screen_colors_updated.emit(raw_rgb.tolist())

            # ★ 미러 flowing: 오디오 OFF에서도 팔레트 프리뷰 갱신
            if (not ep.audio_enabled
                    and ep.color_effect == COLOR_EFFECT_FLOWING
                    and self._mirror_flow_palette is not None):
                palette_colors = [
                    blob.color_current.tolist()
                    for blob in self._mirror_flow_palette.blobs
                ]
                palette_ratios = [
                    blob.width
                    for blob in self._mirror_flow_palette.blobs
                ]
                self.spectrum_updated.emit(
                    {"type": "flow_palette",
                     "colors": palette_colors,
                     "ratios": palette_ratios}
                )

        return False

    # ══════════════════════════════════════════════════════════════
    #  경로 A: 미러링 전용 (D=ON, A=OFF)
    # ══════════════════════════════════════════════════════════════

    def _frame_mirror_only(self, ep, prev_colors,
                           last_good_frame_time, last_recreate_time,
                           led_turned_off, frame_count):
        """미러링 전용 프레임 처리.

        ★MIRROR-FLOWING: color_effect=="flowing"이면 FlowPalette 경로로 분기.
        """
        # ★MIRROR-FLOWING: flowing은 별도 경로
        if ep.color_effect == COLOR_EFFECT_FLOWING:
            return self._frame_mirror_flowing(
                ep, prev_colors,
                last_good_frame_time, last_recreate_time,
                led_turned_off, frame_count,
            )

        pipeline = self._pipeline

        # ── 밝기 / 스무딩 반영 ──
        if ep.master_brightness != self._last_brightness:
            pipeline.update_brightness(ep.master_brightness)
            self._last_brightness = ep.master_brightness
        pipeline.smoothing = ep.smoothing_factor
        pipeline.smoothing_enabled = ep.smoothing_enabled

        # ── ★ 캡처 소스 선택 ──
        frame = self._grab_frame(ep)
        if frame is None:
            return None

        new_last_good = time.monotonic()
        new_led_off = False
        if led_turned_off:
            self.status_changed.emit("디스플레이 미러링 실행 중")

        # ── 해상도 변경 감지 ──
        if not ep.use_media_frame:
            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                return None

            if not getattr(self, '_native_capture', False):
                cap_w = getattr(self._capture, 'screen_w', 0)
                cap_h = getattr(self._capture, 'screen_h', 0)
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    return None
            else:
                cap_w = self._capture.screen_w
                cap_h = self._capture.screen_h
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    return None

        # ── 색상 연산 ──
        _gradient_active = _has_mirror_gradient_effect(
            ep.color_effect, ep.gradient_sv_range, ep.gradient_hue_range
        )

        if self._zone_map is not None:
            try:
                grb_data, raw_preview = self._compute_mirror_zone_colors(frame, ep)
                if _gradient_active:
                    raw_preview = apply_mirror_gradient_modulation(
                        raw_preview, self._clockwise_t, time.monotonic(),
                        color_effect=ep.color_effect,
                        gradient_speed=ep.gradient_speed,
                        gradient_hue_range=ep.gradient_hue_range,
                        gradient_sv_range=ep.gradient_sv_range,
                        gradient_phase=self._gradient_phase,
                    )
                    leds_mod = raw_preview.copy()
                    self._mirror_cc.apply(leds_mod)
                    grb_data = leds_to_grb(leds_mod)
                new_prev = None
            except (ValueError, IndexError, FloatingPointError):
                return None

        elif ep.color_extract_mode == "distinctive":
            try:
                grb_data, raw_preview, new_prev = self._compute_mirror_per_led_distinctive(
                    frame, ep, prev_colors
                )
                if _gradient_active:
                    raw_preview = apply_mirror_gradient_modulation(
                        raw_preview, self._clockwise_t, time.monotonic(),
                        color_effect=ep.color_effect,
                        gradient_speed=ep.gradient_speed,
                        gradient_hue_range=ep.gradient_hue_range,
                        gradient_sv_range=ep.gradient_sv_range,
                        gradient_phase=self._gradient_phase,
                    )
                    leds_mod = raw_preview.copy()
                    self._mirror_cc.apply(leds_mod)
                    grb_data = leds_to_grb(leds_mod)
            except (ValueError, IndexError, FloatingPointError):
                return None

        else:
            if not _gradient_active:
                try:
                    grb_data, rgb_colors = pipeline.process(frame, prev_colors)
                    new_prev = rgb_colors
                except (ValueError, IndexError, FloatingPointError):
                    return None

                if frame_count % 3 == 0:
                    try:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        raw_preview = self._weight_matrix @ grid_flat
                    except Exception:
                        raw_preview = rgb_colors
                else:
                    raw_preview = rgb_colors

            else:
                try:
                    grid = cv2.resize(
                        frame,
                        (pipeline.grid_cols, pipeline.grid_rows),
                        interpolation=cv2.INTER_LINEAR,
                    )
                    grid_flat = grid.reshape(-1, 3).astype(np.float32)
                    raw_rgb = pipeline.weight_matrix @ grid_flat
                except (ValueError, IndexError):
                    return None

                modulated = apply_mirror_gradient_modulation(
                    raw_rgb, self._clockwise_t, time.monotonic(),
                    color_effect=ep.color_effect,
                    gradient_speed=ep.gradient_speed,
                    gradient_hue_range=ep.gradient_hue_range,
                    gradient_sv_range=ep.gradient_sv_range,
                    gradient_phase=self._gradient_phase,
                )

                try:
                    grb_data, rgb_colors = pipeline.process_raw(
                        modulated, prev_colors
                    )
                    new_prev = rgb_colors
                except (ValueError, IndexError, FloatingPointError):
                    return None

                raw_preview = modulated

        return MirrorFrameResult(
            raw_preview=raw_preview,
            grb_data=grb_data,
            prev_colors=new_prev,
            last_good_frame_time=new_last_good,
            led_turned_off=new_led_off,
        )

    # ══════════════════════════════════════════════════════════════
    #  ★MIRROR-FLOWING: 미러링 전용 flowing 렌더링
    # ══════════════════════════════════════════════════════════════

    def _frame_mirror_flowing(self, ep, prev_colors,
                              last_good_frame_time, last_recreate_time,
                              led_turned_off, frame_count):
        """미러링 전용 Flowing — 화면 색 기반 회전, 오디오 반응 없음.

        기존 하이브리드 flowing과 동일한 FlowPalette + render_flowing()을
        사용하되, bass=0, mid=0, high=0으로 호출하여 오디오 반응을 끔.

        흐름:
        1. _grab_frame()으로 화면/미디어 프레임 획득
        2. weight_matrix로 per_led_colors 계산
        3. N프레임마다 FlowPalette.update_from_screen()으로 팔레트 갱신
        4. FlowPalette.tick()으로 위상 진행 (bass=0 → 일정 속도)
        5. render_flowing()으로 최종 LED 색상 산출 (bass=0 → 일정 밝기)
        6. 색상 보정 + GRB 변환
        """
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)

        # ── 캡처 소스 선택 ──
        frame = self._grab_frame(ep)
        if frame is None:
            return None

        new_last_good = time.monotonic()
        new_led_off = False
        if led_turned_off:
            self.status_changed.emit("디스플레이 미러링 실행 중")

        # ── 해상도 변경 감지 (기존과 동일) ──
        if not ep.use_media_frame:
            try:
                current_h, current_w = frame.shape[:2]
            except (AttributeError, ValueError):
                return None
            if not getattr(self, '_native_capture', False):
                cap_w = getattr(self._capture, 'screen_w', 0)
                cap_h = getattr(self._capture, 'screen_h', 0)
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    return None
            else:
                cap_w = self._capture.screen_w
                cap_h = self._capture.screen_h
                if (cap_w > 0 and cap_h > 0
                        and (cap_w != self._active_w or cap_h != self._active_h)):
                    self._display_change_flag.set()
                    return None

        # ── 화면 색 → per_led_colors (weight_matrix 경로) ──
        try:
            grid_flat = frame.reshape(-1, 3).astype(np.float32)
            per_led_raw = self._weight_matrix @ grid_flat
        except (ValueError, IndexError):
            return None

        # ── 팔레트 갱신 (flowing_interval 주기) ──
        palette = self._mirror_flow_palette
        now = time.monotonic()

        if (per_led_raw is not None
                and per_led_raw.sum() > 0
                and (now - self._mirror_flow_last_update) > ep.flowing_interval):
            palette.update_from_screen(per_led_raw)
            self._mirror_flow_last_update = now

        # ── 위상 진행 (bass=0 → 일정 속도, 오디오 반응 없음) ──
        palette.tick(
            frame_interval,
            bass=0.0, mid=0.0, high=0.0,
            base_speed=ep.flowing_speed,
        )

        # ── 렌더링 ──
        # ★ min_brightness=1.0: bass=0이므로 bass_mod = max(1.0, 0.02) = 1.0
        #   → 오디오 반응 없이 brightness만으로 밝기 제어
        raw_rgb = render_flowing(
            self._clockwise_t, palette,
            bass=0.0,
            brightness=ep.master_brightness,
            mid=0.0,
            min_brightness=1.0,
        )

        # ── 색상 보정 + GRB 변환 ──
        leds_out = raw_rgb.copy()
        self._mirror_cc.apply(leds_out)
        grb_data = leds_to_grb(leds_out)

        return MirrorFrameResult(
            raw_preview=raw_rgb,
            grb_data=grb_data,
            prev_colors=None,  # flowing은 자체 스무딩, prev_colors 불필요
            last_good_frame_time=new_last_good,
            led_turned_off=new_led_off,
        )

    # ══════════════════════════════════════════════════════════════
    #  미러링 색상 계산 헬퍼 (기존과 동일)
    # ══════════════════════════════════════════════════════════════

    def _compute_mirror_zone_colors(self, frame, ep):
        grid_flat = frame.reshape(-1, 3).astype(np.float32)
        per_led_raw = self._weight_matrix @ grid_flat
        extract_mode = ep.color_extract_mode

        if extract_mode == "distinctive":
            per_led_raw, _, self._prev_ambient_color = boost_per_led_vivid(
                grid_flat, self._weight_matrix, per_led_raw,
                region_masks=self._vivid_region_masks,
                blend=0.4, ambient_blend=0.2,
                prev_ambient_color=self._prev_ambient_color,
            )
            per_led_raw = smooth_per_led(
                per_led_raw, self._prev_per_led_vivid, smoothing=0.5,
            )
            self._prev_per_led_vivid = per_led_raw.copy()

        if extract_mode == "distinctive":
            zone_colors = extract_zone_dominant(
                per_led_raw, self._zone_map, ep.mirror_n_zones,
                prev_zone_colors=self._prev_zone_dominant,
                smoothing=0.4, saturation_boost=0.3,
            )
            self._prev_zone_dominant = zone_colors.copy()
        else:
            zone_colors = per_led_to_zone_colors(
                per_led_raw, self._zone_map, ep.mirror_n_zones
            )

        leds = zone_colors[self._zone_map]
        raw_preview = leds.copy()
        raw_preview *= ep.master_brightness
        leds *= ep.master_brightness
        self._mirror_cc.apply(leds)
        return leds_to_grb(leds), raw_preview

    def _compute_mirror_per_led_distinctive(self, frame, ep, prev_colors):
        grid_flat = frame.reshape(-1, 3).astype(np.float32)
        per_led_raw = self._weight_matrix @ grid_flat

        per_led_raw, _, self._prev_ambient_color = boost_per_led_vivid(
            grid_flat, self._weight_matrix, per_led_raw,
            region_masks=self._vivid_region_masks,
            blend=0.4, ambient_blend=0.2,
            prev_ambient_color=self._prev_ambient_color,
        )
        per_led_raw = smooth_per_led(
            per_led_raw, self._prev_per_led_vivid, smoothing=0.5,
        )
        self._prev_per_led_vivid = per_led_raw.copy()

        if prev_colors is not None and ep.smoothing_factor > 0:
            s = ep.smoothing_factor
            per_led_raw = per_led_raw * (1.0 - s) + prev_colors * s

        raw_preview = per_led_raw.copy()
        raw_preview *= ep.master_brightness
        leds = per_led_raw * ep.master_brightness
        self._mirror_cc.apply(leds)
        grb_data = leds_to_grb(leds)
        return grb_data, raw_preview, per_led_raw

    def _handle_stale_frame(self, last_good_frame_time, last_recreate_time,
                            led_turned_off, stop_wait):
        now = time.monotonic()
        stale_duration = now - last_good_frame_time

        if stale_duration > _STALE_THRESHOLD:
            if now - last_recreate_time >= _STALE_RECREATE_COOLDOWN:
                last_recreate_time = now
                self.status_changed.emit("캡처 복구 중...")
                self._capture._recreate()
                new_w = self._capture.screen_w
                new_h = self._capture.screen_h
                if (new_w > 0 and new_h > 0
                        and (new_w != self._active_w or new_h != self._active_h)):
                    self._display_change_flag.set()

        if (stale_duration > _STALE_LED_OFF_THRESHOLD
                and not led_turned_off):
            try:
                self._device.turn_off()
                led_turned_off = True
                self.status_changed.emit("캡처 없음 — LED 대기 중")
            except HW_ERRORS:
                pass

        stop_wait(timeout=0.01)
        return last_good_frame_time, last_recreate_time, led_turned_off

    # ══════════════════════════════════════════════════════════════
    #  경로 B: 오디오 ON (D=ON/OFF 통합)
    # ══════════════════════════════════════════════════════════════

    def _frame_audio(self, ep, loop_start, frame_count,
                     last_good_frame_time, last_recreate_time, led_turned_off):
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)

        eng = self._audio_engine
        eng.bass_sensitivity = ep.bass_sensitivity
        eng.mid_sensitivity = ep.mid_sensitivity
        eng.high_sensitivity = ep.high_sensitivity
        eng.smoothing = ep.input_smoothing

        bands = eng.get_band_energies()
        raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
        raw_spectrum = eng.get_spectrum()

        atk = 0.15 + ep.attack * 0.70
        rel = 0.25 - ep.release * 0.245

        self._smooth_bass = _ar(self._smooth_bass, raw_bass, atk, rel)
        self._smooth_mid = _ar(self._smooth_mid, raw_mid, atk, rel)
        self._smooth_high = _ar(self._smooth_high, raw_high, atk, rel)
        for i in range(len(self._smooth_spectrum)):
            self._smooth_spectrum[i] = _ar(
                self._smooth_spectrum[i], raw_spectrum[i], atk, rel
            )

        bass = self._smooth_bass
        mid = self._smooth_mid
        high = self._smooth_high
        spec = self._smooth_spectrum

        if (ep.display_enabled
                and self._capture is not None
                and self._weight_matrix is not None
                and frame_count % SCREEN_UPDATE_INTERVAL == 0):
            self._update_screen_colors(ep)

        if (ep.color_effect != COLOR_EFFECT_STATIC
                and ep.color_effect != COLOR_EFFECT_FLOWING  # ★MIRROR-FLOWING: flowing은 제외
                and not ep.display_enabled):
            self._cached_base_colors = build_base_color_array_animated(
                self._led_band_indices,
                self._audio_engine.n_bands if self._audio_engine else 16,
                self._clockwise_t,
                loop_start,
                color_effect=ep.color_effect,
                rainbow=ep.rainbow,
                solid_color=np.array(ep.base_color, dtype=np.float32),
                gradient_speed=ep.gradient_speed,
                gradient_hue_range=ep.gradient_hue_range,
                gradient_sv_range=ep.gradient_sv_range,
                gradient_phase=self._gradient_phase,
            )

        if (ep.color_effect != COLOR_EFFECT_STATIC
                and ep.color_effect != COLOR_EFFECT_FLOWING  # ★MIRROR-FLOWING: flowing은 제외
                and ep.display_enabled
                and not (self._per_led_colors is not None
                         and self._per_led_colors.sum() > 0)):
            self._cached_base_colors = build_base_color_array_animated(
                self._led_band_indices,
                self._audio_engine.n_bands if self._audio_engine else 16,
                self._clockwise_t,
                loop_start,
                color_effect=ep.color_effect,
                rainbow=ep.rainbow,
                solid_color=np.array(ep.base_color, dtype=np.float32),
                gradient_speed=ep.gradient_speed,
                gradient_hue_range=ep.gradient_hue_range,
                gradient_sv_range=ep.gradient_sv_range,
                gradient_phase=self._gradient_phase,
            )

        if (ep.display_enabled
                and _has_mirror_gradient_effect(
                    ep.color_effect, ep.gradient_sv_range, ep.gradient_hue_range)
                and self._cached_base_colors is not None
                and ep.audio_mode != AUDIO_FLOWING):
            frame_base_colors = apply_mirror_gradient_modulation(
                self._cached_base_colors.copy(),
                self._clockwise_t, loop_start,
                color_effect=ep.color_effect,
                gradient_speed=ep.gradient_speed,
                gradient_hue_range=ep.gradient_hue_range,
                gradient_sv_range=ep.gradient_sv_range,
                gradient_phase=self._gradient_phase,
            )
        else:
            frame_base_colors = self._cached_base_colors

        raw_rgb = self._render_audio_mode(
            ep, frame_base_colors, bass, mid, high, spec,
            raw_bass, frame_interval, loop_start,
        )

        leds_out = raw_rgb.copy()
        self._cc.apply(leds_out)
        grb_data = leds_to_grb(leds_out)

        return raw_rgb, grb_data

    def _render_audio_mode(self, ep, frame_base_colors,
                           bass, mid, high, spec,
                           raw_bass, frame_interval, loop_start):
        audio_mode = ep.audio_mode

        if audio_mode == AUDIO_WAVE:
            self._wave_last_spawn = wave_tick_pulses(
                self._wave_pulses, frame_interval,
                bass, self._wave_prev_bass,
                self._wave_last_spawn, loop_start,
                speed=ep.wave_speed,
            )
            self._wave_prev_bass = bass
            return vectorized_render_wave(
                frame_base_colors, self._led_norm_y,
                self._wave_pulses,
                ep.min_brightness, ep.master_brightness,
                speed=ep.wave_speed,
                min_brightness_mode=ep.min_brightness_mode,
            )

        elif audio_mode == AUDIO_DYNAMIC:
            self._dyn_last_spawn = dynamic_tick_ripples(
                self._dyn_ripples, frame_interval,
                bass, mid, high,
                self._dyn_clockwise_t,
                self._dyn_last_spawn, loop_start,
                prev_bass=self._dyn_prev_bass,
                side_t_ranges=self._dyn_side_t_ranges,
                attack=ep.attack,
                release=ep.release,
                sensitivity=ep.bass_sensitivity,
                raw_bass=raw_bass,
                prev_raw_bass=self._dyn_prev_raw_bass,
            )
            self._dyn_prev_bass = bass
            self._dyn_prev_raw_bass = raw_bass
            return vectorized_render_dynamic(
                frame_base_colors, self._dyn_clockwise_t,
                self._dyn_ripples, high,
                ep.min_brightness, ep.master_brightness,
                min_brightness_mode=ep.min_brightness_mode,
            )

        elif audio_mode == AUDIO_FLOWING and ep.display_enabled:
            return self._render_flowing_mode(
                ep, bass, mid, high, frame_interval, loop_start,
                frame_base_colors,
            )

        elif audio_mode == AUDIO_BASS_DETAIL:
            bd_spec = self._process_bass_detail(self._audio_engine,
                                                 0.15 + ep.attack * 0.70,
                                                 0.25 - ep.release * 0.245, ep)
            return vectorized_render_spectrum(
                frame_base_colors, self._led_band_indices,
                bd_spec, ep.min_brightness, ep.master_brightness,
                min_brightness_mode=ep.min_brightness_mode,
            )

        elif audio_mode == AUDIO_SPECTRUM:
            return vectorized_render_spectrum(
                frame_base_colors, self._led_band_indices,
                spec, ep.min_brightness, ep.master_brightness,
                min_brightness_mode=ep.min_brightness_mode,
            )

        else:  # AUDIO_PULSE (또는 flowing + D=OFF → pulse fallback)
            return vectorized_render_pulse(
                frame_base_colors, bass, mid, high,
                ep.min_brightness, ep.master_brightness,
                min_brightness_mode=ep.min_brightness_mode,
            )

    def _render_flowing_mode(self, ep, bass, mid, high,
                             frame_interval, loop_start, frame_base_colors):
        if (self._per_led_colors is not None
                and self._per_led_colors.sum() > 0
                and self._flow_palette is not None
                and (loop_start - self._flow_last_update) > ep.flowing_interval):
            self._flow_palette.update_from_screen(self._per_led_colors)
            self._flow_last_update = loop_start

        if self._flow_palette is not None:
            self._flow_palette.tick(
                frame_interval, bass, mid, high,
                base_speed=ep.flowing_speed,
            )
            raw_rgb = render_flowing(
                self._clockwise_t, self._flow_palette,
                bass, ep.master_brightness, mid=mid,
                min_brightness=ep.min_brightness,
            )
            self._flow_palette_colors = [
                blob.color_current.tolist() for blob in self._flow_palette.blobs
            ]
            self._flow_palette_ratios = [
                blob.width for blob in self._flow_palette.blobs
            ]
        else:
            raw_rgb = vectorized_render_pulse(
                frame_base_colors, bass, mid, high,
                ep.min_brightness, ep.master_brightness,
                min_brightness_mode=ep.min_brightness_mode,
            )
            self._flow_palette_colors = None
            self._flow_palette_ratios = None

        return raw_rgb

    def _update_screen_colors(self, ep):
        screen_frame = self._grab_frame(ep)
        if screen_frame is None:
            return

        try:
            grid_flat = screen_frame.reshape(-1, 3).astype(np.float32)
            raw_per_led = self._weight_matrix @ grid_flat

            if ep.audio_mode == AUDIO_FLOWING:
                self._per_led_colors = raw_per_led
                return

            self._per_led_colors = raw_per_led

            if ep.color_extract_mode == "distinctive":
                self._per_led_colors, _, self._prev_ambient_color = boost_per_led_vivid(
                    grid_flat, self._weight_matrix,
                    self._per_led_colors,
                    region_masks=self._vivid_region_masks,
                    blend=0.4, ambient_blend=0.2,
                    prev_ambient_color=self._prev_ambient_color,
                )
                self._per_led_colors = smooth_per_led(
                    self._per_led_colors,
                    self._prev_per_led_vivid,
                    smoothing=0.5,
                )
                self._prev_per_led_vivid = self._per_led_colors.copy()

            if (ep.mirror_n_zones != N_ZONES_PER_LED
                    and self._zone_map is not None):
                if ep.color_extract_mode == "distinctive":
                    self._zone_colors = extract_zone_dominant(
                        self._per_led_colors,
                        self._zone_map, ep.mirror_n_zones,
                        prev_zone_colors=self._prev_zone_dominant,
                        smoothing=0.4, saturation_boost=0.3,
                    )
                    self._prev_zone_dominant = self._zone_colors.copy()
                else:
                    self._zone_colors = per_led_to_zone_colors(
                        self._per_led_colors,
                        self._zone_map, ep.mirror_n_zones,
                    )

            self._rebuild_base_colors_for_audio(ep)

        except (ValueError, IndexError):
            pass

    # ══════════════════════════════════════════════════════════════
    #  경로 C: 양쪽 OFF → 정적/애니메이션 색상
    # ══════════════════════════════════════════════════════════════

    def _frame_static(self, ep, current_time):
        n_leds = self._led_count
        n_bands = 16

        if ep.color_effect == COLOR_EFFECT_STATIC:
            if self._static_dirty or self._cached_base_colors is None:
                self._rebuild_base_colors_for_color_panel(ep)
            raw_rgb = self._cached_base_colors.copy()
            raw_rgb *= ep.master_brightness
        else:
            raw_rgb = build_base_color_array_animated(
                self._led_band_indices if self._led_band_indices is not None
                    else _compute_led_band_mapping(self._perimeter_t, n_bands, ep.zone_weights),
                n_bands,
                self._clockwise_t,
                current_time,
                color_effect=ep.color_effect,
                rainbow=ep.rainbow,
                solid_color=np.array(ep.base_color, dtype=np.float32),
                gradient_speed=ep.gradient_speed,
                gradient_hue_range=ep.gradient_hue_range,
                gradient_sv_range=ep.gradient_sv_range,
                gradient_phase=self._gradient_phase,
            )
            raw_rgb = raw_rgb * ep.master_brightness

        leds_out = raw_rgb.copy()
        self._cc.apply(leds_out)
        grb_data = leds_to_grb(leds_out)
        return raw_rgb, grb_data

    # ══════════════════════════════════════════════════════════════
    #  Bass Detail 처리
    # ══════════════════════════════════════════════════════════════

    def _process_bass_detail(self, eng, atk_rate, rel_rate, ep):
        raw_fft = eng.get_raw_fft()
        spec_len = len(raw_fft)
        n = BASS_DETAIL_N_BANDS

        raw_vals = np.zeros(n, dtype=np.float64)
        for i, (lo, hi) in enumerate(self._bd_band_bins):
            if lo < spec_len and hi <= spec_len and hi > lo:
                band_data = raw_fft[lo:hi]
                raw_vals[i] = float(np.sqrt(np.mean(band_data ** 2)))

        agc_atk, agc_rel, agc_floor = 0.3, 0.002, 0.005
        for i in range(n):
            if raw_vals[i] > self._bd_agc[i]:
                self._bd_agc[i] += (raw_vals[i] - self._bd_agc[i]) * agc_atk
            else:
                self._bd_agc[i] *= (1.0 - agc_rel)
                self._bd_agc[i] = max(self._bd_agc[i], agc_floor)

        normalized = raw_vals / self._bd_agc
        val = np.minimum(1.0, (normalized * ep.bass_sensitivity) ** 1.5)

        for i in range(n):
            self._bd_smooth[i] = _ar(self._bd_smooth[i], val[i], atk_rate, rel_rate)

        return self._bd_smooth

    # ══════════════════════════════════════════════════════════════
    #  정리 + 헬퍼
    # ══════════════════════════════════════════════════════════════

    def _cleanup_mode(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None
        self._audio_monitor_only = False
        if self._media_provider:
            self._media_provider.stop()
            self._media_provider = None

    _AUDIO_MODE_NAMES = {
        "pulse": "Pulse", "spectrum": "Spectrum",
        "bass_detail": "Bass Detail", "wave": "Wave",
        "dynamic": "Dynamic", "flowing": "Flowing",
    }

    def _emit_status_message(self, ep):
        if ep.audio_enabled:
            mode_name = self._AUDIO_MODE_NAMES.get(ep.audio_mode, ep.audio_mode)
            suffix = f" · 🔊 {mode_name}"
        else:
            suffix = ""

        if ep.display_enabled and ep.audio_enabled:
            msg = f"하이브리드 실행 중{suffix}"
        elif ep.display_enabled:
            # ★MIRROR-FLOWING: flowing 효과일 때 상태 메시지 구분
            if ep.color_effect == COLOR_EFFECT_FLOWING:
                msg = "디스플레이 Flowing 실행 중"
            else:
                msg = "디스플레이 미러링 실행 중"
        elif ep.audio_enabled:
            msg = f"오디오 반응 실행 중{suffix}"
        else:
            msg = "정적 LED 실행 중"

        key = (ep.display_enabled, ep.audio_enabled, ep.audio_mode, ep.color_effect)
        if key != self._last_status_key:
            self._last_status_key = key
            self.status_changed.emit(msg)