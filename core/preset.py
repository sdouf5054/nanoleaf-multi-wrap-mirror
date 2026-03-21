"""프리셋 관리 — 저장/로드/삭제/목록/수집/비교

프리셋은 "사용 시나리오 스냅샷"으로, 토글 상태 + 핵심 파라미터만 포함.
하드웨어 종속 설정(레이아웃, 디바이스, 색상 보정)은 제외.

[저장 위치]
  config.json과 동일한 디렉토리의 presets/ 하위 폴더.
  → config.py의 get_config_dir()를 공유하여 portable / APPDATA 자동 대응.

[파일 형식]
  presets/{name}.json — UTF-8, indent=2

[사용법]
  from core.preset import (
      list_presets, load_preset, save_preset, delete_preset,
      collect_preset_data, preset_differs,
  )

  # 목록
  names = list_presets()

  # 저장
  data = collect_preset_data(tab_control)
  save_preset("영화 감상", data)

  # 로드
  data = load_preset("영화 감상")

  # 비교 (변경 감지)
  if preset_differs(current_data, saved_data):
      print("수정됨")

순수 Python + json 모듈. Qt 의존성 없음.
"""

import json
import os
import sys
from typing import Optional


# ══════════════════════════════════════════════════════════════════
#  프리셋에 포함할 필드 정의
# ══════════════════════════════════════════════════════════════════

# 프리셋 JSON에 저장되는 필드 목록.
# ★ 슬라이더 raw 값 기준으로 저장 — apply 시 역변환 없이 직접 설정 가능.
# 엔진 변환값(gradient_speed_from_slider 등)이 아닌 슬라이더 정수값을 저장.
PRESET_FIELDS = {
    # ── 토글 상태 ──
    "display_enabled",
    "audio_enabled",
    "media_color_enabled",

    # ── 공용 ──
    "master_brightness",       # int 0~100 (슬라이더 raw)

    # ── 오디오 ──
    "audio_mode",              # str: "pulse", "spectrum", ...
    "min_brightness",          # int 0~100 (슬라이더 raw)
    "bass_sensitivity",        # int 10~300 (슬라이더 raw)
    "mid_sensitivity",         # int 10~300
    "high_sensitivity",        # int 10~300
    "attack",                  # int 0~100
    "release",                 # int 0~100
    "zone_weights",            # [int, int, int] 합계 100
    "wave_speed",              # int 0~100 (슬라이더 raw)
    "flowing_interval",        # int 10~100 (슬라이더 raw, ×0.1초)
    "flowing_speed",           # int 0~100 (슬라이더 raw)

    # ── 색상 (디스플레이 OFF) ──
    "rainbow",                 # bool
    "base_color",              # [int, int, int] RGB 0~255
    "color_effect",            # str: "static", "gradient_cw", ...
    "gradient_speed",          # int 0~100 (슬라이더 raw)
    "gradient_hue",            # int 0~100 (슬라이더 raw)
    "gradient_sv",             # int 0~100 (슬라이더 raw)

    # ── 미러링 (디스플레이 ON) ──
    "smoothing_factor",        # int 0~95 (슬라이더 raw)
    "mirror_n_zones",          # int: -1, 1, 2, 4, 8, 16, 32
    "color_extract_mode",      # str: "average", "distinctive"
    # 미러링 색상 효과 (D=ON용, D=OFF용과 별도 저장)
    "mirror_color_effect",     # str: "static", "gradient_cw", "gradient_ccw"
    "mirror_gradient_speed",   # int 0~100
    "mirror_gradient_hue",     # int 0~100
    "mirror_gradient_sv",      # int 0~100

    # ── 미디어 ──
    "media_source_override",   # str: "auto", "media", "mirror"
}

# 비교 시 부동소수점 허용 오차
_FLOAT_TOLERANCE = 0.001


# ══════════════════════════════════════════════════════════════════
#  경로 — ★ config.py의 get_config_dir() 공유
# ══════════════════════════════════════════════════════════════════

def _presets_dir():
    """presets/ 폴더 경로 — config.json과 동일한 디렉토리 기준."""
    from core.config import get_config_dir
    return os.path.join(get_config_dir(), "presets")


def _ensure_presets_dir():
    """presets/ 폴더가 없으면 생성."""
    d = _presets_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _preset_path(name):
    """프리셋 이름 → 파일 경로."""
    # 파일명 안전하게 변환: 특수문자 제거
    safe_name = "".join(
        c for c in name if c.isalnum() or c in (" ", "-", "_", ".", "가-힣")
    ).strip()
    if not safe_name:
        safe_name = "unnamed"
    return os.path.join(_presets_dir(), f"{safe_name}.json")


# ══════════════════════════════════════════════════════════════════
#  공개 API — CRUD
# ══════════════════════════════════════════════════════════════════

def list_presets():
    """저장된 프리셋 이름 목록 반환 (알파벳 순).

    Returns:
        list[str] — 프리셋 이름 (확장자 제외)
    """
    d = _presets_dir()
    if not os.path.isdir(d):
        return []
    names = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            names.append(f[:-5])  # .json 제거
    return names


def load_preset(name):
    """프리셋 파일 로드.

    Args:
        name: str — 프리셋 이름

    Returns:
        dict — 프리셋 데이터. 파일 없으면 None.
    """
    path = _preset_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # PRESET_FIELDS에 있는 키만 필터링 (안전)
        return {k: v for k, v in data.items() if k in PRESET_FIELDS}
    except (json.JSONDecodeError, OSError):
        return None


def save_preset(name, data):
    """프리셋 저장.

    Args:
        name: str — 프리셋 이름
        data: dict — PRESET_FIELDS 기준 데이터

    Returns:
        bool — 저장 성공 여부
    """
    _ensure_presets_dir()
    path = _preset_path(name)

    # PRESET_FIELDS에 있는 키만 저장
    filtered = {}
    for k, v in data.items():
        if k not in PRESET_FIELDS:
            continue
        # tuple → list 변환 (JSON 호환)
        if isinstance(v, tuple):
            v = list(v)
        filtered[k] = v

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(filtered, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def delete_preset(name):
    """프리셋 삭제.

    Args:
        name: str — 프리셋 이름

    Returns:
        bool — 삭제 성공 여부
    """
    path = _preset_path(name)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
        return False
    except OSError:
        return False


def preset_exists(name):
    """프리셋 파일 존재 여부 확인."""
    return os.path.exists(_preset_path(name))


# ══════════════════════════════════════════════════════════════════
#  수집 — UI 상태 → 프리셋 dict
# ══════════════════════════════════════════════════════════════════

def collect_preset_data(tab_control):
    """tab_control에서 현재 UI 상태를 프리셋 dict로 수집.

    ★ 슬라이더 raw 값을 저장 — 엔진 변환값이 아님.
    각 섹션의 collect_for_preset()을 사용.

    Args:
        tab_control: ControlTab 인스턴스

    Returns:
        dict — 프리셋 데이터 (슬라이더 raw 값 기준)
    """
    data = {
        "display_enabled": tab_control._display_on,
        "audio_enabled": tab_control._audio_on,
        "media_color_enabled": tab_control._media_on,
        "master_brightness": tab_control.slider_master_brightness.value(),
    }

    # 디스플레이 ON → 미러링 섹션, OFF → 색상 섹션
    # ★ 양쪽 모두 수집 — 프리셋에는 두 세트 모두 저장하여
    #    어떤 토글 조합에서 로드해도 올바르게 적용
    if hasattr(tab_control, 'section_mirror'):
        data.update(tab_control.section_mirror.collect_for_preset())
    if hasattr(tab_control, 'section_color'):
        data.update(tab_control.section_color.collect_for_preset())
    if hasattr(tab_control, 'section_audio'):
        data.update(tab_control.section_audio.collect_for_preset())

    # PRESET_FIELDS만 필터링
    return {k: v for k, v in data.items() if k in PRESET_FIELDS}


# ══════════════════════════════════════════════════════════════════
#  비교 — 변경 감지 (* 표시용)
# ══════════════════════════════════════════════════════════════════

def preset_differs(current, saved):
    """현재 UI 상태와 저장된 프리셋이 다른지 비교.

    Args:
        current: dict — collect_preset_data()의 결과
        saved: dict — load_preset()의 결과

    Returns:
        bool — True이면 변경됨 (* 표시 필요)
    """
    if current is None or saved is None:
        return True

    for key in PRESET_FIELDS:
        cv = current.get(key)
        sv = saved.get(key)

        if cv is None and sv is None:
            continue

        if cv is None or sv is None:
            return True

        # tuple/list 호환 비교
        cv_cmp = _normalize_for_compare(cv)
        sv_cmp = _normalize_for_compare(sv)

        if isinstance(cv_cmp, float) and isinstance(sv_cmp, float):
            if abs(cv_cmp - sv_cmp) > _FLOAT_TOLERANCE:
                return True
        elif isinstance(cv_cmp, (list, tuple)) and isinstance(sv_cmp, (list, tuple)):
            if len(cv_cmp) != len(sv_cmp):
                return True
            for a, b in zip(cv_cmp, sv_cmp):
                if isinstance(a, float) and isinstance(b, float):
                    if abs(a - b) > _FLOAT_TOLERANCE:
                        return True
                elif a != b:
                    return True
        else:
            if cv_cmp != sv_cmp:
                return True

    return False


def _normalize_for_compare(value):
    """비교를 위한 값 정규화 — tuple↔list 통일."""
    if isinstance(value, tuple):
        return list(value)
    return value
