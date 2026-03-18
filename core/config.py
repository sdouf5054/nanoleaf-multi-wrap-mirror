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
            "w1_bl": 73, "w1_tl": 66, "w1_tr": 53, "w1_br": 45, "w1_end": 31,
            "w2_tl": 24, "w2_tr": 11, "w2_br": 4, "w2_end": 0,
        },
        "segments": [
            {"start": 73, "end": 66, "side": "left"},
            {"start": 66, "end": 53, "side": "top"},
            {"start": 53, "end": 45, "side": "right"},
            {"start": 45, "end": 31, "side": "bottom"},
            {"start": 31, "end": 24, "side": "left"},
            {"start": 24, "end": 11, "side": "top"},
            {"start": 11, "end": 4,  "side": "right"},
            {"start": 4,  "end": 0,  "side": "bottom"},
        ],
    },
    "color": {
        "wb_r": 1.0,
        "wb_g": 0.85,
        "wb_b": 0.7,
        "gamma_r": 1.00,
        "gamma_g": 1.00,
        "gamma_b": 1.00,
        "green_red_bleed": 0.60,
    },
    "mirror": {
        "monitor_index": 0,
        "target_fps": 30,
        "grid_cols": 64,
        "grid_rows": 32,
        "decay_radius": 0.30,
        "parallel_penalty": 5.0,
        "decay_radius_per_side": {},
        "parallel_penalty_per_side": {},
        "smoothing_factor": 0.5,
        "brightness": 1.0,
        "master_brightness": 1.0,            # ★ 신규: 모든 모드 공용 최대 밝기
        "orientation": "auto",
        "portrait_rotation": "cw",
        "zone_count": -1,
        "color_extract_mode": "average",      # ★ Phase 3에서 추가됨
    },
    # ── 오디오 모드별 파라미터 ──
    "audio_pulse": {
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 50,
        "release": 50,
        "input_smooth": 30,
        "zone_bass": 33,
        "zone_mid": 33,
        "zone_high": 34,
    },
    "audio_spectrum": {
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 50,
        "release": 50,
        "input_smooth": 30,
        "zone_bass": 33,
        "zone_mid": 33,
        "zone_high": 34,
    },
    "audio_bass_detail": {
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 10,
        "release": 70,
        "input_smooth": 30,
        "zone_bass": 48,
        "zone_mid": 26,
        "zone_high": 26,
    },
    "audio_wave": {                           # ★ 신규
        "bass_sens": 120,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 60,
        "release": 40,
        "wave_speed": 50,
        "zone_bass": 33,
        "zone_mid": 33,
        "zone_high": 34,
    },
    "audio_dynamic": {                        # ★ 신규
        "bass_sens": 110,
        "mid_sens": 110,
        "high_sens": 120,
        "brightness": 100,
        "attack": 55,
        "release": 45,
        "zone_bass": 33,
        "zone_mid": 33,
        "zone_high": 34,
    },
    "audio_flowing": {                        # ★ 신규
        "bass_sens": 100,
        "mid_sens": 100,
        "high_sens": 100,
        "brightness": 100,
        "attack": 40,
        "release": 60,
        "zone_bass": 33,
        "zone_mid": 33,
        "zone_high": 34,
    },
    "options": {
        "tray_enabled": True,
        "hotkey_enabled": True,
        "minimize_to_tray": True,
        "hotkey_toggle": "ctrl+shift+o",
        "hotkey_bright_up": "ctrl+shift+up",
        "hotkey_bright_down": "ctrl+shift+down",
        # ★ 신규: 토글 기본값 (기존 default_mode 대체)
        "default_display_enabled": False,
        "default_audio_enabled": False,
        # ★ 통합: 기존 audio_state + hybrid_state → audio_state 하나로
        "audio_state": {
            "sub_mode": "pulse",
            "color_rainbow": True,
            "color_rgb": [255, 0, 80],
            "min_brightness": 2,
            "color_effect": "static",
            "gradient_speed": 50,
            "gradient_hue": 40,
            "gradient_sv": 50,
            # 하이브리드에서 가져온 필드
            "zone_count": 4,
            "color_extract_mode": "average",
            "flowing_interval": 30,
            "flowing_speed": 50,
        },
        "auto_start_engine": False,
        "turn_off_on_lock": True,
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


def _config_path():
    """config.json 경로 — exe 실행 시 exe 폴더, 스크립트 실행 시 루트 폴더"""
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(base_dir, "config.json")


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)