"""HybridEngine — 하이브리드 모드 엔진 (ADR-015 서브클래스)

오디오 + 화면 캡처를 결합. AudioModeEngine과 유사하나,
화면 캡처에서 얻은 per-LED 색상을 base_color로 사용합니다.
"""

import time
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.audio_engine import AudioEngine as AudioCapture, _build_log_bands
from core.constants import HW_ERRORS
from core.engine_utils import (
    MODE_HYBRID, AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SOLID, COLOR_SOURCE_SCREEN, N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _build_led_order_from_segments, _build_led_zone_map_by_side,
    per_led_to_zone_colors,
    build_base_color_array, vectorized_render_pulse,
    vectorized_render_spectrum, leds_to_grb,
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

    def _cleanup_mode(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None

    # ── 색상 배열 캐시 ───────────────────────────────────────────

    def _rebuild_base_colors(self):
        ap = self._current_audio_params
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16

        # 하이브리드: 화면 색상 소스면 per_led_colors를 사용
        screen = (self._per_led_colors
                  if ap.color_source == COLOR_SOURCE_SCREEN
                  and self._per_led_colors is not None
                  and self._per_led_colors.sum() > 0
                  else None)

        self._cached_base_colors = build_base_color_array(
            self._led_band_indices, n_bands,
            rainbow=ap.rainbow,
            solid_color=np.array(ap.base_color, dtype=np.float32),
            screen_colors=screen,
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

            # 디스플레이 변경
            if self._display_change_flag.is_set():
                self._handle_display_change()

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

            if audio_mode == AUDIO_BASS_DETAIL:
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
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
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
