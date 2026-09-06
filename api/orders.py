"""주문 — 접수·정정·취소, 그리고 예수금.

주문 응답이 유실됐을 때 재전송하지 않는다는 규칙이 이 계층의 핵심이다
(중복 주문이 응답 유실보다 훨씬 비싸다). 대신 당일 주문내역을 조회해 대사한다.
거래소 라우팅 거부에 대한 폴백과 미확정 주문 대사도 여기 있다.
"""
import logging
from datetime import datetime, timedelta, timezone
import config
from core import constants
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

# 거래소 코드 오배정으로 거부됐을 때만 나타나는 응답 코드/문구.
#  주문이 '접수되기 전' 검증 단계에서 반려된 것이라 재시도해도 이중 주문이 되지 않는다.
#  다른 실패(잔고 부족·시간 외·통신 오류 등)에는 절대 재시도하지 않는다 — 그쪽은
#  주문이 이미 접수됐을 가능성이 있어 재시도가 곧 이중 주문이다.
_EXCHANGE_REJECT_CODES = ("APBK3026",)
_EXCHANGE_REJECT_HINTS = ("종목정보", "거래소구분", "EXCG")


def _is_exchange_routing_reject(res):
    """SOR 오배정으로 인한 반려인지 판정한다(보수적: 확실할 때만 True)."""
    if not isinstance(res, dict) or str(res.get('rt_cd', '')) == '0':
        return False
    msg_cd = str(res.get('msg_cd', '') or '')
    if msg_cd in _EXCHANGE_REJECT_CODES:
        return True
    # msg_cd가 비어 오는 경우를 대비해 문구도 본다. 단 '종목정보 없음' 계열로 한정한다.
    msg1 = str(res.get('msg1', '') or '')
    return any(h in msg1 for h in _EXCHANGE_REJECT_HINTS) and "없" in msg1


def _order_with_exchange_fallback(url_path, market, category, action, data, code=""):
    """SOR로 주문을 내고, 거래소 오배정 반려면 KRX로 1회만 재시도한다.

    NXT 마스터 로드가 실패하면 is_nxt_tradeable이 전 종목 True를 돌려주므로 ETF 등
    NXT 미지원 종목에도 SOR이 붙는다. 그 결과가 매수라면 기회 손실로 끝나지만
    **매도라면 보유 포지션의 청산이 막힌다** — 추세추종에서 가장 비싼 실패다.
    """
    res = _api().call_api(url_path, market, category, action, data=data, method="POST")
    if not _is_exchange_routing_reject(res):
        return res

    code = code or data.get("PDNO", "")
    logger.warning(f"주문 거부(거래소 코드 SOR): {code} {action} - "
                   f"{res.get('msg_cd', '')} {res.get('msg1', '')} → KRX로 재시도합니다.")
    # 증권사가 실제로 거부한 종목은 마스터보다 권위 있는 사실이다. 기록해 두면 다음
    #  주문(특히 손절)이 왕복 없이 곧바로 KRX로 나간다 — 청산 지연을 1회로 끝낸다.
    if code:
        _api()._NXT_REJECTED_CACHE.add(code)
    retry_data = dict(data)
    retry_data["EXCG_ID_DVSN_CD"] = "KRX"
    retry = _api().call_api(url_path, market, category, action, data=retry_data, method="POST")
    if isinstance(retry, dict) and str(retry.get('rt_cd', '')) == '0':
        logger.info(f"주문 재시도 성공(KRX): {code} {action}")
    return retry


# 주문 응답을 못 받았을 때, 실제로 들어갔는지 확인할 시간 창(초).
#  너무 넓으면 직전 주기의 같은 종목 주문을 오인하고, 너무 좁으면 지연된 접수를 놓친다.
ORDER_RECONCILE_WINDOW_SEC = 180


def _reconcile_unknown_order(action, code, qty, reason):
    """응답이 유실된 주문이 실제로 접수됐는지 **조회로** 확인한다.

    [왜 재전송이 아닌가] 타임아웃은 '실패'가 아니라 '모름'이다. 재전송하면 이미 체결된
      주문 위에 하나가 더 얹힐 수 있고, 그러면 포지션이 두 배가 되어 손절폭·변동성
      한도·포트폴리오 히트 캡이 한꺼번에 무의미해진다. 확인이 먼저다.

    [무엇으로 확인하나] 당일 주문·체결 내역에서 같은 종목·같은 매매구분·같은 수량의
      주문을 찾되, **DB에 없는 주문번호**만 후보로 본다. 시스템이 낸 주문은 접수 응답을
      받는 즉시 DB에 남으므로, 응답을 못 받은 이 주문만이 '거래소에는 있는데 DB에는
      없는' 상태가 된다.

    [애매하면 손대지 않는다] 후보가 둘 이상이면 어느 것이 이번 주문인지 단정할 수 없다.
      그때는 자동으로 정하지 않고 운용자에게 넘긴다 — 잘못 고르면 다음 주기가 그 주문을
      '내 것'으로 알고 관리한다.

    반환: KIS 응답과 같은 모양의 dict. 접수 확인 시 rt_cd='0'.
    """
    unknown = {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
               'msg1': f'주문 결과 불명(응답 유실): {reason}', 'output': {}}
    try:
        rows = _reconcile_rows()
    except Exception as e:
        logger.error(f"[ORDER_UNKNOWN] 대사 조회 실패 — 결과 불명으로 둔다: {e}")
        return unknown

    want_side = '02' if action == 'buy' else '01'      # KIS: 01=매도, 02=매수
    now = datetime.now()
    cands = []
    for r in rows:
        if str(r.get('pdno') or '').strip() != str(code):
            continue
        if str(r.get('sll_buy_dvsn_cd') or '') != want_side:
            continue
        try:
            if int(float(r.get('ord_qty') or 0)) != int(qty):
                continue
        except (TypeError, ValueError):
            continue
        if _order_age_seconds(r, now) > ORDER_RECONCILE_WINDOW_SEC:
            continue
        odno = str(r.get('odno') or '').strip()
        if odno and not _odno_known_to_db(odno):
            cands.append(odno)

    if len(cands) == 1:
        odno = cands[0]
        logger.warning(f"[ORDER_UNKNOWN] 대사 결과 접수 확인 — 재전송하지 않고 이 주문을 "
                       f"이어받습니다: {code} {action} {qty}주 / 주문번호 {odno}")
        return {'rt_cd': '0', 'msg_cd': 'ORDER_RECOVERED',
                'msg1': '응답 유실 주문을 조회로 확인했습니다',
                'output': {'ODNO': odno, 'KRX_FWDG_ORD_ORGNO': '', 'ORD_TMD': ''}}

    if not cands:
        logger.warning(f"[ORDER_UNKNOWN] 대사 결과 접수 흔적 없음 — 미접수로 봅니다: "
                       f"{code} {action} {qty}주")
        return {'rt_cd': '1', 'msg_cd': 'ORDER_NOT_PLACED',
                'msg1': f'주문 미접수(응답 유실 후 대사 확인): {reason}', 'output': {}}

    _api().send_telegram_message(
        f"⚠️ [주문 결과 불명] {code} {action} {qty}주\n"
        f"응답이 유실됐고, 같은 조건의 주문이 {len(cands)}건 조회돼 어느 것인지 "
        f"단정할 수 없습니다.\n재전송하지 않았습니다 — HTS에서 직접 확인해 주세요.\n"
        f"주문번호 후보: {', '.join(cands)}")
    return unknown


def _reconcile_rows():
    """대사에 쓸 '당일 주문' 행. KIS 당일 주문체결조회는 미체결까지 담아 준다.

    토스는 그렇지 않다 — 당일 체결이력이 CLOSED(체결·취소) 주문만 준다. 접수된 채
    미체결로 남은 지정가 주문이 빠지므로, 그것만 보면 방금 낸 주문을 '미접수'로 읽고
    다음 주기가 같은 주문을 또 낸다. 재전송을 막으려던 대사가 재전송을 부르는 셈이다.
    미체결 목록을 합쳐 KIS와 같은 범위로 맞춘다.
    """
    #  [Fix 2026-09-06] **조회 실패를 '주문 없음'으로 읽지 않는다.**
    #   종전에는 실패 응답(rt_cd != '0', output1 없음)이 그대로 빈 목록이 되어,
    #   호출부가 "접수 흔적 없음 → 미접수로 봅니다"로 단정했다. 실측:
    #     당일 주문내역 조회 실패 → msg_cd=ORDER_NOT_PLACED, '대사 확인'이라고 적힌 채.
    #   그 결론은 이 계층이 존재하는 이유와 정반대다 — 응답 유실 뒤 '미접수'로 단정하면
    #   다음 주기가 같은 주문을 다시 낸다([[order-timeout-no-resend]]). 게다가
    #   ORDER_NOT_PLACED 는 운용자에게 알리지도 않아, 이중 주문이 조용히 난다.
    #   여기서 올린 예외는 _reconcile_unknown_order 의 try 가 받아 '결과 불명'으로 남긴다.
    hist = _api().get_today_history()
    if not isinstance(hist, dict) or str(hist.get('rt_cd', '')) != '0':
        raise RuntimeError(
            f"당일 주문내역을 조회하지 못했습니다 — '주문 없음'이 아닙니다"
            f" ({(hist or {}).get('msg_cd')} {(hist or {}).get('msg1')})")
    rows = list(hist.get('output1') or [])
    if config.session.is_toss:
        #  토스 당일 이력은 CLOSED 만 준다 — 미체결을 합쳐야 KIS 와 같은 범위가 된다.
        #  그 조회가 실패하면(None) 범위가 반쪽이고, 반쪽으로 '미접수'를 결론지을 수 없다.
        open_rows = _api()._toss_open_orders('domestic')
        if open_rows is None:
            raise RuntimeError("토스 미체결 주문을 조회하지 못했습니다 — 대사 범위가 반쪽입니다")
        seen = {str(r.get('odno') or '') for r in rows}
        for r in open_rows:
            if str(r.get('odno') or '') not in seen:
                rows.append(r)
    return rows


def _order_age_seconds(row, now):
    """주문 시각으로부터 흐른 초. 시각을 못 읽으면 창 밖으로 본다(보수적).

    산식은 core.utils.order_age_seconds 가 단독 보유한다 — 자정을 넘겼을 때 부호가
    뒤집히는 문제를 두 곳(여기와 engine.manage_unfilled_orders)이 각자 틀렸었다.
    """
    return utils.order_age_seconds(row.get('ord_tmd'), now, ord_dt=row.get('ord_dt'))


def _odno_known_to_db(odno):
    """이 주문번호가 이미 시스템 DB에 있는가(=응답을 받아 기록된 주문인가)."""
    try:
        from datetime import datetime as _dt
        from modules import db_manager
        #  odno 는 당일 채번이라 날짜 없이는 유일하지 않다. 전체 이력에서 찾으면 몇 달 전
        #  같은 번호 때문에 '아는 주문'으로 오판하고, 결과 불명 주문이 조용히 우리 것이 된다.
        today = _dt.now().strftime('%Y-%m-%d')
        for status in ("접수", "체결", "체결(추정)", "체결/취소(추정)"):
            if db_manager.db.check_trade_exists(odno, status, on_date=today):
                return True
    except Exception as e:
        logger.debug(f"[ORDER_UNKNOWN] DB 조회 실패: {e}")
        # 확인 못 하면 '이미 아는 주문'으로 보수적 판정 — 모르는 주문을 함부로
        #  이어받는 것보다 결과 불명으로 남기는 편이 안전하다.
        return True
    return False


def place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
    """주문 전송 통합 함수. market: "domestic"|"overseas", action: "buy"|"sell".

    [불변식 · 2026-09-05] **rt_cd='0' 이면 output.ODNO 가 반드시 있다.**
     주문번호 없는 '성공'은 성공이 아니다 — 서버는 접수했는데 우리는 그 주문을 가리킬
     수단이 없다는 뜻이고, 그 상태는 조용히 나쁘다: 체결 대사도 미체결 자동 취소도
     odno 로 찾으므로 영영 못 찾고, pending_orders 에 '' 로 남아 그 종목이 is_pending 인
     채로 **매도 워커에서 통째로 빠진다**(손절·트레일링 정지). 브로커마다 응답 모양이
     달라 각자 막으면 또 어긋나므로, 모든 주문이 지나는 이 문에서 한 번에 건다.
    """
    # [관찰 모드 하드 가드] 호출부 실수와 무관하게 실주문을 원천 차단한다.
    #  이 게이트는 **가장 바깥**에 있어야 의미가 있다 — 아래 어떤 분기도, 결과 불명
    #  대사 경로(_reconcile_unknown_order 는 실계좌 주문내역을 조회한다)도 타지 않는다.
    if _api()._paper_active():
        from modules import paper_broker
        if market != "domestic":
            return {"rt_cd": "1", "msg_cd": "PAPER_REJECT",
                    "msg1": "[가상투자] 해외 주문은 지원하지 않습니다", "output": {}}
        return paper_broker.place_order(action, code, qty, price)

    res = _place_order_impl(market, action, code, qty, price, ord_dvsn, exchange_code)
    return _require_odno(res, market, action, code, qty)


def _require_odno(res, market, action, code, qty):
    """'성공인데 주문번호가 없는' 응답을 결과 불명으로 되돌린다(place_order 독스트링 참조)."""
    if not isinstance(res, dict) or res.get('rt_cd') != '0':
        return res
    out = res.get('output') or {}
    odno = str(out.get('ODNO') or '').strip()
    if odno:
        return res
    reason = "주문 응답에 주문번호(ODNO)가 없습니다"
    logger.error(f"[ORDER_UNKNOWN] {code} {action} {qty}주 — {reason}. "
                 f"재전송하지 않고 당일 주문내역으로 대사합니다. 응답={res}")
    if market != "domestic":
        return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
                'msg1': f'해외 주문 결과 불명({reason})', 'output': {}}
    return _reconcile_unknown_order(action, code, qty, reason)


def _place_order_impl(market, action, code, qty, price, ord_dvsn, exchange_code=None):
    """실제 브로커 경로. 관찰 모드 가드는 place_order 가 이미 지났다(사본을 두지 않는다)."""
    if config.session.is_toss:
        # [Fix 2026-09-04] 토스도 응답 유실 시 재전송하지 않는다. 종전에는 브로커 계층이
        #  POST /orders 를 타임아웃·5xx에 그대로 다시 보내(최대 3회) 같은 주문이 중복
        #  접수될 수 있었다 — KIS 경로만 2026-08-10에 고쳐져 있었다.
        try:
            return _api()._toss_place_order(market, action, code, qty, price, ord_dvsn)
        except _api().OrderOutcomeUnknown as e:
            if market != "domestic":
                return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
                        'msg1': f'해외 주문 결과 불명(응답 유실): {e}', 'output': {}}
            return _reconcile_unknown_order(action, code, qty, str(e))
    cano, acnt = _api()._prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"][action.upper()]
        category = "trade"
            
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "PDNO": code, "ORD_DVSN": ord_dvsn, 
            "ORD_QTY": str(qty), "ORD_UNPR": str(price)
        }
        
        # [추가] 거래소 코드 적용
        # NXT 거래 가능 종목은 SOR(최적주문집행, KRX+NXT 통합 라우팅), 미지원 종목(ETF 등)은
        # KRX로 지정한다. NXT 미지원 종목에 SOR을 쓰면 APBK3026(종목정보 없음) 오류가 발생한다.
        data["EXCG_ID_DVSN_CD"] = "SOR" if _api().is_nxt_tradeable(code) else "KRX"
        # 마스터 로드 실패로 낙관 배정한 SOR이 거부되면 KRX로 1회 재시도한다.
        # (가상투자는 주문을 가로채므로 이 경로가 실행되지 않는다)
        if data["EXCG_ID_DVSN_CD"] == "SOR":
            # [Fix 2026-08-10] SOR 경로도 응답 유실 시 재전송하지 않고 조회로 확인한다.
            try:
                return _order_with_exchange_fallback(url_path, market, category, action, data)
            except _api().OrderOutcomeUnknown as e:
                return _reconcile_unknown_order(action, code, qty, str(e))
    else: # overseas
        # [Fix] 해외 주문 시 거래소 코드 보정 (3자리 -> 4자리)
        trade_excd = exchange_code
        if exchange_code == "NAS": trade_excd = "NASD"
        elif exchange_code == "NYS": trade_excd = "NYSE"
        elif exchange_code == "AMS": trade_excd = "AMEX"

        url_path = constants.API_URLS["OVERSEAS"]["TRADING"]["ORDER"]
        category = "trade"
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "OVRS_EXCG_CD": trade_excd, "PDNO": code, 
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price), 
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_dvsn
        }

    # [Fix 2026-08-10] 응답이 유실되면 재전송하지 않고 조회로 확인한다.
    #  해외는 당일 주문 대사 경로가 없어 확인 없이 '결과 불명'으로 남긴다(자동매매는 국내 전용).
    try:
        return _api().call_api(url_path, market, category, action, data=data, method="POST")
    except _api().OrderOutcomeUnknown as e:
        if market != "domestic":
            return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
                    'msg1': f'해외 주문 결과 불명(응답 유실): {e}', 'output': {}}
        return _reconcile_unknown_order(action, code, qty, str(e))

def _require_odno_rc(res, action, code, org_no):
    """정정·취소도 '주문번호 없는 성공'을 성공으로 보지 않는다.

    정정은 **새 주문번호를 채번**한다. 그 번호가 없으면 우리는 정정된 주문을 가리킬
    수단이 없다 — 체결 대사(get_trade_by_odno)도 미체결 자동 취소도 odno 로 찾는다.
    다만 주문과 달리 재전송 위험이 없으므로 대사까지 가지 않고 실패로 돌린다:
    취소가 안 됐으면 다음 주기가 미체결을 다시 보고 다시 건다(아래 유실 처리와 같은 정책).
    """
    if not isinstance(res, dict) or res.get('rt_cd') != '0':
        return res
    out = res.get('output') or {}
    if str(out.get('ODNO') or '').strip():
        return res
    logger.error(f"[ORDER_UNKNOWN] 정정/취소 응답에 주문번호(ODNO)가 없습니다 — "
                 f"성공으로 보지 않습니다: {code} {action} 원주문 {org_no} / 응답={res}")
    return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
            'msg1': '정정/취소 결과 불명(응답에 주문번호 없음)', 'output': {}}


def revise_cancel_order(market, action, org_no, code, qty, price, type_cd, ord_dvsn, exchange_code=None):
    """
    정정/취소 통합 함수
    action: "modify" (정정) or "cancel" (취소)
    type_cd: "01"(정정), "02"(취소) - API 스펙상 구분 코드
    """
    # [관찰 모드 하드 가드] 즉시 체결이라 정정/취소 대상이 없다. 실주문 경로로 새지 않게 차단한다.
    if _api()._paper_active():
        return {"rt_cd": "1", "msg_cd": "PAPER_REJECT",
                "msg1": "[가상투자] 즉시 체결되어 정정/취소할 주문이 없습니다", "output": {}}
    if config.session.is_toss:
        try:
            return _api()._toss_revise_cancel(market, action, org_no, code, qty, price, ord_dvsn)
        except _api().OrderOutcomeUnknown as e:
            # 주문과 달리 애매해도 손해가 누적되지 않는다 — 다음 주기가 미체결을 다시 본다.
            logger.warning(f"[ORDER_UNKNOWN] 정정/취소 응답 없음 — 재전송하지 않습니다. "
                           f"다음 주기에 미체결로 다시 잡힙니다: {code} 원주문 {org_no} / {e}")
            return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
                    'msg1': f'정정/취소 결과 불명(응답 유실): {e}', 'output': {}}
    cano, acnt = _api()._prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"]["REVISE_CANCEL"]
        category = "modify"
            
        qty_all_yn = "Y" if qty == 0 else "N" # 0이면 전량으로 간주 (호출부 로직에 따름)
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": org_no, "ORD_DVSN": ord_dvsn, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "ORD_UNPR": str(price), "QTY_ALL_ORD_YN": qty_all_yn}
        
        # [추가] 거래소 코드 적용 (NXT 미지원 종목은 KRX, place_order와 동일)
        data["EXCG_ID_DVSN_CD"] = "SOR" if _api().is_nxt_tradeable(code) else "KRX"
        # 주문과 같은 이유로 거부될 수 있다. 취소가 막히면 미체결이 계속 자리를 차지한다.
        if data["EXCG_ID_DVSN_CD"] == "SOR":
            return _require_odno_rc(
                _order_with_exchange_fallback(url_path, market, category, action, data, code=code),
                action, code, org_no)
    else: # overseas
        # [Fix] 해외 주문 정정/취소 시 거래소 코드 보정
        trade_excd = exchange_code
        if exchange_code == "NAS": trade_excd = "NASD"
        elif exchange_code == "NYS": trade_excd = "NYSE"
        elif exchange_code == "AMS": trade_excd = "AMEX"

        url_path = constants.API_URLS["OVERSEAS"]["TRADING"]["REVISE_CANCEL"]
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "OVRS_EXCG_CD": trade_excd, "PDNO": code, "ORGN_ODNO": org_no, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price)}
        category = "modify"
    
    # action 파라미터는 TR_ID 조회를 위해 사용됨 (modify/cancel)
    # [Fix 2026-08-10] 정정·취소도 응답 유실 시 재전송하지 않는다. 다만 주문과 달리
    #  결과가 애매해도 손해가 누적되지 않는다 — 취소가 안 됐으면 다음 주기가 미체결을
    #  다시 보고 취소를 건다. 여기서는 실패로 돌려 그 흐름에 맡긴다.
    try:
        return _require_odno_rc(
            _api().call_api(url_path, market, category, action, data=data, method="POST"),
            action, code, org_no)
    except _api().OrderOutcomeUnknown as e:
        logger.warning(f"[ORDER_UNKNOWN] 정정/취소 응답 없음 — 재전송하지 않습니다. "
                       f"다음 주기에 미체결로 다시 잡힙니다: {code} 원주문 {org_no} / {e}")
        return {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
                'msg1': f'정정/취소 결과 불명(응답 유실): {e}', 'output': {}}

def get_deposit(cano=None, acnt_prdt_cd=None, retries=None):
    """예수금(주문가능현금) 조회 (국내/모의)"""
    # [관찰 모드] 가상 예수금으로 대체(get_deposit_balance와 동일 기조).
    if _api()._paper_active():
        from modules import paper_broker
        return paper_broker.get_deposit_balance()
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", 
        "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"
    }
    # [수정] TR_ID 명시적 지정 (로그상 CTRP6548R이 호출되고 있어 TTTC8908R로 교정)
    tr_id = "TTTC8908R"
    return _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BUYABLE"], "domestic", "inquiry", "deposit", params=params, retries=retries, tr_id=tr_id)

def get_foreign_deposit(cano=None, acnt_prdt_cd=None, retries=None):
    """외화 예수금 등 실전투자 계좌 잔고 상세 조회"""
    # [관찰 모드] 실계좌 외화 예수금을 읽지 않는다.
    if _api()._paper_active():
        return {}
    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "TR_CONT": "", "INQR_DVSN_1": "", "TR_CRCY_CD": "", "PDNO": "", 
        "ORD_UNPR": "", "ORD_QTY": "", "ORD_DVSN": "00", 
        "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", 
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
        "BSPR_BF_DT_APLY_YN": "N"
    }
    return _api().call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["DEPOSIT"], "domestic", "inquiry", "deposit", params=params, retries=retries)

def get_deposit_balance(cano=None, acnt_prdt_cd=None, skip_balance_check=False, retries=None):
    """예수금 및 자산 현황 조회 (모의/실전/토스/관찰 자동 분기)"""
    # [관찰 모드] 가상 현금. 즉시 결제로 보므로 D+1/D+2 구분이 없다.
    if _api()._paper_active():
        from modules import paper_broker
        return paper_broker.get_deposit_balance()
    # [추가] 토스: 매수가능금액(현금)을 예수금으로 사용. D+1/D+2 구분은 제공되지 않음.
    if config.session.is_toss:
        dep = _api()._toss_krw_deposit()
        return {"deposit": dep, "foreign_deposit": 0, "withdraw": dep,
                "d2_deposit": dep, "order_possible": dep, "d2_real": dep}

    cano, acnt_prdt_cd = _api()._prepare_account_params(cano, acnt_prdt_cd)
    res = {"deposit": 0, "foreign_deposit": 0, "withdraw": 0, "d2_deposit": 0, "order_possible": 0, "d2_real": 0}
    success = False # [추가] 조회 성공 여부 플래그

    # [수정] 실전투자: 주문가능금액(get_deposit)과 계좌잔고(get_foreign_deposit) 모두 조회하여 병합
    # 1. 주문가능금액 조회 (주문가능금액, 출금가능금액)
    data_order = get_deposit(cano, acnt_prdt_cd, retries=retries)
        
    if data_order.get('rt_cd') == '0':
        out = data_order.get('output', {})
        # [실전 주문가능금액] nrcvb_buy_amt(미수없는매수금액)를 **1순위**로 쓴다.
        #  ord_psbl_amt는 계좌에 신용·대용 여력이 있으면 그것까지 포함한 값이 될 수 있다.
        #  자본대비 리스크 한도를 두는 시스템에서 매수여력은 '증권사가 허용하는 최대'가
        #  아니라 '미수 없이 살 수 있는 금액'이어야 한다 — 미수가 나면 연체이자와
        #  반대매매가 붙어 손절 규칙 바깥에서 포지션이 정리된다.
        #  (실측 2026-08-09: 이 계좌들은 ord_psbl_amt 자체가 응답에 없어 이미 폴백으로
        #   안전했으나, 그건 우연이다. 순서를 뒤집어 명시적으로 만든다.)
        #  [Fix 2026-09-06] `A or B` 는 A 가 **진짜 0원일 때** B 로 넘어간다 — 하필
        #   현금이 없어 미수가 날 수 있는 유일한 상황에서 신용·대용 포함 금액을 쓰게 된다.
        #   safe_int 는 '필드 없음'과 '0원'을 똑같이 0으로 만들어 둘을 구분할 수 없다.
        #   실측: nrcvb_buy_amt='0', ord_psbl_amt='9,000,000' → 매수여력 9,000,000원.
        #   값이 **읽혔으면** 그것이 답이다. 폴백은 필드가 없을 때만이다.
        _nrcvb = _api().safe_float(out.get('nrcvb_buy_amt'), default=None)
        res['order_possible'] = (int(_nrcvb) if _nrcvb is not None
                                 else _api().safe_int(out.get('ord_psbl_amt')))
        logger.info(f"[API] 주문가능금액 조회 성공: {res['order_possible']:,}원 (TR_ID: TTTC8908R)")
        res['withdraw'] = _api().safe_int(out.get('ord_psbl_cash')) # 출금가능은 현금 기준
        # 예수금 정보가 없을 경우 주문가능현금으로 대체
        res['deposit'] = _api().safe_int(out.get('ord_psbl_cash'))
        success = True
    else:
        logger.warning(f"[API] 주문가능금액 조회 실패: {data_order.get('msg1')} (Code: {data_order.get('msg_cd')})")

    # 2. 주식 잔고 조회 (예수금, D+2 가수도) - get_domestic_balance 활용
    # get_foreign_deposit 대신 더 안정적인 get_domestic_balance 사용
    holdings, summary_list = _api().get_domestic_balance(cano, acnt_prdt_cd, retries=retries)
    if summary_list and len(summary_list) > 0:
        summary = summary_list[0]
        res['deposit'] = int(float(summary.get('dnca_tot_amt', 0))) # 예수금 (우선)
        res['d2_real'] = int(float(summary.get('prvs_rcdl_excc_amt', 0))) # D+2 가수도 (우선)
            
        # [추가] Fallback: 주문가능금액 조회 실패 시 D+2 가수도 사용
        if res['order_possible'] == 0:
            res['order_possible'] = res['d2_real']
            
        # [추가] Fallback: 출금가능금액 조회 실패 시 예수금 사용
        if res['withdraw'] == 0:
            res['withdraw'] = res['deposit']
                
        success = True

    # 3. 외화 잔고 조회 (보조)
    data_foreign = get_foreign_deposit(cano, acnt_prdt_cd, retries=retries)
    if data_foreign.get('rt_cd') == '0' and data_foreign.get('output2'):
        out2 = data_foreign['output2'][0] if isinstance(data_foreign['output2'], list) else data_foreign['output2']
        res['foreign_deposit'] = int(float(out2.get('frcr_evlu_tota', 0)))
            
        # [추가] 계좌잔고평가 API의 D+2 가수도금(prvs_rcdl_excc_amt)이 더 정확할 수 있음 (매도 대금 반영 등)
        d2_account_val = int(float(out2.get('prvs_rcdl_excc_amt', 0)))
        if d2_account_val > res['d2_real']:
            res['d2_real'] = d2_account_val
                
        # [추가] 예수금도 확인하여 더 큰 값 사용
        deposit_account_val = int(float(out2.get('dnca_tot_amt', 0)))
        if deposit_account_val > res['deposit']:
            res['deposit'] = deposit_account_val
            
    # D+2 예수금(매수여력) 결정: 주문가능금액이 있으면 그것을, 없으면 D+2 잔고를 사용
    if res['order_possible'] > 0:
        res['d2_deposit'] = res['order_possible']
    elif res['d2_real'] > 0:
        res['d2_deposit'] = res['d2_real']
            
    return res if success else None # [수정] 실패 시 None 반환

def check_server_health():
    """서버 상태 점검 (삼성전자 현재가 조회)"""
    if config.session.is_toss:
        try:
            from brokers import toss_api
            res = toss_api.get_price("005930")
            if res is not None:
                return True
        except Exception as e:
            logger.debug(f"check_server_health (toss) error: {e}")
        return False
        
    try:
        # 타임아웃 5초, 재시도 0회로 설정하여 빠르게 확인
        res = _api().call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"], "domestic", "quotations", "price", 
                       params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}, 
                       timeout=5, retries=0)
        if res and res.get('rt_cd') == '0':
            return True
    except Exception as e:
        logger.debug(f"check_server_health error: {e}")
    return False
