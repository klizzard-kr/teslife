# 내 차 연결하기

차의 Fleet Telemetry가 TLS로 수신기에 도착하면, bridge가 JSON 로그를 SQLite에 넣고 process가 주행·충전으로 묶으며 web이 보여 줍니다. 이 서버는 차를 깨우거나 제어하지 않습니다.

## Tesla 개발자 앱

[developer.tesla.com](https://developer.tesla.com)에서 앱을 등록합니다. 자기 도메인이 필요합니다.

공개 키만 그 도메인에 올립니다. 아래의 `example.com`은 그 도메인 자리입니다.

`https://example.com/.well-known/appspecific/com.tesla.3p.public-key.pem`

개인 키는 git에 넣지 않습니다. 차가 있는 지역으로 그 도메인을 개발자 포털에 등록합니다. 위 주소에 공개 키가 있어야 등록됩니다.

## 가상 키

휴대폰에서 테슬라 계정으로 로그인한 뒤 아래 주소를 엽니다.

`https://tesla.com/_ak/example.com`

차가 공개 키를 가상 키로 추가합니다. 텔레메트리 설정을 차가 받아들이려면 이 페어링이 먼저입니다.

## fleet-telemetry

수신기는 Tesla의 fleet-telemetry입니다. TLS로 띄우고, 차에 보내는 설정에는 `hostname`, `port`, `ca`(인증서 체인)가 들어갑니다. 호스트 예시는 `telemetry.example.com`입니다. 인증서 본문은 문서에 적지 않습니다.

이 저장소는 설정 전체를 새로 만들지 않습니다. 이미 받아 둔 JSON에서 `VehicleSpeed` 간격만 1초로 바꾸고, `LongitudinalAcceleration`, `LateralAcceleration`, `PedalPosition`, `BrakePedal`만 뺍니다. 호스트, 포트, 인증서, `alert_types`, `prefer_typed`는 그대로 둡니다. 경고 기록은 `alert_types`의 `service`입니다.

```bash
python -m process.telemetry_delta --current current.json --out next.json
python -m process.send_telemetry --current current.json
python -m process.send_telemetry --current current.json --send --url https://example.com/fleet_telemetry_config
```

`--send`가 없으면 차이만 출력하고 네트워크로 보내지 않습니다. 주소에 `wake`가 있으면 거절합니다. 차량 식별자는 설정 JSON 안에 넣지 않고 요청 주소 쪽에만 둡니다. `current.json`과 `next.json`에는 인증서가 들어 있으므로 커밋하지 않습니다.

수신기 JSON 로그(`record_payload`)는 bridge의 표준 입력으로 넘깁니다.

## Teslife 서버

Python 3.11 이상이고, 추가 패키지는 없습니다. 저장소 폴더에서 실행합니다.

```bash
python bridge/bridge.py
python -m process --db signals.db
python -m web --db signals.db
```

`bridge/bridge.py`는 표준 입력의 로그를 `signals.db`에 넣습니다. 웹을 켜 두면 process가 약 30초마다 다시 돕니다. 화면은 로컬 포트 8080입니다.

환경 변수 이름은 `.env.example`과 같습니다. 그 파일을 `.env`로 복사한 뒤, 값은 서버에서만 채웁니다. git에 있는 예시는 값이 비어 있습니다.

- `NAVER_MAPS_CLIENT_ID`
- `NAVER_MAPS_CLIENT_SECRET`
- `NAVER_SEARCH_CLIENT_ID`
- `NAVER_SEARCH_CLIENT_SECRET`
- `BOT_NOTIFY_WEBHOOK_KEY` — 봇 웹훅을 쓸 때만 둡니다.

## 네이버 지도 (선택)

없어도 됩니다. `NAVER_MAPS_CLIENT_ID`와 `NAVER_MAPS_CLIENT_SECRET`이 있으면 주소 이름에 네이버를 쓰고, 화면 지도도 네이버입니다. 브라우저에는 `NAVER_MAPS_CLIENT_ID`만 갑니다. `NAVER_SEARCH_CLIENT_ID`와 `NAVER_SEARCH_CLIENT_SECRET`은 충전 장소 검색용이고 서버에만 둡니다. 지도 키가 없으면 Leaflet과 OpenStreetMap입니다.

## 비용

Tesla API 요금은 각자 개발자 계정으로 청구됩니다. 계정에는 월 크레딧이 있습니다. 과금은 설정한 필드와 간격 기준이고, DB에 쌓인 행 수와 같지 않습니다. 텔레메트리는 깨어 있는 차가 보내며, 이 서버는 차를 깨우지 않습니다.

## 주의

비밀값은 `.env`에만 둡니다. `.env`, 인증서, 개인 키, 토큰, `current.json`, `next.json`은 커밋하지 않습니다. `.env.example`의 값은 비워 둡니다.
