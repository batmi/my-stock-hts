"""세션 판정과 표기 — 지금이 어느 장(정규·프리·애프터·데이마켓)인가.

미국 거래소 코드(NAS/NYS/AMS 와 주간거래 BAQ/BAY/BAA)의 정규화·후보 산출,
국내외 세션 국면 판정, 화면에 붙는 세션 라벨·태그, 그리고 KRX 종가 확정 이후의
표시 규칙(장 마감 후 어떤 가격을 보여줄 것인가)이 여기 모인다.
"""
import logging
from datetime import datetime, timedelta, timezone
import config

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

# ==========================================================
# [미국 주간거래(데이마켓)] 거래소 코드 매핑 및 세션 판정
# ==========================================================
# KIS는 미국 야간 ATS 세션(ET 20:00~04:00 = KST 09:00~17:00, 서머타임 기준)을
# '주간거래(데이마켓)'로 부르며 정규장과 다른 거래소 코드로 시세를 제공한다.
# 이 코드로 조회하지 않으면 세션 내내 직전 정규장 마감가가 그대로 굳는다
# (실측 2026-07-22 ET 02:50: MU NAS $970.82 +12.17%[전일 마감 동결] vs BAQ $949.00 -2.25%[라이브]).
# 주문 경로(modules/trading.py)는 이미 ord_dvsn '31'로 데이마켓을 인지하고 있었으므로,
# '주문은 되는데 가격은 못 보는' 비대칭을 해소한다.
US_DAY_MARKET_EXCD = {"NAS": "BAQ", "NASD": "BAQ",
                      "NYS": "BAY", "NYSE": "BAY",
                      "AMS": "BAA", "AMEX": "BAA"}
# 주간거래 코드는 exchange_cache/stock.json에 저장하면 안 되므로(정규장 조회가 깨진다) 역매핑을 둔다.
US_REGULAR_EXCD = {"BAQ": "NAS", "BAY": "NYS", "BAA": "AMS"}

# [추가] 미국 거래소 코드 정규화 — 시세(quotations) 조회용 표준 코드는 NAS/NYS/AMS 3종이다.
#  NASD/NYSE/AMEX는 주문·잔고 API에서 쓰는 '같은 거래소의 다른 표기'이고(위 US_DAY_MARKET_EXCD가
#  두 표기를 같은 주간코드로 매핑한다), BAQ/BAY/BAA는 그 거래소의 주간거래 코드다.
#  거래소를 모르는 종목은 후보를 순회하며 탐색하는데, 6개를 다 돌면 같은 거래소를 두 번씩
#  묻는 셈이라 최악 탐색 시간만 2배가 된다(모의투자 2 TPS에서는 그대로 체감 지연이 된다).
US_EXCD_CANONICAL = {
    "NAS": "NAS", "NASD": "NAS", "BAQ": "NAS",
    "NYS": "NYS", "NYSE": "NYS", "BAY": "NYS",
    "AMS": "AMS", "AMEX": "AMS", "BAA": "AMS",
}
# 시세 조회 시 순회할 거래소(중복 없는 표준 코드)
US_EXCD_PROBE_ORDER = ("NAS", "NYS", "AMS")


def us_excd_normalize(excd):
    """거래소 코드를 시세 조회용 표준 코드(NAS/NYS/AMS)로 정규화한다. 모르는 값은 그대로."""
    if not excd:
        return None
    return US_EXCD_CANONICAL.get(str(excd).upper(), excd)


def us_excd_probe_list(cached_ex=None):
    """시세 조회용 거래소 탐색 순서 — 캐시된 거래소를 앞에 두고 중복을 제거한다."""
    out = []
    for e in (cached_ex, *US_EXCD_PROBE_ORDER):
        c = us_excd_normalize(e)
        if c and c not in out:
            out.append(c)
    return out


def us_day_market_session():
    """미국 주간거래(데이마켓) 세션이 열려 있으면 그 세션의 '거래일'(YYYYMMDD), 아니면 None.

    야간 ATS 세션은 ET 20:00에 시작해 다음날 ET 04:00에 끝나고 '다음 거래일' 세션으로 귀속된다
    (ET 21:00 07/21의 체결은 07/22 세션 — KIS 주간거래 응답의 base가 07/21 정규장 종가인 것과 정합).
    세션 귀속일이 실제 거래일일 때만 열린 것으로 보므로, 금요일 밤(→토요일 귀속)·토요일 새벽은
    자동으로 닫힘 처리된다.
      ET 20:00~24:00 → 귀속일 = 다음 날 / ET 00:00~04:00 → 귀속일 = 당일
    """
    et = _api().now_us_eastern()
    if et.hour >= 20:
        target = et + timedelta(days=1)
    elif et.hour < 4:
        target = et
    else:
        return None
    try:
        if _api()._is_closed_day(target, 'US'):
            return None
    except Exception:
        return None
    return target.strftime('%Y%m%d')


def us_excd_candidates(cached_ex=None):
    """미국 시세 조회용 거래소 코드 후보(시도 순서).

    주간거래 세션 중에는 주간 코드를 먼저 시도하고, 값이 없으면 정규 코드로 폴백한다
    (세션 중이라도 해당 종목에 체결이 없으면 주간 코드는 빈 응답을 준다).
    캐시된 거래소가 있으면 그 코드(및 대응 주간 코드)를 최우선으로 두어 호출 수를 줄인다.

    [수정] 후보를 표준 코드(NAS/NYS/AMS)로 정규화한다. NASD/NYSE/AMEX는 같은 거래소의 다른
     표기라 6개를 모두 순회하면 거래소를 못 맞힌 종목마다 같은 곳을 두 번씩 묻게 된다.
     (조회 결과를 재사용하는 것이 아니라 헛호출만 없애므로 시세 신선도와는 무관하다)
    """
    regular = us_excd_probe_list(cached_ex)

    if not us_day_market_session():
        return regular

    day = []
    for e in regular:
        d = US_DAY_MARKET_EXCD.get(e)
        if d and d not in day:
            day.append(d)
    return day + regular


def market_today(is_overseas=False):
    """실시간 현재가 반영 시 '당일' 판정에 쓰는 시장 기준일(YYYYMMDD 문자열).

    국내는 시스템 로컬(KST), 해외(미국)는 동부시간(ET, 서머타임 자동판별) 기준이며,
    주말·공휴일(휴장일)이면 직전 거래일까지 되돌려 반환한다. 비거래일에 현재가(=최종 종가)로
    '가짜 당일 봉'이 추가되어 등락폭/등락률이 0으로 계산되는 문제를 막는다.

    [주간거래] 데이마켓 세션 중에는 그 세션의 귀속 거래일을 돌려준다. ET 20:00~24:00 구간에서
    ET 달력일(=직전 정규장일)을 그대로 쓰면 주간거래 체결가가 '직전 정규장 봉'을 덮어써
    확정된 과거 봉이 오염되므로, 세션 귀속일 기준으로 새 봉을 추가하게 한다.
    """
    if is_overseas:
        day_session = us_day_market_session()
        if day_session:
            return day_session

    dt = datetime.now() if not is_overseas else _api().now_us_eastern()
    country = 'US' if is_overseas else 'KR'
    key = (dt.strftime('%Y%m%d'), country)
    hit = _api()._TRADING_DAY_CACHE.get(key)
    if hit:
        return hit
    res = _api().last_trading_day(dt, country)
    #  [Fix 2026-09-05] 휴장 판정이 '조회 실패를 라이브러리로 메운 잠정값'이면 굳히지
    #   않는다. 굳히면 그 잠정값이 여기서 하루짜리 확정으로 승격돼, 아래 계층에서 재조회를
    #   해 봐야 소용이 없다(임시공휴일에 '거래일'로 굳는 방향이다).
    if not _api().holiday_answer_provisional(key[0], country):
        _api()._TRADING_DAY_CACHE[key] = res
    return res

def domestic_trading_session_open():
    """국내 거래 시간대(KRX 정규장 + NXT 프리/애프터)인가.

    거래일 08:00~20:00이면 True. 야간(20:00~익일 08:00)·주말·공휴일은 False —
    '모든 장이 끝난' 시간대다. _nxt_quote_phase()의 offhours 판정을 그대로 쓴다.
    """
    try:
        return _api()._nxt_quote_phase() != 'offhours'
    except Exception:      # noqa: BLE001 - 판정 실패는 '개장'으로 보고 종전 동작 유지
        return True


# NXT(넥스트레이드) 단독 거래시간 — **주문 구간**의 정본.
#  프리 08:00~08:50 / 애프터 15:30~20:00. 시세 쪽 경계(_nxt_quote_phase / domestic_session_phase
#  의 'nxt_pre' = 08:00~09:00)와 **일부러 다르다**: 08:50~09:00 은 NXT 가 KRX 시가 단일가에
#  맞춰 쉬는 시간이라(auto_trade.common.is_single_price_break 와 같은 경계) 주문은 NXT 가 아니라
#  KRX 동시호가로 들어간다 — 그 구간에서 시장가는 정상 접수된다.
#  시세는 '마지막 NXT 체결가를 계속 보여줄 것인가'를 묻고, 주문은 '지금 NXT 로 나가는가'를
#  묻는다. 두 물음의 답이 다르므로 경계도 다르다.
NXT_ORDER_WINDOWS = (("0800", "0850"), ("1530", "2000"))


def nxt_order_window(now=None):
    """지금 낸 국내 주문이 NXT(대체거래소)로 나가는 구간인가.

    NXT 는 시장가를 받지 않는다. True 면 호출부는 시장가를 현재가 지정가로 바꿔야 한다.
    """
    hm = (now or datetime.now()).strftime("%H%M")
    return any(lo <= hm <= hi for lo, hi in NXT_ORDER_WINDOWS)


# ==========================================================
# [세션 표기] 화면에 뿌리는 현재가가 '어느 시장 · 어느 세션'의 값인지 알리는 라벨
#  같은 표라도 08:30의 현재가는 NXT 프리마켓 체결가, 10:00은 KRX 정규장가, 22:00은
#  이미 마감된 KRX 종가다. 값만 보면 구분이 안 되므로 표 제목에 붙여 오독을 막는다.
# ==========================================================

def domestic_session_phase():
    """국내 시장 세션 단계.
       'nxt_pre'   : NXT 프리마켓 (08:00~09:00)
       'krx'       : KRX 정규장  (09:00~15:30)
       'nxt_after' : NXT 애프터마켓 (15:30~20:00)
       'closed'    : 거래일 야간 (20:00~익일 08:00)
       'holiday'   : 주말·공휴일
    시간 구간은 _nxt_quote_phase()와 동일하게 맞춘다(표기와 시세 처리의 경계 불일치 방지).
    """
    try:
        if _api().is_holiday_today():
            return 'holiday'
    except Exception:      # noqa: BLE001 - 휴장 판정 실패는 거래일로 보고 시간대만 판정
        pass
    hm = datetime.now().strftime('%H%M')
    if "0800" <= hm < "0900":
        return 'nxt_pre'
    if "0900" <= hm < "1530":
        return 'krx'
    if "1530" <= hm <= "2000":
        return 'nxt_after'
    return 'closed'


def us_session_phase():
    """미국 시장 세션 단계 (ET 기준, 서머타임 자동판별).
       'pre'     : 프리마켓 (ET 04:00~09:30)
       'regular' : 정규장   (ET 09:30~16:00)
       'after'   : 애프터마켓 (ET 16:00~20:00)
       'day'     : 데이마켓/주간거래 (ET 20:00~익일 04:00, KST 주간)
       'closed'  : 주말·휴장
    주문 세션(ord_dvsn) 자동판별이 이 함수를 쓴다 — 반대가 아니다. 여기가 정본이다.
    """
    et = _api().now_us_eastern()
    hm = et.strftime('%H%M')
    if hm >= "2000" or hm < "0400":
        # 야간 ATS는 '다음 거래일' 세션에 귀속되므로 귀속일 판정을 그대로 쓴다
        return 'day' if us_day_market_session() else 'closed'
    try:
        if _api()._is_closed_day(et, 'US'):
            return 'closed'
    except Exception:      # noqa: BLE001 - 판정 실패 시 시간대만으로 결정
        pass
    if hm < "0930":
        return 'pre'
    if hm < "1600":
        return 'regular'
    return 'after'


def market_session_label(is_overseas=False, is_domestic_etf=False):
    """현재 세션 라벨과 rich 스타일을 (텍스트, 스타일)로 반환한다.

    스타일 규약: 살아있는 대표시장(KRX 정규장·미국 정규장)=green,
    보조 세션(NXT 프리/애프터, 미국 프리/애프터/데이마켓)=yellow,
    마감·휴장·'값이 멈춘' 구간=dim.

    is_domestic_etf: 국내 ETF/ETN 표인가. NXT는 ETF/ETN을 취급하지 않으므로 NXT 시간대에도
      화면값이 KRX 종가에 멈춘다 — 세션 이름만 띄우면 '지금 거래 중'으로 오독된다.
      (매매 로직도 같은 판정으로 NXT 시간대 ETF 분석·주문을 스킵한다: auto_trade/trader.py)
    """
    if is_overseas:
        et = _api().now_us_eastern()
        clock = f"ET {et.strftime('%H:%M')}"
        phase = us_session_phase()
        if phase == 'regular':
            return (f"정규장 · {clock}", "green")
        if phase == 'pre':
            return (f"프리마켓 · {clock}", "yellow")
        if phase == 'after':
            return (f"애프터마켓 · {clock}", "yellow")
        if phase == 'day':
            return (f"데이마켓 · {clock}", "yellow")
        return (f"휴장(주말·공휴일) · {clock}", "dim")

    phase = domestic_session_phase()
    if phase == 'krx':
        return ("KRX 정규장", "green")
    if phase in ('nxt_pre', 'nxt_after'):
        name, krx = (("NXT 프리마켓", "KRX 개장 전") if phase == 'nxt_pre'
                     else ("NXT 애프터마켓", "KRX 마감"))
        # ETF/ETN은 NXT 비거래 → 세션은 열려 있어도 이 표의 값은 KRX 종가에서 멈춰 있다
        if is_domestic_etf:
            return (f"{name} · ETF 미거래(KRX 종가)", "dim")
        # 모의투자(VTS)는 NXT 미지원이라 이 시간대에도 화면값은 KRX 종가에 머문다
        return (f"{name} · {krx}", "yellow")
    # 마감·휴장: 화면 현재가의 기준이 설정(USE_KRX_CLOSE_AFTER_HOURS)에 따라 갈린다.
    #  ETF/ETN은 NXT 체결 자체가 없어 설정과 무관하게 항상 KRX 종가다.
    try:
        basis = "KRX 종가" if (is_domestic_etf or display_price_krx_fixed(False)) else "NXT 최종가"
    except Exception:      # noqa: BLE001
        basis = "최종가"
    head = "장 마감" if phase == 'closed' else "휴장(주말·공휴일)"
    return (f"{head} · {basis}", "dim")


def market_session_tag(is_overseas=False, is_domestic_etf=False):
    """표 제목 뒤에 붙일 세션 표기(rich 마크업). 판정 실패 시 빈 문자열."""
    try:
        text, style = market_session_label(is_overseas, is_domestic_etf)
    except Exception:      # noqa: BLE001 - 표기는 부가정보이므로 실패해도 화면을 막지 않는다
        return ""
    return f"  [dim]│[/dim] [{style}]{text}[/{style}]"


def market_session_token(is_overseas=False):
    """분석 결과 스냅샷의 '유효 구간' 식별자. 값이 달라지면 그 스냅샷은 만료다.

    (거래일, 세션 단계)로 만든다. 세션이 넘어가면 확정 종가·일봉이 바뀌어 같은 종목의
    상태 판정도 달라지므로, 이전 세션에 계산한 상태를 계속 보여주면 안 된다.
    달력일을 함께 넣어 연휴 등으로 같은 단계가 며칠 이어질 때 묵은 값이 남지 않게 한다.

    미국 데이마켓은 ET 자정을 넘겨 이어지므로, 달력일 대신 세션 귀속 거래일을 쓴다
    (그러지 않으면 세션 한복판인 ET 00:00에 스냅샷이 만료된다).
    """
    if is_overseas:
        phase = us_session_phase()
        day = (us_day_market_session() if phase == 'day' else None) \
            or _api().now_us_eastern().strftime('%Y%m%d')
        return f"US:{day}:{phase}"
    return f"KR:{datetime.now().strftime('%Y%m%d')}:{domestic_session_phase()}"


# KRX 정규장 마감(15:30) + 확정 일봉 반영 여유 10분. 이 시각 이후엔 '당일 확정 종가'가
# 어느 소스(KIS 일봉·pykrx·FDR)에서도 조회된다고 본다. 마감 직후 몇 분은 소스에 따라 아직
# 확정 봉이 없어, 그 순간 받은 캐시를 6시간 붙들면 같은 문제가 다시 생긴다.
_KRX_DAILY_SETTLED_HHMM = (15, 40)


def _krx_close_passed_at():
    """오늘 KRX 정규장 마감이 이미 지났으면 그 기준 시각(datetime), 아니면 None.

    '캐시된 일봉이 당일 확정 종가를 담고 있어야 하는가'의 기준선이다. 차트 캐시는 달력일
    단위로만 유효하므로(오늘자 항목만 조회) 같은 날 안에서의 비교만 하면 된다.
    휴장일(주말·공휴일)은 새로 마감된 세션이 없으므로 None — 검사 자체가 불필요하다.
    """
    try:
        now = datetime.now()
        if market_today(False) != now.strftime('%Y%m%d'):
            return None
        settled = now.replace(hour=_KRX_DAILY_SETTLED_HHMM[0], minute=_KRX_DAILY_SETTLED_HHMM[1],
                              second=0, microsecond=0)
        return settled if now >= settled else None
    except Exception:      # noqa: BLE001 - 판정 실패 시 종전 동작(캐시 유지)
        return None


def krx_last_settled_day():
    """가장 최근 '확정된' KRX 정규장 세션 일자(YYYYMMDD).

    오늘 정규장 마감(+확정 여유 15:40)이 지났으면 오늘, 아직이면 직전 거래일이다.
    '보유한 마지막 일봉이 최신인가'를 판정할 때 쓴다.

    [Fix 2026-07-28] 이 판정에 market_today()를 쓰면 자정~개장 전(00:00~09:00)에 깨진다.
     market_today는 평일이면 아직 열리지도 않은 '오늘'을 돌려주는데, 그 시각에 존재하는
     마지막 확정 봉은 '직전 거래일'이라 `last_bar >= market_today` 비교가 항상 실패했다.
     그 결과 USE_KRX_CLOSE_AFTER_HOURS=True인데도 새벽 시간대의 표시 현재가가 KRX 확정
     종가로 고정되지 않고 마지막 NXT 체결가로 노출됐다(2026-07-28 01:13 실측:
     삼성전자 255,000 = 전날 NXT 종가). 20:00~자정에는 두 값이 같아 증상이 없었다.
    """
    try:
        if _krx_close_passed_at() is not None:
            return market_today(False)
        return _api().last_trading_day(datetime.now() - timedelta(days=1), 'KR')
    except Exception:      # noqa: BLE001 - 판정 실패 시 종전 기준으로 폴백
        return market_today(False)


def chart_overlay_enabled(is_overseas=False):
    """지금 차트 마지막 봉에 실시간가를 반영해도 되는가 (지표 전용 게이트).

    가격을 아직 조회하기 전에 확인해, 어차피 버릴 현재가 API 호출 자체를 생략하는 데 쓴다.

    [Fix 2026-07-28] 국내는 KRX 정규장(09:00~15:30)에만 반영한다 — USE_KRX_CLOSE_AFTER_HOURS와
     무관하다. 지표는 '판단'의 축이므로 언제나 KRX 확정 봉 하나로만 계산한다. 자세한 근거는
     chart_overlay_price 참조.
    """
    if is_overseas:
        return True
    try:
        return _api()._nxt_quote_phase() == 'skip'
    except Exception:      # noqa: BLE001 - 판정 실패는 '정규장'으로 보고 종전 동작 유지
        return True


def display_price_krx_fixed(is_overseas=False):
    """화면 표시 현재가를 KRX 정규장 확정 종가로 고정해야 하는가 (표시 전용 게이트).

    USE_KRX_CLOSE_AFTER_HOURS(기본 True)면 '모든 장이 끝난 뒤'(NXT 애프터마켓 20:00 종료 후·
    주말·휴장일)의 화면 현재가를 KRX 확정 종가로 고정한다. 끄면 그 시간대에도 '마지막 실거래가'
    (= 전날 NXT 종가)를 그대로 노출해, 다음 NXT 개장 전까지 시간대별 최종가가 이어진다.

    NXT 거래시간(프리 08:00~09:00 / 애프터 15:30~20:00)에는 설정과 무관하게 NXT 현재가를
    보여준다 — 살아있는 시장의 가격이기 때문이다.

    ※ 지표는 이 설정과 무관하게 항상 KRX 확정 봉으로 계산한다(chart_overlay_enabled).
    ※ 주문 가격도 이 설정과 무관하게 항상 실시간가를 쓴다(체결 보장).
    """
    if is_overseas:
        return False
    if not getattr(config, 'USE_KRX_CLOSE_AFTER_HOURS', True):
        return False
    try:
        return _api()._nxt_quote_phase() == 'offhours'
    except Exception:      # noqa: BLE001 - 판정 실패 시 고정하지 않음(실시간가 노출)
        return False


def chart_overlay_price(price, is_overseas=False):
    """차트 마지막 봉·지표에 반영해도 되는 실시간가. 반영 불가면 0.0.

    국내는 KRX 정규장(09:00~15:30)에만 반영한다. 정규장 밖의 현재가는 NXT(대체거래소) 체결가
    이고, 이 값이 확정된 KRX 일봉의 종가를 덮어쓰면 EMA·RSI·CCI·ATR·52주 위치가 전부 함께
    흔들린다. NXT 거래량은 정규장의 수백분의 1이라 소수 체결이 봉의 종가·고가·저가를 정하는데,
    특히 ATR은 True Range를 통해 손절폭 → 포지션 크기 → 포트폴리오 리스크로 전파된다.
    과거 일봉이 전부 KRX 정규장 기준(pykrx/FDR)이므로 기준을 맞추는 쪽이 정합적이고,
    백테스트(KRX 일봉)와 실매매의 입력이 갈리지 않는다.

    실측 2026-07-24 SK텔레콤: KRX 종가 100,000 vs 애프터마켓 20:00 99,700
      → EMA5 94,805→94,705, RSI 61.8→61.54, CCI 231.6→230.10, 52주 위치 55.2%→54.8%
    (토스 캔들로 NXT가 과거 봉까지 섞였을 때는 ATR 6~15%·ADX 최대 9.45 왜곡 — krx_daily 참조)

    [Fix 2026-07-28] 종전에는 NXT 거래시간(08:00~20:00)에도 반영해, 그 시간대에 자동매매를
     운용하면 지표가 NXT 기준으로 계산됐다. '무엇을 사고팔지'는 KRX 확정 데이터로 판단하고,
     '지금 얼마인지'(손절·트레일링 트리거, 주문가)만 실시간가로 본다.

    ※ 이 게이트는 '지표' 전용이다.
      - 화면 표시 현재가: display_price_krx_fixed() 참조(설정으로 선택, NXT 장중엔 NXT가).
      - 손절·트레일링 트리거와 주문 가격: 호출부가 실시간가를 직접 쓰므로 영향 없다.
    """
    try:
        p = float(price or 0)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    return p if chart_overlay_enabled(is_overseas) else 0.0
