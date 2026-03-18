"""UnifiedEngine — 4가지 토글 조합을 단일 엔진으로 처리 (Phase 7)

display_enabled / audio_enabled 플래그로 모든 모드를 통합.

[동작 모드]
  D=OFF, A=OFF  → 정적/애니메이션 색상 출력 (plain LED)
  D=ON,  A=OFF  → 미러링 전용 (화면 색 → LED)
  D=OFF, A=ON   → 오디오 비주얼라이저 (사용자 색상 + 오디오 반응)
  D=ON,  A=ON   → 하이브리드 (화면 색 + 오디오 반응)

[Phase 8 변경]
- GradientPhase 도입: gradient_speed 변경 시 색상 점프 방지
- build_base_color_array_animated / apply_mirror_gradient_modulation에
  gradient_phase= 인자 전달
"""

import time
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.color import ColorPipeline
from core.audio_engine import AudioEngine as AudioCapture, _build_log_bands
from core.constants import HW_ERRORS
from core.engine_utils import (
    N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    COLOR_EFFECT_STATIC,
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

# ── stale detection 상수 (MirrorEngine에서 가져옴) ──
_STALE_THRESHOLD = 3.0          # 초: 이 시간 동안 프레임 없으면 recreate 시도
_STALE_RECREATE_COOLDOWN = 3.0  # 초: recreate 재시도 간격
_STALE_LED_OFF_THRESHOLD = 10.0 # 초: 이 시간 동안 프레임 없으면 LED 끔


def _ar(current, target, attack_rate, release_rate):
    """Attack/Release 스무딩."""
    if target > current:
        return current + (target - current) * attack_rate
    else:
        return current + (target - current) * release_rate


class UnifiedEngine(BaseEngine):
    """통합 엔진 — display_enabled/audio_enabled 플래그로 4가지 조합 처리."""

    mode = "unified"

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)

        # ── 오디오 리소스 ──
        self._audio_engine: AudioCapture | None = None
        self._cc: ColorCorrection | None = None

        # ── 둘레 좌표 (모든 모드에서 사용) ──
        self._perimeter_t = None
        self._clockwise_t = None

        # ── 밴드 매핑 (오디오 ON) ──
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
        self._zone_map = None            # N구역 모드용
        self._zone_colors = None
        self._prev_zone_dominant = None

        # ── 미러링 전용 (D=ON, A=OFF) ──
        self._mirror_cc = None           # N구역 미러링용 별도 ColorCorrection
        self._last_brightness = -1.0     # 밝기 변경 감지

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

        # ── Flowing 모드 (D+A ON) ──
        self._flow_palette = None
        self._flow_last_update = 0.0
        self._flow_palette_colors = None
        self._flow_palette_ratios = None

        # ── 정적 모드 색상 캐시 (D=OFF, A=OFF) ──
        self._static_dirty = True

        # ── ★ Phase 8: 그라데이션 누적 위상 ──
        self._gradient_phase = GradientPhase()

    # ══════════════════════════════════════════════════════════════
    #  초기화
    # ══════════════════════════════════════════════════════════════

    def _init_mode_resources(self):
        ep = self._current_params

        # ── 둘레 좌표: 모든 모드에서 필요 (정적 그라데이션에서도 사용) ──
        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._clockwise_t = _compute_led_clockwise_t(self.config)
        self._led_norm_y = compute_led_normalized_y(self.config)
        self._dyn_clockwise_t = self._clockwise_t
        self._dyn_side_t_ranges = compute_side_t_ranges(self.config)

        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        # ── 색상 보정 (공통) ──
        self._cc = ColorCorrection(self.config.get("color", {}))

        # ── 디스플레이 리소스 (D=ON) ──
        if ep.display_enabled:
            self._init_display_resources(ep)

        # ── 오디오 리소스 (A=ON) ──
        if ep.audio_enabled:
            self._init_audio_resources(ep)

        # ── 정적 모드: 기본 색상 초기화 (D=OFF) ──
        if not ep.display_enabled:
            self._rebuild_base_colors_for_color_panel(ep)

    def _init_display_resources(self, ep):
        """디스플레이 ON 공통 리소스: 캡처 + weight_matrix + vivid masks."""
        self._init_capture()

        # 채도 우선순위 v2: LED별 핵심 영역 캐시
        if self._weight_matrix is not None:
            self._vivid_region_masks = build_led_region_masks(
                self._weight_matrix, top_pct=0.10
            )

        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)

        # N구역 모드 준비 (미러링 전용 + 하이브리드 모두에서 사용)
        if ep.mirror_n_zones != N_ZONES_PER_LED:
            self._zone_map = _build_led_zone_map_by_side(
                self.config, ep.mirror_n_zones
            )

        if not ep.audio_enabled:
            # D=ON, A=OFF: ColorPipeline (per-LED average) 또는 직접 처리 (distinctive)
            self._rebuild_pipeline()
            # ColorCorrection: N구역 모드와 per-LED distinctive 모두에서 필요
            self._mirror_cc = ColorCorrection(self.config.get("color", {}))

        self._last_brightness = ep.master_brightness

    def _init_audio_resources(self, ep):
        """오디오 ON 리소스: AudioCapture + 밴드 매핑."""
        self.status_changed.emit("오디오 캡처 초기화...")
        self._audio_engine = AudioCapture(
            device_index=self._audio_device_index,
            sensitivity=1.0, smoothing=0.15,
        )
        self._audio_engine.bass_sensitivity = ep.bass_sensitivity
        self._audio_engine.mid_sensitivity = ep.mid_sensitivity
        self._audio_engine.high_sensitivity = ep.high_sensitivity
        self._audio_engine.start()

        self._init_band_mapping(ep)

        # 하이브리드 전용: FlowPalette
        if ep.display_enabled:
            self._flow_palette = FlowPalette(n_colors=5)
            self._flow_last_update = 0.0

    def _init_band_mapping(self, ep):
        """밴드 매핑 + bass detail 초기화."""
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

        # 오디오 base colors 초기 빌드
        self._rebuild_base_colors_for_audio(ep)

    # ══════════════════════════════════════════════════════════════
    #  색상 배열 관리
    # ══════════════════════════════════════════════════════════════

    def _rebuild_base_colors_for_color_panel(self, ep):
        """색상 패널 설정(무지개/단색)에서 base_colors 빌드."""
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
        """오디오 모드용 base_colors 빌드."""
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
        """색상/효과 변경 감지 → 재빌드."""
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
    #  메인 루프
    # ══════════════════════════════════════════════════════════════

    def _run_loop(self):
        ep = self._current_params
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        # 오디오 상태 추적
        prev_zone_weights = ep.zone_weights
        prev_n_zones = ep.mirror_n_zones

        # 미러링 stale detection 상태
        prev_colors = None
        last_good_frame_time = time.monotonic()
        last_recreate_time = 0.0
        led_turned_off = False

        # ★ Phase 8: 이전 루프 시각 (dt 계산용)
        prev_loop_time = time.monotonic()

        # 모니터 워처 (디스플레이 ON)
        if ep.display_enabled:
            self._start_monitor_watcher()

        # 상태 메시지
        self._emit_status_message(ep)

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ★ Phase 8: dt 계산 + 그라데이션 위상 누적
            dt = loop_start - prev_loop_time
            prev_loop_time = loop_start

            # ── 파라미터 스냅샷 교체 ──
            self._swap_params()
            ep = self._current_params

            # ★ Phase 8: 그라데이션 위상 누적 (모든 모드에서, static이 아니면)
            if ep.color_effect != COLOR_EFFECT_STATIC:
                self._gradient_phase.tick(dt, ep.gradient_speed)

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 모니터 분리 대기 (디스플레이 ON) ──
            if ep.display_enabled and self._monitor_disconnected:
                if stop_wait(timeout=0.5):
                    break
                continue

            # ── 세션 복귀 (절전모드) ──
            self._check_and_handle_session_resume()

            # ── 디스플레이 변경 ──
            if ep.display_enabled and self._display_change_flag.is_set():
                self._handle_display_change()
                if not ep.audio_enabled:
                    prev_colors = None
                last_good_frame_time = time.monotonic()
                led_turned_off = False

            # ── 레이아웃 dirty (디스플레이 ON) ──
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
                        prev_colors = None
                    except (ValueError, IndexError, np.linalg.LinAlgError):
                        pass

            # ── n_zones 런타임 변경 감지 ──
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

            # ── 오디오 대역 비율 변경 → 밴드 매핑 재계산 ──
            if ep.audio_enabled and ep.zone_weights != prev_zone_weights:
                n_bands = self._audio_engine.n_bands
                self._led_band_indices = _compute_led_band_mapping(
                    self._perimeter_t, n_bands, ep.zone_weights
                )
                prev_zone_weights = ep.zone_weights
                self._rebuild_base_colors_for_audio(ep)

            # ── 색상 변경 감지 ──
            self._maybe_rebuild_base_colors(ep)

            # ════════════════════════════════════════════════
            #  렌더링 경로 분기
            # ════════════════════════════════════════════════

            raw_rgb = None
            grb_data = None

            if ep.display_enabled and not ep.audio_enabled:
                result = self._frame_mirror_only(
                    ep, prev_colors,
                    last_good_frame_time, last_recreate_time, led_turned_off,
                    frame_count,
                )
                if result is None:
                    last_good_frame_time, last_recreate_time, led_turned_off = (
                        self._handle_stale_frame(
                            last_good_frame_time, last_recreate_time,
                            led_turned_off, stop_wait,
                        )
                    )
                    if self._stop_event.is_set():
                        break
                    continue
                raw_rgb, grb_data, prev_colors, last_good_frame_time, led_turned_off = result

            elif ep.audio_enabled:
                raw_rgb, grb_data = self._frame_audio(
                    ep, loop_start, frame_count,
                    last_good_frame_time, last_recreate_time, led_turned_off,
                )
                if ep.display_enabled:
                    last_good_frame_time = time.monotonic()
                    led_turned_off = False

            else:
                raw_rgb, grb_data = self._frame_static(ep, loop_start)

            # ── USB 전송 ──
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
                        break
                    continue

            # ── 시그널 (3프레임마다) ──
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
    #  경로 A: 미러링 전용 (D=ON, A=OFF)
    # ══════════════════════════════════════════════════════════════

    def _frame_mirror_only(self, ep, prev_colors,
                           last_good_frame_time, last_recreate_time,
                           led_turned_off, frame_count):
        """미러링 전용 프레임 처리."""
        pipeline = self._pipeline

        # ── 밝기 / 스무딩 반영 ──
        if ep.master_brightness != self._last_brightness:
            pipeline.update_brightness(ep.master_brightness)
            self._last_brightness = ep.master_brightness
        pipeline.smoothing = ep.smoothing_factor
        pipeline.smoothing_enabled = ep.smoothing_enabled

        # ── 캡처 ──
        frame = self._capture.grab()

        if frame is None:
            return None

        new_last_good = time.monotonic()
        new_led_off = False
        if led_turned_off:
            self.status_changed.emit("미러링 실행 중")

        # ── 해상도 변경 감지 ──
        try:
            current_h, current_w = frame.shape[:2]
        except (AttributeError, ValueError):
            return None

        if not getattr(self, '_native_capture', False):
            if current_h != self._active_h or current_w != self._active_w:
                self._active_w, self._active_h = current_w, current_h
                self._capture.screen_w = current_w
                self._capture.screen_h = current_h
                new_gc, new_gr = self._resolve_grid_size(current_w, current_h)
                if (new_gc != self._active_grid_cols
                        or new_gr != self._active_grid_rows):
                    self._display_change_flag.set()
                    return None
                try:
                    self._weight_matrix = self._build_layout(current_w, current_h)
                    self._rebuild_pipeline()
                    pipeline = self._pipeline
                    prev_colors = None
                except (ValueError, IndexError, np.linalg.LinAlgError):
                    pass
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
                    import cv2
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

        return raw_preview, grb_data, new_prev, new_last_good, new_led_off

    def _compute_mirror_zone_colors(self, frame, ep):
        """N구역 미러링 색상 계산."""
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
        """per-LED + distinctive 미러링."""
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
        """stale 프레임 처리."""
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
        """오디오 ON 프레임 처리."""
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)

        # ── 오디오 엔진 파라미터 반영 ──
        eng = self._audio_engine
        eng.bass_sensitivity = ep.bass_sensitivity
        eng.mid_sensitivity = ep.mid_sensitivity
        eng.high_sensitivity = ep.high_sensitivity
        eng.smoothing = ep.input_smoothing

        # ── 오디오 데이터 수집 ──
        bands = eng.get_band_energies()
        raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
        raw_spectrum = eng.get_spectrum()

        # ── Attack/Release 스무딩 ──
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

        # ── 화면 색상 갱신 (D=ON일 때만, N프레임마다) ──
        if (ep.display_enabled
                and self._capture is not None
                and self._weight_matrix is not None
                and frame_count % SCREEN_UPDATE_INTERVAL == 0):
            self._update_screen_colors(ep)

        # ── 색상 효과 애니메이션 (D=OFF + animated 효과) ──
        if (ep.color_effect != COLOR_EFFECT_STATIC
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

        # ── D=ON + animated 효과 (화면 색 미사용 시에만 적용) ──
        if (ep.color_effect != COLOR_EFFECT_STATIC
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

        # ── ★ 하이브리드 그라데이션: 프레임 단위 복사본에 변조 ──
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

        # ── 오디오 모드별 렌더링 ──
        audio_mode = ep.audio_mode

        if audio_mode == AUDIO_WAVE:
            self._wave_last_spawn = wave_tick_pulses(
                self._wave_pulses, frame_interval,
                bass, self._wave_prev_bass,
                self._wave_last_spawn, loop_start,
                speed=ep.wave_speed,
            )
            self._wave_prev_bass = bass
            raw_rgb = vectorized_render_wave(
                frame_base_colors, self._led_norm_y,
                self._wave_pulses,
                ep.min_brightness, ep.master_brightness,
                speed=ep.wave_speed,
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
            raw_rgb = vectorized_render_dynamic(
                frame_base_colors, self._dyn_clockwise_t,
                self._dyn_ripples, high,
                ep.min_brightness, ep.master_brightness,
            )

        elif audio_mode == AUDIO_FLOWING and ep.display_enabled:
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
                )
                self._flow_palette_colors = None
                self._flow_palette_ratios = None

        elif audio_mode == AUDIO_BASS_DETAIL:
            bd_spec = self._process_bass_detail(eng, atk, rel, ep)
            raw_rgb = vectorized_render_spectrum(
                frame_base_colors, self._led_band_indices,
                bd_spec, ep.min_brightness, ep.master_brightness,
            )

        elif audio_mode == AUDIO_SPECTRUM:
            raw_rgb = vectorized_render_spectrum(
                frame_base_colors, self._led_band_indices,
                spec, ep.min_brightness, ep.master_brightness,
            )

        else:  # AUDIO_PULSE (또는 flowing + D=OFF → pulse fallback)
            raw_rgb = vectorized_render_pulse(
                frame_base_colors, bass, mid, high,
                ep.min_brightness, ep.master_brightness,
            )

        # ── 색상 보정 + GRB 변환 ──
        leds_out = raw_rgb.copy()
        self._cc.apply(leds_out)
        grb_data = leds_to_grb(leds_out)

        return raw_rgb, grb_data

    def _update_screen_colors(self, ep):
        """화면 캡처 → per_led_colors 갱신 + base_colors 재빌드."""
        screen_frame = self._capture.grab()
        if screen_frame is None:
            return

        try:
            grid_flat = screen_frame.reshape(-1, 3).astype(np.float32)
            self._per_led_colors = self._weight_matrix @ grid_flat

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
        """양쪽 OFF — 정적/애니메이션 색상 출력."""
        n_leds = self._led_count
        n_bands = 16

        if ep.color_effect == COLOR_EFFECT_STATIC:
            if self._static_dirty or self._cached_base_colors is None:
                self._rebuild_base_colors_for_color_panel(ep)
            raw_rgb = self._cached_base_colors.copy()
            raw_rgb *= ep.master_brightness
        else:
            # 애니메이션: 매 프레임 갱신 — ★ Phase 8: 누적 위상 사용
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
        """Bass Detail 스펙트럼 처리."""
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

    def _emit_status_message(self, ep):
        """토글 조합에 따른 상태 메시지."""
        if ep.display_enabled and ep.audio_enabled:
            self.status_changed.emit("하이브리드 비주얼라이저 실행 중")
        elif ep.display_enabled:
            self.status_changed.emit("미러링 실행 중")
        elif ep.audio_enabled:
            self.status_changed.emit("오디오 비주얼라이저 실행 중")
        else:
            self.status_changed.emit("정적 LED 실행 중")