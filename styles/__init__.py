"""Nanoleaf Screen Mirror — 테마 로더

사용법 (main.py에서):
    from styles import load_theme
    load_theme(app)
"""

import os


def load_theme(app):
    """QSS 테마를 로드하여 QApplication에 적용.

    styles/palette.py의 DARK dict를 dark.qss 템플릿에 치환하고
    app.setStyleSheet()로 적용합니다.

    Args:
        app: QApplication 인스턴스
    """
    from styles.palette import DARK

    styles_dir = os.path.dirname(os.path.abspath(__file__))
    qss_path = os.path.join(styles_dir, "dark.qss")

    with open(qss_path, encoding="utf-8") as f:
        template = f.read()

    app.setStyleSheet(template.format(**DARK))
