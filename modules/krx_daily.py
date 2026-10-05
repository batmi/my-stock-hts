"""국내 일봉을 'KRX 정규장 기준'으로 조회한다.

[출처 순서 · 2026-09-18] KRX Open API(확정분, data/krx_openapi.db 스냅샷) 1순위 → FDR 경로(네이버 일봉)
 폴백. Open API 의 종가는 정규장 15:30 종가고, 아직 안 실린 오늘·직전 영업일 봉만 FDR 경로로
 덧댄다(_fetch_openapi). 아래 실측·수치는 pykrx/FDR 시절(2026-07)의 것이라 순서 서술만 낡았을 뿐
 결론은 같다.

[pykrx 제거 · 2026-10-05] 마지막 폴백이던 pykrx(data.krx.co.kr 화면 스크래핑)를 걷어냈다. 약관
 위반으로 IP 를 차단당한 뒤 KRX_WEB_SCRAPING_ALLOWED(기본 OFF) 뒤에 있었지만, is_available 이
 게이트와 무관하게 패키지를 import 했고 pykrx 는 **import 만으로** 환경변수 KRX_ID/KRX_PW 계정으로
 그 사이트에 로그인했다. 종목명 단건 폴백(get_ticker_name)·과거 수급(get_investor_netbuy)·공식
 상장목록(_listing_map_from_krx)도 같은 원천이라 함께 뺐다.

[FDR 라이브러리를 부르지 않는다 · 2026-10-04] 'FDR' 표기는 그대로지만 FinanceDataReader 는 더 이상
 import 하지 않고, 그것이 받던 **같은 원천**을 직접 받는다(_fetch_fdr = 네이버 fchart, fdr_listing =
 FinanceData GitHub 캐시 CSV). 라이브러리는 ① 모든 요청에 타임아웃이 없어 원천이 응답하지 않으면 조회
 스레드가 무한정 멈추고 ② 국내 일봉을 count=6000(약 24년치, 197KB)으로 받아 오늘 봉 1~2개를 덧대는
 데도 종목마다 그만큼 내려받으며 ③ StockListing 은 매번 **data.krx.co.kr**(약관 위반으로 IP 를 차단당한
 도메인)에 최종 영업일을 먼저 묻고 ④ 실패하면 응답 HTML 을 print 로 화면에 찍었다. 받는 값은 같다
 (실측 4종목 243봉 OHLCV 100% 일치).

[왜 필요한가]
토스 캔들은 SOR 통합값이라 NXT 프리마켓(08:00~09:00)·애프터마켓(15:30~20:00) 체결이
일봉 OHLC에 그대로 섞인다. 2026-07-25 실측(삼성전자 60거래일, yfinance=KRX 공식 대조):

    토스 일봉 종가 KRX와 완전일치 3/60 (평균 괴리 1.16%)   ← NXT 거래 종목
    SK하이닉스 3/60(1.27%) / 에코프로비엠 4/60(1.29%)
    GS건설·카카오 60/60 (0.0000%)                         ← NXT 체결 없는 종목

월별 대조상 오염은 NXT 출범(2025-03)부터 시작되며 그 이전 일봉은 순수 KRX다.
지표 영향은 RSI·EMA에서는 미미하나(RSI 차이 0.3~2.1, EMA60 0.2~0.4%) ADX에서 크다
(에코프로비엠 25.1 vs 34.6 = 9.45 차이) — True Range가 장전·장후 체결로 부풀기 때문이다.

[왜 이 방식인가]
토스 분봉을 09:00~15:30으로 잘라 재구성하면 O/H/L이 KRX와 정확히 일치하지만(실측 확인),
720일치는 종목당 1,360페이지라 35종목 1회 갱신에 2.6시간(레이트리밋 5rps)이 걸리고
매매 경로와 차트 그룹 리밋을 공유해 시세 조회를 굶긴다. DB 영속화를 쓰지 않는 정책이므로
외부 KRX 소스를 쓴다 — 50종목 720일 전량이 pykrx 2.6초 / FDR 5.0초였다(2026-07 실측).

[정확도] pykrx(KRX 공식) 기준 FDR은 O/H/L/C 240/240 완전일치.
         yfinance는 O/H/L은 맞지만 종가가 종목당 2~4일 어긋나 지표용으로 부적합 → 미사용.

[역할 분담] 과거 일봉은 여기서(6시간 캐시), 당일 봉은 토스 실시간 현재가가 덮어쓴다
            (api._get_cached_chart의 오버레이). Open API·FDR은 장중 당일 값을 주지 않는다.

※ 토스 API가 KRX 기준 OHLC를 지원하면 이 모듈을 걷어내고 토스 경로로 되돌린다.
"""
import io
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

import config

logger = logging.getLogger(__name__)

# 6시간 캐시(과거 일봉은 불변, 당일 봉은 호출부가 실시간으로 덮어쓴다).
# api._get_cached_chart의 CHART_CACHE_TTL_MINUTES(기본 360)와 같은 주기를 기본값으로 쓴다.
_CACHE = {}                       # {code: {'df': df, 'ts': epoch, 'day': 'YYYYMMDD'}}
_CACHE_LOCK = threading.RLock()
_CACHE_MAX = 300
_FAIL = {}                        # {code: 마지막 실패 시각} — 연속 실패 시 재시도 폭주 방지
_FAIL_COOLDOWN_SEC = 300

_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume']

# 화면·지표 경로의 기본 조회 창(달력일). 250거래일 = 실측 373달력일이라 여유를 둔 값이다.
# (api._krx_daily_chart가 tail(250)으로 자르고, KIS 경로도 250봉에서 페이징을 멈춘다)
_CHART_FETCH_DAYS = 400

# FDR 경로가 직접 받는 원천(모듈 독스트링 [FDR 라이브러리를 부르지 않는다]).
#  (연결, 읽기) 초 — 네이버 일봉 응답은 실측 0.04~0.09초, GitHub CSV 는 1초 안쪽이다.
_HTTP_TIMEOUT = (5, 15)
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
_NAVER_DAILY_URL = ("https://fchart.stock.naver.com/sise.nhn?timeframe=day&count={count}"
                    "&requestType=0&symbol={code}")
_NAVER_ITEM_RE = re.compile(r'<item data="(.*?)" />', re.DOTALL)


def _http_get_text(url):
    """타임아웃을 건 GET. 200 이 아니면 OSError."""
    import requests
    r = requests.get(url, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise OSError(f"HTTP {r.status_code}: {url}")
    return r.text


def _normalize(df, source):
    """소스별 컬럼명을 ['date','open','high','low','close','volume']로 통일한다.

    date는 KIS/토스 일봉과 동일하게 'YYYYMMDD' 문자열로 맞춘다(지표·차트 경로가 이 형식을 가정).
    """
    if df is None or getattr(df, 'empty', True):
        return None

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    if 'date' not in df.columns:
        df = df.reset_index()
        df = df.rename(columns={c: 'date' for c in df.columns if str(c).lower() in ('index', 'date')})
    if 'date' not in df.columns:
        logger.debug(f"[KRX] {source} 날짜 컬럼 없음: {list(df.columns)[:6]}")
        return None

    missing = [c for c in ('open', 'high', 'low', 'close') if c not in df.columns]
    if missing:
        logger.debug(f"[KRX] {source} 필수 컬럼 누락 {missing}")
        return None
    if 'volume' not in df.columns:
        df['volume'] = 0

    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    df['date'] = df['date'].dt.strftime('%Y%m%d')

    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['open', 'high', 'low', 'close'])

    # 거래정지일 등 0원 봉은 지표를 망가뜨리므로 제거한다.
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)]
    if df.empty:
        return None

    df = df[_COLUMNS].drop_duplicates(subset=['date'], keep='last')
    return df.sort_values('date').reset_index(drop=True)


def _fetch_fdr(code, start, end):
    """FDR 경로 — FDR 의 국내 일봉 원천(네이버 fchart, 수정주가)을 **필요한 봉 수만큼** 직접 받는다.

    count 는 시작일부터 오늘까지의 평일 수 + 여유 10봉이다(거래일은 그보다 적으므로 모자라지 않는다).
    오늘 봉 덧대기는 12봉 남짓이라 FDR(6000봉) 대비 응답이 197KB → 2KB 로 준다.
    """
    s = datetime.strptime(str(start), '%Y%m%d')
    count = max(10, int((datetime.now() - s).days * 5 / 7) + 10)
    rows = _NAVER_ITEM_RE.findall(_http_get_text(_NAVER_DAILY_URL.format(count=count, code=code)))
    if not rows:
        return None
    df = pd.read_csv(io.StringIO("\n".join(rows)), sep="|", header=None, dtype={0: str},
                     names=['date', 'open', 'high', 'low', 'close', 'volume'])
    df = df[(df['date'] >= str(start)) & (df['date'] <= str(end))]
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce')
    return _normalize(df, 'FDR')


#  이음매 재기준 임계값. 같은 날의 시가는 네이버(정수 반올림)와 Open API(수정주가 소수)가 0.01% 안에서
#   맞는다(실측 2026-10-04, 4종목 85일 — 애프터마켓이 바꾸는 것은 종가·고저·거래량이고 시가는 아니다).
#   1% 를 넘게 벌어지면 오늘 수정주가 이벤트(권리락·분할·병합)가 네이버에만 반영된 것이다.
_SEAM_REBASE_MIN = 0.01


def naver_daily(code, start, end):
    """FDR 경로(네이버 일봉)의 공개 이름 — 감사 도구가 종전 fdr.DataReader 자리에 쓴다(tools/audit_common)."""
    return _fetch_fdr(code, start, end)


def _rebase_to_tail(code, base, tail, last):
    """오늘 권리락·분할이 있으면 Open API 확정분을 덧댈 봉의 수정주가 기준으로 맞춘다.

    [왜 · 2026-10-04] Open API 의 수정주가는 그 날의 기준가(종가 − 전일대비)로 만든다 — 이벤트 당일
     D 의 행은 다음 영업일 08:00 에야 실리므로, D 하루 동안 확정분(~D-1)은 **옛 기준**이다. 거기에
     네이버의 D 봉(새 기준)을 붙이면 50:1 분할이면 −98%, 권리락이면 수 % 의 가짜 갭이 생겨 ATR·EMA·
     52주 위치·손절선이 하루 동안 통째로 틀어진다. 네이버는 D 에 과거 봉까지 다시 계산해 주므로,
     겹치는 D-1 시가의 비율이 곧 그 이벤트의 배율이다. 다음 날 Open API 가 같은 배율을 스스로
     적용하므로 그때부터는 이 보정이 1.0 이 되어 아무 일도 하지 않는다.
    """
    hit = tail[tail['date'] == last]
    try:
        b_open = float(base['open'].iloc[-1] or 0)
        t_open = float(hit['open'].iloc[-1]) if not hit.empty else 0.0
    except (TypeError, ValueError):
        return base
    if b_open <= 0 or t_open <= 0:
        return base
    ratio = t_open / b_open
    if abs(ratio - 1.0) < _SEAM_REBASE_MIN:
        return base
    out = base.copy()
    for col in ('open', 'high', 'low', 'close'):
        out[col] = out[col] * ratio
    out['volume'] = out['volume'] / ratio
    out.attrs.update(base.attrs)
    out.attrs['seam_rebase'] = ratio
    logger.info(f"[KRX] {code} 오늘 수정주가 이벤트 — Open API 확정분(~{last})을 네이버 기준으로 ×{ratio:.4f} "
                f"맞춘다(다음 영업일 Open API 게시 뒤엔 불필요)")
    return out


def _fetch_openapi(code, lookback_days):
    """KRX Open API 확정 일봉 + 오늘(및 아직 안 실린 직전 영업일) 봉은 FDR 로 덧댄다.

    Open API 는 전일까지의 확정분만 있다(다음 영업일 08:00 갱신). 화면·판정 경로는 오늘 봉을
    기대하므로(장중엔 진행 중인 봉, 마감 후엔 확정 봉) 마지막 확정일 이후만 FDR 에서 받아
    이어 붙인다. FDR 이 죽으면 확정분만 돌려준다 — 실시간가 오버레이(apply_realtime_price)가
    오늘 봉을 만들어 주므로 판정은 이어진다. 구간이 완전하지 않으면(첫 적재 전) None.
    """
    from modules import krx_openapi
    if not krx_openapi.is_configured():     # 쿨다운은 저장소 읽기를 막지 않는다
        return None
    base = krx_openapi.stock_daily(code, lookback_days)
    if base is None or base.empty:
        return None
    last = str(base['date'].iloc[-1])
    # Open API 의 종가는 정규장 15:30 단일가다(실측 2026-09-14 삼성전자 249,000 = KIS 일봉·거래소 기준가,
    #  포털/FDR 248,500 은 애프터 최종가). 이 날짜까지는 정규장 종가 보정(api.toss._toss_apply_regular_closes)
    #  이 손댈 필요가 없다 — 덧댄 FDR 봉만 대상이다.
    base.attrs['official_close_upto'] = last
    today = datetime.now().strftime('%Y%m%d')
    # 저장소에 봉이 실린 마지막 날(span_end — 휴장·미게시의 빈 날은 제외, krx_openapi.last_data_dd)에
    #  이 종목의 봉이 없으면 그날 거래가 없었다(상장폐지·거래정지). 그런 종목에 FDR 을 덧대면
    #  네이버가 폐지 다음 날에 만드는 거래량 0 유령 봉이 붙는다(실측 2026-09-20 000075: 07-31 폐지
    #  뒤 08-01 봉) → 덧대지 않는다. 쿨다운으로 span_end 자체가 앞당겨진 경우는 종목이 살아 있으니
    #  종전대로 덧댄다.
    if last < today and last >= str(base.attrs.get('span_end') or ''):
        try:
            tail = _fetch_fdr(code, last, today)
        except Exception as ex:     # noqa: BLE001 - 덧대기 실패는 확정분으로 충분하다
            logger.debug(f"[KRX] Open API 뒤 FDR 덧대기 실패({code}): {ex}")
            tail = None
        if tail is not None and not tail.empty:
            base = _rebase_to_tail(code, base, tail, last)
            tail = tail[tail['date'] > last]
            if not tail.empty:
                merged = pd.concat([base, tail[_COLUMNS]], ignore_index=True)
                merged.attrs.update(base.attrs)
                merged.attrs['source'] = 'OPENAPI+FDR'
                return merged
    base.attrs['source'] = 'OPENAPI'
    return base


def is_domestic_code(code):
    """국내 6자리 종목코드인가.

    [Fix] 종전엔 isdigit()만 봐서 문자가 섞인 코드(KODEX K방산TOP10 '0080G0' 등 최근 상장
     ETF/ETN)를 전부 배제했다. 그 종목은 KRX 공식 일봉을 못 받고 토스 캔들로 폴백하는데,
     토스 캔들에는 NXT 연장 체결이 섞여 ATR이 6~15% 부풀고 ADX가 최대 9.45 어긋난다
     (ATR은 손절폭 → 포지션 크기 → 포트폴리오 리스크로 전파된다).
     실측 2026-07-29: pykrx·FDR 모두 '0080G0'을 정상 조회한다(240봉, 종가 9,560 일치).
     KRX 코드는 '숫자로 시작하는 6자리 영숫자'이므로 해외 티커(AAPL 등)와도 구분된다.
    """
    c = str(code or '').strip()
    return len(c) == 6 and c[0].isdigit() and c.isalnum()


def get_daily(code, lookback_days=None, use_cache=True):
    """KRX 정규장 기준 일봉 DataFrame(['date','open','high','low','close','volume']).

    실패 시 None을 반환한다(호출부가 토스 캔들로 폴백). 국내 6자리 종목코드만 지원한다.
    """
    code = str(code or '').strip()
    if not is_domestic_code(code):
        return None

    if lookback_days is None:
        # 화면·지표 경로는 250봉만 쓴다(_krx_daily_chart가 tail(250)) — KIS 경로도 250봉에서
        # 페이징을 멈추므로 모드 간 조회량을 맞춘다. 종전엔 730일(약 490봉)을 받아 절반을
        # 버렸다. 250거래일 ≈ 373달력일이라 _CHART_FETCH_DAYS면 여유 있게 채운다.
        # 설정값(CHART_LOOKBACK_DAYS)이 더 짧으면 사용자 의도를 존중해 그대로 따른다.
        # 백테스트는 lookback_days를 명시 전달하므로 이 상한에 걸리지 않는다.
        configured = config.INDICATOR_PARAMS.get("CHART_LOOKBACK_DAYS", 730)
        try:
            configured = int(configured or 730)
        except (TypeError, ValueError):
            configured = 730
        lookback_days = min(configured, _CHART_FETCH_DAYS)

    now = time.time()
    today = datetime.now().strftime('%Y%m%d')

    lookback_days = int(lookback_days)

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(code)
            # [중요] 캐시본의 조회 기간이 요청보다 짧으면 재사용하지 않는다.
            #  차트 경로(730일)가 먼저 캐시해 두면 백테스트(수년)가 짧은 시계열을 받아
            #  기간이 조용히 잘린 채 검증되기 때문이다. 반대로 더 긴 캐시본은 그대로 쓴다.
            if (hit and hit.get('day') == today
                    and (now - hit['ts']) < _cache_ttl_sec()
                    and hit['ts'] >= _session_settled_ts()
                    and hit.get('lookback', 0) >= lookback_days):
                return hit['df'].copy()
            failed_at = _FAIL.get(code, 0)
        if failed_at and (now - failed_at) < _FAIL_COOLDOWN_SEC:
            return None

    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    s, e = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')

    df = None
    # 순서: Open API(공식·확정분, 오늘 봉은 FDR 로 덧댐) → FDR(네이버). 모듈 독스트링 참조.
    sources = [('OPENAPI', lambda c, s_, e_: _fetch_openapi(c, lookback_days)), ('FDR', _fetch_fdr)]
    for name, fetch in sources:
        try:
            df = fetch(code, s, e)
        except Exception as ex:     # noqa: BLE001 - 어느 소스가 죽어도 다음 소스로 넘어간다
            logger.debug(f"[KRX] {name} 조회 실패({code}): {ex}")
            df = None
        if df is not None and not df.empty:
            df.attrs.setdefault('source', name)
            break

    if df is None or df.empty:
        logger.warning(f"[KRX] 일봉 조회 실패({code}) — Open API·FDR 모두 실패, 토스 캔들로 폴백")
        with _CACHE_LOCK:
            _FAIL[code] = now
        return None

    with _CACHE_LOCK:
        _FAIL.pop(code, None)
        _CACHE[code] = {'df': df, 'ts': now, 'day': today, 'lookback': lookback_days}
        if len(_CACHE) > _CACHE_MAX:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1]['ts'])[:len(_CACHE) - _CACHE_MAX]
            for k, _ in oldest:
                _CACHE.pop(k, None)

    return df.copy()


def _session_settled_ts():
    """직전 정규장 마감(당일 확정 일봉이 존재하는 시각)의 epoch. 마감 전·휴장일이면 0.0.

    이 시각 이전에 받아 둔 캐시는 당일 확정 종가를 담을 수 없으므로 TTL(6시간) 안이라도
    재사용하면 안 된다. 장 마감 후에도 전일 종가가 '현재가'로 표시되던 문제의 한 축이다
    (판정 근거는 api._krx_close_passed_at / api._chart_disk_get 주석 참조).
    """
    try:
        import api                              # 지연 임포트 — api가 이 모듈을 지연 임포트한다
        closed_at = api._krx_close_passed_at()
        return closed_at.timestamp() if closed_at else 0.0
    except Exception:       # noqa: BLE001 - 판정 실패는 '검사 없음'으로 두어 종전 동작 유지
        return 0.0


def _cache_ttl_sec():
    """차트 캐시와 동일 주기(기본 6시간). 0 이하면 캐시를 쓰지 않는다."""
    try:
        minutes = float(getattr(config, 'CHART_CACHE_TTL_MINUTES', 360))
    except (TypeError, ValueError):
        minutes = 360.0
    return max(0.0, minutes * 60)


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAIL.clear()
    with _LISTING_LOCK:
        _LISTING['map'] = None
        _LISTING['ts'] = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 상장 종목 마스터 (종목코드 → 종목명·시가총액)
#  - 용도: AI가 출력한 '종목명(6자리코드)' 표기의 존재/일치 검증(할루시네이션 차단).
#    LLM은 종목코드를 지어내거나 이름-코드를 뒤바꾸는 실패 모드가 흔하고,
#    프롬프트 지시만으로는 막히지 않으므로 출력 후 대조가 유일한 방어선이다.
#  - KRX Open API 종목기본정보로 전 종목(약 2,900행)을 한 번에 받고 {코드: (이름, 시총, 시장)} dict만
#    남겨 라즈베리파이 메모리를 아낀다.
#  - 실패하면 None을 반환해 호출부가 '검증 불가'로 처리하도록 한다(없는 종목으로 오판하면 안 된다).
# ─────────────────────────────────────────────────────────────────────────────
_LISTING = {'map': None, 'ts': 0.0}
_LISTING_LOCK = threading.RLock()
_LISTING_FAIL_TS = [0.0]


def _listing_map_from_openapi():
    """KRX Open API 종목기본정보 + 최신 일별매매 시총 {코드: {'name','marcap','market'}}. 실패 시 None."""
    try:
        from modules import krx_openapi
        if not krx_openapi.is_configured():
            return None
        raw = krx_openapi.listing_map()
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[KRX] Open API 상장목록 실패: {e}")
        return None
    if not raw:
        return None
    return {code: {'name': v.get('name', ''), 'marcap': float(v.get('marcap') or 0.0),
                   'market': str(v.get('market') or '').upper()}
            for code, v in raw.items() if is_domestic_code(code)} or None


def get_listing_map(use_cache=True):
    """{'005930': {'name': '삼성전자', 'marcap': 1458646512696000}, ...} 또는 None.

    **KRX Open API 종목기본정보 하나**(코넥스까지 덮는다).

    [FDR 폴백 제거 · 2026-10-05] 종전엔 Open API 가 실패하면 FDR 상장목록 CSV(제3자 GitHub 캐시)로
     대신했다. 그 저장소는 2026-09-17 이후 갱신이 멈춰 폴백으로서도 날마다 낡아 가고, 'KOSDAQ GLOBAL'
     표기 같은 어휘 차이를 따로 맞춰야 했다. 시장 판정(analysis.get_market_type)은 KIS 마스터가
     1순위라 이 목록이 없어도 이어진다. (탐색 메뉴의 업종·폐지 목록은 대체 원천이 없어 fdr_listing 을
     계속 쓴다 — manage/discover.py)

    None은 '조회 실패'를 뜻한다 — 상장 종목이 없다는 뜻이 아니므로 호출부는
    이 경우 검증을 건너뛰어야 한다.
    """
    now = time.time()
    if use_cache:
        with _LISTING_LOCK:
            hit = _LISTING.get('map')
            if hit and (now - _LISTING.get('ts', 0.0)) < _cache_ttl_sec():
                return hit
            if now - _LISTING_FAIL_TS[0] < _FAIL_COOLDOWN_SEC:
                return None

    result = _listing_map_from_openapi()

    if not result:
        _LISTING_FAIL_TS[0] = now
        return None

    with _LISTING_LOCK:
        _LISTING['map'] = result
        _LISTING['ts'] = now
    return result


def get_market(code):
    """종목코드 → 'KOSPI' | 'KOSDAQ' | 'KONEX'. 판정 불가면 None.

    시장 구분은 '무엇과 비교해 매수할지'를 정한다 — 시장 필터(80일선)와 적응형 임계값이
    이 값으로 코스피 지수를 볼지 코스닥 지수를 볼지 고른다. 그래서 모를 때 KOSPI 로
    단정하면 코스닥 종목이 조용히 코스피 기준으로 판정된다. 여기서는 모르면 None 이다.
    """
    code = str(code or '').strip()
    if not is_domestic_code(code):
        return None
    listing = get_listing_map()
    if not listing:
        return None
    entry = listing.get(code) or {}
    market = str(entry.get('market') or '').strip().upper()
    return market if market in ('KOSPI', 'KOSDAQ', 'KONEX') else None


def get_ticker_name(code):
    """종목코드 → 종목명. 상장 목록에 없으면 '' , 조회 자체가 불가하면 None."""
    code = str(code or '').strip()
    if not is_domestic_code(code):
        return ''

    listing = get_listing_map()
    if listing is None:
        return None
    entry = listing.get(code)
    return entry['name'] if entry else ''


# ---------------------------------------------------------------------------
# FDR 상장목록 — 캐시 지연을 견디며 받는다
# ---------------------------------------------------------------------------
#  [무엇이 문제인가 · 2026-09-08 진단]
#   `fdr.StockListing` 은 KRX 에서 목록을 받지 않는다. ① KRX 에 '최종 영업일(max_work_dt)'을
#   묻고 ② **그 날짜의 CSV** 를 제3자 GitHub 저장소에서 내려받는다. ②는 장 마감 뒤 사람이
#   올리는 파일이라, KRX 가 오늘을 영업일이라고 답한 순간부터 그 파일이 올라오기 전까지
#   **매 거래일** 404 가 난다. 실측(2026-09-08): 오늘 404 · 어제부터 그 이전은 전부 200.
#
#   즉 죽은 엔드포인트가 아니라 **날마다 되풀이되는 시차**다. 그래서 '오늘 안 되면 실패'가
#   아니라 '있는 것 중 가장 최근 것'을 쓴다. 상장목록은 하루 사이에 거의 변하지 않으므로
#   (시총 순위·업종·소속부) 하루 스테일이 기능을 바꾸지 않는다 — 아예 못 쓰는 것보다 낫다.
#
#   [주의] KRX-DELISTING 은 FDR 이 이 404 를 **삼켜서** 빈 프레임을 돌려준다
#   (`except Exception: df = pd.DataFrame()`). '폐지 종목 0개'는 조용히 틀린 답이므로
#   호출부는 빈 목록을 실패로 쳐야 한다.
_FDR_CACHE_BASE = ("https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache"
                   "/refs/heads/master/data/listing")
_FDR_CACHE_DIR = {"KRX": "krx", "KRX-DESC": "desc", "KRX-DELISTING": "delisting"}

#  캐시 저장소에서 며칠까지 거슬러 찾는가(목록 종류별, 기본 10일).
#   [2026-10-03] 업종 목록(desc)·폐지 목록(delisting)은 저장소에서 2026-09-17 이후 갱신이 멈췄다(KRX 스크래핑
#   차단 시점과 같음 — 회복을 기대하지 않는다). 10일 창으로는 못 찾아 탐색 메뉴(7-4)와 업종을 쓰는 감사
#   도구가 통째로 죽었다. 업종·폐지 이력은 몇 달 단위로도 거의 안 바뀌므로 오래된 파일을 쓰고, 기준일은
#   last_listing_date 로 호출부가 밝힌다. 하루 거슬러 갈 때마다 404 한 번(실측 60일 창 3.8초)이라 무한정 넓히진 않는다.
LISTING_LOOKBACK_DAYS = {"KRX-DESC": 120, "KRX-DELISTING": 120}
_DEFAULT_LISTING_LOOKBACK = 10

#  **어느 날짜 파일**을 받았는지. 스냅샷 메타에 적어 두면 여러 목록이 같은 날짜로 고정됐는지
#  나중에 대조할 수 있다(2026-10-04 부터 언제나 채워진다 — 날짜를 모르는 '정상 경로'가 없어졌다).
_LAST_LISTING_DATE = {}


def last_listing_date(kind):
    """직전 fdr_listing 이 받은 파일의 날짜('YYYY-MM-DD'). 실패했으면 None."""
    return _LAST_LISTING_DATE.get(str(kind).upper())


def fdr_listing(kind, lookback=None, on=None):
    """FDR 상장목록(kind: 'KRX' | 'KRX-DESC' | 'KRX-DELISTING'). 못 받으면 None.

    FDR 캐시 저장소(GitHub CSV)에서 기준일부터 거슬러 **올라와 있는 가장 최근 날짜**로 받는다.
    스키마는 FDR StockListing 과 같다 — 그 후처리가 'KRX' 는 reset_index 뿐이고 'KRX-DESC' 는
    ListingDate 파싱뿐이라 여기서 맞춘다.

    [2026-10-04] fdr.StockListing 을 먼저 부르던 '정상 경로'를 걷어냈다. 그 함수가 하는 일은 같은
    CSV 를 받는 것인데, 날짜를 알아내려고 매번 data.krx.co.kr 에 타임아웃 없이 묻는다(모듈 독스트링).
    오늘 파일이 있으면 첫 시도에서 받으므로 결과는 같다.

    on: 기준일('YYYY-MM-DD' 또는 date). 주면 그 날짜부터 거슬러 찾는다 — 감사 유니버스를 여러
      목록에 걸쳐 **같은 날짜로 고정**할 때 쓴다([[audit-universe-reproducibility]]).
    """
    key = str(kind).upper()
    sub = _FDR_CACHE_DIR.get(key)
    if lookback is None:
        lookback = LISTING_LOOKBACK_DAYS.get(key, _DEFAULT_LISTING_LOOKBACK)
    #  직전 호출의 날짜가 남으면 실패한 호출이 옛 날짜를 들고 있게 된다.
    _LAST_LISTING_DATE.pop(key, None)
    if not sub:
        return None
    if on is None:
        start = datetime.now().date()
    elif isinstance(on, str):
        start = datetime.strptime(on, "%Y-%m-%d").date()
    else:
        start = on
    for i in range(max(1, int(lookback))):
        d = (start - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            text = _http_get_text(f"{_FDR_CACHE_BASE}/{sub}/{d}.csv")
            df = pd.read_csv(io.StringIO(text), index_col=0,
                             dtype={"Code": str, "Symbol": str, "Dept": str,
                                    "ChangeCode": str, "MarketId": str})
        except Exception:
            continue            # 그 날짜는 아직/영영 없다(주말·휴장일 포함)
        if df is None or not len(df):
            continue
        df = df.reset_index(drop=True)
        if "ListingDate" in df.columns:
            df["ListingDate"] = pd.to_datetime(df["ListingDate"], errors="coerce")
        if i:
            logger.info(f"[KRX] FDR {kind} — 기준일({start}) 캐시 파일이 없어 "
                        f"{d} 파일을 씁니다(상장목록은 하루 사이 거의 변하지 않습니다).")
        _LAST_LISTING_DATE[key] = d
        return df
    logger.warning(f"[KRX] FDR {kind} — 최근 {lookback}일 안에 받을 수 있는 캐시가 없습니다.")
    return None
