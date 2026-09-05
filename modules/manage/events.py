# modules/manage/events.py
"""관심종목 배당/실적 캘린더 조회.

- 국내(stocks_kr): OpenDART 'alotMatter'로 최근 확정 배당 정보 조회.
- 해외(stocks_us): yfinance로 예정 배당락일/실적발표일 조회.
- ETF는 배당·실적 공시 대상이 아니라 조회에서 제외.
"""
import calendar as _cal
import concurrent.futures
import logging
from datetime import datetime, date, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
from core import utils
from modules.manage.scan import ScanFailures

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    config.silence_yfinance_numpy_warning()  # import 뒤에 걸어야 억제 유효
except Exception:  # pragma: no cover
    yf = None


_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _kr_year_end_holiday(year):
    """KRX 연말 휴장일. **정본은 api.kr_year_end_closing_day 다.**

    [SSOT 2026-09-05] 종전에는 여기와 api/market_calendar 가 규칙을 따로 들고 있었고
     서로 달랐다(그쪽은 무조건 12/31). 2026~2040 중 4년이 갈라진다 — 정본 주석 참조.
     이름은 호출부 호환을 위해 남기고 구현만 위임한다.
    """
    return api.kr_year_end_closing_day(year)


def _is_kr_trading_day(d):
    """한국 증시 거래일 여부 (주말/공휴일/KRX 연말 휴장 제외)."""
    if d.weekday() >= 5:
        return False
    if d == _kr_year_end_holiday(d.year):  # KRX 연말 폐장(공휴일 라이브러리엔 없음)
        return False
    try:
        if api.get_holiday_name(d.strftime("%Y%m%d"), country="KR"):
            return False
    except Exception:
        pass
    return True


def _prev_trading_day(d):
    cur = d - timedelta(days=1)
    for _ in range(20):
        if _is_kr_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _month_end_ex_date(year, month):
    """해당 월말(배당기준일)에 대응하는 '예상' 배당락일 (= 폐장일의 직전 거래일)."""
    close = date(year, month, _cal.monthrange(year, month)[1])
    while not _is_kr_trading_day(close):   # 폐장일(그 달 마지막 거래일)로 롤백
        close -= timedelta(days=1)
    return _prev_trading_day(close)


def _kr_yf_dividends(code):
    """yfinance 배당 지급 이력(배당락일 index) 반환. 없으면 None. .KS→.KQ 순."""
    if yf is None:
        return None
    try:
        for sfx in (".KS", ".KQ"):
            try:
                div = yf.Ticker(code + sfx).dividends
            except Exception:
                div = None
            if div is not None and len(div) > 0:
                return div
    except Exception:
        pass
    return None


def _yf_ex_dates(div_series):
    """배당 시계열 index를 tz-naive date 리스트로 변환."""
    if div_series is None or len(div_series) == 0:
        return []
    idx = div_series.index
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
    except Exception:
        pass
    out = []
    for ts in idx:
        try:
            out.append(ts.date() if hasattr(ts, "date") else ts)
        except Exception:
            pass
    return out


def _kr_dividend_count(div_series, today):
    """최근 약 13개월(400일) 배당 지급 횟수 (주기 추정용)."""
    cutoff = today - timedelta(days=400)
    return sum(1 for d in _yf_ex_dates(div_series) if d >= cutoff)


def _project_next_ex_date(div_series, today, last_confirmed_rec=None):
    """과거 배당락일 패턴을 1년 미뤄 '오늘 이후' 가장 가까운 실제 패턴 기반 배당락일 추정.
    단, 이미 확정 공시된 최근 기준일(last_confirmed_rec)과 비슷한 시기(예: 45일 이내)라면 해당 사이클은 지나간 것으로 간주하고 제외한다."""
    cands = []
    for d in _yf_ex_dates(div_series):
        if d < today - timedelta(days=400):
            continue
        try:
            nd = date(d.year + 1, d.month, d.day)
        except ValueError:  # 2/29 등
            nd = date(d.year + 1, d.month, 28)
        while not _is_kr_trading_day(nd):  # 휴장일이면 직전 거래일로
            nd -= timedelta(days=1)
        if last_confirmed_rec and abs((nd - last_confirmed_rec).days) < 45:
            continue
        cands.append(nd)
    fut = sorted(c for c in cands if c >= today)
    return fut[0] if fut else None


def _kr_dividend_plan(pay_count, acc_month):
    """연간 배당 지급 횟수 + 결산월 -> (기준일 월 목록, 주기 라벨).

    월배당=매월말, 분기배당=3·6·9·12월말, 반기배당=6·12월말(법정), 연배당=결산월말.
    """
    if pay_count >= 10:
        return list(range(1, 13)), "월배당"
    if pay_count >= 4:
        return [3, 6, 9, 12], "분기배당"
    if pay_count >= 2:
        return [6, 12], "반기배당"
    try:
        m = int(acc_month)
    except Exception:
        m = 12
    return [m], "연배당"


def _next_kr_ex_date(months, today):
    """주기별 기준일 월들 중 '오늘 이후' 가장 가까운 예상 배당락일."""
    cands = []
    for y in (today.year, today.year + 1):
        for m in months:
            try:
                cands.append(_month_end_ex_date(y, m))
            except Exception:
                pass
    cands = sorted(c for c in cands if c >= today)
    return cands[0] if cands else None


def _collect_kr(code, name):
    """국내 종목의 확정 배당 정보(DART) + 배당주기 반영 다음 예상 배당락일(추정)."""
    info = api.get_dart_dividend(code)
    if not info:
        return None
    acc = api.get_dart_acc_month(code)
    today = datetime.now().date()
    div = _kr_yf_dividends(code)
    count = _kr_dividend_count(div, today)
    months, freq_label = _kr_dividend_plan(count, acc)

    # 1) DART 공시로 최근 확정된 '현금ㆍ현물배당결정' 내역 확인
    confirmed = False
    decl_dps = None
    last_confirmed_rec = None
    try:
        dec = api.get_dart_dividend_decision(code, days=200)
        if dec and dec.get("record_date"):
            last_confirmed_rec = datetime.strptime(dec["record_date"], "%Y%m%d").date()
    except Exception:
        pass

    # 2) 미래의 확정 기준일이 있다면 추정 대신 확정값으로 바로 사용
    if last_confirmed_rec and last_confirmed_rec >= today:
        ex_date = _prev_trading_day(last_confirmed_rec)
        exact = True
        confirmed = True
        decl_dps = dec.get("dps")
    else:
        # 3) 미래 확정 기준일이 없으면 과거 배당락일 패턴으로 정밀 추정 (이미 확정공시된 과거 사이클은 제외)
        ex_date = _project_next_ex_date(div, today, last_confirmed_rec=last_confirmed_rec)
        exact = ex_date is not None
        # 4) 패턴 실패 시 결산월/주기 일반 규칙 폴백
        if ex_date is None:
            try:
                ex_date = _next_kr_ex_date(months, today)
            except Exception:
                ex_date = None

    return {
        "code": code, "name": name, "overseas": False,
        "year": info.get("year", "-"),
        "dps": info.get("주당배당금", 0.0),
        "yield": info.get("시가배당률", 0.0),
        "acc_month": acc,
        "ex_date": ex_date,
        "freq_label": freq_label,
        "exact": exact,
        "confirmed": confirmed,
        "decl_dps": decl_dps,
    }


_EARNINGS_TITLE_KEYWORDS = ("잠정실적", "손익구조", "매출액또는손익")


def _collect_kr_earnings_est(code, name):
    """전년 잠정실적 공시일 패턴으로 다음 실적발표 예상일 산출 (best-effort).

    최근 400일 공시에서 잠정실적류 공시일을 찾아 +1년(같은 요일대 보정 없이 달력일)으로 예상.
    미래 5~370일 범위일 때만 이벤트 반환.
    """
    try:
        today = datetime.now().date()
        candidates = []
        for d in api.get_dart_disclosures(code, days=400):
            nm = (d.get("report_nm") or "").replace(" ", "")
            if not any(k in nm for k in _EARNINGS_TITLE_KEYWORDS):
                continue
            rcept = datetime.strptime(d["rcept_dt"], "%Y%m%d").date()
            est = rcept + timedelta(days=365)
            if today + timedelta(days=5) <= est <= today + timedelta(days=370):
                candidates.append(est)
        if candidates:
            # 전년 각 분기 공시일 +1년 중 가장 가까운 미래 = 다음 발표 예상일
            return {"code": code, "name": name, "type": "실적발표",
                    "date": min(candidates), "estimated": True, "basis": "전년 공시일 +1년"}
    except Exception:
        pass
    return None


def _parse_us_date(val):
    """yfinance가 돌려주는 다양한 날짜 표현을 datetime.date로 정규화."""
    if val is None:
        return None
    try:
        # list(예: Earnings Date)면 가장 가까운 미래 값 사용
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
            if val is None:
                return None
        if isinstance(val, (int, float)):  # unix timestamp
            return datetime.fromtimestamp(val).date()
        if isinstance(val, datetime):
            return val.date()
        if hasattr(val, "date"):  # pandas.Timestamp
            return val.date()
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _collect_us(code, name):
    """해외 종목의 예정 배당락일/실적발표일(yfinance, best-effort)."""
    if yf is None:
        return None
    events = []
    try:
        tk = yf.Ticker(code)
        cal = {}
        try:
            cal = tk.calendar or {}
        except Exception:
            cal = {}

        ex_div = _parse_us_date(cal.get("Ex-Dividend Date") if isinstance(cal, dict) else None)
        earnings = _parse_us_date(cal.get("Earnings Date") if isinstance(cal, dict) else None)

        # calendar가 비면 info로 폴백
        if ex_div is None or earnings is None:
            try:
                info = tk.info or {}
            except Exception:
                info = {}
            if ex_div is None:
                ex_div = _parse_us_date(info.get("exDividendDate"))
            if earnings is None:
                earnings = _parse_us_date(info.get("earningsTimestamp") or info.get("earningsTimestampStart"))

        if ex_div:
            events.append({"code": code, "name": name, "type": "배당락", "date": ex_div})
        if earnings:
            events.append({"code": code, "name": name, "type": "실적발표", "date": earnings})
    except Exception:
        return None
    return events or None


def _gather_watchlist():
    """배당/실적 캘린더 조회 대상: 국내·해외 '주식'만.

    ETF는 배당·실적 공시 대상이 아니다 — 국내 ETF는 DART(alotMatter) 미제출이라
    원래 조회되지 않고, 미국 ETF는 fundamentals가 없어 yfinance가 404 에러 로그만
    남기므로 처음부터 제외한다.
    """
    kr, us = [], []
    for s in config.session.stock_data.get("stocks_kr", []):
        if s.get("code"):
            kr.append((s["code"], s.get("name", s["code"])))
    for s in config.session.stock_data.get("stocks_us", []):
        if s.get("code"):
            us.append((s["code"], s.get("name", s["code"])))
    return kr, us


def _collect_watchlist_events(kr, us, on_progress=None, failures=None):
    """관심종목 배당/실적 일정 수집 → (예정 일정, 국내 배당 행).

    화면 출력(show_calendar)과 텔레그램(build_telegram_message)이 같은 자료를 쓰도록
    수집만 떼어냈다. on_progress는 작업 하나가 끝날 때마다 호출된다(진행률 표시용).

    failures(ScanFailures)를 주면 종목별 조회 실패를 거기에 모은다 — 배당락일을
    '조회 실패'로 놓치면 배당락 직전 매수처럼 그날만 가능한 조치를 통째로 못 한다.
    """
    events, kr_rows = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {}
        if config.DART_API_KEY:
            for code, name in kr:
                futures[ex.submit(_collect_kr, code, name)] = ("kr", code)
                futures[ex.submit(_collect_kr_earnings_est, code, name)] = ("kr_earn", code)
        for code, name in us:
            futures[ex.submit(_collect_us, code, name)] = ("us", code)

        for fut in concurrent.futures.as_completed(futures):
            kind, fcode = futures[fut]
            try:
                res = fut.result()
            except Exception as e:      # noqa: BLE001 - 일부라도 보여 주되 실패는 밝힌다
                res = None
                if failures is not None:
                    failures.record(fcode, e)
                else:
                    logger.debug(f"[캘린더] {fcode} 조회 실패: {e}")
            if res:
                if kind == "kr":
                    kr_rows.append(res)
                elif kind == "kr_earn":
                    events.append(res)  # 예정 일정 테이블에 합류
                else:
                    events.extend(res)
            if on_progress:
                on_progress()

    # 국내 예상 배당락일(추정/확정)을 예정 일정에 합류
    for r in kr_rows:
        if r.get("ex_date"):
            events.append({
                "code": r["code"], "name": r["name"], "type": "배당락",
                "date": r["ex_date"], "estimated": True,
                "freq": r.get("freq_label", ""),
                "exact": r.get("exact", False),
                "confirmed": r.get("confirmed", False),
                "decl_dps": r.get("decl_dps"),
            })
    return events, kr_rows


def _upcoming_note(e):
    """예정 일정 한 건의 '비고' 문구 → (문구, 추정 여부)."""
    if e.get("confirmed"):
        # 배당결정 공시에서 확정된 기준일 기반 — 추정 아님
        note = "확정(배당결정 공시)"
        if e.get("decl_dps"):
            note += f" 주당 {e['decl_dps']:,.0f}원"
        return note, False
    if e.get("basis"):
        return e["basis"], True
    if e.get("estimated"):
        freq = e.get("freq", "")
        basis = "전년패턴" if e.get("exact") else "추정"
        return (f"{freq}·{basis}" if freq else basis), True
    return "", False


def build_telegram_message(days=30):
    """텔레그램 /calendar 용 메시지 — 주요 경제 이벤트 + 예정 일정.

    화면 6-5와 같은 소스를 쓰되, 국내 배당 정보 테이블(최근 확정분)은 일정이 아니라
    참고 자료라 넣지 않는다. 예정 일정은 앞으로 `days`일 이내만 추린다.
    """
    from modules.manage import econ_events

    today = datetime.now().date()
    lines = [f"📅 [투자 캘린더] 향후 {days}일", ""]
    lines.extend(econ_events.build_lines(days=days))
    lines.append("")

    kr, us = _gather_watchlist()
    if not kr and not us:
        lines.append("▸ 예정 일정")
        lines.append("  등록된 관심종목이 없습니다.")
        return "\n".join(lines)

    failures = ScanFailures("캘린더")
    events, _ = _collect_watchlist_events(kr, us, failures=failures)
    horizon = today + timedelta(days=days)
    upcoming = sorted([e for e in events if today <= e["date"] <= horizon], key=lambda e: e["date"])

    lines.append("▸ 예정 일정 (관심종목 배당·실적)")
    if kr and not config.DART_API_KEY:
        lines.append("  ※ DART API 키가 없어 국내 배당 정보는 조회되지 않습니다.")
    fail_note = failures.telegram_note()
    if fail_note:
        lines.append(f"  {fail_note}")
    if not upcoming:
        lines.append("  표시할 예정 일정이 없습니다.")
        return "\n".join(lines)

    has_estimated = False
    for e in upcoming:
        gap = (e["date"] - today).days
        dday = "D-DAY" if gap == 0 else f"D-{gap}"
        note, est = _upcoming_note(e)
        has_estimated = has_estimated or est
        row = (f"• {e['date'].strftime('%m-%d')}({_WEEKDAY_KR[e['date'].weekday()]}) {dday} "
               f"{e['name']} ({e['code']}) {e['type']}")
        if note:
            row += f" — {note}"
        lines.append(row)

    if has_estimated:
        lines.append("  ※ '추정'은 결산월·전년 패턴 기반 예상치로 실제와 다를 수 있습니다.")
    return "\n".join(lines)


ALERT_LEAD_DAYS = (1, 0)   # 알림을 보내는 시점: 전일(D-1)과 당일(D-DAY)


def _alert_key(kind, ev_date, label, gap):
    """알림 중복방지 키 — 같은 일정이라도 D-1/D-DAY는 각각 한 번씩 보낸다.

    공시 알림 테이블(notified_disclosures)을 그대로 쓰되 'CAL:' 접두로 접수번호와 구분한다.
    """
    return f"CAL:{kind}:{ev_date}:{label}:D{gap}"


def _alert_dday_label(gap, d, prev_td=False):
    """D-n 헤더. prev_td면 '오늘이 직전 거래일'을 덧붙인다.

    주말·공휴일이 끼면 달력 격차가 벌어져 'D-3'으로 찍히는데, 실제로는 오늘이
    그 일정 전의 마지막 거래일이다. 숫자만 보고 아직 여유가 있다고 오독하면
    배당락 직전 매수처럼 당일에만 가능한 조치를 놓친다.
    'D-1'은 이미 '내일'로 나가 오독의 여지가 없으므로 덧붙이지 않는다.
    """
    label = f"▸ {'오늘' if gap == 0 else '내일' if gap == 1 else f'D-{gap}'} " \
            f"({d.strftime('%m-%d')} {_WEEKDAY_KR[d.weekday()]})"
    if prev_td and gap > 1:
        label += " · 오늘이 직전 거래일"
    return label


def check_and_alert_calendar(lead_days=ALERT_LEAD_DAYS):
    """임박한 경제 이벤트·관심종목 일정을 텔레그램으로 푸시 (scheduler 백그라운드용, UI 없음).

    공시 알림과 같은 방식이지만 건당이 아니라 하루 한 통의 요약으로 보낸다 —
    같은 날 FOMC·CPI·배당락이 겹치면 알림이 세 통 오는 게 오히려 묻히기 때문.
    중복방지는 DB(notified_disclosures)에 'CAL:' 접두 키로 기록. 반환: 발송 건수(0/1).
    """
    from modules import db_manager
    from modules.manage import econ_events

    today = datetime.now().date()
    horizon = max(lead_days)
    targets = {}   # gap -> [(icon, 본문)]
    prev_td_gaps = set()   # '직전 거래일' 분기로 걸린 gap (헤더 문구용)
    keys = []

    try:
        econ, _ = econ_events.get_events(days=horizon)
    except Exception:
        econ = []
    for ev in econ:
        d = econ_events._parse_date(ev.get("date"))
        if not d:
            continue
        gap = (d - today).days
        is_alert = (gap in lead_days)
        if not is_alert and today == _prev_trading_day(d):
            is_alert = True
            prev_td_gaps.add(gap)

        if not is_alert:
            continue
        key = _alert_key("econ", ev["date"], ev["name"], gap)
        if db_manager.db.is_disclosure_notified(key):
            continue
        mark = "❗" if ev.get("weight", 3) == 1 else "•"
        targets.setdefault(gap, []).append(f"{mark} {ev['name']} [{ev.get('source', '')}]")
        keys.append(key)

    kr, us = _gather_watchlist()
    if kr or us:
        #  경보 경로는 화면이 없다 — 조회 실패는 로그로 드러낸다. 종목 하나가 계속
        #  실패하면 그 종목의 배당락 알림이 영영 안 나가기 때문이다.
        failures = ScanFailures("캘린더")
        try:
            events, _ = _collect_watchlist_events(kr, us, failures=failures)
        except Exception as e:      # noqa: BLE001
            logger.warning(f"[Calendar] 관심종목 일정 수집 실패 — 이번 주기 종목 알림 없음: {e}")
            events = []
        if failures:
            logger.warning(f"[Calendar] {len(failures)}개 종목을 조회하지 못했습니다 "
                           f"({', '.join(list(failures.failed)[:5])}) — 그 종목의 일정 알림은 빠집니다.")
        for e in events:
            gap = (e["date"] - today).days
            is_alert = (gap in lead_days)
            # 배당락일/실적 등 주식 일정은 달력 D-1 외에 '직전 거래일'에도 추가 알림
            if not is_alert and today == _prev_trading_day(e["date"]):
                is_alert = True
                prev_td_gaps.add(gap)

            if not is_alert:
                continue
            key = _alert_key("stock", e["date"].strftime("%Y-%m-%d"), f"{e['code']}:{e['type']}", gap)
            if db_manager.db.is_disclosure_notified(key):
                continue
            note, _est = _upcoming_note(e)
            # 종목 줄도 경제 이벤트와 같은 '•'로 통일한다 — 줄마다 다른 이모지가 앞에 서면
            #  정작 읽어야 할 종목명의 시작 위치가 어긋나 훑어보기가 나빠진다(/calendar와 동일).
            row = f"• {e['name']} ({e['code']}) {e['type']}"
            if note:
                row += f" — {note}"
            targets.setdefault(gap, []).append(row)
            keys.append(key)

    if not targets:
        return 0

    lines = ["🔔 [캘린더 알림] 임박한 일정"]
    for gap in sorted(targets):   # 오늘 → 내일 순
        lines.append("")
        lines.append(_alert_dday_label(gap, today + timedelta(days=gap),
                                       prev_td=gap in prev_td_gaps))
        lines.extend(targets[gap])

    #  [Fix 2026-09-04] '발송이 성공한 뒤에만 기록한다'는 의도는 맞았지만 수단이 없었다.
    #   send_telegram_message 는 비동기라 예외를 던지지 않아 아래 try 가 아무것도 잡지
    #   못했고, 네트워크가 끊겨 있어도 전부 '보냈다'로 굳었다. 캘린더 알림은 하루 한 번뿐이라
    #   한 번 놓치면 그 일정은 끝이다. 동기로 보내고 전달을 확인한다.
    try:
        delivered = api.send_telegram_message("\n".join(lines), sync=True)
    except Exception as e:
        logger.error(f"[Calendar] 알림 전송 오류: {e}")
        return 0
    if not delivered:
        logger.warning("[Calendar] 알림 전송 실패 — 표시하지 않는다(다음 기회에 다시 시도)")
        return 0

    for key in keys:
        db_manager.db.mark_disclosure_notified(key)
    return 1


def show_calendar():
    """경제 이벤트 + 관심종목 배당/실적 캘린더 출력."""
    utils.clear_screen()
    config.console.print("\n[bold cyan][투자 캘린더][/bold cyan] [dim]경제 이벤트 · 배당 · 실적[/dim]\n")

    # 경제 이벤트는 관심종목과 무관하게 항상 먼저 보여준다 —
    #  FOMC·CPI 같은 매크로 일정은 보유 종목이 없어도 진입 시점 판단에 쓰이기 때문.
    from modules.manage import econ_events
    econ_events.render()

    kr, us = _gather_watchlist()
    if not kr and not us:
        config.console.print("[yellow]등록된 관심종목이 없습니다. 먼저 관심종목을 추가해주세요.[/yellow]")
        return

    if kr and not config.DART_API_KEY:
        config.console.print("[dim yellow]※ DART API 키가 설정되지 않아 국내 배당 정보는 조회되지 않습니다. (환경변수 DART_API_KEY)[/dim yellow]\n")

    failures = ScanFailures("캘린더")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"), console=config.console, transient=True
    ) as progress:
        total = (len(kr) * 2 if config.DART_API_KEY else 0) + len(us)
        task = progress.add_task("[cyan]예정 일정 조회 중...[/cyan]", total=total)
        events, kr_rows = _collect_watchlist_events(
            kr, us, on_progress=lambda: progress.advance(task), failures=failures)
        progress.update(task, completed=total)

    #  '조회 실패'를 '일정 없음'으로 읽게 두지 않는다. 배당락일을 놓치면 그날만 가능한
    #  조치(배당락 직전 매수 등)를 통째로 못 한다.
    failures.announce()

    _print_report_deadline()
    _render_upcoming(events)

    if kr_rows:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"), console=config.console, transient=True
        ) as progress:
            task = progress.add_task("[cyan]국내 배당 정보 정리 중...[/cyan]", total=len(kr_rows))
            import time
            for _ in kr_rows:
                time.sleep(0.02)  # 짧은 애니메이션 효과
                progress.advance(task)

    _render_kr_dividends(kr_rows)


def _print_report_deadline():
    """다음 정기보고서 법정 제출기한 안내 (국내 실적공시 데드라인)."""
    today = datetime.now().date()
    y = today.year
    schedule = [
        (date(y, 3, 31), "사업보고서(연간)"), (date(y, 5, 15), "1분기보고서"),
        (date(y, 8, 14), "반기보고서"), (date(y, 11, 14), "3분기보고서"),
        (date(y + 1, 3, 31), "사업보고서(연간)"),
    ]
    for deadline, label in schedule:
        if deadline >= today:
            d = (deadline - today).days
            config.console.print(f"[dim]▸ 다음 정기보고서 법정 제출기한: {deadline.strftime('%Y-%m-%d')} "
                                 f"({label}, D-{d}) — 국내 실적은 늦어도 이 시점까지 공시됩니다.[/dim]\n")
            return


def _render_upcoming(events):
    """예정 일정(해외 배당락/실적 + 국내 예상 배당락) - 날짜순."""
    today = datetime.now().date()
    upcoming = sorted([e for e in events if e["date"] >= today], key=lambda e: e["date"])

    config.console.print("[bold]▸ 예정 일정[/bold]")
    config.console.print("  [dim]※ 해외 실적발표=yfinance 예정일 · 국내 실적발표=전년 잠정실적 공시일 기반 예상 · ETF는 배당·실적 공시 대상이 아니라 제외됩니다.[/dim]")
    if not upcoming:
        config.console.print("  [dim]표시할 예정 일정이 없습니다.[/dim]\n")
        return

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("D-day", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="center")
    table.add_column("비고", justify="left")

    has_estimated = False
    for e in upcoming:
        d = (e["date"] - today).days
        dday = "D-DAY" if d == 0 else f"D-{d}"
        type_color = "yellow" if e["type"] == "배당락" else "cyan"
        raw_note, est = _upcoming_note(e)
        has_estimated = has_estimated or est
        # 확정분은 초록으로 강조하고, 추정·근거 문구는 흐리게
        note = f"[green]{raw_note}[/green]" if (raw_note and not est) else (f"[dim]{raw_note}[/dim]" if raw_note else "")
        table.add_row(
            e["date"].strftime("%Y-%m-%d (%a)"),
            f"[bold]{dday}[/bold]" if d <= 3 else dday,
            f"{e['name']} ({e['code']})",
            f"[{type_color}]{e['type']}[/]",
            note,
        )
    config.console.print(table)
    if has_estimated:
        config.console.print("  [dim]※ '추정' 배당락일은 결산월·거래일(T+2)로 계산한 예상치입니다. "
                             "선배당후투자 도입·분기/반기 배당 기업은 실제와 다를 수 있습니다.[/dim]")
    config.console.print()


def _render_kr_dividends(rows):
    """국내 배당 정보(최근 확정) - 시가배당률 높은 순."""
    config.console.print("[bold]▸ 국내 배당 정보 (최근 확정 / DART)[/bold]")
    if not rows:
        config.console.print("  [dim]조회된 국내 배당 정보가 없습니다.[/dim]\n")
        return

    rows = sorted(rows, key=lambda r: r["yield"], reverse=True)
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목", justify="left")
    table.add_column("기준연도", justify="center")
    table.add_column("주당배당금", justify="right")
    table.add_column("시가배당률", justify="right")
    table.add_column("배당주기", justify="center")

    freq_color = {"월배당": "magenta", "분기배당": "cyan", "반기배당": "green", "연배당": "white"}
    for r in rows:
        y_color = "red" if r["yield"] >= 3.0 else ("orange3" if r["yield"] >= 1.0 else "white")
        freq = r.get("freq_label") or "-"
        table.add_row(
            f"{r['name']} ({r['code']})",
            str(r["year"]),
            f"{r['dps']:,.0f}원" if r["dps"] else "-",
            f"[{y_color}]{r['yield']:.2f}%[/]" if r["yield"] else "-",
            f"[{freq_color.get(freq, 'white')}]{freq}[/]",
        )
    config.console.print(table)
    config.console.print("  [dim]※ 배당주기는 최근 1년 실제 지급 횟수(yfinance) 기반 추정입니다.[/dim]")
    config.console.print()
