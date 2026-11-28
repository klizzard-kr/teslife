/* Teslife UI language. Korean strings are the keys; English is the lookup.
   Default is Korean. The choice is stored in localStorage as teslife-lang.
   User-entered text is not rewritten: memos, saved place names, and custom
   names are skipped or left alone when they are not a known label. */
(function (root) {
  var LANG_KEY = "teslife-lang";
  var LANG = "ko";
  var applying = false;

  var EN = {
    "홈": "Home",
    "릴리즈 노트": "Release notes",
    "새 기능": "New",
    "고친 점": "Fixed",
    "초기 버전": "First version",
    "장소 지우기": "Clear place",
    "없음": "None",
    "주행": "Trips",
    "충전": "Charging",
    "차계부": "Ledger",
    "정비": "Service",
    "더보기": "More",
    "차량 상태": "Vehicle",
    "배터리": "Battery",
    "배터리 온도(최저~최고)": "Battery temperature (low–high)",
    "충전 멈춤": "Charging paused",
    "공기압": "Tire pressure",
    "공기압 단위": "Tire pressure unit",
    "소프트웨어 업데이트": "Software update",
    "충전 시작": "Charge start",
    "충전 완료": "Charging finished",
    "충전 자리": "Charging spot",
    "완충 권장": "Charge to 100%",
    "감시 경보": "Sentry alarm",
    "차량 알림": "Vehicle alert",
    "정비 시기": "Service due",
    "주행가능거리": "Range",
    "공조 모드": "Climate",
    "과열 방지": "Overheat protection",
    "실내 과열 방지": "Cabin Overheat Protection",
    "충전 상태": "Charging status",
    "감시 모드": "Sentry Mode",
    "감시": "Sentry",
    "타이어 공기압": "Tire pressure",
    "주행 전 → 후": "Before → after",
    "앞 좌측": "Front left",
    "앞 우측": "Front right",
    "뒤 좌측": "Rear left",
    "뒤 우측": "Rear right",
    "앞왼": "FL",
    "앞오": "FR",
    "뒤왼": "RL",
    "뒤오": "RR",
    "실내 온도": "Inside",
    "실외 온도": "Outside",
    "차 위치": "Car location",
    "현재 위치": "Current location",
    "차량 위치": "Vehicle location",
    "집": "Home",
    "집에 있음": "At home",
    "마지막 위치": "Last location",
    "마지막 위치가 없어요": "No last location yet",
    "집을 저장하면 집까지 거리를 알려 드려요": "Save home to see how far the car is.",
    "지금 차 위치를 집으로 저장": "Save the car's location as home",
    "핀 위치를 집으로 저장": "Save the pin as home",
    "핀을 차 위치로 옮겨요": "Move the pin to the car",
    "차 위치: 지도의 차는 위에서 본 차 그림이고, 코가 위를 향해요": "Car location: the map car is a top-down picture, nose pointing up",
    "방향: 차에서 방향이 오면 시계 방향으로 돌아요. 방향이 없으면 그대로 위를 봐요": "Heading: when the car sends a direction, it turns clockwise. With no direction it keeps pointing up",
    "집: 파란 압정이고, 바늘 끝이 집 자리예요": "Home: a blue pushpin, and the needle tip is the home spot",
    "위치 버튼: 동그란 조준 고리 한가운데에 작은 차가 있어요": "Locate button: a small car sits in the middle of a round crosshair",
    "차 위치: 지도를 많이 줄였을 때만, 차가 집 핀에 붙으면 핀만 보여요. 크게 보면 차와 핀을 같이 보여요": "Car location: only when you zoom out far does the car hide if it sits on the home pin. Zoomed in, the car and pin both show",
    "위치 버튼: 줌 버튼과 비슷한 크기이고, 배경을 더 옅게 했어요": "Locate button: it is about the size of the zoom buttons, and the background is lighter",
    "위치 버튼: 지도 안쪽에 32픽셀 원으로 보여요. 고리는 진한 회색이라 밝은 지도에서도 보여요": "Locate button: it shows as a 32-pixel circle inside the map. The ring is dark gray, so it stays visible on a light map",
    "홈: 아래 차량 수신 줄과 버전 줄이 바로 붙어 있어요": "Home: the last-heard line and the version line sit right together",
    "현재 위치: 제목과 동 이름을 보여 줘요. 집이면 동 이름 뒤에 (집)을 붙여요. 받은 시각은 그 줄 오른쪽에 있어요": "Current location: it shows the title and the neighbourhood. At home, (Home) follows the name. The time received sits on the right of that line",
    "차량 위치: 접힌 카드에는 제목과 동 이름만 보여요. 집이면 동 이름 뒤에 (집)을 붙여요": "Vehicle location: the folded card shows only the title and the neighbourhood. At home, (Home) follows the name",
    "홈: 아래 줄은 차량 수신과 오전·오후 시각으로 보여 줘요": "Home: the bottom line shows Updated and a 12-hour time",
    "위치 수신: 지도를 펼치면 오른쪽 위에 받은 시각이 3초 동안 보여요. 지도를 만지거나 움직이면 다시 보여요": "Location received: when you open the map, the time received shows at the top right for 3 seconds. Touching or moving the map shows it again",
    "현재 위치: 접힌 카드에는 제목과 동 이름만 보여요. 집이면 동 이름 뒤에 (집)을 붙여요. 카드 구성은 차량 상태와 같아요": "Current location: the folded card shows only the title and the neighbourhood. At home, (Home) follows the name. The card is laid out like Vehicle",
    "현재 위치: 위치와 상태를 두 칸으로 보여 줘요. 아래에는 지도가 항상 보여요. 카드 구성은 차량 상태와 같아요": "Current location: Location and Status sit in two columns, and the map stays open underneath. The card is laid out like Vehicle",
    "현재 위치: 지도 위에 구분선이 없어요. 집이면 동 이름 뒤에 집 아이콘이 붙어요": "Current location: there is no divider above the map. At home, a house icon follows the neighbourhood name",
    "현재 위치: 위치 값은 동 이름 하나예요. 동이 없으면 구, 구가 없으면 시·군, 그것도 없으면 도를 보여 줘요. 자세한 도로명·지번 주소는 화살표를 누르면 열려요": "Current location: the value is a single name, starting at the neighbourhood, then the district, then the city or county, then the province. Tap the chevron for the full road and lot addresses",
    "메뉴: 이미 보고 있는 탭의 단추를 다시 누르면, 그 화면이 맨 위로 부드럽게 올라가요. 다섯 탭 모두 같아요": "Menu: tapping the tab you are already on smoothly scrolls that screen to the top. All five tabs work the same way",
    "현재 위치: 집 핀을 눌러 고르면 지우기 단추가 나와요. 지도 위 핀 그림을 손가락으로 눌러도 같아요": "Current location: tapping the home pin shows the delete button. Tapping the pin picture on the map works the same way",
    "현재 위치: 집 아이콘과 화살표를 동 이름 글자 가운데에 맞춰요": "Current location: the house icon and the chevron line up with the middle of the neighbourhood name",
    "현재 위치: 동 이름, 집 아이콘, 화살표 줄 전체를 누르면 상세 주소가 열려요": "Current location: tapping anywhere on the name, house icon, and chevron row opens the address box",
    "상세 주소 상자: 도로명·지번 칸 이름은 주소와 겹치지 않게 한 줄로 두고, 복사 단추는 그 항목의 세로 가운데에 둬요": "Address box: the road and lot labels sit on their own line above the address, and each copy button is vertically centered in its item",
    "현재 위치: 집 핀을 눌러도 지우기 단추는 나오지 않아요. 핀을 옮겨 집으로 저장하는 것은 그대로예요": "Current location: tapping the home pin no longer shows a delete button. Dragging the pin to save it as home still works",
    "알림: 짧은 결과는 아래 메뉴 바로 위에 약 2초 동안 보여 줘요. 주소를 복사하면 '도로명 주소를 복사했어요' 또는 '지번 주소를 복사했어요'가 나와요": "Notices: short results show just above the bottom menu for about 2 seconds. Copying an address shows 'Copied the road address' or 'Copied the lot address'",
    "주행 지도: 속도·오토파일럿·이벤트는 색 안내가 지도 위에 있어요. 기본은 안내가 없어요": "Trip map: speed, Autopilot, and events show a color key on the map. The basic layer has no key",
    "주행 지도: 구간 정보창은 어두운 화면에서도 글자가 잘 보여요. 밖을 누르거나 화면을 내리거나 표시를 바꾸면 닫혀요": "Trip map: segment info stays readable in dark mode. It closes if you tap outside, scroll the page, or change the layer",
    "주행 지도: 고른 경로를 눌러도 선택이 풀리지 않아요. 그 구간의 정보창만 열려요": "Trip map: tapping the selected route does not clear the selection. Only that segment's info window opens",
    "도로명 주소를 복사했어요": "Copied the road address",
    "지번 주소를 복사했어요": "Copied the lot address",
    "오토파일럿 켜짐": "Autopilot on",
    "오토파일럿 꺼짐": "Autopilot off",
    "현재 위치: 집 핀을 눌러 고르면 지우기 단추가 나와요. 손가락으로 눌러도 같아요": "Current location: tapping the home pin shows the delete button. A finger tap works the same way",
    "현재 위치: 위치와 상태 글자 높이를 맞춰요. 동 이름과 집 아이콘 사이를 조금 더 띄워요": "Current location: the location and status values sit on the same line. There is a little more space between the neighbourhood name and the house icon",
    "상세 주소 상자: 복사 단추를 작고 둥글게 줄이고, 배경을 카드와 구분해 보여요. 화면을 스크롤하거나 상자 밖을 누르면 닫혀요": "Address box: the copy buttons are small rounded pills, and the background stands apart from the card. It closes if you scroll the page or tap outside it",
    "도로명": "Road",
    "지번": "Lot",
    "복사": "Copy",
    "상세 주소": "Full address",
    "위치 수신: 받은 시각은 네이버 지도와 Leaflet 모두 오른쪽 위에 있어요. 네이버 확대 단추는 왼쪽 위에 있어요": "Location received: the time received sits at the top right on both the Naver map and Leaflet. The Naver zoom control sits at the top left",
    "위치 수신: 지도를 처음 그릴 때 받은 시각이 3초 동안 보여요. 오늘이면 시:분, 아니면 상대 시각이에요. 지도를 만지거나 움직이면 다시 보여요": "Location received: when the map first loads, the time received shows for 3 seconds. Today it is a 24-hour time; on an older day it is relative. Touching or moving the map shows it again",
    "홈: 아래 줄은 차량 수신과 시:분으로 보여 줘요": "Home: the bottom line shows Updated and a 24-hour time",
    "위치 수신: 지도를 펼치면 받은 시각이 지도 오른쪽 아래에 있어요": "Location received: when you open the map, the time received sits at the bottom right",
    "집 핀: 핀을 누르면 지우기, 옮기면 집으로 저장할 수 있어요. 저장·삭제 전에 한 번 더 물어요": "Home pin: tap the pin to delete it, or move it to save that spot as home. It asks once more before saving or deleting",
    "홈: 실내·실외 온도는 차량 상태 카드 안에 있어요": "Home: inside and outside temperatures sit in the Vehicle card",
    "홈: 아래 줄은 신호 수신과 오전·오후 시각으로 보여 줘요": "Home: the bottom line shows Signal received and a 12-hour time",
    "차 위치: 지도를 줄여 차가 집 핀 끝에 붙으면 핀만 보여요": "Car location: zoom out until the car meets the home pin, and only the pin shows",
    "집: 압정 바늘이 더 짧고, 끝은 집 자리에 있어요": "Home: the pushpin needle is shorter, and its tip is the home spot",
    "위치 버튼: 같은 차 그림이에요. 누르면 핀을 차 위치로 옮기고 저장하지 않아요": "Locate button: the same car picture. It moves the pin to the car and does not save",
    "공기압: 가운데 차는 선으로 그린 그림이고, 낮은 바퀴는 붉은 선으로 보여요": "Tire pressure: the car in the middle is a line drawing, and a low tire is shown with a red line",
    "차 위치: 지도는 다른 화면과 같고, 차는 위를 향하는 화살표예요": "Car location: the map matches the other screens, and the car is an arrow pointing up",
    "방향: 차에서 방향이 오면 화살표가 시계 방향으로 돌아요": "Heading: when the car sends a direction, the arrow turns clockwise",
    "집: 핀을 옮긴 자리만 집으로 저장해요": "Home: only the moved pin is saved as home",
    "집 삭제": "Delete home",
    "이미 저장된 집을 이 위치로 바꿀까요?": "Replace the saved home with this spot?",
    "저장된 집을 지울까요?": "Delete the saved home?",
    "선택한 위치를 집으로 저장할까요?": "Save the selected spot as home?",
    "집까지 거리는 도로 기준이에요": "Distance follows the road.",
    "집까지 거리는 직선 기준이에요": "Distance is a straight line.",
    "불러오지 못했어요": "Could not load it.",
    "연결 중": "Connecting",
    "연결 끊김": "Offline",
    "불러오는 중": "Loading",
    "차량 값을 불러오지 못했어요": "Could not load the car",
    "차량 수신 ": "Updated ",
    "신호 수신 ": "Signal received ",
    "위치 수신 ": "Location received ",
    "방금": "Just now",
    "Teslife 대시보드": "Teslife",
    "기간": "Period",
    "월": "Month",
    "연도": "Year",
    "시작일": "Start",
    "종료일": "End",
    "닫기": "Close",
    "보기": "Show",
    "최근": "Recent",
    "오늘": "Today",
    "7일": "7 days",
    "이번 달": "This month",
    "올해": "This year",
    "전체": "All",
    "최근 5건": "Last 5",
    "최근 7일": "Last 7 days",
    "월별 요약": "By month",
    "횟수": "Count",
    "거리": "Distance",
    "전비": "Efficiency",
    "주행 기록": "Trips",
    "더 보기": "Show more",
    "방향": "Direction",
    "이벤트": "Events",
    "경로 표시": "Route style",
    "기본": "Basic",
    "속도": "Speed",
    "오토파일럿": "Autopilot",
    "겹친 경로 고르기": "Choose overlapping trip",
    "전비 추이": "Efficiency trend",
    "km/kWh · 기간 평균": "km/kWh · period average",
    "충전 기록": "Charges",
    "충전량": "Energy",
    "금액": "Amount",
    "충전 방식": "Charge type",
    "배터리 사용 · 주간": "Battery use · week",
    "지출 목록": "Expenses",
    "지출 추가": "Add expense",
    "지출 상세": "Expense",
    "알림": "Notifications",
    "모든 알림 받기": "All notifications",
    "알림 받기": "Enable notifications",
    "알림 권한": "Notification permission",
    "알림 종류": "Notification types",
    "모두 읽음": "Mark all read",
    "모두 삭제": "Delete all",
    "이 탭의 알림을 모두 지울까요?": "Delete every notice in this tab?",
    "알림이 없어요": "No notifications",
    "허용": "Allowed",
    "차단": "Blocked",
    "미설정": "Not set",
    "미지원": "Not supported",
    "정비 시기": "Service due",
    "100% 완충 권장": "Charge to 100%",
    "충전 자리": "Charging spot",
    "차량 알림": "Vehicle alert",
    "기한 지남": "Overdue",
    "아이폰에서는 홈 화면에 추가한 앱에서만 알림을 받을 수 있어요 (iOS 16.4 이상). Safari의 공유 버튼에서 홈 화면에 추가를 누른 뒤, 그 아이콘으로 다시 열어 주세요.":
      "On iPhone, notifications work in the Home Screen app (iOS 16.4 or later). In Safari, tap Share, then Add to Home Screen, and open Teslife from that icon.",
    "아이폰에서는 홈 화면에 추가해야 알림을 받을 수 있어요. 공유 버튼에서 홈 화면에 추가를 눌러 주세요.":
      "On iPhone, add Teslife to the Home Screen to get notifications. Tap Share, then Add to Home Screen.",
    "아이패드에서는 홈 화면에 추가해야 알림을 받을 수 있어요. 공유 버튼에서 홈 화면에 추가를 눌러 주세요.":
      "On iPad, add Teslife to the Home Screen to get notifications. Tap Share, then Add to Home Screen.",
    "아이폰 설정 > 알림 > Teslife에서 알림을 허용해 주세요.":
      "Allow notifications in iPhone Settings > Notifications > Teslife.",
    "아이패드 설정 > 알림 > Teslife에서 알림을 허용해 주세요.":
      "Allow notifications in iPad Settings > Notifications > Teslife.",
    "알림이 차단되어 있어요. 설정 앱 > 알림 > Teslife에서 알림을 허용해 주세요.":
      "Notifications are blocked. In the Settings app, open Notifications, then Teslife, and allow them.",
    "알림이 차단되어 있어요. Chrome에서 이 사이트의 알림을 허용해 주세요. 주소창의 자물쇠 > 권한 > 알림, 또는 Chrome 설정 > 사이트 설정 > 알림.":
      "Notifications are blocked. In Chrome, allow them for this site: tap the lock icon, then Permissions, then Notifications. Or open Chrome settings, Site settings, Notifications.",
    "이 브라우저는 웹 푸시를 지원하지 않아요.": "This browser does not support web push.",
    "합계": "Total",
    "기록 없음": "No records",
    "상세": "Details",
    "저장": "Save",
    "취소": "Cancel",
    "삭제": "Delete",
    "수정": "Edit",
    "이름": "Name",
    "장소": "Place",
    "사업자": "Operator",
    "충전기 출력": "Charger power",
    "충전기 후보": "Possible chargers",
    "출처: 한국환경공단": "Source: Korea Environment Corporation",
    "할인": "Discount",
    "단가": "Unit price",
    "청구 kWh": "Billed kWh",
    "금액 (원)": "Amount (원)",
    "할인(원)": "Discount (원)",
    "할인·포인트 (원)": "Discount and points (원)",
    "할인·포인트": "Discount and points",
    "단가표": "Price table",
    "요금 추가": "Add a rate",
    "적용 시작": "Effective from",
    "회원": "Member",
    "비회원": "Guest",
    "완속 (30kW 미만)": "AC (under 30 kW)",
    "중속 (30~49kW)": "Medium (30–49 kW)",
    "급속 (50~99kW)": "DC fast (50–99 kW)",
    "급속 (100~199kW)": "DC fast (100–199 kW)",
    "초급속 (200kW 이상)": "Ultra-fast (200 kW and over)",
    "단가 (원/kWh)": "Price (원/kWh)",
    "kWh당 단가": "Price per kWh",
    "출력·에너지 상세": "Power and energy",
    "충전기가 보낸 양": "Sent by the charger",
    "차가 받은 양": "Received by the car",
    " · 회원": " · member",
    "가격(원)": "Price (원)",
    "공임(원)": "Labor (원)",
    "결제 정보": "Payment",
    "저장된 장소 선택": "Saved place",
    "직접 입력": "Type it in",
    "직접 지정": "Set manually",
    "저장한 장소": "Saved place",
    "충전 정보 입력": "Enter charge details",
    "이 장소를 위치와 함께 저장 (근처 충전에 이름이 자동으로 붙어요)":
      "Save this place with its location (nearby charges pick up the name)",
    "금액 미입력": "No amount",
    "금액 입력 후 합계에 반영돼요": "Included in the total once you enter an amount",
    "합계에서 제외": "Left out of the total",
    "건 미입력 · 합계에서 제외": "no amount · left out of the total",
    "정상": "OK",
    "임박": "Due soon",
    "초과": "Overdue",
    "주기 미설정": "No interval",
    "필요 시 교체": "Replace when needed",
    "필요 시 점검": "Check when needed",
    "필요 시": "When needed",
    "점검 필요": "Needs service",
    "교체": "Replace",
    "점검": "Inspect",
    "작업": "Service",
    "교체 완료": "Replaced",
    "점검 기록": "Service log",
    "다음 점검": "Next check",
    "알림 꺼짐": "Alerts off",
    "미확인": "Unknown",
    "꺼짐": "Off",
    "켜짐": "On",
    "대기": "Standby",
    "실내 온도 유지": "Keep climate",
    "펫": "Pet",
    "캠핑": "Camp",
    "연결됨": "Plugged in",
    "연결 안 됨": "Unplugged",
    "충전 중": "Charging",
    "충전 완료": "Complete",
    "교류(AC)": "AC",
    "직류(DC)": "DC",
    "슈퍼차저": "Supercharger",
    "완속": "AC",
    "급속": "DC fast",
    "보험": "Insurance",
    "세금": "Tax",
    "세차": "Wash",
    "소모품": "Supplies",
    "기타": "Other",
    "타이어": "Tires",
    "주차": "Parked",
    "주차 중": "Parked",
    "주행 중": "Driving",
    "사용량": "Energy",
    "비용": "Cost",
    "평균 단가": "Avg price",
    "손실률": "Loss",
    "배터리 저장량": "Battery storage",
    "충전 중 사용량": "Use while charging",
    "충전 중 사용": "Use while charging",
    "에너지 흐름": "Energy flow",
    "손실량": "Loss",
    "손실": "Loss",
    "충전기": "Charger",
    "차": "Car",
    "변화 시간": "Change duration",
    "가속도": "Acceleration",
    "충전효율": "Charging efficiency",
    "지금까지 충전량": "Added so far",
    "충전 중 차가 쓴 양": "Used by the car while charging",
    "쓴 전력": "Energy used",
    "평균 속도": "Avg speed",
    "최고 속도": "Top speed",
    "걸린 시간": "Duration",
    "전력": "Power",
    "회생": "Regen",
    "회생률": "Regen rate",
    "회생제동": "Regen braking",
    "급가속": "Hard accel",
    "급제동": "Hard brake",
    "급가속·급제동": "Hard accel and brake",
    "공기압 전": "Pressure before",
    "공기압 후": "Pressure after",
    "실내·외 온도": "Inside and outside",
    "배터리 히터": "Battery heater",
    "평균 실내 온도": "Average inside",
    "평균 실외 온도": "Average outside",
    "최저 배터리 온도": "Lowest battery temperature",
    "최고 배터리 온도": "Highest battery temperature",
    "배터리 온도": "Battery temperature",
    "실내": "Inside",
    "온도": "Temp",
    "실외": "Outside",
    "설정 온도": "Set temperature",
    "조수석 설정": "Passenger set",
    "시트·열선": "Seats and heat",
    "실내·실외": "Inside and outside",
    "A/C·팬": "A/C and fan",
    "시트·핸들": "Seats and wheel",
    "유리·와이퍼": "Glass and wipers",
    "성에 제거": "Defrost",
    "자리": "Seat",
    "운전석 열선": "Driver heat",
    "조수석 열선": "Passenger heat",
    "뒷좌석 왼쪽 열선": "Rear left heat",
    "뒷좌석 가운데 열선": "Rear center heat",
    "뒷좌석 오른쪽 열선": "Rear right heat",
    "에어컨 켬": "A/C on",
    "에어컨 끔": "A/C off",
    "A/C 켬": "A/C on",
    "A/C 끔": "A/C off",
    "설정 온도: 운전석(실선)/조수석(점선)": "Set: driver (solid) / passenger (dashed)",
    "A/C: 켬(초록)/끔(분홍)": "A/C: on (green) / off (pink)",
    "팬 단계(하늘색, 수동일 때만)": "Fan level (sky blue, manual only)",
    "오토": "Auto",
    "수동": "Manual",
    "선": "Line",
    "바람 세기": "Fan level",
    "색": "Color",
    "최고": "High",
    "최저": "Low",
    "끄": "Off",
    "끔": "Off",
    "운전석": "Driver",
    "조수석": "Passenger",
    "운전석 통풍": "Driver vent",
    "조수석 통풍": "Passenger vent",
    "뒷좌석 왼쪽 통풍": "Rear left vent",
    "뒷좌석 가운데 통풍": "Rear center vent",
    "뒷좌석 오른쪽 통풍": "Rear right vent",
    "열선": "Heat",
    "통풍": "Vent",
    "진할수록 높은 단계": "Darker means a higher level",
    "운전석 시트": "Driver seat",
    "조수석 시트": "Passenger seat",
    "뒷좌석 왼쪽": "Rear left",
    "뒷좌석 가운데": "Rear center",
    "뒷좌석 오른쪽": "Rear right",
    "앞유리 성에 제거": "Front defrost",
    "뒷유리 열선": "Rear defrost",
    "와이퍼 열선": "Wiper heat",
    "충전 전력": "Charge power",
    "· 배터리 히터": "· battery heater",
    "평균·최고 속도": "Avg and top speed",
    "최대 출력": "Peak power",
    "최대 출력·회생": "Peak power and regen",
    "주요 메뉴": "Main menu",
    "월 선택": "Choose month",
    "연도 선택": "Choose year",
    "장소 없음": "No place",
    "진행 중": "In progress",
    "약 (앞 주행 끝으로 추정)": "Approx. (end of the previous trip)",
    "차가 보낸 마지막 위치": "Last position from the car",
    "자동": "Auto",
    "라이트": "Light",
    "다크": "Dark",
    "화면 테마, 자동": "Theme, Auto",
    "화면 테마, 라이트": "Theme, Light",
    "화면 테마, 다크": "Theme, Dark",
    "화면 테마, 자동. 누르면 라이트로 바꿉니다": "Theme, Auto. Switch to Light.",
    "화면 테마, 라이트. 누르면 다크로 바꿉니다": "Theme, Light. Switch to Dark.",
    "화면 테마, 다크. 누르면 자동으로 바꿉니다": "Theme, Dark. Switch to Auto.",
    "언어": "Language",
    "설정": "Settings",
    "화면": "Display",
    "화면 모드": "Display mode",
    "충전 결제": "Charging payment",
    "환경부 카드": "Environment card",
    "결제 방식": "Payment method",
    "충전기 번호": "Charger number",
    "충전 속도": "Charge speed",
    "전류": "Current",
    "평균 출력": "Average power",
    "중속": "Medium",
    "초급속": "Ultra-fast",
    "외부 온도": "Outside",
    "히터": "Heater",
    "팬": "Fan",
    "운전석 열선": "Driver seat heat",
    "시트 열선": "Seat heat",
    "조수석 열선": "Passenger seat heat",
    "핸들 열선": "Steering wheel heat",
    "선택 안 함": "Not selected",
    "최대 회생": "Peak regen",
    "주행가능": "Range",
    "회원": "Member",
    "에너지 상세": "Energy detail",
    "운전 패턴": "Driving pattern",
    "차가 쓴 양": "Used by the car",
    "사업자가 없어요": "No operators",
    "언어, 한국어. 누르면 영어로 바꿉니다": "Language, Korean. Switch to English.",
    "충전 기록을 찾지 못했어요": "Could not find that charge",
    "입력을 닫을까요?": "Close without saving?",
    "이 지출을 지울까요?": "Delete this expense?",
    "이 세트와 장착·트레드 기록, 연결된 차계부 기록을 지울까요?":
      "Delete this set, its mounts and tread logs, and the linked ledger rows?",
    "조회하지 못했어요": "Could not load",
    "저장하지 못했어요": "Could not save",
    "지우지 못했어요": "Could not delete",
    "지도를 불러오지 못했어요": "Could not load the map",
    "다시 불러오기": "Try again",
    "이 기간에는 경로 기록이 없어요": "No route in this period",
    "이 기간에는 위치가 있는 충전이 없어요": "No charge with a location in this period",
    "최근 100건만": "Last 100 only",
    "누르면 강조": "tap to highlight",
    "도로 맞춤 전": "Before map match",
    "도로에 맞춘 경로 없음": "No matched route",
    "오토파일럿 기록 없음": "No Autopilot log",
    "모드별 사용": "By mode",
    "시간별 사용": "By hour",
    "주차 · 펫": "Parked · pet",
    "주차 · 캠핑": "Parked · camp",
    "주차 · 실내 온도 유지": "Parked · keep climate",
    "주차 · 과열 방지": "Parked · overheat protection",
    "주차 · 미확인": "Parked · unknown",
    "감시 경보": "Sentry alarm",
    "세트 없음": "No set",
    "보관 중": "In storage",
    "세트 추가": "Add set",
    "세트 수정": "Edit set",
    "지금 세트 만들기": "Create the current set",
    "트레드 깊이": "Tread depth",
    "홈 깊이": "Tread",
    "마모 보증": "Treadwear warranty",
    "여름용": "Summer",
    "겨울용": "Winter",
    "사계절": "All-season",
    "다음 교체": "Next swap",
    "로테이션": "Rotation",
    "정비 주기": "Service schedule",
    "정비 설명서": "Service manual",
    "타이어 관리": "Tire care",
    "타이어 트레드 확인": "Check tire tread",
    "타이어 공기압": "Tire pressure",
    "실내 공기 필터 교체": "Replace the cabin filter",
    "HEPA 필터 교체": "Replace the HEPA filter",
    "브레이크액 확인": "Check brake fluid",
    "브레이크액 점검": "Inspect brake fluid",
    "브레이크액 교환": "Replace brake fluid",
    "와이퍼 블레이드 교체": "Replace wiper blades",
    "워셔액": "Washer fluid",
    "워셔액 보충": "Refill washer fluid",
    "냉각수 배출·보충": "Drain and refill coolant",
    "저전압 배터리": "Low-voltage battery",
    "점프 시동": "Jump start",
    "제동": "Brakes",
    "타이어 로테이션": "Tire rotation",
    "에어컨 필터(캐빈)": "Cabin air filter",
    "HEPA·카본 필터": "HEPA and carbon filter",
    "브레이크액": "Brake fluid",
    "와이퍼": "Wipers",
    "브레이크 캘리퍼 세척·윤활": "Brake caliper clean and lube",
    "워셔액 점검": "Washer fluid check",
    "에어컨 제습제": "A/C desiccant",
    "냉각수": "Coolant",
    "타이어(썸머/윈터)": "Tires (summer/winter)",
    "12V 배터리": "12V battery",
    "브레이크 패드 점검": "Brake pad check",
    "휠 얼라인먼트": "Wheel alignment",
    "주소 검색": "Search address",
    "검색": "Search",
    "주소나 장소 이름": "Address or place name",
    "위치 수정": "Edit location",
    "충전 위치": "Charge location",
    "충전 상세": "Charge details",
    "주행 상세": "Trip details",
    "항목 추가": "Add item",
    "이름 변경": "Rename",
    "주기 수정": "Edit interval",
    "기록 방식": "How to log",
    "이력": "History",
    "이력 보기": "History",
    "분류": "Category",
    "메모": "Note",
    "날짜": "Date",
    "수량": "Qty",
    "구매처": "Vendor",
    "구매일": "Purchase date",
    "제조사": "Brand",
    "모델": "Model",
    "사이즈": "Size",
    "종류": "Type",
    "세트": "Set",
    "장착": "Mount",
    "떼기만": "Remove only",
    "더 이상 안 씀": "Retired",
    "장착 날짜": "Mount date",
    "주행거리(km)": "Odometer (km)",
    "하중·속도 지수": "Load and speed index",
    "마모 보증(km)": "Treadwear warranty (km)",
    "새 타이어 홈 깊이(mm)": "New tread depth (mm)",
    "앞왼(mm)": "FL (mm)",
    "앞오(mm)": "FR (mm)",
    "뒤왼(mm)": "RL (mm)",
    "뒤오(mm)": "RR (mm)",
    "CSV 내려받기": "Download CSV",
    "확인된 지출": "Entered expenses",
    "계산할 정보가 없어요": "Not enough to calculate",
    "최근 기록 없음": "No recent records",
    "차계부를 불러오지 못했어요": "Could not load the ledger",
    "장소를 저장하지 못했어요": "Could not save the place",
    "장착 때 주행거리를 적어 주세요": "Enter the odometer at mount",
    "거리 또는 개월(먼저 도래)": "Distance or months, whichever comes first",
    "주기 km (비우면 미설정)": "Interval km (empty = none)",
    "주기 개월 (비우면 미설정)": "Interval months (empty = none)",
    "기본 기록 종류": "Default log type",
    "주기를 다시 세는 기록": "What restarts the interval",
    "점검·작업·교체 모두": "Inspect, service, and replace",
    "지금 차에 달린 세트": "Set on the car now",
    "지금 세트 기준": "Based on the current set",
    "시즌 교체는 로테이션으로 계산합니다. 따로 적은 기록은 그대로 두고, 그 기록이 더 최근이면 그 뒤를 셉니다.":
      "A seasonal swap counts as a rotation. A log you wrote stays, and if it is newer that one is used.",
    "한국타이어, 미쉐린": "e.g. Hankook, Michelin",
    "T Station 부천소사점, 쿠팡, 현대모비스": "e.g. a shop or store",
    "기여자": "contributors",
    "이전 주": "Previous week",
    "다음 주": "Next week",
    "온도·운전 패턴": "Temperature and driving",
    "속도·에너지 상세": "Speed and energy",
    "충전 출력 곡선": "Charge power",
    "배터리에 들어간 양": "Into the battery",
    "충전기에서 받은 양": "From the charger",
    "완충 예상": "Full by",
    "완충까지": "to full",
    "한도": "limit",
    "시간": "Time",
    "시작": "Start",
    "종료": "End",
    "시각": "Time",
    "주의": "Note",
    "위치": "Location",
    "상태": "Status",
    "완료": "Done",
    "추가": "Add",
    "규칙": "Rule",
    "다음": "Next",
    "공기압": "Pressure",
    "시트": "Seat",
    "핸들": "Wheel",
    "팬": "Fan",
    "열선·팬": "Heaters and fan",
    "공조": "Climate",
    "좌석": "Seats",
    "유리": "Glass",
    "앞 김서림 제거": "Front defrost",
    "뒷유리 열선": "Rear defrost",
    "와이퍼 열선": "Wiper heat",
    "급회전": "Hard turn",
    "운전 시간": "Driving time",
    "평균 출력": "Avg power",
    "원/kWh": "원/kWh",
    "kW": "kW",
    "20 km/h 미만": "under 20 km/h",
    "20–50 km/h": "20–50 km/h",
    "50–80 km/h": "50–80 km/h",
    "80–100 km/h": "80–100 km/h",
    "100 km/h 이상": "100 km/h and over",
    "2–3회": "2–3",
    "4–9회": "4–9",
    "10회 이상": "10 or more",
    "1회": "1",
    "DOT 생산 주차·연도": "DOT week and year",
    "누적 주행거리 (km, 선택)": "Odometer (km, optional)",
    "총액(원, 선택 — 비우면 가격×수량)": "Total (원, optional — blank uses price × qty)",
    "가격(원, 4개 합계)": "Price (원, all four)",
    "앞뒤 배치": "Front/rear layout",
    "앞뒤 배치 메모": "Front/rear note",
    "함께 한 작업": "Done together",
    "패드 교체": "Pad replacement",
    "패드 점검": "Pad check",
    "캘리퍼 세척·윤활": "Caliper clean and lube",
    "얼라인먼트": "Alignment",
    "로테이션 다음까지 약": "About",
    "다음 작업": "Next service",
    "작업·교체": "Service and replace",
    "교체만": "Replace only",
    "개월만": "Months only",
    "거리만": "Distance only",
    "임박 일": "Due within days",
    "임박 km": "Due within km",
    "주기 km": "Interval km",
    "주기 개월": "Interval months",
    "장착할 세트": "Set to mount",
    "지금 장착": "Mounted now",
    "자동 위치로": "Use the car's last position",
    "지도를 누르거나 핀을 끌어 옮겨요.": "Tap the map or drag the pin.",
    "월 하루 평균": "Daily average",
    "충전 기록 기간과 별도": "Separate from the charge period",
    "지나간 길 · 주행": "Roads driven",
    "선택": "Selected",
    "경로": "Routes",
    "(추정)": "(estimated)",
    "(예상)": "(est.)",
    "(최대)": "(max)",
    "(한도": "(limit",
    "알림이 차단되어 있어요. Chrome 설정 > 사이트 설정 > 알림에서 이 사이트를 허용해 주세요.":
      "Notifications are blocked. Allow this site in Chrome Settings > Site settings > Notifications.",
    "Safari 설정 > 웹사이트 > 알림에서 이 사이트를 허용해 주세요.":
      "Allow this site in Safari Settings > Websites > Notifications.",
    "Edge 설정 > 쿠키 및 사이트 권한 > 알림에서 이 사이트를 허용해 주세요.":
      "Allow this site in Edge Settings > Cookies and site permissions > Notifications.",
    "Firefox 설정 > 개인 정보 및 보안 > 알림에서 이 사이트를 허용해 주세요.":
      "Allow this site in Firefox Settings > Privacy and Security > Notifications.",
    "삼성 인터넷 설정 > 사이트 및 다운로드 > 알림에서 이 사이트를 허용해 주세요.":
      "Allow this site in Samsung Internet Settings > Sites and downloads > Notifications.",
    "브라우저 설정에서 이 사이트의 알림을 허용해 주세요.":
      "Allow this site's notifications in your browser settings.",
    "이 기기의 알림 구독이 해제되어 있어요. 설정 앱 > 알림 > Teslife에서 알림을 허용한 뒤 스위치를 다시 켜 주세요.":
      "This device is unsubscribed. In the Settings app, open Notifications, then Teslife, allow them, and turn the switch on again.",
    "이 기기의 알림 구독이 해제되어 있어요. Chrome 설정 > 사이트 설정 > 알림에서 이 사이트를 허용한 뒤 스위치를 다시 켜 주세요.":
      "This device is unsubscribed. In Chrome settings, open Site settings, then Notifications, allow this site, and turn the switch on again.",
    "· 위치 약": "· about",
    "· 히터": "· heater",
    "· 뒤": "· rear"
  };

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var PHRASES = Object.keys(EN).filter(function (k) { return k.length >= 4; })
    .sort(function (a, b) { return b.length - a.length; });

  function readLang(storage) {
    try {
      var saved = storage && storage.getItem(LANG_KEY);
      if (saved === "en" || saved === "ko") return saved;
    } catch (e) {}
    return "ko";
  }

  function writeLang(storage, lang) {
    var v = lang === "en" ? "en" : "ko";
    storage.setItem(LANG_KEY, v);
    return v;
  }

  function safeStorage() {
    try { return root.localStorage || localStorage; } catch (e) { return null; }
  }

  function applyPatterns(s) {
    var out = s;
    out = out.replace(/집에서 (\d+(?:\.\d+)?)km/g, "$1 km from home");
    out = out.replace(/^(.+) \(집\)$/g, "$1 (Home)");
    out = out.replace(/오후 (\d{1,2}:\d{2})/g, "$1 PM");
    out = out.replace(/오전 (\d{1,2}:\d{2})/g, "$1 AM");
    out = out.replace(/(^|[\s·(])약 /g, "$1about ");
    out = out.replace(/\(0\.1% 미만\)/g, "(under 0.1%)");
    out = out.replace(/0\.1% 미만/g, "under 0.1%");
    out = out.replace(/이 (\d+(?:\.\d+)? (?:psi|bar))보다 낮아요/g, " is below $1");
    out = out.replace(/이 권장\(([^)]*)\)보다 낮습니다 · 차가운 상태 기준/g, " is below the recommended $1 (cold)");
    out = out.replace(/이 권장 공기압보다 낮습니다 · 차가운 상태 기준/g, " is below the recommended pressure (cold)");
    out = out.replace(/(\d+)시간 (\d+)분/g, "$1 h $2 min");
    out = out.replace(/(\d+)시간 전/g, "$1 h ago");
    out = out.replace(/(\d+)분 전/g, "$1 min ago");
    out = out.replace(/(\d+)일 전/g, "$1 d ago");
    out = out.replace(/(\d+)시간/g, "$1 h");
    out = out.replace(/(\d+)개월/g, "$1 mo");
    out = out.replace(/(\d+)일/g, "$1 d");
    out = out.replace(/(\d+)건/g, "$1");
    out = out.replace(/(\d+)회/g, "$1");
    out = out.replace(/(\d+(?:\.\d+)?)초/g, "$1 s");
    out = out.replace(/(\d+)번/g, "$1");
    out = out.replace(/(\d+)년 (\d+)월/g, function (_, y, m) {
      var name = MONTHS[Number(m) - 1] || m;
      return name + " " + y;
    });
    out = out.replace(/(\d+)월/g, function (_, m) { return MONTHS[Number(m) - 1] || (m + "월"); });
    out = out.replace(/(\d+)년/g, "$1");
    out = out.replace(/(\d+)시/g, "$1h");
    out = out.replace(/km 남음/g, "km left");
    out = out.replace(/km 지남/g, "km over");
    out = out.replace(/분 전/g, " min ago");
    return out;
  }

  function translateCore(s, lang) {
    var use = lang || LANG;
    if (use !== "en" || s == null) return s;
    var key = String(s);
    if (!/[가-힣]/.test(key)) return key;
    if (Object.prototype.hasOwnProperty.call(EN, key)) return EN[key];
    var out = key;
    for (var i = 0; i < PHRASES.length; i++) {
      var phrase = PHRASES[i];
      if (out.indexOf(phrase) >= 0) out = out.split(phrase).join(EN[phrase]);
    }
    if (out.indexOf(" · ") >= 0) {
      out = out.split(" · ").map(function (part) {
        return Object.prototype.hasOwnProperty.call(EN, part) ? EN[part] : part;
      }).join(" · ");
    }
    if (/[가-힣]/.test(out)) out = applyPatterns(out);
    return out;
  }

  function translateText(s, lang) {
    if (s == null) return s;
    var use = lang || LANG;
    var raw = String(s);
    if (use !== "en") return raw;
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(raw);
    if (!m || !m[2]) return raw;
    return m[1] + translateCore(m[2], use) + m[3];
  }

  function t(s) {
    return translateText(s == null ? "" : String(s));
  }

  function modeFor(el) {
    if (!el || !el.closest) return "full";
    if (el.closest("[data-i18n-skip], script, style, .lr-memo, .trip-route-label, .place-name, textarea")) return "skip";
    if (el.closest("input")) return "skip";
    if (el.closest(".name")) return "exact";
    return "full";
  }

  function paint(node) {
    var cur = node.nodeValue;
    if (cur == null) return;
    var orig = node.__i18nOrig;
    if (orig == null) {
      node.__i18nOrig = cur;
      orig = cur;
    } else {
      var enShown = translateText(orig, "en");
      if (cur !== orig && cur !== enShown) {
        node.__i18nOrig = cur;
        orig = cur;
      }
    }
    var parent = node.parentElement;
    var mode = modeFor(parent);
    if (mode === "skip") return;
    var forced = parent && parent.getAttribute && parent.getAttribute("data-i18n-en");
    var next = orig;
    if (LANG === "en") {
      if (forced) {
        var pad = /^(\s*)([\s\S]*?)(\s*)$/.exec(String(orig)) || ["", "", "", ""];
        next = pad[1] + forced + pad[3];
      } else if (mode === "exact") {
        var trim = String(orig).trim();
        next = Object.prototype.hasOwnProperty.call(EN, trim) ? String(orig).replace(trim, EN[trim]) : orig;
      } else {
        next = translateText(orig, "en");
      }
    }
    if (next !== cur) node.nodeValue = next;
  }

  function paintAttr(el, name) {
    if (!el || !el.getAttribute || modeFor(el) === "skip") return;
    var cur = el.getAttribute(name);
    if (cur == null || cur === "") return;
    el.__i18nAttr = el.__i18nAttr || {};
    var orig = el.__i18nAttr[name];
    if (orig == null) {
      el.__i18nAttr[name] = cur;
      orig = cur;
    } else {
      var enShown = translateText(orig, "en");
      if (cur !== orig && cur !== enShown) {
        el.__i18nAttr[name] = cur;
        orig = cur;
      }
    }
    var next = LANG === "en" ? translateText(orig, "en") : orig;
    if (next !== cur) el.setAttribute(name, next);
  }

  function apply(rootEl) {
    if (!rootEl || typeof document === "undefined") return;
    applying = true;
    try {
      var walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, null);
      var nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(paint);
      ["aria-label", "title", "placeholder", "alt"].forEach(function (name) {
        var list = rootEl.querySelectorAll ? rootEl.querySelectorAll("[" + name + "]") : [];
        Array.prototype.forEach.call(list, function (el) { paintAttr(el, name); });
      });
      if (rootEl === document.documentElement || (rootEl.querySelector && rootEl.querySelector("title"))) {
        var title = document.querySelector("title");
        if (title && title.firstChild) paint(title.firstChild);
      }
    } finally {
      applying = false;
    }
  }

  function setLanguage(lang, persist) {
    LANG = lang === "en" ? "en" : "ko";
    if (persist !== false) {
      try {
        var store = safeStorage();
        if (store) writeLang(store, LANG);
      } catch (e) {}
    }
    if (typeof document === "undefined" || !document.documentElement) return LANG;
    document.documentElement.lang = LANG === "en" ? "en" : "ko";
    document.documentElement.setAttribute("data-lang", LANG);
    var langSel = document.getElementById("lang-select");
    if (langSel) langSel.value = LANG;
    var dates = document.querySelectorAll("input[type=date]");
    Array.prototype.forEach.call(dates, function (el) { el.lang = LANG === "en" ? "en" : "ko"; });
    apply(document.documentElement);
    return LANG;
  }

  function install() {
    if (typeof document === "undefined" || !document.documentElement) return;
    try { LANG = readLang(safeStorage()); } catch (e) { LANG = "ko"; }
    var queued = false;
    var mo = new MutationObserver(function () {
      if (applying || LANG !== "en" || queued) return;
      queued = true;
      (root.queueMicrotask || function (fn) { setTimeout(fn, 0); })(function () {
        queued = false;
        if (!applying && LANG === "en") apply(document.documentElement);
      });
    });
    mo.observe(document.documentElement, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ["aria-label", "title", "placeholder", "alt"]
    });
    setLanguage(LANG, false);
  }

  root.TeslifeI18n = {
    LANG_KEY: LANG_KEY,
    get lang() { return LANG; },
    en: EN,
    ko: {},
    readLang: readLang,
    writeLang: writeLang,
    t: t,
    translateText: translateText,
    setLanguage: setLanguage,
    apply: apply,
    install: install
  };
  Object.keys(EN).forEach(function (k) { root.TeslifeI18n.ko[k] = k; });
})(typeof globalThis !== "undefined" ? globalThis : this);
