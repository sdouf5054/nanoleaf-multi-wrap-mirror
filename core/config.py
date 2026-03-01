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
        "smoothing_factor": 0.5,
        "brightness": 1.0,
        "orientation": "auto",
        "portrait_rotation": "cw",
    },
    "options": {
        "tray_enabled": True,
        "hotkey_enabled": True,
        "minimize_to_tray": True,
    },
}


def _config_path():
    """config.json 경로 — exe 실행 시 exe 폴더, 스크립트 실행 시 루트 폴더"""
    if getattr(sys, 'frozen', False):
        # PyInstaller로 빌드된 exe: 임시 _MEIPASS가 아닌 실제 exe 위치에 저장
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(base_dir, "config.json")


def load_config():
    """config.json 로드. 없으면 기본값으로 생성."""
    path = _config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        # 기본값과 병합 (새 키가 추가됐을 때 대비)
        merged = copy.deepcopy(DEFAULT_CONFIG)
        for section in merged:
            if section in user:
                if isinstance(merged[section], dict):
                    merged[section].update(user[section])
                else:
                    merged[section] = user[section]
        return merged
    else:
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """config.json 저장."""
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
