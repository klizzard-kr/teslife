# Teslife

테슬라에서 받은 주행·충전 기록을 모아, 차계부와 정기관리까지 한 화면에서 보는 개인용 웹앱입니다. 차를 깨우거나 제어하지 않습니다.

## 주요 기능

- 주행 기록과 전비
- 충전 기록, 장소, 단가
- 차계부
- 정기관리와 타이어
- 알림
- 기간별 통계

## 필요한 것

- Python 3.11 이상
- 추가 패키지는 없습니다. 표준 라이브러리만 사용합니다.

## 설치와 실행

저장소를 받은 뒤 그 폴더에서 실행합니다.

```bash
python -m simulator day | python -m ingest --db signals.db
python -m process --db signals.db
python -m web --db signals.db
```

브라우저에서 로컬 포트 8080으로 엽니다. `--no-seed`를 주면 예시 데이터 없이 시작합니다.

테스트:

```bash
python -m unittest discover -s tests -q
```

## 내 차 연결하기

실제 차를 이 저장소에 연결하는 절차는 [docs/SETUP.md](docs/SETUP.md)에 있습니다.

## 라이선스

MIT. Copyright 2026 klizzard-kr.

지도에 포함된 Leaflet 1.9.4는 BSD-2-Clause이며, 고지는 `web/static/vendor/leaflet/LICENSE`에 있습니다.
