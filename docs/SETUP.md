# 내 차 연결하기

차의 Fleet Telemetry 로그를 받아 SQLite에 넣고, 웹에서 주행·충전·차계부를 봅니다. 이 저장소는 차를 깨우거나 명령하지 않습니다.

Python 3.11 이상이면 됩니다. 추가 패키지는 없습니다.

## 1. 전체 구성

차 → [fleet-telemetry](https://github.com/teslamotors/fleet-telemetry) 수신 서버 → JSON 로그 → `bridge` → SQLite → 웹. 웹이 약 30초마다 주행과 충전을 만듭니다.

**대체.** 차 없이 예시 데이터로 먼저 볼 수 있습니다. 시뮬레이터는 이 저장소가 지원합니다.

```bash
python -m simulator day | python -m ingest --db signals.db
python -m process --db signals.db
python -m web --db signals.db
```

브라우저에서 로컬 포트 8080을 엽니다. 시뮬레이터는 위치를 만들지 않아서, 지도의 주행선은 비어 있습니다.

## 2. Tesla 개발자 앱

앱 등록은 [developer.tesla.com](https://developer.tesla.com/)에서 합니다. 이 저장소가 대신 등록하지는 않습니다.

1. 테슬라 계정은 이메일이 확인되어 있고, 다단계 인증이 켜져 있어야 합니다.
2. [개발자 대시보드](https://developer.tesla.com/dashboard)에서 앱을 만듭니다.
3. 키는 `prime256v1`입니다. 개인 키는 서버에만 두고, 공개 키만 올립니다.

```bash
openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out public-key.pem
```

공개 키 주소는 아래 형식입니다. `example.com`은 내 도메인입니다.

`https://example.com/.well-known/appspecific/com.tesla.3p.public-key.pem`

4. 파트너 토큰으로 register를 호출합니다. 쓰는 지역마다 한 번씩입니다. 한국은 아시아·태평양(중국 제외)입니다. 지역 주소는 [지역 문서](https://developer.tesla.com/docs/fleet-api/getting-started/regions-countries)에 있습니다.

순서의 원문은 [What is Fleet API?](https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api)입니다.

**대체.** 도메인과 공개 키 파일은 정적 호스팅이면 됩니다. [GitHub Pages](https://pages.github.com/)도 파일을 그대로 보여줄 수 있습니다. 기본 Jekyll은 `.`으로 시작하는 폴더를 빠뜨리므로, 빈 `.nojekyll` 파일을 두거나 `include`에 그 폴더를 넣어야 합니다. 개인 키는 그 공개 저장소에 넣지 않습니다. 앱 등록과 register를 이 코드가 해 주지는 않습니다.

## 3. 가상 키

텔레메트리 설정을 받으려면 차에 가상 키가 있어야 합니다. 개인이 산 차는 테슬라가 원격으로 키를 넣지 않습니다. 차에 권한이 있는 사람이 테슬라 앱에서 추가합니다.

앱에 `vehicle_device_data`, `vehicle_cmds`, `vehicle_location` 중 하나를 허용한 뒤, 등록한 도메인으로 이 주소를 엽니다.

`https://tesla.com/_ak/example.com`

설명은 [가상 키 문서](https://developer.tesla.com/docs/fleet-api/virtual-keys/developer-guide)에 있습니다. 인텔 아톰 모델 S·X는 가상 키 대신 차의 안전 화면에서 제3자 데이터 스트리밍을 켜는 경우가 있습니다. 차종별 조건은 [Fleet Telemetry](https://developer.tesla.com/docs/fleet-api/fleet-telemetry)에 있습니다.

**대체.** 페어링 화면은 이 저장소에 없습니다. 위 테슬라 주소로 합니다. 다른 방식으로 키를 넣는 코드는 없어서, 그 방식을 원하면 직접 고쳐야 합니다.

## 4. 수신 서버, 인증서, 설정

수신 프로그램은 이 저장소에 없습니다. [fleet-telemetry](https://github.com/teslamotors/fleet-telemetry)를 따로 실행합니다. TLS 인증서와 개인 키는 그 프로그램 설정에 둡니다. 차에 넣는 `ca`는 그 인증서를 만든 전체 체인입니다. 호스트, 포트, 체인은 그 저장소의 `check_server_cert.sh`로 확인합니다.

차 설정은 서명이 필요합니다. 테슬라 문서는 [vehicle-command](https://github.com/teslamotors/vehicle-command)의 HTTP 프록시가 개인 키로 서명한 뒤 Fleet API로 넘기라고 합니다. 그 프록시도 이 저장소에 없습니다.

이 저장소가 하는 일은, 이미 받아 둔 설정에서 몇 칸만 고치는 것입니다. 예전부터 `VehicleSpeed` 간격을 1초로 하고, `LongitudinalAcceleration`, `LateralAcceleration`, `PedalPosition`, `BrakePedal`은 뺍니다. 그 네 칸을 빼는 일은 이번 버전에서 새로 넣은 것이 아닙니다. 이번 버전은 `Gear` 간격도 1초로 맞추고, 설정에 없는 공조 칸(`AutoSeatClimateLeft`, `AutoSeatClimateRight`, `RearDisplayHvacEnabled`, `SeatHeaterRearLeft`, `SeatHeaterRearRight`)을 10초 간격으로 더합니다. 핸들 자동 열선(`HvacSteeringWheelHeatAuto`)은 더하지 않습니다. 이미 있는 칸은 그대로 둡니다. 호스트, 인증서, 포트와 나머지 항목은 그대로 둡니다. 화면 출력에는 인증서와 차대번호를 찍지 않습니다. 이 저장소의 변경만으로 차에 전송하지는 않습니다.

```bash
python -m process.telemetry_delta --current current.json --out next.json
python -m process.send_telemetry --current current.json
```

`--current` 없이 실행하면 내장 예시만 나옵니다. 내 차 설정이 아닙니다. 그 예시를 전송하지 않습니다.

보내기는 `--send`뿐입니다. 토큰과 서명은 붙지 않습니다. 주소에 `wake`가 있으면 거절합니다.

```bash
python -m process.send_telemetry --current current.json --send --url https://example.com/fleet_telemetry_config
```

이 전송이 실제 차에 적용되려면, 그 주소가 이미 서명하는 프록시여야 합니다. 처음 설정을 만들거나 서명하는 기능은 없습니다. vehicle-command를 따로 쓰거나, 코드를 직접 고쳐야 합니다. 설정은 차가 다음에 연결할 때 반영됩니다. 이 서버는 그 연결을 위해 차를 깨우지 않습니다.

`current.json`과 `next.json`에는 인증서가 들어갑니다. git이나 채팅에 붙이지 않습니다.

**대체.**

- 인증서 파일은 [Let's Encrypt](https://letsencrypt.org/)로 발급받아 수신 서버에 넣을 수 있습니다. Teslife는 그 파일을 읽지 않습니다.
- Teslife의 웹, 가공, 입력, 시뮬레이터는 Python 3.11이 있는 집 PC, 라즈베리 파이, 작은 VM에서 실행할 수 있습니다. fleet-telemetry는 공식 저장소의 설치 방법을 따릅니다. 차가 설정한 호스트와 포트로 수신 서버에 닿아야 합니다. 집 공유기의 포트 개방은 이 저장소가 하지 않습니다. 직접 열어야 합니다.
- 로그 파일만 이미 있으면, 설정을 다시 보내지 않고 아래 bridge만 실행해도 됩니다.

## 5. Teslife 실행

`bridge/bridge.py`는 표준 입력의 JSON을 읽습니다. `msg`가 `record_payload`인 줄만 저장합니다. 네트워크와 테슬라 API는 호출하지 않습니다. 주차가 아닐 때의 위치는 버립니다. DB 파일은 실행한 폴더의 `signals.db`입니다. 다른 파일로 바꾸는 옵션은 없습니다. 바꾸려면 그 파일의 `DB`를 직접 고쳐야 합니다.

저장소 폴더에서 실행합니다.

```bash
python -m bridge.bridge < telemetry.log
python -m web --db signals.db
```

`python bridge/bridge.py`만 실행하면 `ingest`를 찾지 못합니다. 저장소 폴더를 모듈 경로에 넣으면 됩니다.

```bash
PYTHONPATH=. python bridge/bridge.py < telemetry.log
```

웹은 로컬 포트 8080이고, 기본 호스트는 이 컴퓨터 자신입니다. `--host`와 `--port`로 바꿀 수 있습니다. 로그인은 없습니다. 한 번만 가공할 때는 `python -m process --db signals.db`입니다. `--no-seed`를 주면 예시 보험과 소모품 없이 시작합니다.

프로그램은 이미 프로세스에 있는 환경변수만 읽습니다. `.env` 파일을 자동으로 열지는 않습니다. 이름은 비워 두고, 실행 전에 셸에서 불러옵니다.

`.env.example`에 있는 이름:

- `NAVER_MAPS_CLIENT_ID`
- `NAVER_MAPS_CLIENT_SECRET`
- `NAVER_SEARCH_CLIENT_ID`
- `NAVER_SEARCH_CLIENT_SECRET`
- `BOT_NOTIFY_WEBHOOK_KEY`

코드가 더 읽는 이름입니다. 모두 선택입니다.

- `TESLIFE_GEOCODER` — 네이버 지도 키가 없을 때 `nominatim`이면 오픈스트리트맵으로 주소를 찾습니다. 비우면 주소 찾기를 하지 않습니다.
- `EV_CHARGER_API_KEY` — 한국 공공 충전기 목록입니다. 없으면 그 조회는 꺼집니다.
- `TESLIFE_ROUTE_MATCH` — `osrm-local`일 때만 내 OSRM으로 도로를 맞춥니다. 비우면 점 사이 직선입니다.
- `TESLIFE_OSRM_URL` — 그 OSRM 주소입니다. 비우면 이 컴퓨터의 5000번 포트입니다.
- `TESLIFE_ALERT_WEBHOOK_URL`, `TESLIFE_ALERT_WEBHOOK_KEY` — `python -m web --alerts`일 때 상태 알림을 보냅니다. 없으면 남기고 건너뜁니다.
- `BOT_NOTIFY_WEBHOOK_URL`, `BOT_PUSH_TOKEN` — 둘 다 있어야 봇 대기 전송을 합니다.
- `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` — 웹 푸시입니다. 비우면 DB 옆에 `vapid.json`을 한 번 만듭니다. 그 파일은 커밋하지 않습니다.

`TESLIFE_MAP_KAKAO_KEY`라는 이름은 있으나 화면은 카카오 지도를 그리지 않습니다. 카카오 지도를 쓰려면 직접 고쳐야 합니다.

공개 데모 OSRM 코드는 남아 있지만, 점 10개까지만 받아 실제 주행에는 맞지 않습니다. OSRM을 설치하거나 지도를 갱신하는 스크립트는 이 저장소에 없습니다. 그 서버를 띄우는 일은 직접 해야 합니다. 주소만 환경변수로 주면 도로 맞추기는 됩니다.

**대체.** 웹, 가공, 입력, 시뮬레이터는 DB 경로를 옵션으로 받습니다. bridge의 DB 이름만 코드에 고정되어 있습니다. 앱은 HTTP만 엽니다. 앞단 프록시는 코드를 고치지 않고 둘 수 있습니다. 앱 안에 HTTPS 인증서를 넣는 기능은 없어서, 그 방식은 직접 고쳐야 합니다.

## 6. 네이버 지도 (선택)

없어도 화면은 열립니다.

- `NAVER_MAPS_CLIENT_ID`가 있으면 지도가 네이버 동적 지도입니다. 브라우저로는 이 값만 갑니다.
- 아이디와 `NAVER_MAPS_CLIENT_SECRET`이 둘 다 있으면 주소 찾기는 네이버가 먼저입니다. 그 호출이 실패하면 오픈스트리트맵으로 넘어갑니다.
- `NAVER_SEARCH_CLIENT_ID`와 `NAVER_SEARCH_CLIENT_SECRET`은 충전 장소 검색에만 쓰고, 브라우저로 보내지 않습니다.

키는 [네이버 클라우드 플랫폼 지도 클라이언트 아이디](https://navermaps.github.io/maps.js.ncp/docs/tutorial-1-Getting-Client-ID.html)에서 발급합니다. 동적 지도(Dynamic Map)를 켜 둡니다.

**대체.** 키가 없으면 저장소에 들어 있는 Leaflet과 오픈스트리트맵 타일로 지도가 열립니다. 주소 이름은 `TESLIFE_GEOCODER=nominatim`일 때만 채워집니다. 장소 검색과 네이버 충전기 이름은 키가 없으면 비어 있습니다. 다른 지도 회사를 화면에 붙이려면 직접 고쳐야 합니다.

## 7. 비용

Fleet API 요금은 각자 개발자 계정에 청구됩니다. 달은 매월 1일에 시작합니다. 결제 수단이 없거나 한도를 넘으면 앱이 꺼질 수 있습니다. 공식 문서는 월 크레딧이라는 말 대신, 개인과 소규모 앱에 월 10달러 할인이 있다고 적습니다. 신호 간격이 짧고 항목이 많으면 사용량이 늘어납니다. 단가는 [과금 문서](https://developer.tesla.com/docs/fleet-api/billing-and-limits)와 [개발자 포털](https://developer.tesla.com/)에 있습니다. 이 저장소는 결제를 대신하지 않습니다.

**대체.** 시뮬레이터만 쓰면 테슬라 API를 호출하지 않습니다. 그 경우 이 계정 청구는 생기지 않습니다.

## 8. 주의

비밀값은 `.env`나 서버의 환경변수에만 둡니다. 이 문서와 채팅에 넣지 않습니다. `.gitignore`는 `.env`, `*.pem`, `*.key`, `vapid.json`, `current.json`, `next.json`, `after.json`을 무시합니다. 그 파일은 강제로 커밋하지 않습니다.

웹에는 계정이 없습니다. 루프백 밖에 열면, 그 주소에 닿는 사람이 기록을 볼 수 있습니다. 막을 장치는 앞에 직접 둡니다. 서버는 차를 깨우거나 제어하지 않습니다.

**대체.** 비밀을 코드에 넣는 기능은 없습니다. 그 방식으로 바꾸려면 직접 고쳐야 합니다.

## 서버를 직접 두기 싫다면

아래 서비스는 이 저장소와 연결되어 있지 않습니다.

- [TeslaMate](https://github.com/teslamate-org/teslamate) — 직접 띄우는 테슬라 주행 기록입니다. 문서는 [docs.teslamate.org](https://docs.teslamate.org/)입니다.
- [Tessie](https://www.tessie.com/) — 테슬라 관리와 통계를 제공하는 서비스입니다.
- [TezLab](https://tezlabapp.com/) — 주행과 충전 통계를 보여주는 전기차 앱입니다.
