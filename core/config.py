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
    },
    "options": {
        "tray_enabled": True,
        "hotkey_enabled": True,
        "minimize_to_tray": True,
        # 글로벌 핫키 문자열 — keyboard 라이브러리 형식
        # 단일 키: "f13", "f14", "f15"
        # 조합 키: "ctrl+shift+o", "ctrl+shift+up"
        "hotkey_toggle": "ctrl+shift+o",
        "hotkey_bright_up": "ctrl+shift+up",
        "hotkey_bright_down": "ctrl+shift+down",
    },
}


def _deep_merge(base, override):
    """base dict에 override를 재귀적으로 병합합니다.

    규칙:
    - base에만 있는 키 → 유지 (새 버전에서 추가된 기본값 보존)
    - override에만 있는 키 → 유지 (사용자 커스텀 값 보존)
    - 양쪽 모두 dict인 키 → 재귀 병합
    - 그 외 (list, 스칼라 등) → override 값 우선

    Args:
        base: 기본값 dict (deep copy된 DEFAULT_CONFIG)
        override: 사용자 파일에서 읽은 dict

    Returns:
        병합된 dict (base를 직접 수정하여 반환)
    """
    for key, override_val in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(override_val, dict)
        ):
            # 양쪽 모두 dict → 재귀
            _deep_merge(base[key], override_val)
        else:
            # override 값 우선 (list, 스칼라, 타입 불일치 포함)
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
    예) DEFAULT_CONFIG["layout"]["corners"]에 새 키가 추가되면,
        기존 사용자의 corners에 해당 키가 기본값으로 추가됩니다.
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
