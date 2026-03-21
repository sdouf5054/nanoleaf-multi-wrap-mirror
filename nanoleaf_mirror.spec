# -*- mode: python ; coding: utf-8 -*-
"""
Nanoleaf Screen Mirror — PyInstaller spec (DLL 제외, dxcam 폴백)

빌드:
    pyinstaller nanoleaf_mirror.spec

출력:
    dist/NanoleafMirror/NanoleafMirror.exe  (--onedir 모드)

참고:
    - fast_capture.dll 미포함 → 자동으로 dxcam 모드로 동작
    - winrt 패키지는 optional → 누락 시 미디어 연동 비활성 (graceful)
    - --onedir 권장: --onefile은 시작이 느리고 임시 폴더 문제 있음
"""

import sys
import os

block_cipher = None

# ── 프로젝트 루트 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[
        # assets: 아이콘, SVG chevron 등
        (os.path.join(PROJECT_ROOT, 'assets'), 'assets'),
        # styles: theme.qss, palette.py
        (os.path.join(PROJECT_ROOT, 'styles'), 'styles'),
    ],
    hiddenimports=[
        # ── 필수 ──
        'dxcam',
        'comtypes',              # dxcam 내부 의존
        'comtypes.stream',
        'pyaudiowpatch',
        'hid',                   # hidapi
        'keyboard',
        'PIL',
        'PIL.Image',
        'cv2',
        'numpy',
        'psutil',

        # ── PySide6 플러그인 (누락 방지) ──
        'PySide6.QtSvg',

        # ── winrt (optional — 없어도 앱은 동작) ──
        # 아래 주석을 해제하면 미디어 연동이 exe에서도 동작함.
        # 패키지가 설치되어 있지 않으면 빌드 에러가 나므로 주의.
        # 'winrt.windows.media.control',
        # 'winrt.windows.storage.streams',
        # 'winrt.windows.foundation',
        # 'winrt.windows.media',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 불필요한 대형 모듈 제외 (빌드 크기 절감)
        'matplotlib',
        'scipy',
        'pandas',
        'tkinter',
        'unittest',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,    # --onedir 모드
    name='NanoleafMirror',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # --windowed (콘솔 창 없음)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, 'assets', 'icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NanoleafMirror',
)
