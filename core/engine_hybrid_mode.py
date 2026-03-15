"""HybridEngine — 하이브리드 모드 엔진 (ADR-015 서브클래스)

오디오 + 화면 캡처를 결합. AudioModeEngine과 유사하나,
화면 캡처에서 얻은 per-LED 색상을 base_color로 사용합니다.

[변경] 절전 복귀 대응:
- _run_loop에서 _check_and_handle_session_resume() 호출
- USB 끊김 시 force_reconnect() 시도

[변경] Dynamic v2:
- onset 기반 spawn (prev_bass 추적)
- 면 균등 위치 (side_t_ranges 초기화)
"""

import time
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.audio_engine import AudioEngine as AudioCapture, _build_log_bands
from core.constants import HW_ERRORS
from core.engine_utils import (
    MODE_HYBRID, AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    AUDIO_WAVE, AUDIO_DYNAMIC,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN, N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _compute_led_clockwise_t,
    _build_led_order_from_segments, _build_led_zone_map_by_side,
    per_led_to_zone_colors, compute_side_t_ranges,
    build_base_color_array, vectorized_render_pulse,
    vectorized_render_spectrum, vectorized_render_wave,
    vectorized_render_dynamic, leds_to_grb,
    compute_led_normalized_y,
    WavePulse, wave_tick_pulses,
    DynamicRipple, dynamic_tick_ripples,
)
from core.engine_audio_mode import _ar


class HybridEngine(BaseEngine):
    """하이브리드 모드 엔진 — 오디오 + 화면 색상."""

    mode = MODE_HYBRID

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self._audio_engine: AudioCapture | None = None
        self._cc: ColorCorrection | None = None

        self._perimeter_t = None
        self._led_band_indices = None
        self._led_order = []

        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # 화면 색상
        self._per_led_colors = None
        self._hybrid_zone_map = None
        self._hybrid_zone_colors = None

        # ADR-014: 캐시
        self._cached_base_colors = None
        self._cached_rainbow = None
        self._cached_base_color_tuple = None

        # Wave 모드 상태
        self._wave_pulses = []
        self._wave_last_spawn = 0.0
        self._wave_prev_bass = 0.0
        self._led_norm_y = None

        # Dynamic 모드 상태
        self._dyn_ripples = []
        self._dyn_last_spawn = 0.0
        self._dyn_prev_bass = 0.0          # ★ Dynamic onset용 prev_bass
        self._dyn_prev_raw_bass = 0.0      # ★ raw_bass onset용
        self._dyn_side_t_ranges = None     # ★ 면별 t 범위
        self._dyn_clockwise_t = None       # ★ Dynamic 전용 비대칭 둘레 좌표

    # ── 서브클래스 인터페이스 ─────────────────────────────────────

    def _init_mode_resources(self):
        # 캡처 + 가중치 행렬 (미러링과 동일)
        self._init_capture()

        # 오디오
        self._cc = ColorCorrection(self.config.get("color", {}))
        self.status_changed.emit("오디오 캡처 초기화...")
        ap = self._current_audio_params
        self._audio_engine = AudioCapture(
            device_index=self._audio_device_index,
            sensitivity=1.0, smoothing=0.15,
        )
        self._audio_engine.bass_sensitivity = ap.bass_sensitivity
        self._audio_engine.mid_sensitivity = ap.mid_sensitivity
        self._audio_engine.high_sensitivity = ap.high_sensitivity
        self._audio_engine.start()

        self._init_band_mapping()

        # 하이브리드 리소스
        self._per_led_colors = np.zeros((self._led_count, 3), dtype=np.float32)
        if ap.n_zones != N_ZONES_PER_LED:
            self._hybrid_zone_map = _build_led_zone_map_by_side(
                self.config, ap.n_zones
            )

    def _init_band_mapping(self):
        ap = self._current_audio_params
        n_bands = self._audio_engine.n_bands

        self._perimeter_t = _compute_led_perimeter_t(self.config)
        self._led_band_indices = _compute_led_band_mapping(
            self._perimeter_t, n_bands, ap.zone_weights
        )
        segments = self.config.get("layout", {}).get("segments", [])
        self._led_order = _build_led_order_from_segments(segments, self._led_count)

        self._smooth_bass = self._smooth_mid = self._smooth_high = 0.0
        self._smooth_spectrum = np.zeros(n_bands, dtype=np.float64)

        fft_freqs = self._audio_engine.fft_freqs
        self._bd_band_bins = _build_log_bands(
            BASS_DETAIL_N_BANDS, BASS_DETAIL_FREQ_MIN,
            BASS_DETAIL_FREQ_MAX, fft_freqs
        )
        self._bd_agc = np.full(BASS_DETAIL_N_BANDS, 0.01, dtype=np.float64)
        self._bd_smooth = np.zeros(BASS_DETAIL_N_BANDS, dtype=np.float64)

        self._rebuild_base_colors()

        # Wave 모드용 정규화 y좌표
        self._led_norm_y = compute_led_normalized_y(self.config)

        # ★ Dynamic v3.1: clockwise 둘레 좌표 + 면별 t 범위
        self._dyn_clockwise_t = _compute_led_clockwise_t(self.config)
        self._dyn_side_t_ranges = compute_side_t_ranges(self.config)

    def _cleanup_mode(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None

    # ── 색상 배열 캐시 ───────────────────────────────────────────

    def _rebuild_base_colors(self):
        """하이브리드 기본 색상 배열 재빌드.

        Fix #4: n_zones != per-LED일 때 구역 평균을 적용.
        Fix #5: 화면 색상은 weight_matrix 결과를 직접 사용 (미러링과 동일 품질).
        """
        ap = self._current_audio_params
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16

        if (ap.color_source == COLOR_SOURCE_SCREEN
                and self._per_led_colors is not None
                and self._per_led_colors.sum() > 0):

            if (ap.n_zones != N_ZONES_PER_LED
                    and self._hybrid_zone_map is not None):
                # N구역: 구역별 평균 → zone_map으로 LED에 재할당
                zone_avg = per_led_to_zone_colors(
                    self._per_led_colors, self._hybrid_zone_map, ap.n_zones
                )
                screen = zone_avg[self._hybrid_zone_map]
            else:
                # per-LED: weight_matrix 결과를 직접 사용
                screen = self._per_led_colors.copy()

            self._cached_base_colors = screen
        else:
            self._cached_base_colors = build_base_color_array(
                self._led_band_indices, n_bands,
                rainbow=ap.rainbow,
                solid_color=np.array(ap.base_color, dtype=np.float32),
            )

        self._cached_rainbow = ap.rainbow
        self._cached_base_color_tuple = ap.base_color

    # ── 메인 루프 ────────────────────────────────────────────────

    def _run_loop(self):
        ap = self._current_audio_params
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait
        prev_zone_weights = ap.zone_weights
        prev_n_zones = ap.n_zones

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")
        self._start_monitor_watcher()

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            self._swap_params()
            ap = self._current_audio_params

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ★ 세션 복귀 (절전모드)
            self._check_and_handle_session_resume()

            # 디스플레이 변경
            if self._display_change_flag.is_set():
                self._handle_display_change()

            # n_zones 런타임 변경 감지 → zone_map 재빌드
            if ap.n_zones != prev_n_zones:
                prev_n_zones = ap.n_zones
                if ap.n_zones != N_ZONES_PER_LED:
                    self._hybrid_zone_map = _build_led_zone_map_by_side(
                        self.config, ap.n_zones
                    )
                else:
                    self._hybrid_zone_map = None
                self._rebuild_base_colors()

            # 대역 비율 변경
            if ap.zone_weights != prev_zone_weights:
                n_bands = self._audio_engine.n_bands
                self._led_band_indices = _compute_led_band_mapping(
                    self._perimeter_t, n_bands, ap.zone_weights
                )
                prev_zone_weights = ap.zone_weights
                self._rebuild_base_colors()

            # 오디오 파라미터
            eng = self._audio_engine
            eng.bass_sensitivity = ap.bass_sensitivity
            eng.mid_sensitivity = ap.mid_sensitivity
            eng.high_sensitivity = ap.high_sensitivity
            eng.smoothing = ap.input_smoothing

            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            atk = 0.15 + ap.attack * 0.70
            rel = 0.25 - ap.release * 0.245

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

            # ── 화면 색상 갱신 ──
            if (ap.color_source != COLOR_SOURCE_SOLID
                    and self._capture is not None
                    and self._weight_matrix is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                screen_frame = self._capture.grab()
                if screen_frame is not None:
                    try:
                        grid_flat = screen_frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat

                        if (ap.n_zones != N_ZONES_PER_LED
                                and self._hybrid_zone_map is not None):
                            self._hybrid_zone_colors = per_led_to_zone_colors(
                                self._per_led_colors,
                                self._hybrid_zone_map,
                                ap.n_zones,
                            )

                        # 화면 색상이 갱신되면 base_colors도 재빌드
                        self._rebuild_base_colors()
                    except (ValueError, IndexError):
                        pass

            # ── 렌더링 (ADR-014 벡터화) ──
            audio_mode = ap.audio_mode
            bd_spec = None

            if audio_mode == AUDIO_WAVE:
                self._wave_last_spawn = wave_tick_pulses(
                    self._wave_pulses, frame_interval,
                    bass, self._wave_prev_bass,
                    self._wave_last_spawn, loop_start,
                    speed=ap.wave_speed,
                )
                self._wave_prev_bass = bass
                raw_rgb = vectorized_render_wave(
                    self._cached_base_colors, self._led_norm_y,
                    self._wave_pulses,
                    ap.min_brightness, ap.brightness,
                    speed=ap.wave_speed,
                )
            elif audio_mode == AUDIO_DYNAMIC:
                # ★ Dynamic v3.2: raw_bass로 onset 감지, smooth bass로 energy
                self._dyn_last_spawn = dynamic_tick_ripples(
                    self._dyn_ripples, frame_interval,
                    bass, mid, high,
                    self._dyn_clockwise_t,
                    self._dyn_last_spawn, loop_start,
                    prev_bass=self._dyn_prev_bass,
                    side_t_ranges=self._dyn_side_t_ranges,
                    attack=ap.attack,
                    release=ap.release,
                    sensitivity=ap.bass_sensitivity,
                    raw_bass=raw_bass,
                    prev_raw_bass=self._dyn_prev_raw_bass,
                )
                self._dyn_prev_bass = bass
                self._dyn_prev_raw_bass = raw_bass
                raw_rgb = vectorized_render_dynamic(
                    self._cached_base_colors, self._dyn_clockwise_t,
                    self._dyn_ripples, high,
                    ap.min_brightness, ap.brightness,
                )
            elif audio_mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel, ap)
                raw_rgb = vectorized_render_spectrum(
                    self._cached_base_colors, self._led_band_indices,
                    bd_spec, ap.min_brightness, ap.brightness,
                )
            elif audio_mode == AUDIO_SPECTRUM:
                raw_rgb = vectorized_render_spectrum(
                    self._cached_base_colors, self._led_band_indices,
                    spec, ap.min_brightness, ap.brightness,
                )
            else:
                raw_rgb = vectorized_render_pulse(
                    self._cached_base_colors, bass, mid, high,
                    ap.min_brightness, ap.brightness,
                )

            leds_out = raw_rgb.copy()
            self._cc.apply(leds_out)
            grb_data = leds_to_grb(leds_out)

            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김 — 재연결 시도 중...")
                # ★ 즉시 재연결 시도
                self._device.force_reconnect()
                if self._device.connected:
                    self.status_changed.emit("USB 재연결 성공 — 하이브리드 실행 중")
                else:
                    if stop_wait(timeout=2.0):
                        break
                continue

            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if audio_mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())
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

    # ── Bass Detail ──────────────────────────────────────────────

    def _process_bass_detail(self, eng, atk_rate, rel_rate, ap):
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
        val = np.minimum(1.0, (normalized * ap.bass_sensitivity) ** 1.5)

        for i in range(n):
            self._bd_smooth[i] = _ar(self._bd_smooth[i], val[i], atk_rate, rel_rate)

        return self._bd_smooth