"""오디오 엔진 — WASAPI Loopback + FFT + 다중 대역 에너지 추출 v4

[주요 변경 v4]
- ★ get_raw_fft() 추가 — 최신 FFT magnitude 스펙트럼 원본 반환
  bass_detail 등 커스텀 밴드 분할 모드에서 사용
- 3밴드(bass/mid/high) + N밴드(기본 16) 스펙트럼 동시 출력
- 대역별 감도 (bass_sensitivity, mid_sensitivity, high_sensitivity)
- 로그 주파수 스케일 밴드 분할
- 밴드별 독립 AGC
"""

import numpy as np
import threading

try:
    import pyaudiowpatch as pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False

# ── 주파수 대역 정의 (Hz) ─────────────────────────────────────────
BAND_BASS = (20, 250)
BAND_MID = (250, 2000)
BAND_HIGH = (2000, 16000)

# ── 기본 설정 ─────────────────────────────────────────────────────
DEFAULT_BLOCK_SIZE = 2048      # FFT 윈도우 (2048 → ~23Hz 해상도 @ 48kHz)
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_N_BANDS = 16           # 세밀 스펙트럼 밴드 수
FREQ_MIN = 20                  # 스펙트럼 최저 주파수
FREQ_MAX = 16000               # 스펙트럼 최고 주파수
ENERGY_SMOOTHING = 0.15        # 내부 EMA (노이즈 제거)


def _find_wasapi_loopback(p):
    """WASAPI Loopback 디바이스 자동 탐지."""
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"]
        )
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if (dev.get("isLoopbackDevice", False)
                    and default_speakers["name"] in dev["name"]):
                return dev
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice", False):
                return dev
    except Exception:
        pass
    return None


def list_loopback_devices():
    """WASAPI Loopback 디바이스 목록."""
    if not HAS_PYAUDIO:
        return []
    result = []
    try:
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice", False):
                sr = int(dev.get("defaultSampleRate", DEFAULT_SAMPLE_RATE))
                ch = int(dev.get("maxInputChannels", 2))
                result.append((i, dev["name"], sr, ch))
        p.terminate()
    except Exception:
        pass
    return result


def _build_log_bands(n_bands, freq_min, freq_max, fft_freqs):
    """로그 스케일로 N개 밴드의 FFT 빈 범위를 계산합니다.

    Returns:
        list of (start_bin, end_bin) — 각 밴드의 FFT 빈 인덱스 범위
    """
    log_min = np.log10(max(freq_min, 1))
    log_max = np.log10(freq_max)
    edges = np.logspace(log_min, log_max, n_bands + 1)

    band_bins = []
    for i in range(n_bands):
        lo = edges[i]
        hi = edges[i + 1]
        mask = (fft_freqs >= lo) & (fft_freqs < hi)
        indices = np.where(mask)[0]
        if len(indices) > 0:
            band_bins.append((indices[0], indices[-1] + 1))
        else:
            closest = np.argmin(np.abs(fft_freqs - (lo + hi) / 2))
            band_bins.append((closest, closest + 1))

    return band_bins


class AudioEngine:
    """WASAPI Loopback 오디오 캡처 + 다중 대역 FFT 에너지 추출.

    두 가지 출력을 동시 제공:
    1. 3밴드 (bass/mid/high) — pulse 모드용
    2. N밴드 스펙트럼 (기본 16) — spectrum/wave 모드용

    v4 추가:
    3. raw FFT magnitude — bass_detail 등 커스텀 밴드 분할용
    """

    def __init__(self, device_index=None, block_size=DEFAULT_BLOCK_SIZE,
                 n_bands=DEFAULT_N_BANDS, sensitivity=1.0,
                 smoothing=ENERGY_SMOOTHING):
        if not HAS_PYAUDIO:
            raise ImportError("PyAudioWPatch가 필요합니다.\npip install PyAudioWPatch")

        self.block_size = block_size
        self.n_bands = n_bands
        self.smoothing = smoothing

        self.bass_sensitivity = sensitivity
        self.mid_sensitivity = sensitivity
        self.high_sensitivity = sensitivity

        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._lock = threading.Lock()

        # 디바이스 탐지
        if device_index is not None:
            self._device_info = self._pa.get_device_info_by_index(device_index)
        else:
            self._device_info = _find_wasapi_loopback(self._pa)
            if self._device_info is None:
                self._pa.terminate()
                raise RuntimeError(
                    "WASAPI Loopback 디바이스를 찾을 수 없습니다.\n"
                    "pip install PyAudioWPatch 확인."
                )

        self._device_index = int(self._device_info["index"])
        self._sample_rate = int(self._device_info["defaultSampleRate"])
        self._channels = int(self._device_info["maxInputChannels"])

        # ── FFT 사전 계산 ──
        self._fft_freqs = np.fft.rfftfreq(block_size, 1.0 / self._sample_rate)

        # 3밴드 마스크
        self._bass_mask = (self._fft_freqs >= BAND_BASS[0]) & (self._fft_freqs < BAND_BASS[1])
        self._mid_mask = (self._fft_freqs >= BAND_MID[0]) & (self._fft_freqs < BAND_MID[1])
        self._high_mask = (self._fft_freqs >= BAND_HIGH[0]) & (self._fft_freqs < BAND_HIGH[1])

        # N밴드 (로그 스케일)
        self._band_bins = _build_log_bands(n_bands, FREQ_MIN, FREQ_MAX, self._fft_freqs)

        # Hann 윈도우
        self._window = np.hanning(block_size).astype(np.float32)

        # ── 에너지 상태 ──
        self._bass = 0.0
        self._mid = 0.0
        self._high = 0.0
        self._peak = 0.0
        self._spectrum = np.zeros(n_bands, dtype=np.float64)

        # ★ raw FFT magnitude (스무딩/AGC 미적용)
        self._raw_fft = np.zeros(block_size // 2 + 1, dtype=np.float64)

        # ── 밴드별 AGC ──
        self._agc_3band = np.full(3, 0.01, dtype=np.float64)
        self._agc_nband = np.full(n_bands, 0.01, dtype=np.float64)
        self._agc_attack = 0.3
        self._agc_release = 0.002
        self._agc_floor = 0.005

        self._running = False

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def fft_freqs(self):
        """FFT 주파수 배열 — 외부에서 커스텀 밴드 분할 시 사용."""
        return self._fft_freqs

    @property
    def device_index(self):
        return self._device_index

    @property
    def device_name(self):
        return self._device_info.get("name", "Unknown")

    def start(self):
        if self._running:
            return
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._sample_rate,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=self.block_size,
            stream_callback=self._audio_callback,
        )
        self._stream.start_stream()
        self._running = True

    def stop(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        try:
            self._pa.terminate()
        except Exception:
            pass
        with self._lock:
            self._bass = self._mid = self._high = self._peak = 0.0
            self._spectrum[:] = 0.0
            self._raw_fft[:] = 0.0

    def get_band_energies(self):
        """3밴드 에너지. Returns dict with bass/mid/high/peak (0.0~1.0)."""
        with self._lock:
            return {
                "bass": self._bass, "mid": self._mid,
                "high": self._high, "peak": self._peak,
            }

    def get_spectrum(self):
        """N밴드 스펙트럼. Returns np.array (n_bands,) 0.0~1.0."""
        with self._lock:
            return self._spectrum.copy()

    def get_raw_fft(self):
        """★ 최신 FFT magnitude 스펙트럼 원본.

        Returns np.array (block_size//2+1,) — AGC/스무딩 미적용.
        외부에서 커스텀 밴드 분할에 사용.
        """
        with self._lock:
            return self._raw_fft.copy()

    def _audio_callback(self, in_data, frame_count, time_info, status):
        try:
            audio_data = np.frombuffer(in_data, dtype=np.float32)

            if self._channels > 1:
                n_frames = len(audio_data) // self._channels
                audio_data = audio_data[:n_frames * self._channels]
                audio = audio_data.reshape(n_frames, self._channels).mean(axis=1)
            else:
                audio = audio_data

            if len(audio) > self.block_size:
                audio = audio[:self.block_size]

            if len(audio) == len(self._window):
                audio = audio * self._window

            spectrum = np.abs(np.fft.rfft(audio))
            spec_len = len(spectrum)

            # ── 3밴드 에너지 ──
            bm = self._bass_mask[:spec_len]
            mm = self._mid_mask[:spec_len]
            hm = self._high_mask[:spec_len]

            raw_3 = np.array([
                float(np.sqrt(np.mean(spectrum[bm] ** 2))) if bm.any() else 0.0,
                float(np.sqrt(np.mean(spectrum[mm] ** 2))) if mm.any() else 0.0,
                float(np.sqrt(np.mean(spectrum[hm] ** 2))) if hm.any() else 0.0,
            ])

            # AGC for 3band
            atk, rel, flr = self._agc_attack, self._agc_release, self._agc_floor
            for i in range(3):
                if raw_3[i] > self._agc_3band[i]:
                    self._agc_3band[i] += (raw_3[i] - self._agc_3band[i]) * atk
                else:
                    self._agc_3band[i] *= (1.0 - rel)
                    self._agc_3band[i] = max(self._agc_3band[i], flr)

            norm_3 = raw_3 / self._agc_3band

            sens = np.array([self.bass_sensitivity, self.mid_sensitivity,
                             self.high_sensitivity])
            val_3 = np.minimum(1.0, (norm_3 * sens) ** 1.5)

            # ── N밴드 스펙트럼 ──
            raw_n = np.zeros(self.n_bands, dtype=np.float64)
            for i, (lo, hi) in enumerate(self._band_bins):
                if lo < spec_len and hi <= spec_len and hi > lo:
                    band_data = spectrum[lo:hi]
                    raw_n[i] = float(np.sqrt(np.mean(band_data ** 2)))

            # AGC for N-band
            for i in range(self.n_bands):
                if raw_n[i] > self._agc_nband[i]:
                    self._agc_nband[i] += (raw_n[i] - self._agc_nband[i]) * atk
                else:
                    self._agc_nband[i] *= (1.0 - rel)
                    self._agc_nband[i] = max(self._agc_nband[i], flr)

            norm_n = raw_n / self._agc_nband

            third = self.n_bands // 3
            band_sens = np.ones(self.n_bands, dtype=np.float64)
            band_sens[:third] = self.bass_sensitivity
            band_sens[third:third*2] = self.mid_sensitivity
            band_sens[third*2:] = self.high_sensitivity

            val_n = np.minimum(1.0, (norm_n * band_sens) ** 1.5)

            # ── EMA 스무딩 ──
            s = self.smoothing
            with self._lock:
                self._bass = self._bass * s + float(val_3[0]) * (1 - s)
                self._mid = self._mid * s + float(val_3[1]) * (1 - s)
                self._high = self._high * s + float(val_3[2]) * (1 - s)
                self._peak = self._peak * s + float(max(val_3)) * (1 - s)
                self._spectrum = self._spectrum * s + val_n * (1 - s)
                # ★ raw FFT 저장 (스무딩/AGC 미적용)
                self._raw_fft[:spec_len] = spectrum[:spec_len]

        except Exception:
            pass

        return (None, pyaudio.paContinue)
