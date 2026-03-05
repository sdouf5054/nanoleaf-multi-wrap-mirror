# 네이티브 캡처 모듈 — 설치 및 적용 가이드

## 파일 구성

```
native_capture/
├── fast_capture.cpp      ← C++ DLL 소스코드
├── build.bat             ← 빌드 스크립트 (더블클릭으로 실행)
├── native_capture.py     ← Python 래퍼 (ctypes)
├── test_native_capture.py ← 테스트 스크립트
└── GUIDE.md              ← 이 파일
```

## Step 1: Visual Studio 설치

1. https://visualstudio.microsoft.com/ko/vs/community/ 에서 다운로드
2. 설치 프로그램에서 **"C++를 사용한 데스크톱 개발"** 워크로드 체크
3. 설치 (약 5-8GB)

## Step 2: DLL 빌드

1. `build.bat`를 **더블클릭**
2. 성공하면 같은 폴더에 `fast_capture.dll` 생성됨

빌드가 안 되면:
- 시작 메뉴 → "x64 Native Tools Command Prompt for VS 2022" 열기
- 해당 폴더로 이동 후 수동 빌드:
```
cd C:\경로\native_capture
cl /LD /O2 /EHsc fast_capture.cpp /link d3d11.lib dxgi.lib /OUT:fast_capture.dll
```

## Step 3: 테스트

```
cd native_capture
python test_native_capture.py
```

성공하면 이런 출력이 나옴:
```
[1/5] DLL 초기화...
  ✅ 성공
  화면 해상도: 2560 × 1440
  출력 크기:   64 × 32
...
```

## Step 4: 프로젝트에 적용

### 4a. 파일 배치

```
nanoleaf-mirror/
├── core/
│   ├── capture.py        ← 기존 (유지)
│   └── ...
├── native_capture.py     ← 여기에 복사
├── fast_capture.dll      ← 여기에 복사
└── ...
```

### 4b. mirror.py 수정 (2줄만 변경)

```python
# 변경 전:
from core.capture import ScreenCapture

# 변경 후:
try:
    from native_capture import NativeScreenCapture as ScreenCapture
except ImportError:
    from core.capture import ScreenCapture
```

`_init_resources()` 메서드에서 ScreenCapture 생성 부분:

```python
# 변경 전:
self._capture = ScreenCapture(mirror_cfg["monitor_index"])

# 변경 후:
self._capture = ScreenCapture(
    monitor_index=mirror_cfg["monitor_index"],
    grid_cols=mirror_cfg["grid_cols"],
    grid_rows=mirror_cfg["grid_rows"],
)
```

### 4c. 동작 방식

NativeScreenCapture.grab()은 이미 64×32 크기의 RGB 프레임을 반환합니다.
mirror.py의 color 파이프라인에서 cv2.resize가 호출되어도
64×32 → 64×32 리사이즈라 사실상 no-op이 됩니다.

### 4d. 폴백

DLL 로드에 실패하면 자동으로 기존 dxcam으로 폴백합니다.
PyInstaller로 exe 빌드 시에도 fast_capture.dll을
--add-data로 포함하면 동작합니다.

## 기대 효과

| 구간 | dxcam | 네이티브 |
|------|-------|---------|
| GPU→CPU 복사 | 14MB (풀 프레임) | 14MB (동일*) |
| Python 전달 | 14MB numpy | 8KB numpy |
| cv2.resize | 매 프레임 | 불필요 |
| numpy astype | 매 프레임 | 불필요 |

*GPU→CPU DMA 복사 자체는 동일하지만,
Python 인터프리터가 14MB 배열을 생성·관리·GC하는
오버헤드가 제거됩니다.

## 제한사항

- Windows 전용 (DXGI Desktop Duplication)
- 관리자 권한 불필요
- 다른 앱이 Desktop Duplication을 점유하고 있으면
  초기화 실패 가능 (-6 에러)
- HDR 모니터: DXGI_FORMAT이 다를 수 있어 추가 처리 필요
- 멀티 GPU: 기본 GPU만 지원 (device_idx=0 고정)
