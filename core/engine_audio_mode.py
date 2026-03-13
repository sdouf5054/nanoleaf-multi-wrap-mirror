"""AudioModeEngine — 오디오 모드 엔진 (ADR-015 서브클래스)

BaseEngine을 상속하여 오디오 비주얼라이저 전용 메인 루프를 구현합니다.
ADR-014 벡터화 렌더링 함수를 사용하여 per-LED Python 루프를 제거.
"""

import time
import numpy as np

from core.base_engine import BaseEngine
from core.color_correction import ColorCorrection
from core.audio_engine import AudioEngine as AudioCapture, _build_log_bands
from core.constants import HW_ERRORS
from core.engine_utils import (
    MODE_AUDIO, AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    BASS_DETAIL_FREQ_MIN, BASS_DETAIL_FREQ_MAX, BASS_DETAIL_N_BANDS,
    _compute_led_perimeter_t, _compute_led_band_mapping,
    _build_led_order_from_segments,
    build_base_color_array, vectorized_render_pulse,
    vectorized_render_spectrum, leds_to_grb,
)


class AudioModeEngine(BaseEngine):
    """오디오 비주얼라이저 모드 엔진."""

    mode = MODE_AUDIO

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self._audio_engine: AudioCapture | None = None
        self._cc: ColorCorrection | None = None

        # 밴드 매핑
        self._perimeter_t = None
        self._led_band_indices = None
        self._led_order = []

        # 스무딩 상태
        self._smooth_bass = 0.0
        self._smooth_mid = 0.0
        self._smooth_high = 0.0
        self._smooth_spectrum = None

        # Bass Detail
        self._bd_band_bins = None
        self._bd_agc = None
        self._bd_smooth = None

        # ADR-014: 캐시된 기본 색상 배열
        self._cached_base_colors = None
        self._cached_rainbow = None
        self._cached_base_color_tuple = None

    # ── 서브클래스 인터페이스 ─────────────────────────────────────

    def _init_mode_resources(self):
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

    def _init_band_mapping(self):
        """밴드 매핑 + bass detail 초기화."""
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

        # ADR-014: 초기 색상 배열 빌드
        self._rebuild_base_colors()

    def _cleanup_mode(self):
        if self._audio_engine:
            self._audio_engine.stop()
            self._audio_engine = None

    # ── ADR-014: 색상 배열 캐시 ──────────────────────────────────

    def _rebuild_base_colors(self):
        """색상/모드 변경 시 기본 색상 배열 재빌드."""
        ap = self._current_audio_params
        n_bands = self._audio_engine.n_bands if self._audio_engine else 16

        self._cached_base_colors = build_base_color_array(
            self._led_band_indices, n_bands,
            rainbow=ap.rainbow,
            solid_color=np.array(ap.base_color, dtype=np.float32),
        )
        self._cached_rainbow = ap.rainbow
        self._cached_base_color_tuple = ap.base_color

    def _maybe_rebuild_base_colors(self, ap):
        """색상 설정이 변경되었으면 재빌드."""
        if (ap.rainbow != self._cached_rainbow
                or ap.base_color != self._cached_base_color_tuple):
            self._rebuild_base_colors()

    # ── 메인 루프 ────────────────────────────────────────────────

    def _run_loop(self):
        ap = self._current_audio_params
        frame_interval = 1.0 / self.config["mirror"].get("target_fps", 60)
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait
        prev_zone_weights = ap.zone_weights

        self.status_changed.emit("오디오 비주얼라이저 실행 중")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ADR-003: 파라미터 스냅샷 교체
            self._swap_params()
            ap = self._current_audio_params

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # 대역 비율 변경 → 밴드 매핑 재계산
            if ap.zone_weights != prev_zone_weights:
                n_bands = self._audio_engine.n_bands
                self._led_band_indices = _compute_led_band_mapping(
                    self._perimeter_t, n_bands, ap.zone_weights
                )
                prev_zone_weights = ap.zone_weights
                self._rebuild_base_colors()

            # 색상 변경 → 기본 색상 재빌드
            self._maybe_rebuild_base_colors(ap)

            # 오디오 엔진 파라미터 반영
            eng = self._audio_engine
            eng.bass_sensitivity = ap.bass_sensitivity
            eng.mid_sensitivity = ap.mid_sensitivity
            eng.high_sensitivity = ap.high_sensitivity
            eng.smoothing = ap.input_smoothing

            # 오디오 데이터 수집
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # Attack/Release 스무딩
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
            else:  # AUDIO_PULSE
                raw_rgb = vectorized_render_pulse(
                    self._cached_base_colors, bass, mid, high,
                    ap.min_brightness, ap.brightness,
                )

            # 보정 + GRB 변환
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

            # 시그널 (3프레임마다)
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

    # ── Bass Detail 처리 ─────────────────────────────────────────

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


# ── 모듈 레벨 헬퍼 ───────────────────────────────────────────────

def _ar(current, target, attack_rate, release_rate):
    """Attack/Release 스무딩."""
    if target > current:
        return current + (target - current) * attack_rate
    else:
        return current + (target - current) * release_rate
