# modules/manage/econ_events.py
"""주요 경제 이벤트(FOMC·CPI·고용보고서 등) 일정 조회.

소스는 셋으로 나뉘며, 하나가 죽어도 나머지는 그대로 나온다(부분 실패 허용).
  - FRED API   : 미국 지표 발표일(CPI/고용보고서/PPI/PCE/GDP/소매판매/JOLTS).
                 발표 '예정일'을 미래분까지 제공하므로 추정이 아니라 확정값이다.
  - Fed 캘린더 : FOMC 금리결정·의사록·베이지북 (federalreserve.gov 공식 JSON, 키 불필요).
  - 계산/시드  : 국내·미국 선물옵션 동시만기는 규칙으로 계산하고,
                 기계 판독이 불가능한 일정(한은 금통위)만 시드 파일에서 읽는다.

수집은 호출할 때마다 실제로 한다(외부 호출 8회, 실측 1초 내외). 디스크 캐시는
읽기 가속용이 아니라 폴백 전용이다 — 수집이 통째로 실패했을 때만 마지막 성공분을 쓴다.
"""
import concurrent.futures
import logging
import os
import re
from datetime import datetime, date, timedelta

import requests
from rich.table import Table
from rich import box

import api
import config
import jsonio

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(config.JSON_DIR, "econ_calendar_cache.json")
SEED_FILE = os.path.join(config.JSON_DIR, "econ_calendar_seed.json")

FRED_BASE_URL = "https://api.stlouisfed.org/fred"
FED_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"

# FRED release_id → (표시명, 중요도). 중요도 1=최상위(장 전체를 흔드는 지표)
FRED_RELEASES = {
    10: ("미국 CPI", 1),
    50: ("미국 고용보고서", 1),
    54: ("미국 PCE 물가", 1),
    46: ("미국 PPI", 2),
    53: ("미국 GDP", 2),
    9: ("미국 소매판매", 2),
    192: ("미국 JOLTS", 3),
}

# Fed 공식 캘린더의 title → (표시명, 중요도)
FED_TITLES = {
    "FOMC Meeting": ("FOMC 금리결정", 1),
    "FOMC Minutes": ("FOMC 의사록", 2),
    "Beige Book": ("베이지북", 3),
}

HTTP_TIMEOUT = 20   # FRED가 간헐적으로 10초를 넘겨 응답한다
HTTP_RETRIES = 2


def _parse_date(text):
    """'2026-08-12' 형태의 문자열을 date로. 실패 시 None."""
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _fetch_fred_release(release_id, start, end):
    """FRED 단일 릴리스의 발표 예정일 목록.

    include_release_dates_with_no_data=true 여야 아직 데이터가 없는 '미래 예정일'까지 돌려준다.
    (이 옵션이 없으면 과거 발표분만 나와서 캘린더로는 쓸 수 없다.)
    """
    label, weight = FRED_RELEASES[release_id]
    params = {
        "release_id": release_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "realtime_start": start.strftime("%Y-%m-%d"),
        "realtime_end": end.strftime("%Y-%m-%d"),
        "sort_order": "asc",
        "limit": 100,
    }
    last_err = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            res = requests.get(f"{FRED_BASE_URL}/release/dates", params=params, timeout=HTTP_TIMEOUT)
            res.raise_for_status()
            break
        except Exception as e:  # 읽기 타임아웃이 산발적으로 나므로 한 번 더 두드린다
            last_err = e
            if attempt == HTTP_RETRIES:
                raise
            logger.debug(f"[econ] FRED {label} 재시도 {attempt + 1}/{HTTP_RETRIES}: {e}")

    out = []
    for row in res.json().get("release_dates", []):
        d = _parse_date(row.get("date"))
        if d and start <= d <= end:
            out.append({"date": d.strftime("%Y-%m-%d"), "name": label,
                        "country": "US", "weight": weight, "source": "FRED"})
    return out


def _fetch_fred(start, end):
    """FRED 관심 릴리스 전체 수집 → (이벤트, 전부 성공했는지).

    릴리스 하나가 실패해도 나머지는 살리되, '불완전'하다는 사실을 함께 돌려준다.
    이걸 알려주지 않으면 일시적 타임아웃으로 빠진 지표가 하루치 캐시로 굳어
    종일 누락된 채 보인다.
    """
    if not config.FRED_API_KEY:
        return [], True  # 키 미설정은 실패가 아니라 '해당 없음'

    out, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch_fred_release, rid, start, end) for rid in FRED_RELEASES]
        for fut in concurrent.futures.as_completed(futures):
            try:
                out.extend(fut.result())
            except Exception as e:
                failed += 1
                logger.warning(f"[econ] FRED 릴리스 조회 실패: {e}")
    return out, failed == 0


def _fetch_fed(start, end):
    """연준 공식 캘린더에서 FOMC 금리결정·의사록·베이지북 추출 → (이벤트, 성공 여부).

    JSON은 날짜를 month('2026-07') + days('29')로 쪼개 담고 있고,
    과거 항목 중에는 month가 빈 문자열인 레코드가 섞여 있어 그런 건 버린다.
    """
    try:
        res = requests.get(FED_CALENDAR_URL, timeout=HTTP_TIMEOUT)
        res.raise_for_status()
        # 응답 선두에 BOM이 붙어 있어 res.json()이 실패한다 — 인코딩을 명시해서 파싱한다
        res.encoding = "utf-8-sig"
        events = res.json().get("events", [])
    except Exception as e:
        logger.warning(f"[econ] 연준 캘린더 조회 실패: {e}")
        return [], False

    out = []
    for ev in events:
        title = str(ev.get("title", "")).strip()  # 원본에 앞뒤 공백이 섞여 있다
        if title not in FED_TITLES:
            continue
        month = str(ev.get("month", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            continue
        # 2일 회의는 결정일(둘째 날)만 들어오지만, 혹시 '27-28' 형태면 마지막 날을 쓴다
        days = re.findall(r"\d+", str(ev.get("days", "")))
        if not days:
            continue
        d = _parse_date(f"{month}-{int(days[-1]):02d}")
        if not d or not (start <= d <= end):
            continue

        label, weight = FED_TITLES[title]
        out.append({"date": d.strftime("%Y-%m-%d"), "name": label, "country": "US",
                    "weight": weight, "source": "Fed",
                    "note": str(ev.get("time", "")).strip()})
    return out, True


def _nth_weekday(year, month, weekday, nth):
    """해당 월의 n번째 요일(weekday: 월=0 … 일=6)."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)  # 그 달 첫 번째 해당 요일
    return d + timedelta(days=7 * (nth - 1))


def _prev_business_day(d, country):
    """주말·휴장일이면 직전 영업일로 앞당긴다(만기는 뒤로 밀리지 않고 당겨진다)."""
    for _ in range(10):
        if d.weekday() < 5 and not api.get_holiday_name(d.strftime("%Y%m%d"), country=country):
            return d
        d -= timedelta(days=1)
    return d


# (국가, 요일, 몇째 주, 표시명) — 3·6·9·12월에 적용되는 동시만기 규칙
_EXPIRY_SPECS = [
    ("KR", 3, 2, "국내 선물옵션 동시만기"),  # 둘째 목요일 (KOSPI200 선물·옵션)
    ("US", 4, 3, "미국 선물옵션 동시만기"),  # 셋째 금요일 (quadruple witching)
]


def _option_expiry(start, end):
    """국내·미국 선물옵션 동시만기(네 마녀의 날) — 외부 소스 없이 계산한다.

    해당일이 휴장일이면 만기는 직전 영업일로 앞당겨진다. 미국 6월 셋째 금요일이
    준틴스데이(6/19)와 겹치는 해가 실제로 있어 이 보정이 필요하다(2026·2027년).

    주의: 미국 판정은 holidays.US(연방휴일)를 쓰는데 NYSE는 콜럼버스데이·재향군인의 날에
    휴장하지 않는다. 둘 다 10·11월이라 분기 만기월(3·6·9·12)과 겹치지 않아 현재는
    문제가 없지만, 월물 만기로 확장한다면 거래소 휴장일 기준으로 바꿔야 한다.
    """
    out = []
    for year in range(start.year, end.year + 1):
        for month in (3, 6, 9, 12):
            for country, weekday, nth, label in _EXPIRY_SPECS:
                d = _prev_business_day(_nth_weekday(year, month, weekday, nth), country)
                if start <= d <= end:
                    out.append({"date": d.strftime("%Y-%m-%d"), "name": label,
                                "country": country, "weight": 2, "source": "계산"})
    return out


def _load_seed(start, end):
    """시드 파일의 수기 일정(한은 금통위 등). 파일이 없거나 비어 있어도 정상 동작한다."""
    data = jsonio.load_json(SEED_FILE, default={}) or {}
    out = []
    for ev in data.get("events", []):
        d = _parse_date(ev.get("date"))
        if not d or not (start <= d <= end):
            continue
        out.append({"date": d.strftime("%Y-%m-%d"), "name": ev.get("name", "?"),
                    "country": ev.get("country", "KR"),
                    "weight": int(ev.get("weight", 2)), "source": "시드"})
    return out


def _collect(start, end):
    """모든 소스를 합쳐 날짜순 정렬 → (이벤트, 전 소스 성공 여부).

    같은 날 같은 이름은 중복 제거한다.
    """
    events, complete = [], True
    for fn in (_fetch_fred, _fetch_fed):           # 네트워크 소스 (성공 여부를 함께 돌려준다)
        try:
            got, ok = fn(start, end)
            events.extend(got)
            complete = complete and ok
        except Exception as e:
            logger.warning(f"[econ] {fn.__name__} 실패: {e}")
            complete = False
    for fn in (_option_expiry, _load_seed):        # 로컬 소스 (계산·파일이라 실패할 일이 없다)
        try:
            events.extend(fn(start, end))
        except Exception as e:
            logger.warning(f"[econ] {fn.__name__} 실패: {e}")
            complete = False

    seen, uniq = set(), []
    for ev in sorted(events, key=lambda e: (e["date"], e["weight"], e["name"])):
        key = (ev["date"], ev["name"])
        if key not in seen:
            seen.add(key)
            uniq.append(ev)
    return uniq, complete


def get_events(days=60):
    """향후 `days`일간의 경제 이벤트와 수집 상태.

    호출할 때마다 실제로 수집한다. 발표 일정은 몇 주에 한 번 바뀌는 데이터라
    하루 캐시로 읽어도 손해는 크지 않지만, 수집 비용이 1초 내외라 굳이 신선도
    판정을 껴 두느니 매번 받는 편이 단순하고 정확하다.

    캐시는 폴백 전용이다 — 수집이 통째로 실패하면(네트워크 단절 등) 날짜가 지난
    캐시라도 그대로 쓴다. 빈 화면보다 하루 이틀 묵은 일정이 낫기 때문이다.
    대신 묵었다는 사실을 status로 함께 돌려줘 화면에서 밝힌다.

    Returns:
        (events, status) — status = {"stale_since": 'YYYY-MM-DD'|None, "complete": bool}
        stale_since가 있으면 그 날짜에 받아둔 저장분을 보여주고 있다는 뜻이다.
    """
    today = datetime.now().date()
    end = today + timedelta(days=days)

    events, complete = _collect(today, end)
    stale_since = None
    if events:
        jsonio.save_json(CACHE_FILE, {"fetched": today.strftime("%Y-%m-%d"),
                                      "covers_until": end.strftime("%Y-%m-%d"),
                                      "complete": complete,
                                      "events": events})
    else:
        cache = jsonio.load_json(CACHE_FILE, default={}) or {}
        events = cache.get("events", [])
        if events:
            stale_since = cache.get("fetched")
            complete = False

    return ([e for e in events
             if (_parse_date(e.get("date")) or today) >= today
             and (_parse_date(e.get("date")) or today) <= end],
            {"stale_since": stale_since, "complete": complete})


_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def build_lines(days=45):
    """경제 이벤트를 텍스트 줄 목록으로 (텔레그램 /calendar 용).

    화면(render)과 같은 자료·같은 경고를 쓰되 rich 테이블 대신 한 줄씩 만든다.
    """
    lines = ["▸ 주요 경제 이벤트"]

    if not config.FRED_API_KEY:
        lines.append("  ※ FRED API 키가 없어 미국 지표(CPI·고용보고서 등)는 제외됩니다.")

    events, status = get_events(days=days)

    if status.get("stale_since"):
        lines.append(f"  ※ 수집 실패로 {status['stale_since']} 기준 저장분을 표시합니다.")
    elif not status.get("complete", True):
        lines.append("  ※ 일부 소스 조회에 실패해 일정이 누락됐을 수 있습니다.")

    if not events:
        lines.append("  표시할 경제 이벤트가 없습니다.")
        return lines

    today = datetime.now().date()
    for ev in events:
        d = _parse_date(ev["date"])
        if not d:
            continue
        gap = (d - today).days
        dday = "D-DAY" if gap == 0 else f"D-{gap}"
        lines.append(f"• {d.strftime('%m-%d')}({_WEEKDAY_KR[d.weekday()]}) {dday} "
                     f"{ev['name']} [{ev.get('source', '')}]")

    lines.append("  ※ 미국 지표는 현지시각 기준 발표일입니다 (한국시각 대체로 익일 새벽).")
    return lines


def render(days=45):
    """경제 이벤트 일정 테이블 출력."""
    config.console.print("[bold]▸ 주요 경제 이벤트[/bold]")

    if not config.FRED_API_KEY:
        config.console.print("  [dim yellow]※ FRED API 키가 없어 미국 지표(CPI·고용보고서 등)는 제외됩니다. "
                             "(환경변수 FRED_API_KEY — https://fredaccount.stlouisfed.org/apikeys 무료 발급)[/dim yellow]")

    events, status = get_events(days=days)

    # 수집이 실패했는데 조용히 옛 일정을 보여주면 그게 언제 기준인지 알 길이 없다 —
    #  FOMC가 이미 지나갔는지 여부까지 걸린 문제라 반드시 밝힌다.
    if status.get("stale_since"):
        config.console.print(f"  [yellow]※ 일정 수집에 실패해 {status['stale_since']} 기준 저장분을 표시합니다 "
                             f"(그 이후 변경·추가된 일정은 반영되지 않습니다).[/yellow]")
    elif not status.get("complete", True):
        config.console.print("  [dim yellow]※ 일부 소스 조회에 실패해 일정이 누락됐을 수 있습니다.[/dim yellow]")

    if not events:
        config.console.print("  [dim]표시할 경제 이벤트가 없습니다.[/dim]\n")
        return

    today = datetime.now().date()
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("D-day", justify="center")
    table.add_column("이벤트", justify="left")
    table.add_column("구분", justify="center")

    for ev in events:
        d = _parse_date(ev["date"])
        if not d:
            continue
        gap = (d - today).days
        dday = "D-DAY" if gap == 0 else f"D-{gap}"
        # 중요도 1(FOMC·CPI·고용보고서)은 눈에 띄어야 한다 — 포지션 조절 판단의 기준점
        name_color = {1: "bold red", 2: "yellow"}.get(ev.get("weight", 3), "white")

        table.add_row(
            d.strftime("%Y-%m-%d") + f" ({_WEEKDAY_KR[d.weekday()]})",
            f"[bold]{dday}[/bold]" if gap <= 3 else dday,
            f"[{name_color}]{ev['name']}[/]",
            f"[dim]{ev.get('source', '')}[/dim]",
        )

    config.console.print(table)
    config.console.print("  [dim]※ 미국 지표는 현지시각 기준 발표일입니다 (한국시각으로는 대체로 익일 새벽).[/dim]")
    config.console.print()
