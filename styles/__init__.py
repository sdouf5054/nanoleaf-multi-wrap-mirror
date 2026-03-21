"""Nanoleaf Screen Mirror — 테마 로더

사용법 (main.py에서):
    from styles import load_theme
    load_theme(app, "dark")   # 또는 "light"

런타임 테마 전환:
    from styles import load_theme
    load_theme(app, "light")  # 즉시 전체 UI 갱신
"""

import os
import sys
from styles.palette import get_palette, set_current


def _get_base_dir():
    """프로젝트 루트 (frozen/스크립트 공용)."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_assets_dir():
    """assets 디렉토리의 절대 경로를 반환."""
    return os.path.join(_get_base_dir(), "assets")


def _get_styles_dir():
    """styles 디렉토리의 절대 경로를 반환.

    frozen 환경에서는 __file__이 _MEIPASS 내부를 가리키지 않을 수 있으므로
    _get_base_dir() 기준으로 명시적으로 해석.
    """
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "styles")
    return os.path.dirname(os.path.abspath(__file__))


def load_theme(app, theme="light"):
    """QSS 테마를 로드하여 QApplication에 적용.

    theme.qss 템플릿에 해당 팔레트의 값을 치환하고
    app.setStyleSheet()로 적용합니다.
    SVG 아이콘 경로도 팔레트에 주입합니다.

    Args:
        app: QApplication 인스턴스
        theme: "dark" 또는 "light"
    """
    set_current(theme)
    palette = get_palette(theme)

    # ★ SVG 아이콘 경로를 팔레트에 주입
    #   다크 테마 → 밝은 chevron, 라이트 테마 → 어두운 chevron
    assets_dir = _get_assets_dir()
    if theme == "dark":
        chevron_down = os.path.join(assets_dir, "chevron-down-light.svg")
        chevron_up = os.path.join(assets_dir, "chevron-up-light.svg")
    else:
        chevron_down = os.path.join(assets_dir, "chevron-down.svg")
        chevron_up = os.path.join(assets_dir, "chevron-up.svg")

    # QSS url()은 forward slash 필요 (Windows 포함)
    pal_with_icons = dict(palette)
    pal_with_icons["chevron_down"] = chevron_down.replace("\\", "/")
    pal_with_icons["chevron_up"] = chevron_up.replace("\\", "/")

    styles_dir = _get_styles_dir()
    qss_path = os.path.join(styles_dir, "theme.qss")

    with open(qss_path, encoding="utf-8") as f:
        template = f.read()

    app.setStyleSheet(template.format(**pal_with_icons))
