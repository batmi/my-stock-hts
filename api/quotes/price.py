"""현재가·호가·수급 — 판단에 들어가는 시세 일체.

현재가와 호가, 체결강도·투자자별 매매동향·공매도·외국인 지분율, 해외 종목 상세와
매수/매도 가능 수량이 여기 모인다. 자동매매의 트리거와 주문 가격이 모두 이 값을 본다.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import config
from core import constants
from core import context
from core import indicators
from core import utils
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

def _overseas_tv_fallback_price(code, fast_info_ttl=3.0):
    """해외 현재가: KIS/토스 조회 실패 시 TradingView 시세로 폴백한다.
    성공 시 KIS get_current_price_data 형태({rt_cd:'0', output:{...}})를, 실패 시 None을 반환한다.
    가격·등락률 일관성을 위해 diff/rate는 전일 종가(base) 기준으로 함께 재계산해 채운다.
    (yfinance는 장외가 미제공이라 src=='tv'만 채택. mode 1/2/3 공통 폴백 경로)
    """
    try:
        fi = _api().get_yf_fast_info(code, ttl=fast_info_ttl)
        if fi and fi.get('src') == 'tv' and fi.get('last_price'):
            last_v = float(fi['last_price'])
            prev_v = fi.get('regular_market_previous_close')
            if last_v > 0:
                out_o = {'last': str(last_v), '_src': 'tv_fallback'}
                if prev_v is not None and float(prev_v) > 0:
                    prev_v = float(prev_v)
                    out_o['base'] = str(prev_v)
                    out_o['diff'] = str(round(last_v - prev_v, 4))
                    out_o['rate'] = str(round((last_v - prev_v) / prev_v * 100, 2))
                return {'rt_cd': '0', 'output': out_o}
    except Exception as e:
        logger.debug(f"[API] 해외 현재가 TV 폴백 실패({code}): {e}")
    return None


def get_current_price_data(code, is_overseas, include_nxt=True, cache_ttl=3.0, fast_info_ttl=3.0):
    """현재가 조회. include_nxt=False면 NXT(대체거래소) 보조 호출을 생략한다.
    (대량 개요 조회 시 종목당 1콜을 줄여 전역 TPS 부담을 낮춘다. 주문/상세 경로는 기본값 True 유지)
    cache_ttl: 캐시 재사용 허용 시간(초). 개요/예열 경로는 더 큰 값으로 백그라운드 예열 데이터를 재사용한다.
    fast_info_ttl(해외 전용): KIS 조회 실패 시 TV 폴백에 쓰는 fast_info 캐시 허용 시간(초).
      개요(대량) 경로는 TV 일괄 예열 캐시를 재사용하도록 크게(예: 30초) 주고,
      주문/개별 분석 경로는 기본 3초로 실시간성을 유지한다.
      (해외 현재가는 KIS last/diff/rate를 1차 신뢰하며, TV는 KIS 실패 시에만 사용. yfinance 미사용)
    """
    if config.session.is_toss:
        return _api()._toss_current_price_data(code, is_overseas)
    # NXT 포함 여부에 따라 캐시를 분리하여 주문 경로(NXT 포함)와 개요 경로(NXT 미포함)가 섞이지 않게 한다.
    cache_key = f"cp_{code}_{is_overseas}" if include_nxt else f"cp_{code}_{is_overseas}_nonxt"
    cached = _api()._get_micro_cache(cache_key, ttl=cache_ttl) # [수정] 실시간 시세 반영을 위해 캐시 유지 시간을 3초로 단축
    if cached: return cached

    if not is_overseas:
        res = _api().call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"], "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=3)
        if res.get('rt_cd') == '0':
            # [추가] 액면분할 종목의 52주 고가/저가가 KIS API에서 원주가로 반환되는 스펙 한계 보정
            try:
                out = res['output']
                curr = _api().safe_float(out.get('stck_prpr'), default=0.0)
                w52h = _api().safe_float(out.get('w52_hgpr'), default=0.0)
                # 52주 고점과 현재가가 2.5배 이상 차이나면(액면분할 의심), 
                # 차트 데이터를 조회하여 52주 최고/최저가를 수정주가 기준으로 덮어씌움
                if curr > 0 and w52h > 0 and (w52h / curr) > 2.5:
                    df = _api().get_chart_data(code, is_overseas=False)
                    if df is not None and not df.empty:
                        # 창 정의는 core.indicators 하나만 쓴다(화면·판정과 같은 52주).
                        real_h52, real_l52 = indicators.w52_band(df)
                        if real_h52 > 0 and real_l52 > 0:
                            out['w52_hgpr'] = str(int(real_h52))
                            out['w52_lwpr'] = str(int(real_l52))
            except Exception as e:
                logger.debug(f"[API] 52주 고가 보정 중 오류: {e}")

            # [추가] NXT(대체거래소) 시세 조회 및 병합 (NX 코드 사용)
            # [수정] 모의투자(VTS)는 NXT 미지원이라 fetch_nxt_price가 0을 반환한다.
            # [최적화] 정규장(09:00~15:30)엔 KRX가 대표가이므로 NXT 보조호출을 생략(_nxt_quote_window)해
            #  종목당 호출을 절반으로 줄인다. NXT 단독시간(프리/애프터)에만 NXT를 조회한다.
            out = res.get('output', {})
            # 모의투자(VTS)는 NXT 미지원 → 항상 KRX 종가. 실전만 NXT 병합/회상.
            if include_nxt:
                phase = _api()._nxt_quote_phase()
                if phase in ('active', 'offhours'):
                    # 거래시간이든 야간이든 KIS 라이브 NXT가를 먼저 시도한다.
                    nxt_price = _api().fetch_nxt_price(code)
                    if nxt_price > 0:
                        out['ats_prpr'] = str(nxt_price)
                        _api()._nxt_remember_close(code, nxt_price)         # 받은 값은 항상 기억
                    elif phase == 'offhours':
                        # 야간에 KIS가 NXT를 안 주면 기억한 마지막 NXT 종가를 노출(다음 개장 전까지)
                        recalled = _api()._nxt_recalled_close(code)
                        if recalled > 0:
                            out['ats_prpr'] = str(recalled)

            _api()._set_micro_cache(cache_key, res)
        return res
    
    if is_overseas:
        cached_ex = config.session.exchange_cache.get(code)
        # [주간거래] 데이마켓 세션 중이면 주간 거래소 코드(BAQ/BAY/BAA)를 먼저 시도한다.
        #  이 분기가 없으면 세션 내내 직전 정규장 마감가가 그대로 굳는다.
        exchanges = _api().us_excd_candidates(cached_ex)

        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            data = _api().call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["PRICE"], "overseas", "quotations", "price", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                if float(data.get('output', {}).get('last', 0) or 0) > 0:
                    # [주간거래] 캐시·stock.json에는 항상 '정규장' 코드를 저장한다.
                    #  주간 코드(BAQ 등)가 저장되면 정규장 시간대 조회와 주문 경로가 깨진다.
                    reg_excd = _api().US_REGULAR_EXCD.get(excd, excd)
                    if cached_ex != reg_excd: config.session.update_cache_and_save(code, reg_excd)

                    # [수정] 장외(프리/애프터) 시세: KIS 응답을 1차 신뢰한다.
                    #  KIS 현재체결가의 last/diff/rate는 프리·애프터장에도 갱신되므로, 기존의
                    #  TV/yfinance 덮어쓰기(특히 yfinance fast_info는 정규장가만 제공)가 오히려
                    #  신선한 KIS 가격을 정지시키고 등락률과 불일치를 만들던 문제를 제거.
                    #  단, last만 동결되고 rate는 갱신되는 비정합 응답에 대비해 KIS 자체 필드로 역산 보정한다.
                    try:
                        out_o = data['output']
                        base_v = float(out_o.get('base', 0) or 0)
                        rate_v = float(out_o.get('rate', 0) or 0)
                        last_v = float(out_o.get('last', 0) or 0)
                        if base_v > 0 and rate_v != 0:
                            expected = base_v * (1 + rate_v / 100.0)
                            # 0.1% 이상 괴리 = last 동결 감지 (rate 반올림 오차 최대 0.005%의 20배 여유)
                            if abs(last_v - expected) / base_v > 0.001:
                                out_o['last'] = str(round(expected, 4))
                                logger.debug(f"[API] {code} 해외 last 정합성 보정: {last_v} -> {expected:.4f} (base {base_v}, rate {rate_v}%)")
                    except Exception as e:
                        logger.debug(f"[API] 해외 last 정합성 보정 오류({code}): {e}")

                    _api()._set_micro_cache(cache_key, data)
                    return data

        # [폴백] KIS 전 거래소 조회 실패 시에만 TradingView 시세로 대체한다.
        #  (yfinance는 장외가 미제공이라 사용하지 않음. fast_info_ttl: 개요 경로는 예열 캐시 재사용)
        res_tv = _overseas_tv_fallback_price(code, fast_info_ttl)
        if res_tv is not None:
            _api()._set_micro_cache(cache_key, res_tv)
            return res_tv

        res_err = {'rt_cd': '9999'}
        return res_err
    return {'rt_cd': '9999'}

def get_current_price(code, is_overseas):
    """현재가 단일 값 조회 (실패 시 0 반환)"""
    # [추가] 지수 목록 상품(KRX 금현물)은 증권사에 종목 코드가 없다 — 포지션 분석처럼
    #  '종목 자리'로 들어오는 경로가 지수 화면과 같은 전용 소스를 보게 한다.
    if _api().index_source_kind(code) == 'krx_gold':
        from modules import analysis
        gold_df = analysis.get_krx_gold_data(config.KRX_GOLD_TICKERS[code])
        if gold_df is not None and not gold_df.empty:
            return float(gold_df['close'].iloc[-1])
        return 0
    # [WS] 실시간 피드에 신선한 현재가가 있으면 REST 호출 없이 즉시 반환(TPS 절감).
    #  미구독/끊김/정규장 외(KRX 정지)면 None → 아래 REST 경로로 자동 폴백한다.
    if not is_overseas and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
        try:
            from brokers import realtime
            p = realtime.get_feed().get_price(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if p and p > 0:
                return p
        except Exception:
            pass
    data = get_current_price_data(code, is_overseas)
    if data.get('rt_cd') == '0':
        output = data.get('output', {})
        if is_overseas:
            try:
                return _api().safe_float(output.get('last'), default=0.0)
            except Exception as e:
                logger.debug(f"get_current_price float cast error: {e}")
                return 0.0
        else:
            ats_val = output.get('ats_prpr')
            if ats_val and _api().safe_int(ats_val) > 0:
                return _api().safe_int(ats_val)
            return _api().safe_int(output.get('stck_prpr'))
    return 0

def get_price_limits(code):
    """국내 종목의 (상한가, 하한가). 구하지 못하면 (0, 0).

    주문가가 가격제한폭을 벗어나면 접수 자체가 거부되므로 지정가를 만들 때 클램프에 쓴다
    (utils.clamp_to_price_limit). ±30%를 직접 계산하지 않고 **증권사 값을 그대로 쓴다** —
    신규상장·정리매매 등 제한폭이 30%가 아닌 종목이 있고, 권리락이 있으면 기준가도 바뀐다.

    get_current_price_data는 3초 마이크로 캐시를 쓰므로 주문 직전 시세 조회와 대개
    같은 응답을 재사용한다(추가 TPS 부담 없음). 토스(관찰) 모드는 이 필드를 주지 않아
    (0, 0)이 되고, 그러면 호출부가 클램프를 건너뛴다.
    """
    try:
        data = get_current_price_data(code, False)
        if data.get('rt_cd') != '0':
            return 0, 0
        out = data.get('output', {}) or {}
        return _api().safe_int(out.get('stck_mxpr')), _api().safe_int(out.get('stck_llam'))
    except Exception as e:
        logger.debug(f"get_price_limits 실패({code}): {e}")
        return 0, 0


def get_order_book(code, is_overseas=False):
    """호가창 데이터 조회 (최대 10호가)"""
    if config.session.is_toss:
        return _api()._toss_order_book(code)
    cache_key = f"ob_{code}_{is_overseas}"
    cached = _api()._get_micro_cache(cache_key, ttl=2.0)
    if cached: return cached

    if not is_overseas:
        url_path = "uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        res = _api().call_api(url_path, "domestic", "quotations", "order_book", params=params, tr_id="FHKST01010200", timeout=3)
        if res.get('rt_cd') == '0':
            _api()._set_micro_cache(cache_key, res)
        return res
    else:
        cached_ex = config.session.exchange_cache.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
            if e not in exchanges: exchanges.append(e)
        
        url_path = "uapi/overseas-price/v1/quotations/inquire-asking-price"
        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            res = _api().call_api(url_path, "overseas", "quotations", "order_book", params=params, tr_id="HHDFS76200200", timeout=3)
            if res.get('rt_cd') == '0':
                out = res.get('output1', {})
                #  [Fix 2026-09-05] float() 직접 호출은 값이 '' 일 때 ValueError 를 내고,
                #   그 예외가 이 탐색 루프를 통째로 끊었다 — 정작 루프가 존재하는 이유인
                #   '이 거래소가 아니다'(KIS 는 rt_cd='0' 에 빈 output 을 준다)에서
                #   나머지 거래소를 못 보게 된다.
                if out and (_api().safe_float(out.get('pask1')) > 0
                            or _api().safe_float(out.get('pbid1')) > 0):
                    if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                    _api()._set_micro_cache(cache_key, res)
                    return res
        return {'rt_cd': '9999'}

def is_strength_display_window():
    """장중 체결 지표(체결강도·매도잔량비)를 표에 보여도 되는 시간창.

    거래일 08:00~20:00 — NXT 프리마켓부터 애프터마켓까지다. 그 밖(야간·주말·휴장일)에는
    호가가 서지 않아 값이 굳거나 0으로 내려온다. 굳은 값을 그대로 두면 '지금 이 종목의
    체결강도'로 읽히므로, 값이 아니라 **컬럼 표기 자체를 생략**한다.

    [2026-09-04] 종전에는 이 창을 토스 매도비만 지켰고(is_toss_ask_bid_window),
     KIS 체결강도는 시간과 무관하게 늘 표기됐다 — 같은 자리의 같은 성격의 값인데
     모드에 따라 규칙이 달랐다. 하나로 모은다.
    """
    try:
        if _api().is_holiday_today():
            return False
    except Exception:
        pass
    _now_hhmm = datetime.now().strftime("%H%M")
    return "0800" <= _now_hhmm <= "2000"


def is_toss_ask_bid_window():
    """[토스] 매도잔량비 유효 시간창. 정의는 is_strength_display_window 하나다."""
    return is_strength_display_window()


def get_ask_bid_ratio(code, is_overseas=False):
    """매도/매수 총잔량 비율(비대칭성)만 필요한 수급 게이트용 헬퍼.

    10호가 상세가 필요없는 경로(매수후보·매도조건 분석의 ask_bid_ratio)에서 사용한다.
    WS 실시간 호가 총잔량이 신선하면 REST 없이 즉시 계산 → 종목당 호가 REST 1콜을 절감한다.
    미구독/끊김/해외/토스면 REST(get_order_book) out1 총잔량으로 자동 폴백한다.

    반환: float 비율(매도/매수). 매수잔량 0·매도만 존재 시 99.9. 데이터 없으면 None.
    """
    # [토스] 매도잔량비 유효 시간창 게이트 — KIS 모드의 체결강도 표시와 동일하게
    #  NXT 운영시간(프리 08:00 개장 ~ 애프터 20:00 마감, 휴장일 제외)에만 유효값을 반환한다.
    #  그 외 시간대의 토스 호가는 마지막 스냅샷(동결)이라 수급 지표로서 의미가 없어
    #  None(표시 경로에서는 매도비 표기 자체를 생략)으로 처리한다. 자동매매는 어차피 이 시간창 안에서만 돌므로 영향 없음.
    if config.session.is_toss and not is_overseas and not is_toss_ask_bid_window():
        return None

    # [WS] 국내주식 실시간 호가 총잔량 우선 사용(REST 절감)
    if not is_overseas and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
        try:
            from brokers import realtime
            ob = realtime.get_feed().get_orderbook(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if ob:
                ta = ob.get('total_ask') or 0
                tb = ob.get('total_bid') or 0
                if tb > 0:
                    return ta / tb
                if ta > 0:
                    return 99.9
                # 둘 다 0이면 유효 데이터 없음으로 보고 REST로 폴백
        except Exception:
            pass

    # [폴백] REST 호가창 out1 총잔량
    ob_data = get_order_book(code, is_overseas)
    if ob_data and ob_data.get('rt_cd') == '0':
        out1 = ob_data.get('output1', {})
        total_ask = _api().safe_int(out1.get('total_askp_rsqn'))
        total_bid = _api().safe_int(out1.get('total_bidp_rsqn'))
        if total_bid > 0:
            return total_ask / total_bid
        if total_ask > 0:
            return 99.9
    return None


def get_daily_short_selling(code: str, limit: int = 30):
    """국내 주식 기간별 공매도 추이 조회 (최근 limit일치)"""
    if config.session.is_toss:
        from brokers import toss_api
        return toss_api.get_short_selling(code, count=limit)

    url_path = "/uapi/domestic-stock/v1/quotations/daily-short-sale"
    tr_id = "FHPST04830000"
    import datetime
    end_dt = datetime.datetime.now()
    start_dt = end_dt - datetime.timedelta(days=limit * 2) # 여유있게 조회
    
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d")
    }

    res = _api().call_api(url_path, "domestic", "quotations", "short_sale_trend", params=params, tr_id=tr_id, timeout=3)
    if res and res.get('rt_cd') == '0':
        return res.get('output2', [])
    return []

def get_investor_trend(code, market_div="J"):
    """투자자별 순매수(개인·외국인·기관) 일별 목록.

    [반환 계약] **최신 거래일이 [0]** 이다. 소비하는 쪽이 전부 위치로 읽는다 —
     표의 수급 셀은 inv_list[0], 스마트머니 판정(analysis.check_smart_money_turnaround)은
     [0]=당일·[1]=전일·[2]=전전일로 '턴어라운드'를 본다. 즉 순서가 뒤집히면 표시가 아니라
     **판단이 뒤집힌다**(과거를 최신으로 읽고 턴어라운드 방향이 거꾸로 선다).
     KIS 응답은 최신이 먼저라 여태 성립했지만, 그것은 어느 계층에도 적혀 있지 않았고
     토스 어댑터는 응답 순서를 그대로 흘려보냈다(2026-09-04 감사). 경계에서 맞춘다.

    [반환 계약 2 · 2026-09-05] **조회 실패는 `None`, '수급 미제공'은 `[]`** 이다.
     종전에는 둘 다 `[]` 였고, 더 나쁘게는 그 `[]` 를 5분 마이크로 캐시에 굳혔다.
     소비자 쪽(analysis.check_smart_money_turnaround)은 그 빈 값을 'ETF·미제공 종목'으로
     읽고 **1시간짜리 부정 캐시**에 다시 굳힌다 — 토스/KIS 가 한 번 흔들리면 그 종목의
     스마트머니가 한 시간 동안 False 로 고정되고, 그 사이 재조회는 0회다(실측).
     실패는 캐시하지 않는다.
    """
    cache_key = f"inv_{code}_{market_div}"
    cached = _api()._get_micro_cache(cache_key, ttl=300.0) # [수정] 수급 정보는 장중 잠정치가 천천히 갱신되는 일단위 집계라 5분 캐시로 REST/TPS 절감
    if cached is not None: return cached

    # [추가] 토스 수급 연동 (1.2.14)
    if config.session.is_toss and market_div == "J":
        from brokers import toss_api
        try:
            toss_res = toss_api.get_investor_trend(code, count=30)
            #  `toss_api.get_investor_trend` 는 요청 실패를 `or {}` 로 감춘다 — 빈 응답은
            #  '수급이 없다'가 아니라 '못 물어봤다'이므로 캐시하지 않고 모름으로 올린다.
            if not toss_res:
                logger.debug(f"[Toss] get_investor_trend 응답 없음({code}) — 실패로 본다")
                return None
            records = toss_res.get('records') or []
            kis_output = []
            for r in records:
                date_str = r.get('date', '').replace('-', '')
                prsn = r.get('individual') or {}
                frgn = r.get('foreigner') or {}
                orgn = r.get('institution') or {}
                
                item = {
                    'stck_bsop_date': date_str,
                    'prsn_ntby_qty': str(prsn.get('netBuyVolume', 0) or 0),
                    'frgn_ntby_qty': str(frgn.get('netBuyVolume', 0) or 0),
                    'orgn_ntby_qty': str(orgn.get('netBuyVolume', 0) or 0),
                }
                kis_output.append(item)
            #  응답 순서를 신뢰하지 않고 날짜로 세운다(최신 우선) — 위 반환 계약.
            #  날짜가 없는 행은 뒤로 보내 [0] 을 차지하지 못하게 한다.
            kis_output.sort(key=lambda x: x['stck_bsop_date'] or "", reverse=True)
            _api()._set_micro_cache(cache_key, kis_output)
            return kis_output
        except Exception as e:
            logger.debug(f"[Toss] get_investor_trend 에러: {e}")
            return None

    if config.session.is_toss:
        return []          # 해외/업종 — 토스가 주지 않는다('없음'이 맞다)

    # [수정] 업종(지수)인 경우 별도 TR_ID(FHPTJ04040000) 및 URL 사용
    action = "investor"
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INVESTOR"]
    params = {"FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": code}

    if market_div == "U":
        # 1. 일별 추이 조회 (FHPTJ04040000) 시도
        action = "index_investor"
        url = "uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"
        
        today = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d") # 기간 확대
        params.update({
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": today,
            "FID_PERIOD_DIV_CODE": "D"
        })
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[API] get_investor_trend(Daily) Req: {code}, Params={params}")

        data = _api().call_api(url, "domestic", "quotations", action, params=params)
        
        if data.get('rt_cd') == '0':
            output = data.get('output', [])
            if output:
                _api()._set_micro_cache(cache_key, output)
                return output
            
        # 2. 실패/빈값 시 현재가 투자자 조회 (FHKUP01010900) Fallback
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[API] get_investor_trend(Daily) Empty/Fail. Trying Current Trend Fallback.")
            
        action = "index_investor_current"
        url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_INVESTOR_CURRENT"]
        params = {"FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": code}

    # 주식(J)이거나 업종(U) Fallback 실행
    data = _api().call_api(url, "domestic", "quotations", action, params=params)
    
    if data.get('rt_cd') == '0':
        output = data.get('output', [])
        # [수정] output 키가 없거나 비어있을 경우 output1, output2 등 대체 키 확인 (지수 조회 시 필드명이 다를 수 있음)
        if not output: output = data.get('output1', [])
        if not output: output = data.get('output2', [])
        
        # [추가] output이 dict인 경우 list로 변환 (market.py 호환성)
        if isinstance(output, dict):
            output = [output]
        
        _api()._set_micro_cache(cache_key, output)
        return output

    #  rt_cd != '0' — 조회가 실패한 것이지 '수급이 없는' 것이 아니다. 굳히지 않는다.
    logger.debug(f"[API] get_investor_trend 실패({code}): "
                 f"rt_cd={data.get('rt_cd')} msg={data.get('msg1')}")
    return None

def get_daily_foreign_rate(code):
    """주식 일자별 시세 (최근 30일, 외인소진율 포함) 조회"""
    # [추가] 토스 수급 연동 (1.2.14) - 외국인 소진율
    if config.session.is_toss:
        from brokers import toss_api
        try:
            toss_res = toss_api.get_investor_trend(code, count=30)
            records = toss_res.get('records', [])
            kis_output = []
            for r in records:
                date_str = r.get('date', '').replace('-', '')
                frgn_hold = r.get('foreignerHolding') or {}
                holding_rate = frgn_hold.get('holdingRate')
                
                if holding_rate is not None:
                    # Toss: 소수비율(0.5089) -> KIS: 백분율 문자열("50.89")
                    rate_pct = str(float(holding_rate) * 100)
                else:
                    # [모름 · 2026-09-05] 종전에는 "0" 이었다 — 그러면 표에 **외국인
                    #  소진율 0.00%** 로 찍힌다(단정). 소비자(analysis 표)는 이미
                    #  빈 값을 '-' 로 그리는 길을 갖고 있는데 그 길이 한 번도 안 열렸다.
                    rate_pct = ""
                    
                item = {
                    'stck_bsop_date': date_str,
                    'hts_frgn_ehrt': rate_pct
                }
                kis_output.append(item)
            return kis_output
        except Exception as e:
            logger.debug(f"[Toss] get_daily_foreign_rate 에러: {e}")
            return []
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["DAILY_PRICE"]
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0"
    }
    data = _api().call_api(url, "domestic", "quotations", "daily_price", params=params, tr_id="FHKST01010400", timeout=3)
    if data.get('rt_cd') == '0':
        return data.get('output', [])
    return []

def get_realtime_vol_strength(code, is_overseas=False, exchange_code=None, include_nxt=True, cache_ttl=3.0):
    # [추가] 토스 미제공: 체결강도 없음
    if config.session.is_toss: return None
    if is_overseas: return None

    # [WS] 실시간 피드에 신선한 체결강도(H0STCNT0)가 있으면 REST 호출 없이 즉시 반환(TPS 절감).
    if getattr(config, 'USE_WEBSOCKET', True):
        try:
            from brokers import realtime
            v = realtime.get_feed().get_vol_strength(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if v is not None and v > 0:
                return v
        except Exception:
            pass

    # NXT 포함 여부에 따라 캐시 분리 (대량 개요 조회는 NXT 생략하여 종목당 1콜 절감)
    cache_key = f"vol_{code}" if include_nxt else f"vol_{code}_nonxt"
    cached = _api()._get_micro_cache(cache_key, ttl=cache_ttl) # [수정] 체결강도의 실시간성 확보를 위해 캐시 유지 시간을 3초로 단축
    if cached is not None: return cached
    
    final_vol = None
    
    for attempt in range(3):
        # [수정] Timeout을 2초에서 3초로 늘려 로그에 나타난 ReadTimeoutError 빈도 완화
        data = _api().call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"], "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=3, retries=0)
        if data.get('rt_cd') == '0':
            items = data.get('output', [])
            if items:
                # [추가] 체결강도 HTS 괴리 분석을 위한 원본(Raw) 데이터 정밀 추적 로그
                if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                    logger.debug(f"[VOL_STRENGTH_RAW_DATA] [{code}] Attempt {attempt+1} | Raw Output[0]: {json.dumps(items[0], ensure_ascii=False)}")
                    
                tday_rltv = items[0].get('tday_rltv')
                if tday_rltv and str(tday_rltv).strip():
                    try:
                        valid_val = float(str(tday_rltv).replace(',', ''))
                        if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                            logger.debug(f"[VOL_STRENGTH_PARSED] [{code}] Extracted Value: {valid_val}%")
                        # [수정] 체결강도 0은 해당 거래소 당일 무거래(NXT 단독시간대엔 KRX(J)가 닫혀 항상 0)를
                        #  의미하는 무효값이므로 채택하지 않는다.
                        #  NXT 단독시간대(프리/애프터)에는 아래 NX 조회가 실제값을 채우고,
                        #  정규장에는 채우지 않고 None(판단 불가)으로 남겨 호출부가 보류하게 한다.
                        #  (0을 그대로 채택→캐시하면 [0%]로 오표시되는 문제도 함께 차단)
                        if valid_val > 0:
                            final_vol = valid_val
                    except Exception as e:
                        if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]: logger.debug(f"[VOL_STRENGTH_ERROR] [{code}] Parse Error: {e}")
                        pass
            # [수정] rt_cd=0 정상 응답이면 값이 0(무거래)이어도 재시도는 무의미하므로 즉시 종료한다.
            #  (NXT 단독시간대에 J 조회를 3회 반복하면 EGW00201 스로틀만 악화 → 팬아웃 지연)
            break
        elif data.get('msg_cd') == 'EGW00201': time.sleep(0.2)
        else: time.sleep(0.2)
            
    # [추가] NXT(대체거래소) 체결강도 조회 및 병합 (NX 코드 사용)
    # [수정] 모의투자(VTS)는 NXT 미지원 → NX 조회 스킵 (불필요한 ReadTimeout 방지)
    try:
        # [Fix 2026-07-27] 정규장(phase=='skip')에서는 NX 폴백을 쓰지 않는다.
        #  종전엔 J가 0/실패면 정규장에도 NX로 보강해 [0%] 오표시를 막았으나, 이 값은
        #  표시에만 쓰이지 않고 매수 수급 게이트(BUY_VOL_STRENGTH)로 그대로 들어간다.
        #  정규장 중 NXT 체결강도는 정규장의 수백분의 1 거래량에서 나온 다른 시장의
        #  매수/매도 비율이라 소수 체결로 극단값이 되기 쉽고, J가 스로틀(EGW00201)로 실패한
        #  종목만 이종 기준으로 판정되는 비일관 상태를 만든다.
        #  → KRX 기준을 못 구하면 None으로 두어 '판단 불가=보류'로 넘긴다(다음 주기 재조회).
        #    캐시에도 저장하지 않으므로 다음 주기에 정상적으로 다시 조회된다.
        #  - phase=='active'(프리/애프터): NXT가 유일한 거래 시장 → NX가 정당한 대표값.
        #  - phase=='skip'(정규장): NX 조회 생략(TPS 절감 겸용).
        #  - phase=='offhours'(야간·휴장): NXT 미개장 → 조회 생략.
        _nxt_phase = _api()._nxt_quote_phase()
        if include_nxt and _nxt_phase == 'active':
            # [수정] retries=1: NXT 단독시간대엔 NX가 유일한 유효 체결강도 소스인데, 개요 팬아웃(다워커
            #  동시호출) 중 EGW00201(초당 거래건수 초과)에 걸려 retries=0으로 즉시 실패하면 J의 0으로
            #  폴백돼 [0%]로 오표시된다(간헐적·종목마다 뒤바뀜). call_api의 스로틀 백오프로 회복시킨다.
            #  (fetch_nxt_price의 동일 EGW00201 대응과 일관)
            nxt_data = _api().call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"], "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": code}, timeout=2, retries=1)
            if nxt_data and nxt_data.get('rt_cd') == '0':
                nxt_items = nxt_data.get('output', [])
                if nxt_items:
                    nxt_tday_rltv = nxt_items[0].get('tday_rltv')
                    if nxt_tday_rltv and str(nxt_tday_rltv).strip():
                        nxt_vol = float(str(nxt_tday_rltv).replace(',', ''))
                        if nxt_vol > 0:
                            final_vol = nxt_vol
    except Exception as e:
        logger.debug(f"[API] NXT(대체거래소) 체결강도 조회 오류 (NX 코드 시도): {e}")

    if final_vol is not None:
        _api()._set_micro_cache(cache_key, final_vol)
        return final_vol
        
    return None

def _tv_overseas_fundamentals(code):
    """[토스] 해외 종목/ETF의 PER/PBR/상장주수를 TradingView 스캐너로 조회한다.

    반환은 KIS 상세(fetch_overseas_detail_price) 형태의 부분 dict: {'perx','pbrx','shar'}
    (미확보 필드는 생략). 실패/미매칭 시 {}.
      - 기본 스캐너 쿼리는 type=stock(공통주·DR)만 반환하므로 filter2(type 제한)를 제거해
        ETF(fund)도 매칭한다. (라이브러리 내부 키라 미존재 시 pop은 무해한 no-op)
      - ETF는 total_shares_outstanding이 비어 있어 aum/nav로 상장주수를 역산한다.
      - PBR은 TradingView 표시 기준과 동일한 price_book_fq(직전 분기)를 사용한다.
      - PER은 KIS와 동일 방식으로 채운다: price_earnings_ttm이 있으면 그대로,
        없으면(적자 기업은 EPS<0이라 TV가 None 반환) 주가/|EPS|로 직접 계산한다.
        (KIS는 적자 종목도 EPS 절댓값 기준 양수 PER을 표기 — 역산으로 확인)
    """
    try:
        from tradingview_screener import Query, Column
        q = (Query()
             .select('close', 'price_earnings_ttm', 'price_book_fq',
                     'earnings_per_share_diluted_ttm', 'earnings_per_share_basic_ttm',
                     'earnings_per_share_diluted_fy',
                     'total_shares_outstanding', 'aum', 'nav', 'type')
             .set_markets('america')
             .where(Column('name') == code))
        q.query.pop('filter2', None)  # type=stock 제한 제거 → ETF/fund 포함
        _, df = q.limit(1).get_scanner_data()
    except Exception as e:
        logger.debug(f"[API] 해외 펀더멘털 TV 조회 실패({code}): {e}")
        return {}
    if df is None or df.empty:
        return {}
    row = df.iloc[0]
    out = {}
    per = row.get('price_earnings_ttm')
    if pd.notna(per):
        out['perx'] = f"{float(per):.2f}"
    else:
        # 적자 기업: TV는 PER을 None으로 주므로 KIS와 동일하게 주가/|EPS|로 계산.
        # EPS는 희석(TTM) → 기본(TTM) → 희석(FY) 순으로 사용 가능한 값을 채택.
        close_v = row.get('close')
        eps = None
        for f in ('earnings_per_share_diluted_ttm', 'earnings_per_share_basic_ttm',
                  'earnings_per_share_diluted_fy'):
            v = row.get(f)
            if pd.notna(v) and float(v) != 0:
                eps = float(v)
                break
        if pd.notna(close_v) and eps:
            out['perx'] = f"{float(close_v) / abs(eps):.2f}"
    pbr = row.get('price_book_fq')
    if pd.notna(pbr):
        out['pbrx'] = f"{float(pbr):.2f}"
    shar = row.get('total_shares_outstanding')
    if pd.isna(shar) or not shar:
        aum, nav = row.get('aum'), row.get('nav')
        if pd.notna(aum) and pd.notna(nav) and float(nav) > 0:
            shar = float(aum) / float(nav)  # ETF: 순자산/기준가로 상장주수 역산
    if pd.notna(shar) and shar:
        out['shar'] = float(shar)
    return out


def fetch_overseas_detail_price(code, excd):
    # [토스] 해외 상세(PER/PBR/상장주수)는 토스 미제공 → TradingView 스캐너로 보강한다.
    # (52주 위치는 가격 기반이라 호출부에서 토스 캔들로 별도 산출)
    if config.session.is_toss:
        cache_key = f"detail_{code}"
        cached = _api()._get_micro_cache(cache_key, ttl=300.0)  # 펀더멘털은 일단위 변동 → 5분 캐시
        if cached is not None:
            return cached
        data = _tv_overseas_fundamentals(code)
        #  [Fix 2026-09-05] 실패/미매칭({})은 굳히지 않는다. 같은 함수의 KIS 분기는
        #   h52p 가 유효할 때만 캐시하는데(아래) 이쪽만 무조건 넣어, TradingView 가 한 번
        #   흔들리면 그 종목의 PER/PBR/상장주수가 5분간 '없음'으로 굳었다.
        if data:
            _api()._set_micro_cache(cache_key, data)
        return data
    cache_key = f"detail_{code}"
    cached = _api()._get_micro_cache(cache_key, ttl=60.0) # [수정] 상세 정보 유지 시간 연장
    if cached is not None: return cached

    exchanges = []
    if excd: exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in exchanges: exchanges.append(e)

    # [주간거래] 데이마켓 세션 중에는 주간 코드를 먼저 시도한다.
    #  이 TR의 52주 고저·PER/PBR은 정규/주간이 동일하고 last만 라이브로 갱신되므로,
    #  주간 코드를 쓰지 않으면 52주 위치가 직전 정규장 종가 기준으로 계산된다.
    if _api().us_day_market_session():
        day = []
        for e in exchanges:
            d = _api().US_DAY_MARKET_EXCD.get(e)
            if d and d not in day:
                day.append(d)
        exchanges = day + exchanges

    for target_excd in exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code}
        data = _api().call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["DETAIL"], "overseas", "quotations", "detail", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            output = data.get('output', {})
            if _api().safe_float(output.get('h52p'), default=0.0) > 0:
                # [주간거래] 캐시·stock.json에는 항상 '정규장' 코드를 저장한다(주간 코드가 박히면
                #  정규장 시간대 조회·주문 경로가 깨진다). update_cache_and_save는 파일에 영속된다.
                reg_excd = _api().US_REGULAR_EXCD.get(target_excd, target_excd)
                if reg_excd != excd: config.session.update_cache_and_save(code, reg_excd)
                _api()._set_micro_cache(cache_key, output)
                return output
    return {}

def fetch_domestic_period_price(code, days=100):
    """국내 주식 기간별 시세 조회 (기본 100일, 단일 호출 최대 약 100건 반환)"""
    today = datetime.now().strftime("%Y%m%d")
    past = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": past, "FID_INPUT_DATE_2": today, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
    data = _api().call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"], "domestic", "quotations", "chart", params=params)
    if data.get('rt_cd') == '0': return data.get('output2', [])
    return []

def fetch_overseas_period_price(code, excd):
    today = datetime.now().strftime("%Y%m%d")
    
    target_exchanges = []
    if excd: target_exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in target_exchanges: target_exchanges.append(e)
    
    for target_excd in target_exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code, "GUBN": "0", "BYMD": today, "MODP": "1", "KEYB": code}
        data = _api().call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["CHART"], "overseas", "quotations", "chart", params=params, timeout=5)
        if data.get('rt_cd') == '0':
            items = data.get('output2')
            if items:
                if target_excd != excd: config.session.update_cache_and_save(code, target_excd)
                df = pd.DataFrame(items).drop_duplicates(subset=['xymd'])
                df.rename(columns={'xymd': 'date', 'clos': 'close', 'tovol': 'volume', 'high': 'high', 'low': 'low'}, inplace=True)
                if 'volume' not in df.columns:
                    if 'tvol' in df.columns: df['volume'] = df['tvol']
                    else: df['volume'] = 0
                df = df.astype({'close': float, 'high': float, 'low': float, 'volume': float})
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
    return None

def fetch_buyable_quantity(stock_code, price):
    # [관찰 모드] 가상 현금 기준으로 답한다. 가로채지 않으면 CANO="PAPER"로 실계좌
    #  API를 때려 INVALID_CHECK_ACNO(rt_cd=2)가 나고 0을 돌려준다. 신규 매수는
    #  예수금 폴백이 있어 살아남지만 **피라미딩은 폴백이 없어 '예수금 부족'으로
    #  영구히 보류된다** — 관찰 모드에서 증액이 한 번도 발동하지 못한다.
    #  (2026-08-05 실측: 라즈베리파이 로그의 inquire-psbl-order INVALID_CHECK_ACNO)
    if _api()._paper_active():
        from modules import paper_broker
        if not price or float(price) <= 0:
            return 0
        return int(paper_broker.get_cash() * 0.998 / float(price))
    if config.session.is_toss:
        return _api()._toss_buyable_qty(stock_code, price, "KRW")
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "PDNO": stock_code, "ORD_UNPR": str(price), "ORD_DVSN": "00" if price > 0 else "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N", "CRDT_TYPE": "00"}
    data = _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BUYABLE"], "domestic", "inquiry", "buyable", params=params, timeout=5)
    if data.get('rt_cd') == '0':
        out = data.get('output', {})
        # 미수 없는 수량을 1순위로 쓴다(get_deposit_balance의 order_possible과 같은 이유).
        #  [Fix 2026-09-06] `or` 사슬은 nrcvb_buy_qty 가 **진짜 0주일 때** 신용 포함
        #   수량으로 넘어갔다 — 현금이 없어 미수가 날 수 있는 바로 그 상황이다.
        #   실측: nrcvb_buy_qty='0', ord_psbl_qty='900' → 매수가능 900주.
        #   값이 읽혔으면 그것이 답이고, 폴백은 필드가 없을 때만이다.
        _nq = _api().safe_float(out.get('nrcvb_buy_qty'), default=None)
        if _nq is not None:
            api_qty = int(_nq)
        else:
            api_qty = _api().safe_int(out.get('ord_psbl_qty')) or _api().safe_int(out.get('max_buy_qty'))
        if price > 0:
            cash = _api().safe_int(out.get('ord_psbl_cash'))
            return min(api_qty, int(cash / price))
        return api_qty
    return 0

def fetch_sellable_quantity(stock_code):
    # [관찰 모드] 가상 보유 수량으로 답한다. **가로채지 않으면 매도가 원천 차단된다** —
    #  실계좌 API가 INVALID_CHECK_ACNO로 0을 돌려주고, 트레이더는 그것을 '팔 수 없는
    #  상태'로 읽어 매도를 중단한 뒤 미관리 포지션 경보까지 띄운다.
    #  손절·트레일링·점수매도가 전부 죽으므로 청산 검증 자체가 성립하지 않는다.
    if _api()._paper_active():
        from modules import paper_broker
        for p in paper_broker.get_positions():
            if p["code"] == stock_code:
                return int(p["qty"])
        return 0
    if config.session.is_toss:
        return _api()._toss_sellable_qty(stock_code)
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "01", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["SELLABLE"], "domestic", "inquiry", "sellable", params=params)
    # [중요] 조회 실패와 '진짜 못 판다'를 가른다. 종전에는 둘 다 0이었고, 호출부는 0을
    #  '팔 수 없는 상태'로 읽어 **매도를 중단**했다. 즉 일시적 조회 실패가 손절을 거르는
    #  결과로 이어졌다. 매수 경로는 반대로 조회 실패 시 예수금 폴백으로 주문을 내는데,
    #  추세추종에서는 못 사는 것보다 못 파는 것이 훨씬 비싸다 — 방향이 거꾸로였다.
    #  실패는 None(=알 수 없음)으로 돌려 호출부가 판단하게 한다.
    if data.get('rt_cd') != '0':
        return None
    for item in data.get('output1', []):
        if item.get('pdno') == stock_code:
            return _api().safe_int(item.get('ord_psbl_qty'))
    # 보유 중인 줄 알고 물었는데 응답에 없다 — 페이징(이 조회는 첫 페이지만 본다)이나
    #  잔고 스냅샷 불일치다. '보유 0'으로 단정하지 않는다.
    return None

def fetch_overseas_buyable_quantity(stock_code, price, excd):
    # [관찰 모드] 해외 주문은 지원하지 않는다(place_order가 거부). 실계좌를 조회할 이유가
    #  없으므로 0으로 답한다 — 호출부는 예수금 폴백을 타고, 주문은 발주 단계에서 막힌다.
    if _api()._paper_active():
        return 0
    if config.session.is_toss:
        return _api()._toss_buyable_qty(stock_code, price, "USD")
    trade_excd = excd
    if excd == "NAS": trade_excd = "NASD"
    elif excd == "NYS": trade_excd = "NYSE"
    elif excd == "AMS": trade_excd = "AMEX"
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": trade_excd, "OVRS_ORD_UNPR": str(price), "ITEM_CD": stock_code}
    data = _api().call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BUYABLE"], "overseas", "inquiry", "buyable", params=params)
    if data.get('rt_cd') == '0':
        out = data.get('output', {})
        return _api().safe_int(out.get('ovrs_ord_psbl_qty')) or _api().safe_int(out.get('ord_psbl_qty'))
    return 0

def fetch_overseas_sellable_quantity(stock_code, excd):
    # [관찰 모드] 가상 계좌에는 해외 포지션이 없다(get_overseas_balance가 빈 목록).
    if _api()._paper_active():
        return 0
    if config.session.is_toss:
        return _api()._toss_sellable_qty(stock_code)
    trade_excds = []
    primary_excd = excd
    if excd == "NAS": primary_excd = "NASD"
    elif excd == "NYS": primary_excd = "NYSE"
    elif excd == "AMS": primary_excd = "AMEX"
    
    # [수정] 실전/모의 모두 모든 거래소 확인 (종목별 상장 거래소가 다를 수 있음)
    trade_excds = []
    trade_excds.append(primary_excd)
    for e in ["NASD", "NYSE", "AMEX"]:
        if e != primary_excd: trade_excds.append(e)
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    for target_excd in trade_excds:
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": target_excd, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = _api().call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "sellable", params=params)
        if data.get('rt_cd') == '0':
            for item in data.get('output1', []):
                if item.get('ovrs_pdno') == stock_code:
                    qty = _api().safe_int(item.get('ord_psbl_qty'))
                    if qty > 0: return qty
    return 0

def resolve_overseas_exchange(code):
    """해외 티커의 KIS 거래소 코드(NAS/NYS/AMS)를 판별해 캐시·stock.json에 저장한다.

    KIS 모드는 시세 응답 기반(find_best_exchange_code)을 쓰고, 토스 모드는 KIS API를
    쓸 수 없어 TradingView 스캐너의 거래소 접두사(NASDAQ/NYSE/AMEX)로 판별한다.
    (스캐너 기본 필터는 ETF(type=fund)를 제외하므로 필터를 직접 구성한다)
    실패 시 None — 표시는 '-' 유지.
    """
    cached = config.session.exchange_cache.get(code)
    if cached:
        return cached
    if not config.session.is_toss:
        return find_best_exchange_code(code)
    try:
        from tradingview_screener import Query
        q = Query().set_markets('america').select('close')
        q.query['filter'] = [{'left': 'name', 'operation': 'equal', 'right': code}]
        q.query.pop('filter2', None)
        _, df = q.get_scanner_data()
        if df is not None and not df.empty:
            tv_ex = str(df.iloc[0]['ticker']).split(':')[0]
            excd = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}.get(tv_ex)
            if excd:
                config.session.update_cache_and_save(code, excd)
                return excd
    except Exception as e:
        logger.debug(f"[TV] 거래소 판별 실패({code}): {e}")
    return None

def find_best_exchange_code(stock_code):
    # [추가] 토스: 주문 시 거래소 코드가 불필요(토스 내부 라우팅). 기본값 반환.
    if config.session.is_toss:
        return "NAS"
    token_to_use = _api().get_current_token()
    cached = config.session.exchange_cache.get(stock_code)
    if cached: return cached

    for excd in ["NAS", "NYS", "AMS"]:
        params = {"AUTH": "", "EXCD": excd, "SYMB": stock_code}
        data = _api().call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["PRICE"], "overseas", "quotations", "price", params=params)
        if data.get('rt_cd') == '0' and float(str(data.get('output', {}).get('last', '0')).strip() or 0) > 0:
            config.session.update_cache_and_save(stock_code, excd)
            return excd
    return None

def _prepare_account_params(cano, acnt_prdt_cd):
    """계좌 파라미터 준비 및 컨텍스트 설정 (내부 헬퍼)"""
    # 인자가 없으면 현재 설정/컨텍스트 값 사용
    if not cano:
        if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
            cano = config.session.auto_cano
            acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        else:
            cano = config.session.cano
            acnt_prdt_cd = config.session.acnt_prdt_cd
    
    # 요청 계좌가 자동매매 계좌와 일치하면 컨텍스트 전환 (토큰/Key 변경)
    if cano == config.session.auto_cano and config.session.auto_app_key:
        context.trade_context.use_auto_account = True
    elif cano == config.session.cano:
        context.trade_context.use_auto_account = False
    else:
        #  [2026-09-05] 호출부가 계좌를 **알고 지정했는데** 우리가 라우팅하지 못하는
        #   경우다. 종전에는 조용히 끝나 CANO 파라미터만 그 계좌를 가리키고 앱키·토큰은
        #   현재 스레드 기본값으로 나갔다 — 계좌와 앱키가 어긋난 요청이 된다.
        #   동작은 그대로 두되(예외를 올리면 조회 경로가 통째로 죽는다) 보이게 한다.
        utils._warn_unroutable_cano("_prepare_account_params", cano)

    return cano, acnt_prdt_cd
