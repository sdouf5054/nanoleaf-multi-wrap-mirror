"""Nanoleaf Screen Mirror — 테마 컬러 팔레트

모든 하드코딩 색상의 단일 출처 (Single Source of Truth).
theme.qss 템플릿의 {key} 자리표시자에 치환됩니다.

DARK / LIGHT 두 팔레트를 제공하며, 키 이름은 동일합니다.

동적 인라인 스타일에서 현재 팔레트를 참조하려면:
    from styles.palette import current
    lbl.setStyleSheet(f"color: {current()['accent_green']};")
"""

# ══════════════════════════════════════════════════════════════
#  현재 활성 팔레트 (런타임에 set_current()로 설정)
# ══════════════════════════════════════════════════════════════

_current_theme = "light"


def current():
    """현재 활성 팔레트 dict를 반환."""
    return DARK if _current_theme == "dark" else LIGHT


def set_current(theme_name):
    """활성 팔레트를 변경. load_theme()에서 호출."""
    global _current_theme
    _current_theme = theme_name


def get_palette(theme_name="dark"):
    """이름으로 팔레트를 반환."""
    return DARK if theme_name == "dark" else LIGHT


# ══════════════════════════════════════════════════════════════
#  DARK 팔레트
# ══════════════════════════════════════════════════════════════

DARK = {
    # ── 배경 ──
    "bg_primary":       "#1e1e22",
    "bg_secondary":     "#2b2b30",
    "bg_tertiary":      "#363640",
    "bg_hover":         "#3a3a44",
    "bg_pressed":       "#2a2a34",
    "bg_selected":      "#2a3a50",
    "bg_input":         "#2b2b2b",

    # ── 텍스트 ──
    "text_primary":     "#e0e0e0",
    "text_secondary":   "#bdc3c7",
    "text_muted":       "#9a9aa0",
    "text_dim":         "#6a6a74",
    "text_disabled":    "#555555",

    # ── 보더 ──
    "border":           "#444448",
    "border_light":     "#555555",
    "border_hover":     "#666666",
    "border_focus":     "#2e86c1",

    # ── 액센트 ──
    "accent_blue":          "#2e86c1",
    "accent_blue_hover":    "#3498db",
    "accent_blue_light":    "#2980b9",
    "accent_green":         "#2d8c46",
    "accent_green_hover":   "#35a352",
    "accent_green_bright":  "#27ae60",
    "accent_green_light":   "#2ecc71",
    "accent_red":           "#c0392b",
    "accent_red_hover":     "#e74c3c",
    "accent_orange":        "#e67e22",
    "accent_orange_dark":   "#d35400",
    "accent_navy":          "#2c3e50",
    "accent_navy_hover":    "#34495e",
    "accent_gold":          "#b7950b",
    "accent_gold_hover":    "#d4ac0d",

    # ── 슬라이더 ──
    "slider_groove":        "#3a3a42",
    "slider_handle":        "#aaaaaa",
    "slider_handle_hover":  "#cccccc",
    "slider_sub":           "#4a6a8a",

    # ── 에너지 바 ──
    "bar_bass":     "#e74c3c",
    "bar_mid":      "#27ae60",
    "bar_high":     "#3498db",
    "bar_bg":       "#2b2b2b",

    # ── 상태 태그 ──
    "tag_off_bg":           "#2b2b2b",
    "tag_off_text":         "#6a6a74",
    "tag_display_bg":       "#1a3456",
    "tag_display_text":     "#7ec8e3",
    "tag_audio_bg":         "#2e1a45",
    "tag_audio_text":       "#c49be8",
    "tag_media_bg":         "#2d3a1a",
    "tag_media_text":       "#a3d977",

    # ── 상태 라벨 ──
    "cpu_normal":       "#d35400",
    "cpu_warning":      "#e67e22",
    "cpu_danger":       "#c0392b",
    "ram_color":        "#27ae60",
    "fps_color":        "#888888",

    # ── 미디어 카드 ──
    "card_bg":          "#2a2a30",
    "card_border":      "#444444",
    "card_inner_bg":    "#1a1a1e",

    # ── 미디어 소스 상태 ──
    "media_active":     "#a3d977",
    "media_phase1":     "#d4c85a",
    "media_mirror":     "#7ec8e3",
    "media_idle":       "#e6a85a",

    # ── 핫키 입력 ──
    "hotkey_idle_bg":       "#2b2b2b",
    "hotkey_idle_border":   "#555555",
    "hotkey_listen_bg":     "#1a3a5c",
    "hotkey_listen_text":   "#7ec8e3",
    "hotkey_listen_border": "#3a8fc7",

    # ── 프리뷰 토글 ──
    "preview_toggle_bg":        "#34495e",
    "preview_toggle_text":      "#bdc3c7",
    "preview_toggle_checked":   "#2980b9",

    # ── 미러링 패널 버튼 ──
    "btn_refresh_bg":       "#363640",
    "btn_refresh_hover":    "#46465a",
    "btn_refresh_pressed":  "#2a2a34",
}


# ══════════════════════════════════════════════════════════════
#  LIGHT 팔레트
# ══════════════════════════════════════════════════════════════

LIGHT = {
    # ── 배경 (밝은 순) ──
    "bg_primary":       "#f0f0f4",
    "bg_secondary":     "#ffffff",
    "bg_tertiary":      "#e8e8ec",
    "bg_hover":         "#dcdce0",
    "bg_pressed":       "#d0d0d4",
    "bg_selected":      "#cce0f5",
    "bg_input":         "#f8f8fa",

    # ── 텍스트 ──
    "text_primary":     "#1a1a1e",
    "text_secondary":   "#4a4a50",
    "text_muted":       "#7a7a80",
    "text_dim":         "#9a9aa0",
    "text_disabled":    "#bbbbbb",

    # ── 보더 ──
    "border":           "#cccccc",
    "border_light":     "#dddddd",
    "border_hover":     "#aaaaaa",
    "border_focus":     "#2e86c1",

    # ── 액센트 ──
    "accent_blue":          "#2e86c1",
    "accent_blue_hover":    "#3498db",
    "accent_blue_light":    "#5dade2",
    "accent_green":         "#27864a",
    "accent_green_hover":   "#2d9d52",
    "accent_green_bright":  "#229954",
    "accent_green_light":   "#28b463",
    "accent_red":           "#b03a2e",
    "accent_red_hover":     "#cb4335",
    "accent_orange":        "#d68910",
    "accent_orange_dark":   "#b9770e",
    "accent_navy":          "#5d6d7e",
    "accent_navy_hover":    "#7f8c8d",
    "accent_gold":          "#b7950b",
    "accent_gold_hover":    "#d4ac0d",

    # ── 슬라이더 ──
    "slider_groove":        "#aaaaaa",
    "slider_handle":        "#666666",
    "slider_handle_hover":  "#444444",
    "slider_sub":           "#6ea8d6",

    # ── 에너지 바 ──
    "bar_bass":     "#e74c3c",
    "bar_mid":      "#27ae60",
    "bar_high":     "#3498db",
    "bar_bg":       "#e0e0e0",

    # ── 상태 태그 ──
    "tag_off_bg":           "#e0e0e0",
    "tag_off_text":         "#888888",
    "tag_display_bg":       "#d4e6f1",
    "tag_display_text":     "#1a5276",
    "tag_audio_bg":         "#e8daef",
    "tag_audio_text":       "#6c3483",
    "tag_media_bg":         "#d5f5e3",
    "tag_media_text":       "#1e8449",

    # ── 상태 라벨 ──
    "cpu_normal":       "#b9770e",
    "cpu_warning":      "#d68910",
    "cpu_danger":       "#b03a2e",
    "ram_color":        "#1e8449",
    "fps_color":        "#7f8c8d",

    # ── 미디어 카드 ──
    "card_bg":          "#f5f5f8",
    "card_border":      "#cccccc",
    "card_inner_bg":    "#e8e8ec",

    # ── 미디어 소스 상태 ──
    "media_active":     "#1e8449",
    "media_phase1":     "#b7950b",
    "media_mirror":     "#2874a6",
    "media_idle":       "#b9770e",

    # ── 핫키 입력 ──
    "hotkey_idle_bg":       "#e8e8ec",
    "hotkey_idle_border":   "#cccccc",
    "hotkey_listen_bg":     "#d4e6f1",
    "hotkey_listen_text":   "#1a5276",
    "hotkey_listen_border": "#2e86c1",

    # ── 프리뷰 토글 ──
    "preview_toggle_bg":        "#2e86c1",
    "preview_toggle_text":      "#5d6d7e",
    "preview_toggle_checked":   "#d5d8dc",

    # ── 미러링 패널 버튼 ──
    "btn_refresh_bg":       "#e8e8ec",
    "btn_refresh_hover":    "#dcdce0",
    "btn_refresh_pressed":  "#d0d0d4",
}
