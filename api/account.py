"""잔고·체결내역·미체결 — 계좌 상태 조회.

국내/해외 잔고, 당일 손익, 매매 이력, 진입일 산출, 미체결 주문 조회를 담는다.
계좌 귀속이 어긋나면 기록 자체가 무의미해지므로, 호출부는 계좌 컨텍스트를 명시해 넘긴다.
"""
import logging
import math
import time
from datetime import datetime, timedelta, timezone
import config
from core import constants
from core import context

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

def _paper_active():
    """관찰(페이퍼) 모드 여부. 잔고·예수금·주문 가로채기의 단일 판정점."""
    try:
        return bool(getattr(config.session, 'is_paper', False))
    except Exception:
        return False


def get_domestic_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """국내 주식 잔고 조회"""
    # [관찰 모드] 가상 포트폴리오로 대체. 토스 분기보다 먼저 와야 실계좌 조회가 나가지 않는다.
    if _paper_active():
        from modules import paper_broker
        return paper_broker.get_domestic_balance()
    if config.session.is_toss:
        return _api()._toss_domestic_balance()
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    
    # [수정] 조회 구분: 모의투자는 '02'(종목별), 실전투자는 '01'(대출일별 - API 제한 대응)
    inqr_dvsn = "01"
    
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": inqr_dvsn, "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BALANCE"], "domestic", "inquiry", "balance", params=params, retries=retries)

    if data.get('rt_cd') == '0':
        output1 = data.get('output1', [])
        output2 = data.get('output2', [])
        
        # [디버깅] 잔고 조회 결과 로그 출력
        count = len(output1)
        summary_eval = 0
        if output2:
            summary_tmp = output2[0] if isinstance(output2, list) and output2 else (output2 if isinstance(output2, dict) else {})
            summary_eval = _api().safe_int(summary_tmp.get('scts_evlu_amt'))
            
        logger.info(f"[API] 잔고 조회 결과: 종목수={count}, 총평가금={summary_eval:,}원 (RT_CD={data.get('rt_cd')})")
        
        return output1, output2
    
    # [추가] 실패 시 로그 출력 (네트워크 장애와 API 논리 오류를 구분)
    if data.get('msg_cd') == 'NETERR':
        msg = f"잔고 조회 실패(일시적 네트워크/서버 장애): {data.get('msg1')}"
    else:
        msg = f"잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})"
    logger.debug(f"{msg}")
    if hasattr(context, 'SYSTEM_LOGGER') and context.SYSTEM_LOGGER:
        context.SYSTEM_LOGGER(f"[API] {msg}")

    return None, None

def get_overseas_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """해외 주식 잔고 조회"""
    # [관찰 모드] 가상 계좌는 국내 전용이다(place_order가 해외를 거부). 실계좌를 읽지 않는다.
    if _paper_active():
        return []
    if config.session.is_toss:
        return _api()._toss_overseas_balance()
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    all_holdings = []
    
    for exc in target_exchanges:
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": exc, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = _api().call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "balance", params=params, retries=retries)
        
        # Rate Limit 발생 시 잠시 대기 후 재시도 (call_api 내부 재시도와 별개로 루프 내 처리)
        if data.get('msg_cd') == 'EGW00201':
            time.sleep(0.5)
            data = _api().call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "balance", params=params)

        if data.get('rt_cd') == '0':
            for item in data.get('output1', []):
                if '_exchange' not in item: item['_exchange'] = exc
                all_holdings.append(item)
                
    return all_holdings

def get_today_profit_summary(cano=None, acnt_prdt_cd=None, target_date=None):
    """금일 투자 손익 요약 조회"""
    # [관찰 모드] 실계좌 손익이 아니다. 가상 손익은 paper_broker/DB가 갖고 있다.
    if _paper_active():
        return {'rt_cd': '0', 'output2': []}
    # [추가] 토스: 금일 손익 요약 미제공 → 빈 값
    if config.session.is_toss:
        return {'rt_cd': '0', 'output2': []}
    # [수정] 모의투자 서버는 기간별 손익 조회(TTTC8494R/VTTC8494R)를 지원하지 않음 (OPSQ0002 에러 발생)
    # 따라서 모의투자일 경우 API 호출을 생략하고 빈 값 반환하여 에러 로그 방지

    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", 
        "PDNO": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "AFHR_FLPR_YN": "N", "OFL_YN": "N", "UNPR_DVSN": "01",          
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "COST_ICLD_YN": "Y" 
    }
    return _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["PROFIT"], "domestic", "inquiry", "profit", params=params)

def get_today_history(cano=None, acnt_prdt_cd=None, retries=None, target_date=None):
    """금일 체결 내역 조회"""
    # [관찰 모드] 실계좌 체결 내역을 조회하지 않는다. 가상 주문은 즉시 체결되어
    #  대사(reconcile)할 미체결이 없고, 체결 기록은 paper DB가 갖고 있다.
    #  (mode 1가 KIS 시세를 쓰게 되면서 is_toss 분기가 더 이상 막아주지 않는다)
    if _paper_active():
        return {'rt_cd': '0', 'output1': [], 'output2': {}}
    # [추가] 토스: CLOSED 주문 이력에서 당일 국내 체결을 KIS 형태로 변환
    if config.session.is_toss:
        return _api()._toss_today_history(overseas=False)
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    
    # [수정] 주식일별주문체결조회 (inquire-daily-ccld) 사용
    # 실전: TTTC8001R, 모의: VTTC8001R
    url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["HISTORY"]
    tr_id = "TTTC8001R"
    
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "00", # [수정] 00: 전체 조회 (취소/미체결 포함)
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    return _api().call_api(url, "domestic", "inquiry", "history", params=params, retries=retries, tr_id=tr_id)

# 진입일 캐시 TTL — 체결이 나면 보유수량이 바뀌어 캐시 키가 무효화되므로 길게 잡아도 안전하다.
_ENTRY_DATE_CACHE_TTL = 900.0   # 15분


def _replay_entry_date(rows, current_qty=None, window_start=None):
    """체결 내역을 시간순으로 재생해 '현 포지션의 진입일'을 구한다. 'YYYYMMDD' 또는 None.

    진입일 = 누적 보유수량이 0에서 1 이상으로 바뀐 마지막 시점. 최근 매수일을 쓰면
    분할 매수·피라미딩으로 1주만 더 담아도 보유일수가 리셋되고, 첫 매수일을 쓰면 그 사이
    전량 청산 후 재진입한 이력이 지워진다. 그래서 매수·매도를 모두 재생한다.

    rows: [(date 'YYYYMMDD', is_buy, qty)] — 순서 무관(내부에서 정렬)
    current_qty: 현재 보유수량. 조회 구간보다 오래된 포지션을 판별하는 데 쓴다.
      구간 시작 시점의 보유수량 = 현재수량 - 구간 내 순증감. 이 값이 0보다 크면
      진입이 조회 구간 밖이라는 뜻이므로, 확인 가능한 하한(window_start)을 돌려준다.
      None이면 구간 시작을 0으로 가정한다(DB 재생과 동일).
    """
    rows = sorted([r for r in (rows or []) if r and len(r) == 3], key=lambda r: r[0])
    if not rows:
        return None

    net = sum(q if is_buy else -q for _, is_buy, q in rows)
    try:
        base = 0 if current_qty is None else max(0, int(current_qty) - net)
    except (TypeError, ValueError):
        base = 0

    running = base
    entry = None
    for date, is_buy, qty in rows:
        if is_buy:
            if running <= 0:
                entry = date          # 0 → 1 이상: 이번 포지션의 진입
            running += qty
        else:
            running = max(0, running - qty)
            if running == 0:
                entry = None          # 전량 청산 — 다음 매수가 새 진입

    if entry is None:
        # 구간 시작 시점에 이미 보유 중이었다(진입이 조회 범위보다 과거).
        #  최근 매수일로 되돌아가면 보유일수가 크게 짧아지므로, 확인 가능한 가장 이른
        #  시점을 하한으로 쓴다.
        entry = window_start if base > 0 and window_start else None
    return entry


def get_period_entry_dates(codes, qty_map=None, cano=None, acnt_prdt_cd=None, months=12):
    """보유 종목의 '진입일'을 증권사 체결 내역에서 복원한다. {code: 'YYYYMMDD'}

    HTS·MTS로 직접 매수한 포지션은 시스템 DB에 매수 기록이 없어 보유일수를 알 수 없다.
    주식일별주문체결조회(inquire-daily-ccld)를 기간으로 훑어 매수·매도 체결을 모두 모은 뒤
    수량 흐름을 재생해, 누적 보유수량이 0에서 1 이상으로 바뀐 시점을 진입일로 삼는다.
    (종전에는 '최근 매수 체결일'을 썼다 — 분할 매수·피라미딩으로 1주만 더 담아도 보유일수가
    리셋되어 시간청산 시계가 무한히 미뤄졌다.)
    종목별이 아니라 기간 단위 조회이므로 보유 종목 수와 무관하게 호출 수가 고정된다.

    qty_map: {code: 현재 보유수량}. 진입이 조회 구간보다 과거인지 판별하는 데 쓴다.

    [중요] KIS는 3개월 경계로 TR이 갈린다 — 최근 3개월은 TTTC8001R, 그 이전은 CTSC9115R.
    한 TR로 계속 거슬러 올라가면 3개월 이전 구간이 통째로 빈다. 3개월씩 끊어 최신 구간부터
    조회하되 두 번째 구간부터 과거용 TR로 바꾼다. 수량 재생은 구간 전체가 있어야 정확하므로
    '찾으면 조기 종료'는 하지 않고, 모든 종목의 보유수량이 0까지 역산되면 그때 멈춘다.

    실패는 조용히 빈 dict로 흘려보낸다(보유일수는 부가 정보이므로 잔고 조회를 막아선 안 된다).
    """
    # [관찰 모드] 증권사 체결 이력이 존재하지 않는다(가상 체결). 매수일은 paper DB가 갖고 있다.
    if _paper_active():
        return {}
    if not codes:
        return {}

    qty_map = qty_map or {}

    # [최적화] 진입일은 새 체결이 있어야만 바뀐다. 잔고 화면·자동매매 리포트가 같은 종목을
    #  반복 조회하므로 (종목, 보유수량) 조합을 키로 캐시한다 — 체결이 나면 수량이 바뀌어
    #  키가 자동으로 무효화되므로 오래된 값을 붙들 위험이 없다.
    cache_key = ("period_entry_dates", tuple(sorted(set(codes))),
                 tuple(sorted((c, qty_map.get(c)) for c in set(codes))), int(months))
    cached = _api()._get_micro_cache(cache_key, ttl=_ENTRY_DATE_CACHE_TTL)
    if cached is not None:
        return dict(cached)

    # 토스 모드는 KIS TR이 없다. 주문 이력 API(기간 조회)로 같은 값을 만든다.
    #  (이 분기가 없던 동안 토스 모드는 HTS 매수분 보유일수가 전부 0일로 굳었다)
    if config.session.is_toss:
        try:
            found = _api()._toss_period_entry_dates(codes, qty_map=qty_map, months=months)
            _api()._set_micro_cache(cache_key, found)
            return found
        except Exception as e:
            logger.debug(f"[Toss] 기간 진입일 조회 실패: {e}")
            return {}

    wanted = set(codes)

    try:
        def _done(rows):
            return all(_replay_entry_date(
                [(r['date'], r['is_buy'], r['qty']) for r in rows[c]], qty_map.get(c))
                for c in wanted)

        rows, window_start = _fetch_period_executions(
            wanted, cano=cano, acnt_prdt_cd=acnt_prdt_cd, months=months, should_stop=_done)

        found = {}
        for code in wanted:
            d = _replay_entry_date(
                [(r['date'], r['is_buy'], r['qty']) for r in rows[code]],
                qty_map.get(code), window_start)
            if d:
                found[code] = d
        _api()._set_micro_cache(cache_key, found)
        return found
    except Exception as e:
        logger.debug(f"기간 진입일 조회 실패: {e}")
        return {}


def _fetch_period_executions(codes, cano=None, acnt_prdt_cd=None, months=12, should_stop=None):
    """기간 체결 내역을 종목별로 모은다. ({code: [체결 dict...]}, window_start) 반환.

    체결 dict: date(YYYYMMDD) · time(HHMMSS) · is_buy · qty · price · odno · name · type_name

    [중요] KIS는 3개월 경계로 TR이 갈린다 — 최근 3개월은 TTTC8001R, 그 이전은 CTSC9115R.
      한 TR로 계속 거슬러 올라가면 3개월 이전 구간이 통째로 빈다. 3개월씩 끊어 최신 구간부터
      조회하되 두 번째 구간부터 과거용 TR로 바꾼다.

    should_stop(rows) -> bool 을 주면 구간마다 호출해 조기 종료한다(불필요한 과거 조회 절약).
    """
    wanted = set(codes)
    rows = {c: [] for c in wanted}
    window_start = None

    # [관찰 모드] 가상 계좌에는 증권사 체결 이력이 없다. 호출부(get_period_entry_dates·
    #  get_period_executions)도 각자 막고 있지만, 계좌 파라미터를 실어 보내는 함수는
    #  자신이 마지막 방어선을 갖는다 — 새 호출부가 생겨도 실계좌를 긁지 않게.
    if _paper_active():
        return rows, None

    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["HISTORY"]
    tr_recent = constants.TR_ID_CONFIG["domestic"]["inquiry"]["history"]
    tr_old = constants.TR_ID_CONFIG["domestic"]["inquiry"]["history_old"]

    end = datetime.now()

    for chunk in range(max(1, int(math.ceil(months / 3.0)))):
        start = end - timedelta(days=90)
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
            "INQR_STRT_DT": start.strftime("%Y%m%d"),
            "INQR_END_DT": end.strftime("%Y%m%d"),
            "SLL_BUY_DVSN_CD": "00",   # 00: 전체 (수량 흐름을 재생하려면 매도도 필요)
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "01",         # 01: 체결분만 (미체결·취소 제외)
            "ORD_GNO_BRNO": "", "ODNO": "",
            "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }

        res = _api().call_api(url, "domestic", "inquiry", "history", params=params,
                       tr_id=(tr_recent if chunk == 0 else tr_old))
        if not res or res.get('rt_cd') != '0':
            # 과거 조회 TR을 지원하지 않는 계좌·환경이면 여기서 멈춘다(모은 것까지 쓴다).
            break

        window_start = start.strftime("%Y%m%d")
        for row in (res.get('output1') or []):
            parsed = _parse_execution_row(row, wanted)
            if parsed:
                rows[parsed['code']].append(parsed)

        if should_stop and should_stop(rows):
            break

        end = start - timedelta(days=1)

    for c in wanted:
        rows[c].sort(key=lambda r: (r['date'], r['time'], r['odno']))
    return rows, window_start


def _parse_execution_row(row, wanted):
    """inquire-daily-ccld 의 output1 한 줄을 체결 dict 로 바꾼다. 대상 밖이면 None."""
    code = str(row.get('pdno') or '').strip()
    date = str(row.get('ord_dt') or '').strip()
    if code not in wanted or len(date) != 8:
        return None
    try:
        qty = int(float(row.get('tot_ccld_qty') or 0))
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None

    # 02=매수 / 01=매도. 구분값이 없으면 이름으로 판정한다.
    dvsn = str(row.get('sll_buy_dvsn_cd') or '').strip()
    if dvsn == '02':
        is_buy = True
    elif dvsn == '01':
        is_buy = False
    else:
        is_buy = '매수' in str(row.get('sll_buy_dvsn_cd_name') or '')

    # 체결평균가 우선. 없으면 체결금액/수량, 그것도 없으면 주문단가.
    price = _api().safe_int(row.get('avg_prvs'))
    if price <= 0:
        amt = _api().safe_int(row.get('tot_ccld_amt'))
        price = int(amt / qty) if amt > 0 else _api().safe_int(row.get('ord_unpr'))

    return {
        'code': code, 'date': date,
        'time': str(row.get('ord_tmd') or '').strip().zfill(6),
        'is_buy': is_buy, 'qty': qty, 'price': price,
        'odno': str(row.get('odno') or '').strip(),
        'name': str(row.get('prdt_name') or '').strip(),
        'type_name': str(row.get('sll_buy_dvsn_cd_name') or '').strip(),
    }


def get_period_executions(codes, cano=None, acnt_prdt_cd=None, months=12):
    """보유 종목의 기간 체결 내역(공개 진입점). 실패 시 빈 dict."""
    if not codes or _paper_active() or config.session.is_toss:
        return {}
    try:
        rows, _ = _fetch_period_executions(codes, cano=cano, acnt_prdt_cd=acnt_prdt_cd, months=months)
        return rows
    except Exception as e:
        logger.debug(f"기간 체결 내역 조회 실패: {e}")
        return {}


def get_overseas_today_history(cano=None, acnt_prdt_cd=None, retries=None, target_date=None):
    """금일 해외주식 체결 내역 조회"""
    # [관찰 모드] 가상 계좌는 국내 전용이라 해외 체결이 존재하지 않는다.
    if _paper_active():
        return {'rt_cd': '0', 'output1': [], 'output2': {}}
    # [추가] 토스: CLOSED 주문 이력에서 당일 해외 체결을 KIS 형태로 변환
    if config.session.is_toss:
        return _api()._toss_today_history(overseas=True)
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    
    url = constants.API_URLS["OVERSEAS"]["INQUIRY"]["HISTORY"]
    tr_id = "TTTS3035R"
    
    # [Fix] OVRS_EXCG_CD는 필수 입력값이므로, 거래소별로 순회하며 조회 후 병합
    all_trades = []
    final_res = {'rt_cd': '0', 'output': []} # 성공 시 반환할 기본 구조

    for excg_cd in ["NASD", "NYSE", "AMEX"]:
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": excg_cd, # [Fix] 거래소 코드 추가
            "PDNO": "%",
            "ORD_STRT_DT": today,
            "ORD_END_DT": today,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00", # [수정] 00: 전체 조회 (취소/미체결 포함)
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": ""
        }
        
        # [Fix] 모의투자 환경에서 SORT_SQN 누락 시 에러(OPSQ2001) 발생 대응
        params["SORT_SQN"] = "01"
        
        res = _api().call_api(url, "overseas", "inquiry", "history", params=params, retries=retries, tr_id=tr_id)
        if res.get('rt_cd') == '0':
            all_trades.extend(res.get('output', []))
        elif res.get('rt_cd') != '1': # '1'은 데이터 없음이므로 에러가 아님
            return res # 실제 에러 발생 시 즉시 반환

    final_res['output'] = all_trades
    return final_res

def get_unfilled_orders(cano=None, acnt_prdt_cd=None):
    """미체결 내역 조회 (국내주식) - get_domestic_open_orders의 Alias"""
    return get_domestic_open_orders(cano, acnt_prdt_cd)

def get_domestic_open_orders(cano=None, acnt_prdt_cd=None):
    """국내주식 미체결 내역 조회 (실전/토스/관찰 분기 처리)"""
    # [관찰 모드] 즉시 전량 체결로 모델링하므로 미체결은 항상 없다.
    #  (미체결·부분체결 재현은 1단계 범위 밖 — paper_broker 모듈 주석 참조)
    # [Fix] 이 함수는 **주문 dict의 리스트**를 반환하는 계약이다(실전·토스 경로 모두 list).
    #  응답 봉투 dict를 돌려주면 호출부가 그대로 순회하면서 키 문자열을 원소로 받아
    #  'str' object has no attribute 'get' 로 터진다. 실제로 가상투자에서 매 주기
    #  engine.manage_unfilled_orders 가 실패했고, trader._get_toss_open_buy_reserved 도
    #  같은 이유로 예외를 내 입금 자동 감지가 조용히 건너뛰어졌다.
    if _paper_active():
        return []
    if config.session.is_toss:
        return _api()._toss_open_orders('domestic')
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    
    # [수정] 실전투자: 주식정정취소가능주문조회(TTTC8036R) 사용
    url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["OPEN_ORDERS"]
    tr_id = "TTTC8036R"
        
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
        "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"
    }
        
    res = _api().call_api(url, "domestic", "inquiry", "open_orders", params=params, tr_id=tr_id)
        
    if res.get('rt_cd') == '0':
        return res.get('output', [])
            
    return []

def get_overseas_open_orders(cano=None, acnt_prdt_cd=None):
    """해외주식 미체결 내역 조회"""
    # [관찰 모드] 가상 주문은 즉시 체결되어 미체결이 존재하지 않는다.
    if _paper_active():
        return []
    if config.session.is_toss:
        return _api()._toss_open_orders('overseas')
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    all_orders = []
    # [수정] 실전 투자 시에도 모든 거래소 조회 (NYSE, AMEX 누락 방지)
    # 단, API 호출 횟수가 늘어나므로 Rate Limit 주의 필요
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    
    for exc in target_exchanges:
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": exc, 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        # [Fix] 모의투자 서버는 SORT_SQN 파라미터가 없으면 에러가 발생할 수 있으므로 빈 값으로 전송
        params["SORT_SQN"] = "01"
            
        res = _api().call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["OPEN_ORDERS"], "overseas", "inquiry", "open_orders", params=params)
        if res.get('rt_cd') == '0':
            orders = res.get('output', [])
            if orders:
                for o in orders:
                    if not o.get('ovrs_excg_cd'): o['ovrs_excg_cd'] = exc
                all_orders.extend(orders)
    return all_orders
