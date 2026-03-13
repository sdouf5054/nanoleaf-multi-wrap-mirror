"""오디오/하이브리드 엔진 Mixin — 루프 + 렌더링 + 색상 헬퍼

UnifiedEngine에 mix-in으로 결합되어 오디오/하이브리드 모드의
메인 루프와 렌더링 로직을 제공합니다.

[변경] 하이브리드 캡처 통합
- ScreenSampler 제거 → self._capture + weight_matrix 직접 사용
- _get_screen_color: per-LED 색상 또는 구역별 평균에서 색상 취득
- 프리뷰는 항상 색상 보정 전(raw) 전송

[self 속성 의존성] — UnifiedEngine에서 초기화
    _stop_event, _paused, _zone_dirty, _audio_engine,
    bass_sensitivity, mid_sensitivity, high_sensitivity,
    input_smoothing, _smooth_bass, _smooth_mid, _smooth_high,
    _smooth_spectrum, audio_mode, audio_brightness, audio_min_brightness,
    _led_count, _led_band_indices, _cc, _bd_band_bins, _bd_agc, _bd_smooth,
    base_color, rainbow, color_source, mode, n_zones, _per_led_colors,
    _hybrid_zone_map, _hybrid_zone_colors, _capture, _weight_matrix,
    target_fps, attack, release,
    _display_change_flag,
    # 시그널
    status_changed, fps_updated, energy_updated, spectrum_updated,
    screen_colors_updated, _device,
    # 메서드
    _rebuild_band_mapping,
    _handle_display_change,
"""

import time
import numpy as np

from core.engine_utils import (
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SCREEN, COLOR_SOURCE_SOLID,
    MODE_HYBRID, N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL, BASS_DETAIL_N_BANDS,
    per_led_to_zone_colors,
)
from core.constants import HW_ERRORS


class AudioEngineMixin:
    """오디오/하이브리드 모드의 메인 루프와 렌더링 메서드."""

    # ══════════════════════════════════════════════════════════════
    #  오디오 루프
    # ══════════════════════════════════════════════════════════════

    def _run_audio(self):
        """오디오 비주얼라이저 메인 루프."""
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        self.status_changed.emit("오디오 비주얼라이저 실행 중")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            if self._zone_dirty:
                self._rebuild_band_mapping()

            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            atk = 0.15 + self.attack * 0.70
            rel = 0.25 - self.release * 0.245

            self._smooth_bass = self._ar(self._smooth_bass, raw_bass, atk, rel)
            self._smooth_mid = self._ar(self._smooth_mid, raw_mid, atk, rel)
            self._smooth_high = self._ar(self._smooth_high, raw_high, atk, rel)
            for i in range(len(self._smooth_spectrum)):
                self._smooth_spectrum[i] = self._ar(
                    self._smooth_spectrum[i], raw_spectrum[i], atk, rel
                )

            bass = self._smooth_bass
            mid = self._smooth_mid
            high = self._smooth_high
            spec = self._smooth_spectrum

            mode = self.audio_mode

            raw_rgb = None
            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data, raw_rgb = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data, raw_rgb = self._render_spectrum(spec)
            else:
                grb_data, raw_rgb = self._render_pulse(bass, mid, high)

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
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

                if raw_rgb is not None:
                    self.screen_colors_updated.emit(raw_rgb)

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
    #  하이브리드 루프
    # ══════════════════════════════════════════════════════════════

    def _run_hybrid(self):
        """하이브리드 비주얼라이저 메인 루프.

        ★ 변경: ScreenSampler 대신 self._capture + weight_matrix를 직접 사용.
        화면 색상은 미러링과 동일한 품질로 per-LED 단위로 계산됩니다.
        """
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")
        self._start_monitor_watcher()

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ★ 디스플레이 변경 처리
            if self._display_change_flag.is_set():
                self._handle_display_change()

            if self._zone_dirty:
                self._rebuild_band_mapping()

            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            atk = 0.15 + self.attack * 0.70
            rel = 0.25 - self.release * 0.245

            self._smooth_bass = self._ar(self._smooth_bass, raw_bass, atk, rel)
            self._smooth_mid = self._ar(self._smooth_mid, raw_mid, atk, rel)
            self._smooth_high = self._ar(self._smooth_high, raw_high, atk, rel)
            for i in range(len(self._smooth_spectrum)):
                self._smooth_spectrum[i] = self._ar(
                    self._smooth_spectrum[i], raw_spectrum[i], atk, rel
                )

            bass = self._smooth_bass
            mid = self._smooth_mid
            high = self._smooth_high
            spec = self._smooth_spectrum

            # ★ 화면 색상 갱신: capture + weight_matrix
            if (self.color_source != COLOR_SOURCE_SOLID
                    and self._capture is not None
                    and self._weight_matrix is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                screen_frame = self._capture.grab()
                if screen_frame is not None:
                    try:
                        grid_flat = screen_frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat
                        # N구역 모드: 구역별 평균 캐시 갱신
                        if (self.n_zones != N_ZONES_PER_LED
                                and self._hybrid_zone_map is not None):
                            self._hybrid_zone_colors = per_led_to_zone_colors(
                                self._per_led_colors,
                                self._hybrid_zone_map,
                                self.n_zones,
                            )
                    except (ValueError, IndexError):
                        pass

            mode = self.audio_mode
            bd_spec = None

            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data, _raw = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data, _raw = self._render_spectrum(spec)
            else:
                grb_data, _raw = self._render_pulse(bass, mid, high)

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
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

                # ★ 프리뷰: 항상 보정 전 raw 색상
                if _raw is not None:
                    self.screen_colors_updated.emit(_raw)

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
    #  렌더링 — Pulse
    # ══════════════════════════════════════════════════════════════

    def _render_pulse(self, bass, mid, high):
        """Pulse 렌더링.

        Returns: (grb_bytes, raw_rgb_for_preview)
        raw_rgb는 색상 보정 전 값 (프리뷰용).
        """
        n_leds = self._led_count
        n_bands = len(self._smooth_spectrum) if self._smooth_spectrum is not None else 16
        min_b = self.audio_min_brightness
        intensity = max(min_b, bass) * self.audio_brightness

        leds = np.zeros((n_leds, 3), dtype=np.float32)
        for led_idx in range(n_leds):
            color = self._get_base_color(led_idx, n_bands)
            c = color * (0.7 + mid * 0.3)
            white_mix = high * 0.3
            c = c * (1 - white_mix) + 255.0 * white_mix
            leds[led_idx] = c * intensity

        # ★ 프리뷰용: 보정 전 raw
        raw_rgb = leds.copy()

        # LED 출력용: 보정 적용
        self._cc.apply(leds)
        return self._leds_to_grb(leds), raw_rgb

    # ══════════════════════════════════════════════════════════════
    #  렌더링 — Spectrum
    # ══════════════════════════════════════════════════════════════

    def _render_spectrum(self, spec):
        """Spectrum 렌더링.

        Returns: (grb_bytes, raw_rgb_for_preview)
        raw_rgb는 색상 보정 전 값 (프리뷰용).
        """
        n_leds = self._led_count
        n_bands = len(spec)
        min_b = self.audio_min_brightness
        leds = np.zeros((n_leds, 3), dtype=np.float32)

        for led_idx in range(n_leds):
            band_f = self._led_band_indices[led_idx]
            band_lo = max(0, min(int(band_f), n_bands - 1))
            band_hi = min(band_lo + 1, n_bands - 1)
            frac = band_f - int(band_f)
            energy = spec[band_lo] * (1 - frac) + spec[band_hi] * frac

            color = self._get_base_color(led_idx, n_bands)
            intensity = max(min_b, energy) * self.audio_brightness
            leds[led_idx] = color * intensity

        # ★ 프리뷰용: 보정 전 raw
        raw_rgb = leds.copy()

        # LED 출력용: 보정 적용
        self._cc.apply(leds)
        return self._leds_to_grb(leds), raw_rgb

    # ══════════════════════════════════════════════════════════════
    #  Bass Detail 처리
    # ══════════════════════════════════════════════════════════════

    def _process_bass_detail(self, eng, atk_rate, rel_rate):
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
        val = np.minimum(1.0, (normalized * self.bass_sensitivity) ** 1.5)

        for i in range(n):
            self._bd_smooth[i] = self._ar(self._bd_smooth[i], val[i], atk_rate, rel_rate)

        return self._bd_smooth

    # ══════════════════════════════════════════════════════════════
    #  색상 헬퍼
    # ══════════════════════════════════════════════════════════════

    def _get_base_color(self, led_idx, n_bands):
        """LED의 기본 색상을 결정.

        하이브리드 + screen 소스: per-LED 화면 색상 사용
        그 외: 단색 또는 무지개
        """
        source = self.color_source

        if source == COLOR_SOURCE_SCREEN and self.mode == MODE_HYBRID:
            return self._get_screen_color(led_idx, n_bands)

        return self._get_solid_color(led_idx, n_bands)

    def _get_solid_color(self, led_idx, n_bands):
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    def _get_screen_color(self, led_idx, n_bands):
        """★ 하이브리드 화면 색상 — per-LED 또는 구역별 평균.

        self._per_led_colors가 capture + weight_matrix로 계산된
        보정 전 raw RGB이므로, 그대로 반환합니다.

        N구역 모드에서는 _hybrid_zone_colors 캐시를 사용합니다.
        캐시는 _update_hybrid_zone_colors()에서 스크린 갱신 시 계산됩니다.
        """
        if self._per_led_colors is None:
            return self._get_solid_color(led_idx, n_bands)

        if self.n_zones == N_ZONES_PER_LED:
            # per-LED: weight_matrix 결과를 직접 사용
            return self._per_led_colors[led_idx].copy()

        # N구역 모드: 캐시된 구역 색상에서 LED의 구역 인덱스로 참조
        if (self._hybrid_zone_map is not None
                and self._hybrid_zone_colors is not None):
            zone_idx = self._hybrid_zone_map[led_idx]
            if 0 <= zone_idx < len(self._hybrid_zone_colors):
                return self._hybrid_zone_colors[zone_idx].copy()

        return self._get_solid_color(led_idx, n_bands)

    @staticmethod
    def _band_color(t):
        keypoints = [
            (0.000, 255,   0,   0),
            (0.130, 255, 127,   0),
            (0.260, 255, 255,   0),
            (0.400,   0, 255,   0),
            (0.540,   0, 180, 255),
            (0.680,   0,  50, 255),
            (0.820,  80,   0, 255),
            (1.000, 160,   0, 220),
        ]

        t = max(0.0, min(1.0, t))

        for i in range(len(keypoints) - 1):
            t0, r0, g0, b0 = keypoints[i]
            t1, r1, g1, b1 = keypoints[i + 1]
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                r = r0 + (r1 - r0) * f
                g = g0 + (g1 - g0) * f
                b = b0 + (b1 - b0) * f
                return np.array([r, g, b], dtype=np.float32)

        return np.array([160, 0, 220], dtype=np.float32)

    @staticmethod
    def _ar(current, target, attack_rate, release_rate):
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    @staticmethod
    def _leds_to_grb(leds):
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]
        grb[:, 1] = u8[:, 0]
        grb[:, 2] = u8[:, 2]
        return grb.tobytes()
