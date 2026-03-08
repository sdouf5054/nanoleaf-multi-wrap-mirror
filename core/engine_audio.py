"""오디오/하이브리드 엔진 Mixin — 루프 + 렌더링 + 색상 헬퍼

UnifiedEngine에 mix-in으로 결합되어 오디오/하이브리드 모드의
메인 루프와 렌더링 로직을 제공합니다.

[self 속성 의존성] — UnifiedEngine에서 초기화
    _stop_event, _paused, _zone_dirty, _audio_engine,
    bass_sensitivity, mid_sensitivity, high_sensitivity,
    input_smoothing, _smooth_bass, _smooth_mid, _smooth_high,
    _smooth_spectrum, audio_mode, audio_brightness, audio_min_brightness,
    _led_count, _led_band_indices, _cc, _bd_band_bins, _bd_agc, _bd_smooth,
    base_color, rainbow, color_source, mode, n_zones, _per_led_colors,
    _led_zone_map, _screen_sampler, _weight_matrix,
    target_fps, attack, release,
    # 시그널
    status_changed, fps_updated, energy_updated, spectrum_updated,
    screen_colors_updated, _device,
    # 메서드
    _rebuild_band_mapping, _ensure_screen_sampler,
"""

import time
import numpy as np

from core.engine_utils import (
    AUDIO_PULSE, AUDIO_SPECTRUM, AUDIO_BASS_DETAIL,
    COLOR_SOURCE_SCREEN, COLOR_SOURCE_SOLID,
    MODE_HYBRID, N_ZONES_PER_LED,
    SCREEN_UPDATE_INTERVAL, BASS_DETAIL_N_BANDS,
)
from core.constants import HW_ERRORS


class AudioEngineMixin:
    """오디오/하이브리드 모드의 메인 루프와 렌더링 메서드.

    UnifiedEngine(QThread)와 함께 다중 상속으로 결합됩니다.
    self.* 속성은 모두 UnifiedEngine.__init__에서 초기화됩니다.
    """

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

            # ── 일시정지 ──
            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 대역 비율 변경 반영 ──
            if self._zone_dirty:
                self._rebuild_band_mapping()

            # ── 오디오 엔진 파라미터 실시간 반영 ──
            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            # ── 오디오 데이터 획득 ──
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # ── Attack/Release 스무딩 ──
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

            # ── 렌더링 ──
            raw_rgb = None
            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data, raw_rgb = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data, raw_rgb = self._render_spectrum(spec)
            else:  # pulse
                grb_data, raw_rgb = self._render_pulse(bass, mid, high)

            # ── USB 전송 ──
            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── UI 시그널 (매 3프레임) ──
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

                # 프리뷰 (보정 전 색상)
                if raw_rgb is not None:
                    self.screen_colors_updated.emit(raw_rgb)

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  하이브리드 루프
    # ══════════════════════════════════════════════════════════════

    def _run_hybrid(self):
        """하이브리드 비주얼라이저 메인 루프 — 오디오 + 화면 색상 소스."""
        frame_interval = 1.0 / self.target_fps
        frame_count = 0
        fps_start = time.monotonic()
        fps_display = fps_start
        stop_wait = self._stop_event.wait

        self.status_changed.emit("하이브리드 비주얼라이저 실행 중")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # ── 일시정지 ──
            if self._paused:
                if stop_wait(timeout=0.05):
                    break
                continue

            # ── 대역 비율 변경 반영 ──
            if self._zone_dirty:
                self._rebuild_band_mapping()

            # ── screen 소스 보장 ──
            if self.color_source != COLOR_SOURCE_SOLID:
                self._ensure_screen_sampler()

            # ── 오디오 엔진 파라미터 실시간 반영 ──
            eng = self._audio_engine
            eng.bass_sensitivity = self.bass_sensitivity
            eng.mid_sensitivity = self.mid_sensitivity
            eng.high_sensitivity = self.high_sensitivity
            eng.smoothing = self.input_smoothing

            # ── 오디오 데이터 획득 ──
            bands = eng.get_band_energies()
            raw_bass, raw_mid, raw_high = bands["bass"], bands["mid"], bands["high"]
            raw_spectrum = eng.get_spectrum()

            # ── Attack/Release 스무딩 ──
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

            # ── 스크린 갱신 (매 N프레임) ──
            if (self._screen_sampler is not None
                    and frame_count % SCREEN_UPDATE_INTERVAL == 0):
                self._screen_sampler.update()

                # per-LED 미러링: weight_matrix로 frame → per-LED 색상
                if (self.n_zones == N_ZONES_PER_LED
                        and self._weight_matrix is not None):
                    frame = self._screen_sampler.get_last_frame()
                    if frame is not None:
                        grid_flat = frame.reshape(-1, 3).astype(np.float32)
                        self._per_led_colors = self._weight_matrix @ grid_flat

            # ── 렌더링 (렌더 메서드는 audio와 공유) ──
            mode = self.audio_mode
            bd_spec = None

            if mode == AUDIO_BASS_DETAIL:
                bd_spec = self._process_bass_detail(eng, atk, rel)
                grb_data, _raw = self._render_spectrum(bd_spec)
            elif mode == AUDIO_SPECTRUM:
                grb_data, _raw = self._render_spectrum(spec)
            else:  # pulse
                grb_data, _raw = self._render_pulse(bass, mid, high)

            # ── USB 전송 ──
            try:
                self._device.send_rgb(grb_data)
            except HW_ERRORS:
                pass

            if not self._device.connected:
                self.status_changed.emit("USB 연결 끊김")
                if stop_wait(timeout=1.0):
                    break
                continue

            # ── UI 시그널 (매 3프레임) ──
            if frame_count % 3 == 0:
                self.energy_updated.emit(bass, mid, high)
                if mode == AUDIO_BASS_DETAIL and bd_spec is not None:
                    self.spectrum_updated.emit(bd_spec.copy())
                else:
                    self.spectrum_updated.emit(spec.copy())

                # screen_colors_updated 시그널 (프리뷰용 — 항상 per-LED로 변환)
                if self._screen_sampler is not None:
                    if self.n_zones == N_ZONES_PER_LED:
                        if self._per_led_colors is not None:
                            self.screen_colors_updated.emit(
                                self._per_led_colors.copy()
                            )
                    else:
                        # zone_colors → per-LED 매핑
                        if self.n_zones == 1:
                            zc = self._screen_sampler.get_global_color().reshape(1, 3)
                        else:
                            zc = self._screen_sampler.get_zone_colors()
                        if (self._led_zone_map is not None
                                and zc is not None and len(zc) > 0):
                            led_colors = np.zeros(
                                (self._led_count, 3), dtype=np.float32
                            )
                            for i in range(self._led_count):
                                zi = self._led_zone_map[i]
                                if 0 <= zi < len(zc):
                                    led_colors[i] = zc[zi]
                            self.screen_colors_updated.emit(led_colors)

            # ── FPS 계산 ──
            frame_count += 1
            now = time.monotonic()
            if now - fps_display >= 1.0:
                fps = frame_count / (now - fps_start) if (now - fps_start) > 0 else 0
                self.fps_updated.emit(fps)
                fps_display = now

            # ── 프레임 간격 대기 ──
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                if stop_wait(timeout=sleep_time):
                    break

    # ══════════════════════════════════════════════════════════════
    #  렌더링 — Pulse
    # ══════════════════════════════════════════════════════════════

    def _render_pulse(self, bass, mid, high):
        """Bass 에너지 기반 전체 밝기 + mid/high 색상 변조.

        Returns:
            (grb_data, raw_rgb): GRB bytes + 보정 전 RGB (프리뷰용)
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

        raw_rgb = leds.copy()
        self._cc.apply(leds)
        return self._leds_to_grb(leds), raw_rgb

    # ══════════════════════════════════════════════════════════════
    #  렌더링 — Spectrum (spectrum + bass_detail 공용)
    # ══════════════════════════════════════════════════════════════

    def _render_spectrum(self, spec):
        """주파수 밴드별 밝기 — 각 LED의 둘레 위치에 매핑.

        Returns:
            (grb_data, raw_rgb): GRB bytes + 보정 전 RGB (프리뷰용)
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

        raw_rgb = leds.copy()
        self._cc.apply(leds)
        return self._leds_to_grb(leds), raw_rgb

    # ══════════════════════════════════════════════════════════════
    #  Bass Detail 처리
    # ══════════════════════════════════════════════════════════════

    def _process_bass_detail(self, eng, atk_rate, rel_rate):
        """raw FFT에서 저역(20~500Hz)만 16밴드로 분할 + AGC + 스무딩."""
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
        """LED 인덱스 → RGB 색상.

        색상 소스에 따라 분기:
        - solid: 단색 또는 무지개
        - screen: 화면 구역색 또는 per-LED 미러링색
        """
        source = self.color_source

        if source == COLOR_SOURCE_SCREEN and self.mode == MODE_HYBRID:
            return self._get_screen_color(led_idx, n_bands)

        return self._get_solid_color(led_idx, n_bands)

    def _get_solid_color(self, led_idx, n_bands):
        """단색 또는 무지개 색상."""
        if self.rainbow:
            t = self._led_band_indices[led_idx] / max(1, n_bands - 1)
            return self._band_color(t)
        else:
            return self.base_color.copy()

    def _get_screen_color(self, led_idx, n_bands):
        """화면 색상 소스 — zone 매핑 또는 per-LED 미러링."""
        if self._screen_sampler is None or not self._screen_sampler.has_data:
            return self._get_solid_color(led_idx, n_bands)

        if self.n_zones == N_ZONES_PER_LED:
            if self._per_led_colors is not None:
                return self._per_led_colors[led_idx].copy()
            return self._get_solid_color(led_idx, n_bands)

        if self.n_zones == 1:
            return self._screen_sampler.get_global_color().copy()

        zone_idx = self._led_zone_map[led_idx]
        zone_colors = self._screen_sampler.get_zone_colors()

        if zone_idx >= len(zone_colors):
            zone_idx = zone_idx % len(zone_colors)

        return zone_colors[zone_idx].copy()

    @staticmethod
    def _band_color(t):
        """밴드 위치(0=저음, 1=고음) → RGB 무지개색."""
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
        """Attack/Release 스무딩."""
        if target > current:
            return current + (target - current) * attack_rate
        else:
            return current + (target - current) * release_rate

    @staticmethod
    def _leds_to_grb(leds):
        """RGB float32 배열 → GRB bytes."""
        np.clip(leds, 0, 255, out=leds)
        u8 = leds.astype(np.uint8)
        grb = np.empty_like(u8)
        grb[:, 0] = u8[:, 1]
        grb[:, 1] = u8[:, 0]
        grb[:, 2] = u8[:, 2]
        return grb.tobytes()
