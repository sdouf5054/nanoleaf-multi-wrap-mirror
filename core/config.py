"""설정 관리 — config.json 로드/저장 (Phase 0 통합)

[변경 이력]
- DEFAULT_CONFIG에 audio_wave, audio_dynamic, audio_flowing 키 추가
- options에 default_display_enabled / default_audio_enabled 추가
- options.audio_state에 color_effect, gradient 슬라이더 값, min_brightness 포함
- options.hybrid_state → options.audio_state로 통합 (마이그레이션)
- master_brightness를 mirror 섹션에 추가
- _migrate_config(): 기존 config.json을 새 구조로 안전하게 변환

[Phase 8 변경]
- _migrate_config: default_mode 마이그레이션 후 삭제 (중복 실행 방지)
- auto_start_mirror → auto_start_engine 마이그레이션 추가

[Phase 9 변경]
- ★ 각 audio 모드에 min_brightness 기본값 추가 (모드별 독립 저장)

[미디어 연동 추가]
- options에 default_media_enabled: False 추가

[exe 배포]
- ★ _config_dir(): 쓰기 가능한 디렉토리를 결정하는 단일 함수 추가
  - exe 폴더에 쓰기 가능하면 그대로 사용 (portable 배포)
  - 실패 시 %APPDATA%/NanoleafMirror/ 로 fallback
- save_config / load_config / _config_path 가 _config_dir() 공유
"""

import json
import os
import copy
import sys

DEFAULT_CONFIG = {
    "device": {
        "vendor_id": "0x37FA",
        "product_id": "0x8202",
        "led_count": 75,
    },
    "layout": {
        "corners": {
            "w1_bl": 74, "w1_tl": 56, "w1_tr": 37, "w1_br": 19, "w1_end": 0,
        },
        "segments": [
            {"start": 74, "end": 56, "side": "left"},
            {"start": 56, "end": 37, "side": "top"},
            {"start": 37, "end": 19, "side": "right"},
            {"start": 19, "end": 0,  "side": "bottom"},
        ],
    },
    "color": {
        "wb_r": 1.0,
        "wb_g": 0.86,
        "wb_b": 0.67,
        "gamma_r": 1.00,
        "gamma_g": 1.00,
        "gamma_b": 1.00,
        "green_red_bleed": 0.60,
    },
    "mirror": {
        "monitor_index": 0,
        "target_fps": 60,
        "grid_cols": 64,
        "grid_rows": 32,
        "decay_radius": 0.30,
        "parallel_penalty": 5.0,
        "decay_radius_per_side": {},
        "parallel_penalty_per_side": {},
        "smoothing_factor": 0.5,
        "brightness": 1.0,
        "master_brightness": 1.0,
        "orientation": "auto",
        "portrait_rotation": "cw",
        "zone_count": -1,
        "color_extract_mode": "average",
        "color_effect": "static",
        "gradient_speed": 80,
        "gradient_hue": 5,
        "gradient_sv": 30,
        "media_source_override": "auto",
        "flowing_interval": 30,
        "flowing_speed": 50,
    },
    "audio_pulse": {
        "min_brightness": 50,
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 50,
        "release": 80,
        "input_smooth": 30,
        "zone_bass": 34,
        "zone_mid": 33,
        "zone_high": 33,
    },
    "audio_spectrum": {
        "min_brightness": 50,
        "bass_sens": 100,
        "mid_sens": 115,
        "high_sens": 180,
        "brightness": 100,
        "attack": 75,
        "release": 50,
        "input_smooth": 30,
        "zone_bass": 65,
        "zone_mid": 15,
        "zone_high": 20,
    },
    "audio_bass_detail": {
        "min_brightness": 50,
        "bass_sens": 120,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 20,
        "release": 80,
        "input_smooth": 75,
        "zone_bass": 50,
        "zone_mid": 25,
        "zone_high": 25,
    },
    "audio_wave": {
        "min_brightness": 80,
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 60,
        "release": 20,
        "wave_speed": 50,
        "zone_bass": 34,
        "zone_mid": 33,
        "zone_high": 33,
    },
    "audio_dynamic": {
        "min_brightness": 50,
        "bass_sens": 100,
        "mid_sens": 110,
        "high_sens": 120,
        "brightness": 100,
        "attack": 40,
        "release": 30,
        "zone_bass": 34,
        "zone_mid": 33,
        "zone_high": 33,
    },
    "audio_flowing": {
        "min_brightness": 70,
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 40,
        "release": 60,
        "zone_bass": 34,
        "zone_mid": 33,
        "zone_high": 33,
    },
    "options": {
        "tray_enabled": True,
        "hotkey_enabled": True,
        "minimize_to_tray": True,
        "hotkey_toggle": "ctrl+shift+o",
        "hotkey_bright_up": "ctrl+shift+up",
        "hotkey_bright_down": "ctrl+shift+down",
        "hotkey_audio_cycle": "ctrl+shift+a",
        "hotkey_compact_view": "ctrl+shift+s",
        "default_display_enabled": True,
        "default_audio_enabled": True,
        "default_media_enabled": True,
        "audio_state": {
            "sub_mode": "pulse",
            "default_audio_mode": "dynamic",
            "color_rainbow": False,
            "color_rgb": [150, 0, 255],
            "min_brightness": 50,
            "color_effect": "static",
            "gradient_speed": 25,
            "gradient_hue": 0,
            "gradient_sv": 50,
            "zone_count": -1,
            "color_extract_mode": "average",
            "flowing_interval": 30,
            "flowing_speed": 50,
        },
        "auto_start_engine": False,
        "turn_off_on_lock": True,
        "last_preset": None,
        "default_preset": None,
    },
}


def _deep_merge(base, override):
    """base dict에 override를 재귀적으로 병합합니다.

    규칙:
    - base에만 있는 키 → 유지 (새 버전에서 추가된 기본값 보존)
    - override에만 있는 키 → 유지 (사용자 커스텀 값 보존)
    - 양쪽 모두 dict인 키 → 재귀 병합
    - 그 외 (list, 스칼라 등) → override 값 우선
    """
    for key, override_val in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(override_val, dict)
        ):
            _deep_merge(base[key], override_val)
        else:
            base[key] = override_val
    return base


def _migrate_config(config):
    """기존 config.json 구조를 새 구조로 마이그레이션.

    안전하게 동작: 이미 마이그레이션된 config에 대해서도 무해.

    [1] default_mode → default_display_enabled + default_audio_enabled
    [2] hybrid_state → audio_state로 통합
    [3] mirror.brightness → mirror.master_brightness (없으면 복사)
    [4] auto_start_mirror → auto_start_engine (키 이름 변경)
    """
    opts = config.get("options", {})

    # ── [1] default_mode → 토글 기본값 ──
    #   ★ 마이그레이션 후 삭제하여 다음 로드 시 재실행 방지
    if "default_mode" in opts:
        mode = opts["default_mode"]
        opts["default_display_enabled"] = mode in ("mirror", "hybrid")
        opts["default_audio_enabled"] = mode in ("audio", "hybrid")
        del opts["default_mode"]

    # ── [2] hybrid_state → audio_state 통합 ──
    if "hybrid_state" in opts:
        hybrid = opts["hybrid_state"]
        audio = opts.setdefault("audio_state", {})

        _hybrid_only_keys = (
            "zone_count", "color_extract_mode",
            "flowing_interval", "flowing_speed",
        )
        for key in _hybrid_only_keys:
            if key in hybrid and key not in audio:
                audio[key] = hybrid[key]

        if "sub_mode" in hybrid and "sub_mode" not in audio:
            audio["sub_mode"] = hybrid["sub_mode"]

        if "min_brightness" in hybrid and "min_brightness" not in audio:
            audio["min_brightness"] = hybrid["min_brightness"]

        _effect_keys = ("color_effect", "gradient_speed", "gradient_hue", "gradient_sv")
        for key in _effect_keys:
            if key in hybrid and key not in audio:
                audio[key] = hybrid[key]

    # ── [3] master_brightness 초기화 ──
    mirror = config.get("mirror", {})
    if "master_brightness" not in mirror:
        mirror["master_brightness"] = mirror.get("brightness", 1.0)

    # ── [4] auto_start_mirror → auto_start_engine ──
    #   ★ 키 이름 변경: 기존 값을 새 키로 이전 후 삭제
    if "auto_start_mirror" in opts and "auto_start_engine" not in opts:
        opts["auto_start_engine"] = opts["auto_start_mirror"]
    if "auto_start_mirror" in opts:
        del opts["auto_start_mirror"]

    return config


# ══════════════════════════════════════════════════════════════════
#  ★ 경로 결정 — portable 우선, fallback to %APPDATA%
# ══════════════════════════════════════════════════════════════════

def _exe_dir():
    """exe(또는 스크립트) 기준 디렉토리."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(__file__))


def _appdata_dir():
    """%APPDATA%/NanoleafMirror/ 경로."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "NanoleafMirror")
    # APPDATA 환경변수가 없는 경우 (극히 드묾) → exe 옆에 저장
    return _exe_dir()


def _is_writable(directory):
    """디렉토리에 쓰기 가능한지 테스트."""
    try:
        test_path = os.path.join(directory, ".write_test")
        with open(test_path, "w") as f:
            f.write("t")
        os.remove(test_path)
        return True
    except OSError:
        return False


# ★ 캐시: 앱 수명 동안 한 번만 결정
_resolved_config_dir = None


def _config_dir():
    """config.json과 presets/를 저장할 디렉토리를 결정.

    우선순위:
    1. exe 폴더에 config.json이 이미 존재 → exe 폴더 (기존 설정 유지)
    2. exe 폴더에 쓰기 가능 → exe 폴더 (portable 배포)
    3. 위 두 조건 모두 실패 → %APPDATA%/NanoleafMirror/
    """
    global _resolved_config_dir
    if _resolved_config_dir is not None:
        return _resolved_config_dir

    exe = _exe_dir()

    # 기존 config.json이 exe 폴더에 있으면 무조건 거기 사용
    if os.path.exists(os.path.join(exe, "config.json")):
        _resolved_config_dir = exe
        return exe

    # exe 폴더에 쓰기 가능하면 portable 모드
    if _is_writable(exe):
        _resolved_config_dir = exe
        return exe

    # fallback: %APPDATA%
    appdata = _appdata_dir()
    os.makedirs(appdata, exist_ok=True)
    _resolved_config_dir = appdata
    return appdata


def _config_path():
    """config.json 경로."""
    return os.path.join(_config_dir(), "config.json")


def load_config():
    """config.json 로드. 없으면 기본값으로 생성.

    ★ 재귀 deep merge로 중첩 dict의 새 키도 안전하게 보존합니다.
    ★ 마이그레이션 적용: 기존 구조를 새 구조로 변환.
    """
    path = _config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        merged = copy.deepcopy(DEFAULT_CONFIG)
        _deep_merge(merged, user)
        _migrate_config(merged)
        return merged
    else:
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """config.json 저장."""
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # 쓰기 실패 — 무시 (로그 없는 환경에서 조용히 실패)


def get_config_dir():
    """외부에서 config 디렉토리를 참조할 때 사용 (preset.py 등).

    ★ 공개 API — _config_dir()의 래퍼.
    """
    return _config_dir()


def get_audio_mode_defaults(mode_name):
    """오디오 모드의 기본값을 DEFAULT_CONFIG에서 추출.

    Args:
        mode_name: str — "pulse", "spectrum", "bass_detail" 등

    Returns:
        dict — 해당 모드의 기본 파라미터. 없으면 pulse 기본값 반환.
    """
    key = f"audio_{mode_name}"
    return DEFAULT_CONFIG.get(key, DEFAULT_CONFIG.get("audio_pulse", {})).copy()
