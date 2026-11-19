"""Turn a stored notice into the sentence, summary, and rows the screen shows.

The notifications table is not updated. Old titles such as '주행' and a dotted
body are converted here, at read time.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WHEEL_LABELS = ("앞 왼쪽", "앞 오른쪽", "뒤 왼쪽", "뒤 오른쪽")
# Trip detail, then the alert-only rows. Tires stay last, as on the request.
_TRIP_ROW_ORDER = (
    "거리", "시간", "쓴 전력", "전비",
    "배터리", "주행가능",
    "회생", "급가속", "급제동",
    "최대 출력", "최대 회생",
    *WHEEL_LABELS,
)
# Charge detail: the headline numbers, the charger, then the energy rows.
_CHARGE_ROW_ORDER = (
    "충전량", "금액", "시간", "kWh당 단가",
    "배터리",
    "사업자", "충전 속도", "정격 출력", "전류",
    "배터리 저장량", "충전 중 사용량", "손실률", "충전효율",
    "평균 충전 전력", "최대 충전 전력", "충전 전력",
    "종료 사유",
)
_LABEL_ALIAS = {
    "주행가능거리": "주행가능",
    "에너지": "배터리 저장량",
    "종류": "전류",
}
WHEEL_KEYS = ("fl", "fr", "rl", "rr")
_WHEEL_KEY = dict(zip(WHEEL_LABELS, WHEEL_KEYS))
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_STAMP = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{2}):(\d{2})")
_KM = re.compile(r"(\d+(?:\.\d+)?)\s*km(?!/)", re.I)
_MIN = re.compile(r"(\d+)\s*분")
_SOC = re.compile(r"(\d+)\s*%\s*(?:→|->)\s*(\d+)\s*%")
_KWH = re.compile(r"(\d+(?:\.\d+)?)\s*kWh", re.I)
_BAT = re.compile(r"배터리\s*(\d+)\s*%")
_RANGE = re.compile(r"주행가능\s*(\d+)\s*km", re.I)
_TIRES = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
_REC_PHRASE = re.compile(r"(?:[,·]\s*)?권장(?:\s*공기압)?\s*\d+(?:\.\d+)?\s*psi")
_TIME_LABELS = {"시작 시각", "종료 시각", "시각"}
_COORD = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")
_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_LIST_TABS = {
    "sentry": "security",
    "vehicle": "car",
    "tire": "car",
    "maintenance": "car",
    "software": "car",
}


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    text = str(ts).strip().replace("Z", "+00:00")
    if _STAMP.fullmatch(text):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _full(ts: str) -> str:
    dt = _parse(ts)
    if dt is None:
        return ts
    local = dt.astimezone(KST)
    return f"{local.year}. {local.month}. {local.day}. {local.hour:02d}:{local.minute:02d}"


def _clock(ts: str) -> str:
    dt = _parse(ts)
    if dt is None:
        return ""
    local = dt.astimezone(KST)
    return f"{local.hour:02d}:{local.minute:02d}"


def _event_ts(notice_id: str, body: str, stored: str) -> str:
    if notice_id.startswith("trip_start:"):
        rest = notice_id.split(":", 1)[1]
        if _parse(rest):
            return _iso(_parse(rest))
    stamp = _STAMP.search(body or "")
    year = (stored or "")[:4]
    if stamp and year.isdigit():
        month, day, hour, minute = (int(part) for part in stamp.groups())
        try:
            local = datetime(int(year), month, day, hour, minute, tzinfo=KST)
        except ValueError:
            local = None
        if local is not None:
            return _iso(local)
    return stored


def _sentence(title: str, body: str, notice_id: str) -> str:
    text = (title or "").strip()
    if text.endswith(("요", "예요", "어요", "세요", "해요")) and "·" not in text and text not in ("주행",):
        return text
    blob = f"{body}\n{notice_id}"
    if "주행 종료" in blob or notice_id.startswith("trip_end"):
        return "주행을 마쳤어요"
    if "주행 시작" in blob or notice_id.startswith("trip_start") or text == "주행":
        return "주행을 시작했어요"
    if notice_id.startswith("charge_end") or text in ("충전", "충전 완료"):
        return "충전이 완료되었어요"
    if "충전 시작" in blob or notice_id.startswith("charge_start") or text == "충전 시작":
        return "충전을 시작했어요"
    if "감시" in blob or notice_id.startswith("sentry"):
        return "감시 경보가 울렸어요"
    return text or "알림"


def inbox_tab(kind: str) -> str | None:
    """Security and vehicle notices open the list on this tab, not a detail popup."""
    return _LIST_TABS.get(kind or "")


def _clean_place(text: str) -> str:
    cleaned = text.replace("(근사)", "").replace("（근사）", "")
    cleaned = _COORD.sub("", cleaned)
    cleaned = re.sub(r"(^|\s)위치\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ,·")


def _list_full_summary(body: str) -> str:
    """Security and vehicle rows show the useful text in the list, without a place prefix."""
    raw = (body or "").split("\n", 1)[0]
    parts = [part.strip() for part in raw.split("·")]
    kept = []
    for index, part in enumerate(parts):
        if index == 0 and _STAMP.search(part):
            continue
        cleaned = _clean_place(part)
        if cleaned:
            kept.append(cleaned)
    return " · ".join(kept)


def _local_spot(text: str) -> str:
    """Dong and road, the part a person recognizes, without a city prefix."""
    cleaned = _clean_place(text).replace("차 주변을 확인해 보세요", "")
    cleaned = re.sub(r"^근처\s+", "", cleaned.strip())
    found = re.search(r"(\S+(?:동|읍|면))(?:\s+(\S+(?:로|길|대로)))?", cleaned)
    if found:
        road = found.group(2) or ""
        return (found.group(1) + (" " + road if road else "")).strip()
    bits = [bit for bit in re.split(r"[\s,·]+", cleaned) if bit and bit not in ("근처", "위치")]
    if len(bits) >= 2:
        return " ".join(bits[-2:])
    return bits[-1] if bits else ""


def _iga(name: str) -> str:
    """이 after a batchim, 가 after a vowel."""
    for ch in reversed(name or ""):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            return "이" if (code - 0xAC00) % 28 else "가"
    return "이"


def open_parts(notice_id: str, body: str, details: list | None = None) -> list[str]:
    """Part names such as 충전구 or 창문. The stored sentence is not trusted."""
    found: list[str] = []
    for pair in details or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and str(pair[0]).strip() == "열린 곳":
            name = str(pair[1]).strip()
            if name and name not in found:
                found.append(name)
    if found:
        return found
    state = notice_id.split(":", 1)[1] if notice_id.startswith("open_after_park:") else ""
    if state and state != "closed":
        return [part for part in state.split(",") if part]
    return []


def _longer(title: str, summary: str) -> str:
    """A server summary stays longer than its title and does not repeat that title."""
    text = (summary or "").strip()
    head = (title or "").strip()
    if head and head in text:
        text = text.replace(head, "").strip(" .")
    if len(text) > len(head):
        return text
    extra = "자세한 내용은 앱에서 확인해 주세요"
    if extra in text:
        extra = "화면에서 이어서 볼 수 있어요"
    return f"{text} {extra}".strip() if text else extra


def open_after_copy(parts: list[str]) -> tuple[str, str]:
    """Short topic, then a longer sentence that does not repeat the part."""
    names = parts or ["문"]
    if len(names) == 1:
        title = f"{names[0]}{_iga(names[0])} 열려 있어요"
        summary = "주차한 뒤에도 닫히지 않았어요. 확인해 주세요"
    else:
        title = "여러 곳이 열려 있어요"
        summary = f"주차한 뒤에도 {', '.join(names)} 닫히지 않았어요. 닫아 주세요"
    return title, _longer(title, summary)


# Code -> (title, summary). The summary never includes the English code.
VEHICLE_FALLBACK = {
    "CP_a058_acChargeRetryPending": (
        "완속 충전이 잠시 안 돼요",
        "잠시 후 다시 시도해요. 안 되면 다른 충전기를 써 보세요",
    ),
    "CP_a004_proximityRationality": (
        "충전기를 알아보지 못해요",
        "다시 시도해 주세요. 그래도 안 되면 다른 장비를 써 보세요",
    ),
    "CP_a046_lostCommsEVSE": (
        "충전기와 연결이 끊겼어요",
        "전원 쪽을 먼저 확인해 주세요. 그래도 안 되면 다른 곳을 써 보세요",
    ),
    "CP_a054_cableNotSecured": (
        "충전 케이블이 끝까지 안 들어갔어요",
        "이물질이 없는지 봐 주세요. 다시 꽂아 주세요",
    ),
    "CP_a055_chargeStoppedNoPilot": (
        "충전기와 연결이 끊겼어요",
        "전원 쪽을 먼저 확인해 주세요. 그래도 안 되면 다른 곳을 써 보세요",
    ),
    "CP_a056_proxDisconnected": (
        "충전 케이블이 빠졌어요",
        "충전구를 닫고 브레이크를 밟은 뒤 다시 시도해 주세요",
    ),
    "CP_a062_scOutOfService": (
        "이 슈퍼차저 칸은 쓸 수 없어요",
        "다른 칸으로 옮겨 주세요. 이 칸은 잠시 쉬고 있어요",
    ),
    "CP_a064_superchargingBlocked": (
        "슈퍼차저를 쓸 수 없어요",
        "완속 충전을 이용해 보세요. 이 자리는 지금 막혀 있어요",
    ),
    "CP_a066_proxLatchedIdlePilot": (
        "충전기가 아직 준비되지 않았어요",
        "안내를 따라 시작해 주세요. 준비가 끝나면 충전이 이어져요",
    ),
    "CP_a078_latchNotDisengagedCold": (
        "충전 케이블이 얼어서 안 빠져요",
        "앱에서 성에 제거를 켠 뒤 다시 시도해 주세요",
    ),
}
_ALERT_CATEGORY = {
    "CP": "충전",
    "BMS": "배터리",
    "DI": "구동",
    "PM": "타이어",
    "TAS": "타이어",
    "TPMS": "타이어",
    "VCFRONT": "문",
    "VCLEFT": "문",
    "VCRIGHT": "문",
    "VCSEC": "문",
    "THC": "공조",
    "HVAC": "공조",
    "UI": "화면",
    "APP": "주행",
    "DAS": "주행",
}


def alert_code(notice_id: str) -> str:
    if not notice_id.startswith("vehicle_alert:"):
        return ""
    rest = notice_id.split(":", 1)[1]
    return rest.split("|", 1)[0]


def alert_category(code: str) -> str:
    prefix = (code or "").split("_", 1)[0]
    return _ALERT_CATEGORY.get(prefix, "차")


def vehicle_copy(code: str, ended: bool = False) -> tuple[str, str]:
    """Title and one or two sentences. The English code stays out of both."""
    known = VEHICLE_FALLBACK.get(code)
    category = alert_category(code)
    if ended:
        title = "차 알림이 해제됐어요"
        if category == "차":
            summary = "지금은 괜찮아졌어요. 같은 일이 반복되면 앱에서 확인해 주세요"
        else:
            summary = f"{category} 상태는 다시 정상이에요. 같은 일이 반복되면 앱에서 확인해 주세요"
        return title, summary
    if known:
        title, summary = known
        return title, _longer(title, summary)
    title = "차에서 알림이 왔어요"
    if category == "차":
        summary = "자세한 내용은 앱에서 확인해 주세요. 화면에서 이어서 볼 수 있어요"
    else:
        summary = f"{category} 관련 알림이에요. 자세한 내용은 앱에서 확인해 주세요"
    return title, summary


def _car_notice(notice_id: str, body: str, details: list | None) -> dict | None:
    """Display copy for parked-open and vehicle alerts. Stored columns stay as they are."""
    if notice_id.startswith("open_after_park:"):
        parts = open_parts(notice_id, body, details)
        if not parts and "closed" in notice_id:
            return None
        title, summary = open_after_copy(parts)
        rows = [[ "열린 곳", part] for part in parts]
        return {"title": title, "summary": summary, "rows": rows}
    if notice_id.startswith("vehicle_alert:"):
        code = alert_code(notice_id)
        ended = "|end" in notice_id
        title, summary = vehicle_copy(code, ended)
        rows = []
        if title:
            rows.append(["알림", title if not ended else summary])
        if code:
            rows.append(["코드", code])
        return {"title": title, "summary": summary, "rows": rows}
    return None


def _km_text(value: str | float | int) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if isinstance(value, float):
        return str(int(value))
    return str(value)


def format_trip_distance(km: float) -> str:
    """Under 1 km is whole meters. A longer trip stays in kilometres."""
    if km < 1:
        return f"{int(round(float(km) * 1000))}m"
    return f"{_km_text(float(km))} km"


def _distance_km(raw: str) -> tuple[float | None, str]:
    """Kilometres, and the piece that goes before '달렸어요' (meters already include m)."""
    text = (raw or "").strip()
    if not text:
        return None, ""
    number = _number(text)
    if number is None:
        return None, ""
    if text.lower().endswith("km"):
        value = float(number)
        if value < 1:
            return value, f"{int(round(value * 1000))}m"
        return value, _km_text(value)
    if text.endswith("m"):
        return float(number) / 1000.0, f"{int(round(float(number)))}m"
    value = float(number)
    if value < 1:
        return value, f"{int(round(value * 1000))}m"
    return value, _km_text(value)


def _minutes(raw: str) -> float | None:
    text = raw or ""
    hours = re.search(r"(\d+)\s*시간", text)
    mins = re.search(r"(\d+)\s*분", text)
    if not hours and not mins:
        return None
    return (int(hours.group(1)) if hours else 0) * 60 + (int(mins.group(1)) if mins else 0)


def spot_move(km: float | None, minutes: float | None) -> bool:
    """A short shuffle, not a drive. About 200 m and under three minutes."""
    if km is None or km >= 0.2:
        return False
    if minutes is None:
        return True
    return minutes < 3


def _labeled_body(body: str, details: list | None = None) -> dict[str, str]:
    labeled = _detail_map(details)
    for line in (body or "").splitlines():
        if "|" not in line:
            continue
        label, value = line.split("|", 1)
        labeled.setdefault(label.strip(), value.strip())
    return labeled


def trip_end_summary(body: str, details: list | None = None) -> str:
    """Distance, then battery and range when those values are already stored."""
    text = body or ""
    labeled = _labeled_body(text, details)
    km: str | None = None
    km_value: float | None = None
    if labeled.get("거리"):
        km_value, km = _distance_km(labeled["거리"])
        if km_value is None:
            km = None
    if km is None:
        head = text.split("주행가능")[0]
        match = _KM.search(head)
        if match:
            km_value = float(match.group(1))
            km = _km_text(km_value)
    minutes = _minutes(labeled.get("시간") or "")
    battery = labeled.get("배터리") or ""
    if not battery:
        bat = _BAT.search(text)
        if bat:
            battery = bat.group(1) + "%"
        else:
            soc = _SOC.search(text)
            if soc:
                battery = soc.group(2) + "%"
    battery = _list_battery(battery) if battery else ""
    range_num = _number(labeled.get("주행가능") or labeled.get("주행가능거리") or "")
    if range_num is None:
        found = _RANGE.search(text)
        if found:
            range_num = int(found.group(1))
    tail: list[str] = []
    if battery:
        shown = battery if "%" in battery else f"{battery}%"
        tail.append(f"배터리 {shown}")
    if range_num is not None:
        tail.append(f"{_km_text(range_num)}km 남았어요")
    moved = spot_move(km_value, minutes)
    title = "자리를 옮겼어요" if moved else "주행을 마쳤어요"
    if km and moved:
        phrase = f"{km} 이동했어요"
        if tail:
            return _longer(title, phrase + ". " + ", ".join(tail))
        return _longer(title, phrase + ". 도착 기록을 앱에서 볼 수 있어요")
    if km and tail:
        unit = "" if str(km).endswith("m") else "km"
        return _longer(title, f"{km}{unit} 달렸어요. " + ", ".join(tail))
    if km:
        unit = "" if str(km).endswith("m") else "km"
        return _longer(title, f"{km}{unit} 달렸어요. 도착 기록을 앱에서 볼 수 있어요")
    if tail:
        return _longer(title, "도착했어요. " + ", ".join(tail))
    return _longer(title, "도착했어요. 주행 기록을 앱에서 확인해 주세요")


def _charge_pace(text: str) -> str:
    if "직류" in text or "(DC)" in text:
        return "급속"
    if "교류" in text or "(AC)" in text or "완속" in text:
        return "완속"
    return ""


def _kw_text(text: str) -> str:
    found = re.search(r"(\d+(?:\.\d+)?)\s*kW(?!h)", text, re.I)
    if not found:
        return ""
    return f"{float(found.group(1)):.1f}"


def charge_start_summary(body: str, details: list | None = None) -> str:
    """Start percent, target, power, and AC/DC. Missing numbers are left out."""
    text = (body or "") + "\n" + "\n".join(
        f"{label}|{value}" for label, value in _labeled_body(body, details).items()
    )
    bat = _BAT.search(text) or re.search(r"배터리\|(\d+)\s*%", text)
    target = re.search(r"목표\s*(\d+)\s*%", text)
    kw = _kw_text(text)
    pace = _charge_pace(text)
    title = "충전을 시작했어요"
    if bat and target and kw:
        pace_bit = f" {pace}" if pace else ""
        return _longer(title, f"{bat.group(1)}%에서 {target.group(1)}%까지, 약 {kw}kW{pace_bit}이에요")
    if bat and target:
        return _longer(title, f"{bat.group(1)}%에서 {target.group(1)}%까지 충전해요. 전력은 앱에서 확인해 주세요")
    if kw:
        pace_bit = f" {pace}" if pace else ""
        return _longer(title, f"약 {kw}kW{pace_bit}이에요. 배터리 상태는 앱에서 확인해 주세요")
    if bat:
        return _longer(title, f"배터리 {bat.group(1)}%에서 시작해요. 목표는 앱에서 확인해 주세요")
    return _longer(title, "충전 상태를 앱에서 확인해 주세요. 전력과 목표를 이어서 볼 수 있어요")


def charge_end_summary(body: str, details: list | None = None) -> str:
    text = (body or "") + "\n" + "\n".join(
        f"{label}|{value}" for label, value in _labeled_body(body, details).items()
    )
    soc = _SOC.search(text)
    kwh = _KWH.search(text)
    pace = _charge_pace(text)
    title = "충전이 완료되었어요"
    bits: list[str] = []
    if soc:
        bits.append(f"{soc.group(1)}%에서 {soc.group(2)}%까지 충전했어요")
    elif kwh:
        bits.append(f"{kwh.group(1)}kWh 충전했어요")
    if soc and kwh:
        bits.append(f"{kwh.group(1)}kWh를 넣었어요")
    if pace and bits:
        bits.append(f"{pace}이에요")
    if bits:
        return _longer(title, ". ".join(bits))
    return _longer(title, "충전량을 앱에서 확인해 주세요. 끝난 시각의 기록을 볼 수 있어요")


def _park_summary(body: str) -> str:
    text = body or ""
    left = re.search(r"(\d+시간(?: \d+분)?|\d+분) 남았어요", text)
    stamp = re.search(r"(\d{1,2}/\d{1,2}\s+\d{2}:\d{2})", text)
    title = "충전 자리"
    if left and stamp:
        return _longer(title, f"{left.group(1)} 남았어요. {stamp.group(1)}까지 옮겨 주세요")
    if stamp and "지났어요" in text:
        return _longer(title, f"{stamp.group(1)}에 제한 시간이 지났어요. 얼른 옮겨 주세요")
    return _longer(title, "제한 시간이 가까워요. 자리를 옮겨 주세요")


def _summary(sentence: str, body: str, kind: str = "", details: list | None = None, notice_id: str = "") -> str:
    """One or two sentences for the list and the push. Longer than the title."""
    text = body or ""
    if sentence == "주행을 시작했어요" or notice_id.startswith("trip_start"):
        rng = _RANGE.search(text)
        bat = _BAT.search(text)
        title = "주행을 시작했어요"
        if rng and bat:
            return _longer(title, f"{rng.group(1)}km 갈 수 있어요. 배터리 {bat.group(1)}%에서 출발해요")
        if rng:
            return _longer(title, f"{rng.group(1)}km 갈 수 있어요. 출발 배터리는 앱에서 확인해 주세요")
        if bat:
            return _longer(title, f"배터리 {bat.group(1)}%에서 출발해요. 주행가능 거리는 아직 없어요")
        return _longer(title, "출발했어요. 배터리와 주행가능 거리를 앱에서 확인해 주세요")
    if sentence == "주행을 마쳤어요" or notice_id.startswith("trip_end"):
        return trip_end_summary(text, details)
    if sentence == "충전이 완료되었어요" or notice_id.startswith("charge_end"):
        return charge_end_summary(text, details)
    if sentence == "충전을 시작했어요" or notice_id.startswith("charge_start"):
        return charge_start_summary(text, details)
    if notice_id.startswith("park14h") or notice_id.startswith("parkdc") or kind == "parking":
        return _park_summary(text)
    if kind == "sentry" or sentence.startswith("감시") or "감시 경보" in text:
        spot = _local_spot(text)
        title = "감시 경보가 울렸어요"
        if spot:
            return _longer(title, f"{spot} 근처에서 감지됐어요. 주변을 확인해 주세요")
        return _longer(title, "주변에서 움직임이 감지됐어요. 한번 확인해 주세요")
    if sentence == "공기가 빠지고 있어요" or "서서히" in text:
        wheel = re.search(r"(앞 왼쪽|앞 오른쪽|뒤 왼쪽|뒤 오른쪽)", text)
        drop = re.search(r"약\s*(\d+(?:\.\d+)?)\s*psi", text, re.I)
        title = "공기가 빠지고 있어요"
        if wheel and drop:
            return _longer(title, f"{wheel.group(1)} 공기가 약 {drop.group(1)} psi 줄었어요. 공기를 넣어 주세요")
        if wheel:
            return _longer(title, f"{wheel.group(1)} 공기를 확인해 주세요. 다른 바퀴보다 낮은지 봐 주세요")
        return _longer(title, "바퀴 공기가 줄고 있어요. 네 바퀴를 확인해 주세요")
    if kind == "tire" or "공기압" in text or sentence == "공기압이 낮아요":
        first = text.split("\n", 1)[0].strip()
        title = "공기압이 낮아요"
        if "보다 낮아요" in first:
            line = first.split("·")[-1].strip()
            if "넣어" not in line:
                line += ". 공기를 넣어 주세요"
            return _longer(title, line)
        wheel = re.search(r"(앞 왼쪽|앞 오른쪽|뒤 왼쪽|뒤 오른쪽)", text)
        if wheel:
            return _longer(title, f"{wheel.group(1)}을 확인해 주세요. 공기를 넣어 주세요")
        return _longer(title, "타이어를 확인해 주세요. 공기를 넣어 주세요")
    if kind == "maintenance":
        return _maint_summary(text, notice_id, details, sentence)
    if kind == "software" or "소프트웨어" in text or notice_id.startswith("software"):
        return _longer("소프트웨어 업데이트", "설치할 버전이 있어요. 앱에서 설치할 수 있어요")
    if sentence in ("고전압 계통을 확인해 주세요", "고전압 계통") or notice_id.startswith("hv_fault"):
        return _longer("고전압 계통", "서비스 점검이 필요해요. 운행 전에 상태를 봐 주세요")
    if kind == "vehicle":
        return _longer(sentence or "차량", "자세한 내용은 앱에서 확인해 주세요. 화면에서 이어서 볼 수 있어요")
    return _longer(sentence or "알림", "자세한 내용은 앱에서 확인해 주세요. 화면에서 이어서 볼 수 있어요")


def _rows(body: str, details: list, event_ts: str, sentence: str) -> list[list[str]]:
    rows: list[list[str]] = []
    if event_ts:
        label = "시작 시각" if sentence == "주행을 시작했어요" else "시각"
        rows.append([label, _full(event_ts)])
    seen = {rows[0][0]} if rows else set()
    for pair in details or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        label, value = str(pair[0]).strip(), str(pair[1]).strip()
        if not label or not value or label in seen or "권장" in label:
            continue
        rows.append([label, value])
        seen.add(label)
    text = body or ""
    bat = _BAT.search(text)
    if bat and "배터리" not in seen:
        rows.append(["배터리", f"{bat.group(1)}%"])
        seen.add("배터리")
    rng = _RANGE.search(text)
    if rng and "주행가능" not in seen:
        rows.append(["주행가능", f"{rng.group(1)} km"])
        seen.add("주행가능")
    tires = _TIRES.search(text)
    if tires:
        for label, value in zip(WHEEL_LABELS, tires.groups()):
            if label not in seen:
                rows.append([label, f"{value} psi"])
                seen.add(label)
    return _drop_same_time(rows, event_ts)


def _alias_label(label: str) -> str:
    return _LABEL_ALIAS.get(label, label)


def _align_detail_rows(rows: list[list[str]], notice_id: str, body: str) -> list[list[str]]:
    """Put alert rows in the trip or charge detail order. Stored rows are not changed."""
    trip = notice_id.startswith("trip_")
    charge = notice_id.startswith("charge_") and not notice_id.startswith("charge_full")
    if not trip and not charge:
        return rows
    aligned: list[list[str]] = []
    seen: set[str] = set()
    for label, value in rows:
        name = _alias_label(label)
        if not name or name in seen:
            continue
        aligned.append([name, value])
        seen.add(name)
    if trip and notice_id.startswith("trip_end") and "거리" not in seen:
        found = _KM.search((body or "").split("\n", 1)[0])
        if found:
            aligned.append(["거리", f"{found.group(1)} km"])
            seen.add("거리")
    order = _TRIP_ROW_ORDER if trip else _CHARGE_ROW_ORDER
    rank = {name: index for index, name in enumerate(order)}
    return sorted(aligned, key=lambda pair: (0, rank[pair[0]]) if pair[0] in rank else (1, 0))


def _drop_same_time(rows: list[list[str]], event_ts: str) -> list[list[str]]:
    """The popup header already shows the event time, so a matching time row is omitted."""
    header = _full(event_ts)
    clock = _clock(event_ts)
    kept = []
    for label, value in rows:
        text = value.strip()
        same = text == header or (bool(clock) and text == clock)
        if label in _TIME_LABELS and same:
            continue
        kept.append([label, value])
    return kept


def _strip_recommended(text: str) -> str:
    cleaned = _REC_PHRASE.sub("", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,·])", r"\1", cleaned)
    return cleaned.strip(" ,·")


def _signed_days(text: str) -> int | None:
    found = re.search(r"(-?\d+)\s*일", text or "")
    return int(found.group(1)) if found else None


def _due_phrase(text: str) -> str:
    found = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if not found:
        return ""
    return f"{int(found.group(2))}월 {int(found.group(3))}일"


def maint_state(notice_id: str, body: str, details: list | None = None) -> tuple[int | None, str | None, str]:
    """Signed remaining days, overdue/soon, and a due phrase such as 9월 19일."""
    days = _signed_days(body)
    due = _due_phrase(body)
    for pair in details or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        label, value = str(pair[0]), str(pair[1])
        number = _signed_days(value)
        if label == "지난 날" and number is not None:
            days = -abs(number)
        elif label == "남은 날" and number is not None:
            days = abs(number) if not str(value).strip().startswith("-") else -abs(number)
        if label in ("기한", "예정일"):
            due = _due_phrase(value) or value
    if ":overdue:" in (notice_id or ""):
        status = "overdue"
        if days is not None:
            days = -abs(days)
    elif ":soon:" in (notice_id or ""):
        status = "soon"
        if days is not None:
            days = abs(days)
    elif days is None:
        status = None
    else:
        status = "overdue" if days < 0 else "soon"
    return days, status, due


_TITLE_TAILS = (" 기한이 지났어요", " 점검 시기예요")
_BATTERY_ACTION = "100%까지 한 번 충전해 주세요"
_WASHER_ACTION = "워셔액을 점검해 주세요"


def _service_name(notice_id: str, title: str) -> tuple[str, str]:
    parts = (notice_id or "").split(":")
    item_id = parts[1] if len(parts) >= 2 and parts[0] == "maint" else ""
    text = (title or "").strip()
    for tail in _TITLE_TAILS:
        if text.endswith(tail):
            text = text[: -len(tail)].strip()
            break
    if text in ("", "정비", "정비 시기를 확인해 주세요"):
        text = ""
    return item_id, text


def _eul_reul(name: str) -> str:
    """을 after a batchim, 를 after a vowel. The last Hangul syllable decides."""
    for ch in reversed(name or ""):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            return "을" if (code - 0xAC00) % 28 else "를"
    return "를"


def maint_action(notice_id: str, title: str) -> str:
    """Short thing to do for one service item. Display only."""
    item_id, name = _service_name(notice_id, title)
    if item_id == "cons-battery-full" or name.startswith("배터리 완충"):
        return _BATTERY_ACTION
    if item_id == "cons-washer" or name == "워셔액" or name.startswith("워셔액"):
        return _WASHER_ACTION
    label = name or "정비"
    return f"{label}{_eul_reul(label)} 점검해 주세요"


def _maint_summary(body: str, notice_id: str, details: list | None = None, title: str = "") -> str:
    days, status, _due = maint_state(notice_id, body, details)
    action = maint_action(notice_id, title)
    if days is None and status is None:
        return _longer(title, "정비 시기를 앱에서 확인해 주세요. 기한이 지났는지 봐 주세요")
    count = abs(days or 0)
    if status == "overdue" or (days is not None and days < 0):
        return _longer(title, f"{count}일 지났어요, {action}")
    return _longer(title, f"{count}일 남았어요, 미리 {action}")


def _maint_rows(rows: list[list[str]], notice_id: str, body: str, details: list | None) -> list[list[str]]:
    days, status, due = maint_state(notice_id, body, details)
    kept = []
    for label, value in rows:
        if label in ("남은 날", "지난 날", "예정일", "기한"):
            continue
        if label in _TIME_LABELS:
            kept.append([label, value])
            continue
        kept.append([label, value])
    if days is not None:
        kept.append(["지난 날" if status == "overdue" or days < 0 else "남은 날", f"{abs(days)}일"])
    if due:
        kept.append(["기한", due])
    return kept


def _full_summary(body: str, details: list | None) -> str:
    for pair in details or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and str(pair[0]) == "마지막 100% 충전":
            if str(pair[1]).strip() and str(pair[1]).strip() != "기록 없음":
                return "마지막 완충 후 한 달이 지났어요. 100%까지 한 번 채워 주세요"
            return "한 달 동안 완충 기록이 없어요. 100%까지 한 번 채워 주세요"
    if "기록이 없어요" in (body or "") or "기록 없음" in (body or ""):
        return "한 달 동안 완충 기록이 없어요. 100%까지 한 번 채워 주세요"
    return "마지막 완충 후 한 달이 지났어요. 100%까지 한 번 채워 주세요"


def _full_rows(details: list | None, body: str) -> list[list[str]]:
    wanted = []
    for pair in details or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        label, value = str(pair[0]).strip(), str(pair[1]).strip()
        if label in ("마지막 100% 충전", "최근 30일 최고") and value:
            wanted.append([label, value])
    if any(label == "마지막 100% 충전" for label, _value in wanted):
        return wanted
    return [["마지막 100% 충전", "기록 없음"]]


def format_soc(value: float) -> str:
    """Home battery text: one decimal, trailing zeros removed, then %."""
    text = f"{float(value):.1f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + "%"


def _soc_hundredths(value: float) -> int:
    """Two-decimal percent as hundredths. Matches Math.round(value * 100)."""
    return int(math.floor(float(value) * 100 + 0.5))


def _soc_hundredths_text(hundredths: int) -> str:
    sign = "-" if hundredths < 0 else ""
    whole, frac = divmod(abs(int(hundredths)), 100)
    return f"{sign}{whole}.{frac:02d}"


def format_soc_change(start: float | None, end: float | None) -> str | None:
    """Detail battery line. Two decimals, no delta.

    Equal displayed values are a single number. A missing side is left out.
    The comparison uses the hundredths that are actually shown.
    """
    if start is None and end is None:
        return None
    if start is None:
        return _soc_hundredths_text(_soc_hundredths(end)) + "%"  # type: ignore[arg-type]
    if end is None:
        return _soc_hundredths_text(_soc_hundredths(start)) + "%"
    a = _soc_hundredths(start)
    b = _soc_hundredths(end)
    if a == b:
        return _soc_hundredths_text(a) + "%"
    return f"{_soc_hundredths_text(a)}% → {_soc_hundredths_text(b)}%"


def _list_battery(text: str) -> str:
    """List and push keep one battery number. A detail arrow uses the arrival side."""
    raw = (text or "").strip()
    if not raw:
        return ""
    found = re.search(r"→\s*(\d+(?:\.\d+)?)\s*%", raw)
    if found:
        return found.group(1) + "%"
    if "%" in raw:
        return raw
    return raw + "%"


def format_range_km(value: float) -> str:
    """Home range text. The number is rounded the way the home tile rounds."""
    rounded = int(float(value) + 0.5)
    return f"약 {rounded} km"


def _detail_map(details: list | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in details or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            label, value = str(pair[0]).strip(), str(pair[1]).strip()
            if label and value:
                out[label] = value
    return out


def _number(text: str) -> float | None:
    found = re.search(r"(\d+(?:\.\d+)?)", text or "")
    if not found:
        return None
    number = found.group(1)
    return float(number) if "." in number else int(number)


def event_facts(body: str, details: list | None = None, notice_id: str = "") -> dict:
    """Numbers and a neighborhood the bot can turn into a sentence. No coordinates or VIN."""
    blob = body or ""
    for pair in details or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            blob += "\n" + str(pair[0]) + " " + str(pair[1])
    blob = _clean_place(_VIN.sub(" ", _COORD.sub(" ", blob)))
    facts: dict = {}
    rng = _RANGE.search(blob)
    if rng:
        facts["range_km"] = int(rng.group(1))
    for match in _KM.finditer(blob):
        if rng and rng.start() <= match.start() and match.end() <= rng.end():
            continue
        number = match.group(1)
        facts["km"] = float(number) if "." in number else int(number)
        break
    bat = _BAT.search(blob)
    if bat:
        facts["battery_pct"] = int(bat.group(1))
    soc = _SOC.search(blob)
    if soc:
        facts["soc_start"] = int(soc.group(1))
        facts["soc_end"] = int(soc.group(2))
    regen = re.search(r"회생\s*(\d+(?:\.\d+)?)\s*kWh", blob, re.I)
    if regen:
        facts["regen_kwh"] = float(regen.group(1))
    else:
        kwh = _KWH.search(blob)
        if kwh:
            facts["kwh"] = float(kwh.group(1))
    minutes = _MIN.search(blob)
    if minutes:
        facts["minutes"] = int(minutes.group(1))
    if notice_id.startswith("open_after_park:"):
        parts = open_parts(notice_id, body, details)
        if parts:
            facts["open"] = parts[0] if len(parts) == 1 else parts
    elif notice_id.startswith("vehicle_alert:"):
        code = alert_code(notice_id)
        if code:
            facts["alert"] = code
            _title, summary = vehicle_copy(code, "|end" in notice_id)
            facts["text"] = summary
    elif notice_id.startswith("maint:") or any(
        isinstance(pair, (list, tuple)) and len(pair) >= 2 and str(pair[0]) in ("지난 날", "남은 날")
        for pair in (details or [])
    ):
        days, status, due = maint_state(notice_id, body, details)
        if days is not None:
            facts["remaining_days"] = days
        if status:
            facts["status"] = status
        if due:
            facts["due"] = due
    elif notice_id.startswith("charge_full"):
        for pair in details or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            label, value = str(pair[0]), str(pair[1]).strip()
            if label == "마지막 100% 충전":
                facts["last_full"] = None if value == "기록 없음" else value
            elif label == "최근 30일 최고":
                number = re.search(r"(\d+)", value)
                if number:
                    facts["peak_pct"] = int(number.group(1))
        facts["summary"] = _full_summary(body, details)
    else:
        days = re.search(r"(\d+)\s*일", blob)
        if days:
            facts["days"] = int(days.group(1))
    tires = _TIRES.search(blob)
    if tires:
        facts["tires_psi"] = [float(value) for value in tires.groups()]
    labeled = _detail_map(details)
    battery_text = labeled.get("배터리", "")
    arrival = re.search(r"→\s*(\d+(?:\.\d+)?)\s*%", battery_text)
    if arrival:
        number = arrival.group(1)
        facts["battery_pct"] = float(number) if "." in number else int(number)
    else:
        battery = _number(battery_text)
        if battery is not None:
            facts["battery_pct"] = battery
    distance = _number(labeled.get("주행가능") or labeled.get("주행가능거리") or "")
    if distance is not None:
        facts["range_km"] = int(distance)
    wheels = {}
    for label, key in _WHEEL_KEY.items():
        pressure = _number(labeled.get(label, ""))
        if pressure is not None and "psi" in labeled.get(label, "").lower():
            wheels[key] = pressure
    if wheels:
        facts["tires_psi"] = wheels
    found = re.search(r"(\S+(?:동|읍|면))(?:\s+(\S+(?:로|길|대로)))?", blob)
    if found:
        road = found.group(2) or ""
        facts["place"] = (found.group(1) + (" " + road if road else "")).strip()
    return facts


def present(row: dict) -> dict:
    """Display fields for one stored notice. Stored columns are not changed."""
    notice_id = str(row.get("id") or "")
    body = str(row.get("body") or "")
    stored = str(row.get("ts") or "")
    event_ts = _event_ts(notice_id, body, stored)
    if row.get("composed") and str(row.get("summary") or "").strip():
        title = str(row.get("title") or "").strip() or "알림"
        summary = _strip_recommended(_clean_place(str(row.get("summary")))) or "알림이 있어요"
        return {
            "title": title,
            "summary": summary,
            "push_body": summary,
            "event_ts": event_ts,
            "rows": _align_detail_rows(_rows(body, row.get("details") or [], event_ts, title), notice_id, body),
        }
    kind = str(row.get("kind") or "")
    details = row.get("details") or []
    car = _car_notice(notice_id, body, details)
    if car:
        return {
            "title": car["title"],
            "summary": car["summary"],
            "push_body": car["summary"],
            "event_ts": event_ts,
            "rows": car["rows"],
        }
    if kind == "maintenance" or notice_id.startswith("maint:"):
        sentence = str(row.get("title") or "").strip() or "정비 시기"
        summary = _maint_summary(body, notice_id, details, sentence)
        rows = _maint_rows(_rows(body, details, event_ts, sentence), notice_id, body, details)
        return {"title": sentence, "summary": summary, "push_body": summary, "event_ts": event_ts, "rows": rows}
    if kind == "charge_full" or notice_id.startswith("charge_full"):
        sentence = "완충 시기"
        summary = _longer(sentence, _full_summary(body, details))
        return {
            "title": sentence,
            "summary": summary,
            "push_body": summary,
            "event_ts": event_ts,
            "rows": _full_rows(details, body),
        }
    sentence = _sentence(str(row.get("title") or ""), body, notice_id)
    if notice_id.startswith("trip_end") or sentence == "주행을 마쳤어요":
        labeled = _labeled_body(body, row.get("details") or [])
        km_value, _shown = _distance_km(labeled.get("거리") or "")
        if spot_move(km_value, _minutes(labeled.get("시간") or "")):
            sentence = "자리를 옮겼어요"
    if notice_id.startswith("hv_fault") or sentence == "고전압 계통을 확인해 주세요":
        sentence = "고전압 계통"
    elif notice_id.startswith("software") or kind == "software":
        sentence = "소프트웨어 업데이트"
    elif notice_id.startswith("park14h") or notice_id.startswith("parkdc") or kind == "parking":
        sentence = "충전 자리"
    summary = _strip_recommended(_summary(sentence, body, kind, details, notice_id))
    summary = _longer(sentence, summary)
    push_body = summary
    return {
        "title": sentence,
        "summary": summary,
        "push_body": push_body,
        "event_ts": event_ts,
        "rows": _align_detail_rows(_rows(body, details, event_ts, sentence), notice_id, body),
    }
