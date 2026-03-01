# Nanoleaf Multi-Wrap Mirror

Nanoleaf 4D LED 스트립을 모니터 뒷면에 여러 겹 감아 화면 색상을 실시간 미러링하는 Windows 앱.

## 주요 기능
- **실시간 화면 미러링** — dxcam + USB HID, 최대 30fps
- **색상 보정** — 화이트밸런스, 감마, Green→Red 비선형 채널 믹싱
- **LED 캘리브레이션** — 순차 점등으로 코너 찾기, 세그먼트 자동 생성
- **시스템 트레이 + 글로벌 핫키** — Ctrl+Shift+O (on/off), Ctrl+Shift+↑↓ (밝기)
- **잠금 감지** — Win+L 시 자동 off, 해제 시 자동 재시작
- **세로 모드 지원** — 모니터 회전 자동 감지

## 실행
```
pip install -r requirements.txt
python main.py
```
