"""휴장일과 시각 — '오늘 장이 서는가'의 기준.

국내·미국 공휴일과 거래소(MIC) 달력, 그리고 해외 시각 변환(미 동부·런던·중부유럽)을 담는다.
여기서 나오는 답이 매매 시간 판정의 출발점이라, 라이브러리 버전이 바뀌면 조용히
'휴장 없음'으로 퇴화할 수 있다는 점을 requirements.txt 의 holidays 주석에 함께 적어 두었다.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
import config
from brokers import toss_api

#  로거 이름은 분해 전(api.py)과 같은 'api' 로 둔다 — 로그 필터·레벨 설정이 이름을 보므로
#  서브모듈마다 다른 이름을 쓰면 기존 설정이 조용히 빗나간다.
logger = logging.getLogger("api")

def _api():
    """패키지 네임스페이스(api)를 돌려준다 — 다른 계층의 이름은 반드시 이걸 통해 부른다.

    분해 전에는 전부 한 모듈이었으므로 테스트의 patch.object(api, 'X') 가 모든 호출부에
    걸렸다. 서브모듈이 상대 모듈을 직접 import 하면 그 patch 가 닿지 않는다 —
    같은 규약을 쓰는 modules/auto_trade 의 _pkg() 와 같은 이유다.
    """
    import api
    return api

# [추가] 휴장일 캐시 — **확정된 답만** 담는다.
_HOLIDAY_CACHE = {}

#  조회에 실패해 라이브러리로 메운 답. (키 -> (값, 다시 물어도 되는 시각))
#  [왜 나눠 두나 · 2026-09-05] 종전에는 실패한 답도 _HOLIDAY_CACHE 에 그대로 넣었다.
#   그러면 `if date_str in _HOLIDAY_CACHE: return` 에 걸려 **API 를 두 번 다시 묻지 않는다**
#   — 기동 직후(라즈베리파이 부팅·토큰 발급 전)에 한 번 실패하면 그 답이 하루 종일 굳는다.
#   실측 재현: check_holiday 가 None 을 돌려준 뒤 API 가 '휴장'이라고 답해도 재조회 0회.
#   방향이 나쁘다 — holidays 라이브러리는 **임시공휴일을 모른다**(그 해에 정해지므로
#   설치된 버전에 없다). 즉 실패의 기본값이 '거래일'이고, 닫힌 시장에 매매 루프가 돈다.
#   ('조회 실패 ≠ 없음'의 또 한 자리다 — [[unknown-vs-empty]])
_HOLIDAY_PROVISIONAL = {}
HOLIDAY_RETRY_SEC = 300.0      # 실패한 답을 다시 물어보기까지. 5분이면 하루 288회다.

def check_holiday(date_str):
    """한국투자증권 휴장일 조회 API 호출"""
    url_path = "uapi/domestic-stock/v1/quotations/chk-holiday"
    tr_id = "CTCA0903R"
    params = {"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
    
    res = _api().call_api(url_path, "domestic", "quotations", "chk_holiday", params=params, tr_id=tr_id, retries=1)
    
    if res and res.get('rt_cd') == '0':
        output = res.get('output', [])
        if output:
            for day_info in output:
                if day_info.get('bass_dt') == date_str:
                    opnd_yn = day_info.get('opnd_yn', 'Y')
                    bzdy_yn = day_info.get('bzdy_yn', 'Y')
                    if opnd_yn == 'N' or bzdy_yn == 'N': return True
                    return False
    return None

def _cached_holiday(key):
    """확정 답이 있으면 (True, 값). 아직 유효한 잠정 답이 있으면 (True, 잠정값)."""
    if key in _HOLIDAY_CACHE:
        return True, _HOLIDAY_CACHE[key]
    entry = _HOLIDAY_PROVISIONAL.get(key)
    if entry and time.time() < entry[1]:
        return True, entry[0]
    return False, None


def _remember_holiday(key, value, confirmed):
    """확정 답이면 굳히고, 실패로 메운 답이면 재조회 시각과 함께 따로 둔다."""
    if confirmed:
        _HOLIDAY_CACHE[key] = value
        _HOLIDAY_PROVISIONAL.pop(key, None)
    else:
        _HOLIDAY_PROVISIONAL[key] = (value, time.time() + HOLIDAY_RETRY_SEC)
    return value


def holiday_answer_provisional(date_str, country='KR'):
    """그 날짜의 휴장 판정이 '조회 실패를 라이브러리로 메운 잠정값'인가.

    잠정값에서 나온 결과는 캐시에 굳히면 안 된다 — 굳히면 재조회의 의미가 사라진다
    (market_today 의 거래일 캐시가 이 함수를 본다).
    """
    key = date_str if country == 'KR' else f"{country}_{date_str}"
    return key not in _HOLIDAY_CACHE and key in _HOLIDAY_PROVISIONAL


def is_holiday_on(date_str):
    """지정 일자(YYYYMMDD)가 주말 또는 공휴일(휴장일)인지 확인합니다.

    '오늘'이 아닌 날짜도 물을 수 있어야 한다 — 코스피200 야간선물처럼 자정을 넘겨
    이어지는 세션은 새벽(00:00~05:00)에 '전날'이 거래일이었는지가 개폐를 가른다.
    (토스 market-calendar 는 조회일 기준 응답 해석이 오늘에만 확실하므로 오늘에만 쓴다)

    조회에 실패해 holidays 라이브러리로 메운 답은 **굳히지 않는다** — 잠깐 그 값을 쓰되
    HOLIDAY_RETRY_SEC 뒤에 다시 묻는다(_HOLIDAY_PROVISIONAL 주석 참조).
    """
    hit, value = _cached_holiday(date_str)
    if hit:
        return value

    if datetime.strptime(date_str, "%Y%m%d").weekday() > 4:
        return _remember_holiday(date_str, True, confirmed=True)

    is_today = date_str == datetime.now().strftime("%Y%m%d")
    # [추가] 토스 모드일 경우 토스 API의 market-calendar 이용
    if config.session.is_toss and is_today:
        from brokers import toss_api
        try:
            today_formatted = datetime.now().strftime("%Y-%m-%d")
            res = toss_api.get_market_calendar("KR", today_formatted)
            if res and res.get('today'):
                is_holiday = not bool(res['today'].get('integrated'))
                return _remember_holiday(date_str, is_holiday, confirmed=True)
        except Exception as e:
            logger.debug(f"Toss market-calendar error: {e}")

    # 실전투자 모드일 경우에만 API 우선 조회 시도
    if not config.session.is_toss:
        res = check_holiday(date_str)
        if res is not None:
            return _remember_holiday(date_str, res, confirmed=True)

    # holidays 라이브러리 자체 판단.
    #  물어볼 권위가 있었는데 못 물은 경우(= 아래 asked_authority)만 잠정으로 둔다.
    #  토스 모드의 '오늘이 아닌 날짜'처럼 애초에 물을 곳이 없으면 이게 유일한 답이므로 확정이다.
    asked_authority = (not config.session.is_toss) or is_today
    is_holiday = get_holiday_name(date_str, country='KR') is not None
    return _remember_holiday(date_str, is_holiday, confirmed=not asked_authority)

def is_holiday_today():
    """오늘이 주말 또는 공휴일(휴장일)인지 확인합니다."""
    return is_holiday_on(datetime.now().strftime("%Y%m%d"))

def is_us_holiday_on(date_str):
    """지정 일자(YYYYMMDD)가 주말 또는 미국 공휴일(휴장일)인지 확인합니다.

    미국 정규장은 KST 22:30~익일 05:00이라 세션의 대부분이 '한국 날짜'로는 이튿날이다.
    개장 여부를 물을 때는 반드시 '미국 동부 날짜'로 물어야 한다(now_us_eastern()).
    (토스 market-calendar 는 조회일 기준 응답 해석이 오늘에만 확실하므로 오늘에만 쓴다)
    """
    cache_key = f"US_{date_str}"
    hit, value = _cached_holiday(cache_key)
    if hit:
        return value

    if datetime.strptime(date_str, "%Y%m%d").weekday() > 4:
        return _remember_holiday(cache_key, True, confirmed=True)

    # [추가] 토스 모드일 경우 토스 API의 market-calendar 이용
    if config.session.is_toss and date_str == datetime.now().strftime("%Y%m%d"):
        from brokers import toss_api
        try:
            today_formatted = datetime.now().strftime("%Y-%m-%d")
            res = toss_api.get_market_calendar("US", today_formatted)
            if res and res.get('today'):
                today_info = res['today']
                has_market = any(k in today_info and today_info[k] is not None for k in ['dayMarket', 'preMarket', 'regularMarket', 'afterMarket'])
                is_holiday = not has_market
                _HOLIDAY_CACHE[cache_key] = is_holiday
                return is_holiday
        except Exception as e:
            logger.debug(f"Toss US market-calendar error: {e}")
            pass

    # [삭제 2026-08-22] KIS 해외 휴장일 TR(overseas-stock/.../chk-holiday, CTCA0904R)은
    #  실전 서버에서 404다(존재하지 않는 엔드포인트). 호출해봐야 항상 None을 돌려주면서
    #  화면에 HTTP 404 경고만 찍었다 → holidays 라이브러리 판정으로 일원화한다.
    #  달력은 연방공휴일이 아니라 NYSE(XNYS)를 쓴다 — 증시는 성금요일에 쉬고 콜럼버스데이·
    #  재향군인의 날에는 열어서, 연방공휴일로 보면 양방향으로 틀렸다.
    is_holiday = is_exchange_holiday(datetime.strptime(date_str, "%Y%m%d"), "XNYS")
    _HOLIDAY_CACHE[cache_key] = is_holiday

    return is_holiday

def is_us_holiday_today():
    """오늘(한국 날짜)이 주말 또는 미국 공휴일(휴장일)인지 확인합니다.

    '오늘 밤 열릴 미국장이 휴장인가'를 묻는 장전 알림(scheduler) 기준이라 한국 날짜를 쓴다.
    실시간 개장 여부 판정에는 미국 동부 날짜로 is_us_holiday_on()을 직접 호출해야 한다.
    """
    return is_us_holiday_on(datetime.now().strftime("%Y%m%d"))

def kr_year_end_closing_day(year):
    """그 해 KRX 연말 휴장일(date). **12/31이 주말이면 직전 평일**이 휴장일이다.

    [SSOT 2026-09-05] 이 규칙이 두 곳에 서로 다르게 적혀 있었다. 여기(get_holiday_name)는
     **무조건 12/31**로 못박았고, modules/manage/events._kr_year_end_holiday 는 주말 롤백을
     했다. 2026~2040년 15년 중 **4년(2028·2033·2034·2039)이 갈라진다** — 예컨대 12/31이
     일요일인 2028년의 폐장일은 12/29(금)인데, 이쪽 정의로는 그 날이 평범한 거래일이다.

     이 판정은 _is_closed_day → last_trading_day → market_today 로 이어져 '오늘이 어느
     거래일인가'를 정한다. 틀리면 휴장일에 가짜 당일 봉이 붙고(등락률 0%), 주말에 물었을 때
     직전 거래일을 하루 늦게 짚는다. 규칙은 여기 하나만 둔다 — events 는 이걸 부른다.

     (KRX 규정: 12월 31일은 휴장하고, 그 날이 휴일이면 직전 영업일이 휴장한다.)
    """
    d = datetime(year, 12, 31).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_holiday_name(date_str, country='KR'):
    """holidays 라이브러리를 이용하여 공휴일 이름을 반환합니다."""
    try:
        import holidays
        dt = datetime.strptime(date_str, "%Y%m%d").date()
        
        if country == 'KR':
            h_cal = holidays.KR()
            h_cal[dt.replace(month=5, day=1)] = "근로자의 날" # 법정공휴일이 아닌 근로자의 날 강제 추가
            h_cal[kr_year_end_closing_day(dt.year)] = "연말 폐장일" # 한국거래소(KRX) 연말 휴장일 강제 추가
            name = h_cal.get(dt)
            return name
        elif country == 'US':
            h_cal = holidays.US(observed=True)
            name = h_cal.get(dt)
            return name
    except Exception as e:
        logger.debug(f"get_holiday_name error: {e}")
        return None
    return None

# ==========================================================
# [추가] 거래소 휴장 달력 — 공휴일 ≠ 거래소 휴장이다.
#  NYSE는 성금요일에 쉬고 콜럼버스데이·재향군인의 날에는 연다. Xetra는 독일 공휴일 중
#  통일기념일·승천일에 열고 12/24·12/31에는 쉰다. 그래서 국가 공휴일 목록 대신
#  holidays 라이브러리의 거래소(MIC) 달력을 쓴다.
EXCHANGE_CALENDARS = {
    "XNYS": {"financial": "XNYS"},   # 뉴욕증권거래소 — 미국 정규장 지수
    "XCME": {"financial": "XCME"},   # CME 글로벡스 — 지수선물·원자재·금리·FX
    "XJPX": {"financial": "XJPX"},   # 도쿄증권거래소
    "XTAI": {"financial": "XTAI"},   # 타이완증권거래소
    "XHKG": {"financial": "XHKG"},   # 홍콩거래소
    "XSHG": {"financial": "XSHG"},   # 상하이증권거래소
    "XETR": {"financial": "XETR"},   # 프랑크푸르트(Xetra) — Eurex·STOXX 달력과 같다
    # 런던은 MIC 달력이 없다(IFEU는 ICE 선물용이라 3일뿐). LSE는 잉글랜드 뱅크홀리데이에
    #  쉬므로 그 목록을 쓴다(2026년 9일 = 실제 LSE 휴장일과 일치).
    "XLON": {"country": "GB", "subdiv": "ENG"},
}

# (거래소, 연도) -> 휴장일 date 집합. 지수 한 화면에 판정이 수십 번 불려 매번 달력을
#  만들면 느리다. 연 단위라 상한 불필요.
_EXCHANGE_HOLIDAY_CACHE = {}
_EXCHANGE_CAL_WARNED = set()   # 달력 조회 실패 경고를 거래소당 한 번만 내기 위한 표시


def _exchange_holiday_dates(exchange, year):
    """해당 거래소·연도의 휴장일 date 집합. 조회 실패 시 빈 집합(=휴장 없음으로 본다)."""
    key = (exchange, year)
    if key in _EXCHANGE_HOLIDAY_CACHE:
        return _EXCHANGE_HOLIDAY_CACHE[key]
    dates = frozenset()
    try:
        import holidays
        spec = EXCHANGE_CALENDARS[exchange]
        if "financial" in spec:
            cal = holidays.financial_holidays(spec["financial"], years=year)
        else:
            cal = holidays.country_holidays(spec["country"], subdiv=spec.get("subdiv"), years=year)
        dates = frozenset(cal.keys())
    except Exception as e:      # noqa: BLE001 - 달력이 없으면 '휴장 아님'으로 두고 계속한다
        # 구버전 holidays 에는 일부 MIC 달력이 없다. 조용히 퇴화하면 휴장일에 개장 표기가
        #  붙는 것으로만 드러나므로, 거래소당 한 번은 경고로 남긴다.
        if exchange not in _EXCHANGE_CAL_WARNED:
            _EXCHANGE_CAL_WARNED.add(exchange)
            logger.warning(f"거래소 휴장 달력 조회 실패({exchange}): {e} — 휴장일 판정 없이 진행합니다")
    _EXCHANGE_HOLIDAY_CACHE[key] = dates
    return dates


def is_exchange_holiday(dt, exchange):
    """dt(그 거래소 '현지' 날짜)가 휴장일인가. 주말은 보지 않는다(호출부가 따로 본다)."""
    try:
        return dt.date() in _exchange_holiday_dates(exchange, dt.year)
    except Exception:      # noqa: BLE001
        return False


# (계산일 YYYYMMDD, country) -> 직전 거래일 YYYYMMDD. 하루 2건 수준이라 상한 불필요.
_TRADING_DAY_CACHE = {}

def _is_closed_day(dt, country):
    """해당 일자가 주말·휴장일이면 True. 오늘(KR)은 실시간 캘린더(토스/KIS API) 판정을
    우선해 holidays 라이브러리에 없는 임시휴장까지 반영하고, 그 외 일자는 달력으로 판정한다.

    [Fix 2026-09-04] 미국은 **연방공휴일이 아니라 NYSE(XNYS) 달력**을 쓴다. 같은 파일이
     is_us_holiday_on 에서 이미 그렇게 고쳤는데(EXCHANGE_CALENDARS 주석: "증시는 성금요일에
     쉬고 콜럼버스데이·재향군인의 날에는 열어서, 연방공휴일로 보면 양방향으로 틀렸다")
     이 함수만 연방공휴일에 남아 있었다. 이 판정은 market_today(True) → last_trading_day
     경로로 '미국 종목의 오늘'을 정하므로, 틀리면 양방향으로 봉이 오염된다.
       · 성금요일(휴장·연방공휴일 아님) → 거래일로 봐서 가짜 당일 봉이 붙고 등락률 0%
       · 콜럼버스데이·재향군인의 날(개장·연방공휴일) → 직전 거래일로 되돌려, 실제 거래일의
         현재가가 **확정된 전날 봉을 덮어쓴다**
    """
    if dt.weekday() > 4:
        return True
    d_str = dt.strftime('%Y%m%d')
    if country == 'KR':
        if d_str == datetime.now().strftime('%Y%m%d'):
            try:
                return bool(is_holiday_today())
            except Exception:
                pass
        return get_holiday_name(d_str, country='KR') is not None
    if country == 'US':
        return is_exchange_holiday(dt, "XNYS")
    return get_holiday_name(d_str, country=country) is not None

def last_trading_day(dt, country='KR'):
    """dt(datetime)부터 주말·공휴일(휴장일)을 거슬러 올라간 가장 가까운 거래일(YYYYMMDD)을 반환한다."""
    for _ in range(15):  # 최장 연휴(추석 등)+주말 연속 상한
        if not _is_closed_day(dt, country):
            break
        dt -= timedelta(days=1)
    return dt.strftime('%Y%m%d')

def now_us_eastern():
    """미국 동부시간(ET) 현재 시각(naive datetime). 서머타임(DST) 자동 판별.
    (trading.py 주문 세션 판별과 동일 규칙 — 3월 둘째 일요일 ~ 11월 첫째 일요일)"""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    year = now_utc.year
    march_first = datetime(year, 3, 1)
    march_second_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
    dst_start_utc = march_second_sunday.replace(hour=7, minute=0, second=0)
    nov_first = datetime(year, 11, 1)
    nov_first_sunday = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)
    dst_end_utc = nov_first_sunday.replace(hour=6, minute=0, second=0)
    is_dst = dst_start_utc <= now_utc < dst_end_utc
    return now_utc - timedelta(hours=4 if is_dst else 5)

def _last_sunday(year, month):
    """해당 연·월의 마지막 일요일(naive datetime, 00:00)."""
    first_next = datetime(year + (month == 12), (month % 12) + 1, 1)
    last_day = first_next - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() + 1) % 7)


def _is_europe_dst(now_utc):
    """유럽 서머타임 구간인가 — 3월 마지막 일요일 01:00 UTC ~ 10월 마지막 일요일 01:00 UTC.
    (영국·중부유럽이 같은 순간에 함께 전환하므로 플래그 하나로 둘 다 판정한다)"""
    year = now_utc.year
    start = _last_sunday(year, 3).replace(hour=1)
    end = _last_sunday(year, 10).replace(hour=1)
    return start <= now_utc < end


def now_europe_london():
    """영국 시간(GMT/BST) 현재 시각(naive datetime). 서머타임 자동 판별."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return now_utc + timedelta(hours=1 if _is_europe_dst(now_utc) else 0)


def now_europe_central():
    """중부 유럽 시간(CET/CEST) 현재 시각(naive datetime). 서머타임 자동 판별."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return now_utc + timedelta(hours=2 if _is_europe_dst(now_utc) else 1)
