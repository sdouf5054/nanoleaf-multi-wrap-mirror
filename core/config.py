"""설정 관리 — config.json 로드/저장"""

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
        "orientation": "auto",
        "portrait_rotation": "cw",
        "zone_count": -1,           # ★ 추가: 미러링 구역 수 (-1 = per-LED)
    },
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
        "attack": 50,
        "release": 50,
        "input_smooth": 30,
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
        "default_mode": "mirror",    # ★ 추가: 앱 시작 시 기본 모드
        "audio_state": {             # ★ 추가: 오디오 모드 UI 상태
            "sub_mode": "pulse",
            "color_rainbow": True,
            "color_rgb": [255, 0, 80],
            "min_brightness": 2,
        },
        "hybrid_state": {            # ★ 추가: 하이브리드 모드 UI 상태
            "sub_mode": "pulse",
            "zone_count": 4,
            "min_brightness": 5,
        },
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
    """
    path = _config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        merged = copy.deepcopy(DEFAULT_CONFIG)
        _deep_merge(merged, user)
        return merged
    else:
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """config.json 저장."""
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
