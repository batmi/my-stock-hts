"""NXT(대체거래소) 시세와 멀티 시세 배치.

정규장 밖 시간대의 기준가·현재가를 NXT 에서 받아 오고, 여러 종목을 한 번에 묻는
멀티 시세 배치를 담는다. 배치가 실패하면 쿨다운을 두고 일시 비활성화한다
(영구 비활성이 아니라, 낡은 값을 계속 쓰는 것을 막기 위한 장치다).
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
import config
import constants

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

def _nxt_quote_window():
    """NXT(대체거래소) 보조 시세 조회가 의미있는 시간대인지 판단한다(TPS 절감용 시간대 게이트).

    정규장(09:00~15:30)에는 KRX가 대표가이고 NXT와 사실상 동일하므로, 종목당 NXT 보조
    호출을 생략해 전역 TPS 부담을 줄인다(분석 속도 개선). KRX가 닫혀 NXT 시세가 유일하게
    유효한 NXT 단독 거래시간(프리 08:00~09:00, 애프터 15:30~20:00)에만 조회한다.
    그 외 시간(야간)·휴장일은 NXT가 닫혀 빈 응답이므로 생략한다.
    """
    try:
        if _api().is_holiday_today():
            return False
    except Exception:
        pass
    now = datetime.now().strftime("%H%M")
    return ("0800" <= now < "0900") or ("1530" <= now <= "2000")

def fetch_nxt_price(code):
    """NXT(대체거래소) 현재가만 단독 조회한다. (모의투자/오류/미체결 시 0 반환)

    base 현재가를 이미 확보한 경로(개요 테이블 등)에서 NXT 시세만 추가로 병합할 때
    사용한다. 모의투자(VTS)는 NXT 미지원이라 ReadTimeout 방지를 위해 조회를 건너뛴다.
    """
    if config.session.is_simulation:
        return 0
    try:
        nxt_url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"]
        # [수정] retries=1: 장전(08:00~09:00) 오버뷰 팬아웃 중 EGW00201(초당 거래건수 초과)에 걸리면
        #  call_api의 스로틀 백오프 재시도가 작동해 회복되도록 한다(retries=0이면 즉시 0→KRX 전일종가
        #  폴백→등락률 0% stale). nxtSupported=false 종목은 rt_cd 0·stck_prpr 0 정상응답이라 무관.
        nxt_res = _api().call_api(nxt_url, "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": code}, timeout=2, retries=1)
        if nxt_res and nxt_res.get('rt_cd') == '0' and nxt_res.get('output'):
            nxt_price = nxt_res['output'].get('stck_prpr')
            if nxt_price and _api().safe_int(nxt_price) > 0:
                return _api().safe_int(nxt_price)
    except Exception as e:
        logger.debug(f"[API] NXT(대체거래소) 시세 조회 오류 (NX 코드 시도): {e}")
    return 0

# ==========================================================
# NXT(대체거래소) 마지막 종가 기억 — 야간/주말/휴장 시 현재가 표시용 (실전 전용)
#  거래시간(프리 08:00~09:00, 애프터 15:30~20:00) 동안 받은 NXT 현재가를 보관했다가,
#  거래가 없는 시간대(야간 20:00~익일 08:00 / 주말 / 휴장일)에는 KRX 정규장 종가 대신
#  '마지막 NXT 종가'를 현재가로 노출한다(다음 거래일 개장 전까지). 디스크에 영속하여 재시작에도 보존.
#  모의투자(VTS)는 NXT 미지원이므로 이 경로를 타지 않는다(항상 KRX 종가).
# ==========================================================
_nxt_last_close = {}
_nxt_last_close_lock = threading.RLock()
_nxt_last_close_loaded = False
_nxt_last_close_dirty = False
_nxt_last_close_saved_at = 0.0
_NXT_RECALL_MAX_AGE_DAYS = 5   # 연휴 고려: 마지막 NXT 종가를 최대 5일까지 유효로 인정

def _nxt_close_file():
    base = getattr(config, 'DATA_DIR', None) or getattr(config, 'JSON_DIR', '.')
    return os.path.join(base, 'nxt_last_close.json')

def _nxt_load_last_close():
    global _nxt_last_close_loaded
    if _nxt_last_close_loaded:
        return
    _nxt_last_close_loaded = True
    try:
        path = _nxt_close_file()
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _nxt_last_close_lock:
                    _nxt_last_close.update(data)
    except Exception as e:
        logger.debug(f"[NXT] 마지막 종가 캐시 로드 실패: {e}")

def _nxt_save_last_close(force=False):
    """디스크 쓰기를 60초 throttle 한다(SD카드 보호). force=True면 즉시 저장."""
    global _nxt_last_close_dirty, _nxt_last_close_saved_at
    if not _nxt_last_close_dirty:
        return
    now = time.time()
    if not force and (now - _nxt_last_close_saved_at) < 60:
        return
    try:
        with _nxt_last_close_lock:
            snapshot = dict(_nxt_last_close)
        with open(_nxt_close_file(), 'w', encoding='utf-8') as f:
            json.dump(snapshot, f)
        _nxt_last_close_dirty = False
        _nxt_last_close_saved_at = now
    except Exception as e:
        logger.debug(f"[NXT] 마지막 종가 캐시 저장 실패: {e}")

def _nxt_remember_close(code, price):
    """거래시간에 받은 NXT 현재가를 '마지막 종가'로 기억한다."""
    global _nxt_last_close_dirty
    try:
        p = int(price)
    except (TypeError, ValueError):
        return
    if p <= 0:
        return
    _nxt_load_last_close()
    with _nxt_last_close_lock:
        _nxt_last_close[code] = {'price': p, 'date': datetime.now().strftime('%Y%m%d')}
    _nxt_last_close_dirty = True
    _nxt_save_last_close()

def _nxt_recalled_close(code):
    """야간/주말/휴장 시 보여줄 NXT 마지막 종가. 너무 오래된(>5일) 값은 폐기(0 반환)."""
    _nxt_load_last_close()
    with _nxt_last_close_lock:
        e = _nxt_last_close.get(code)
    if not e:
        return 0
    try:
        d = datetime.strptime(e.get('date', ''), '%Y%m%d')
        if (datetime.now() - d).days > _NXT_RECALL_MAX_AGE_DAYS:
            return 0
        return int(e.get('price', 0))
    except Exception:
        return 0

def _nxt_quote_phase():
    """실전 NXT 시세 처리 단계를 '한 번의 휴장 판정'으로 결정한다(중복 휴장조회 방지).
       'active'   : NXT 거래시간(프리 08:00~09:00 / 애프터 15:30~20:00) → 라이브 NXT 사용
       'offhours' : 야간(20:00~익일 08:00)·주말·휴장 → 라이브 NXT 시도 후 없으면 마지막 종가
       'skip'     : 정규장(09:00~15:30) 등 → KRX 대표가만 사용
    """
    try:
        holiday = _api().is_holiday_today()   # 주말·공휴일 포함
    except Exception:
        holiday = False
    now = datetime.now().strftime("%H%M")
    if not holiday and (("0800" <= now < "0900") or ("1530" <= now <= "2000")):
        return 'active'
    if holiday or now >= "2000" or now < "0800":
        return 'offhours'
    return 'skip'

# [최적화] 관심종목 멀티시세 세션 비활성 플래그 (TR 미지원 서버에서 1회 실패 후 재시도 방지)
_MULTI_PRICE_DISABLED = False
_MULTI_PRICE_DISABLED_AT = 0.0
# [Fix] 멀티시세 배치 실패 시 '세션 영구 비활성' → '쿨다운 일시 비활성'.
#  EGW00201(초당 거래건수 초과)·타임아웃 같은 일시 오류 1회로 세션 내내 배치가 꺼지면,
#  장전/장후(NXT)엔 현재가만 전일 종가로 굳고(등락률 0%) 체결강도는 별도 콜이라 신선하게
#  갱신되는 비대칭 stale이 생긴다(실측: 현대건설 강도 132%·등락률 0%). 쿨다운 후 재시도해
#  일시 오류에서 자동 복구한다. (TR 미지원 환경이어도 쿨다운당 1콜 낭비에 그침)
_MULTI_PRICE_RETRY_COOLDOWN_SEC = 600

def get_multi_current_prices(codes, market_div="J"):
    """[최적화] 관심종목(멀티종목) 시세조회(FHKST11300006)로 국내 현재가를 30종목/1콜 일괄 수집.

    종목당 1콜씩 나가던 현재가 REST를 N/30콜로 줄여 TPS 소모를 대폭 절감한다
    (모의투자 2 TPS 환경에서 특히 효과 큼). 응답 필드를 개별 현재가 API(output) 이름으로
    정규화해 반환하므로 호출측은 기존 필드명 그대로 사용한다.
      stck_prpr←inter2_prpr, prdy_vrss←inter2_prdy_vrss, stck_oprc/hgpr/lwpr←inter2_*,
      stck_sdpr←inter2_sdpr, stck_prdy_clpr←inter2_prdy_clpr,
      rprs_mrkt_kor_name←kospi_kosdaq_cls_name (prdy_ctrt/prdy_vrss_sign/acml_vol는 동일명)
    52주 고저(w52_*)는 이 TR이 제공하지 않으므로 '_src'='multi' 마커를 남기고,
    호출측(_analyze_table_row)이 차트(250봉)로 보강한다.

    반환: {code: 정규화 output dict}. TR 미지원(모의 등)·오류 시 None을 반환하며,
    쿨다운(_MULTI_PRICE_RETRY_COOLDOWN_SEC) 동안 비활성화되어 호출측이 종목별 조회로 폴백한다.
    (쿨다운 경과 후 자동 재시도 — 일시 오류로 세션 전체가 영구 비활성되지 않도록)
    """
    global _MULTI_PRICE_DISABLED, _MULTI_PRICE_DISABLED_AT
    if _MULTI_PRICE_DISABLED:
        if time.time() - _MULTI_PRICE_DISABLED_AT < _MULTI_PRICE_RETRY_COOLDOWN_SEC:
            return None
        _MULTI_PRICE_DISABLED = False  # 쿨다운 경과 → 재시도 허용
    if not codes or config.session.is_toss:
        return None
    if not getattr(config, 'USE_MULTI_PRICE', True):
        return None
    result = {}
    try:
        for i in range(0, len(codes), 30):
            chunk = codes[i:i + 30]
            params = {}
            for j, c in enumerate(chunk, start=1):
                params[f"FID_COND_MRKT_DIV_CODE_{j}"] = market_div
                params[f"FID_INPUT_ISCD_{j}"] = c
            res = _api().call_api("uapi/domestic-stock/v1/quotations/intstock-multprice",
                           "domestic", "quotations", "multi_price", params=params,
                           tr_id="FHKST11300006", timeout=5, retries=1)
            if not res or res.get('rt_cd') != '0':
                raise RuntimeError(f"rt_cd={res.get('rt_cd') if res else None} msg={res.get('msg1', '') if res else ''}")
            outputs = res.get('output') or res.get('output1') or []
            for row in outputs:
                code = str(row.get('inter_shrn_iscd', '')).strip()
                prpr = str(row.get('inter2_prpr', '')).strip()
                if not code or not prpr:
                    continue
                result[code] = {
                    '_src': 'multi',
                    'stck_prpr': prpr,
                    'prdy_vrss': row.get('inter2_prdy_vrss', '0'),
                    'prdy_vrss_sign': row.get('prdy_vrss_sign', ''),
                    'prdy_ctrt': row.get('prdy_ctrt', '0'),
                    'acml_vol': row.get('acml_vol', '0'),
                    'stck_oprc': row.get('inter2_oprc', '0'),
                    'stck_hgpr': row.get('inter2_hgpr', '0'),
                    'stck_lwpr': row.get('inter2_lwpr', '0'),
                    'stck_sdpr': row.get('inter2_sdpr', '0'),
                    'stck_prdy_clpr': row.get('inter2_prdy_clpr', '0'),
                    'rprs_mrkt_kor_name': row.get('kospi_kosdaq_cls_name', ''),
                }
        if not result:
            raise RuntimeError("응답에 유효 종목 없음")

        # [보강] 실전 응답에서 kospi_kosdaq_cls_name이 빈 값으로 오는 경우가 실측 확인되어,
        # 관심목록(stock.json)의 exchange 정보로 시장구분을 보강한다.
        # (시장 국면 보정에서 코스닥 종목이 KOSPI로 오분류되는 것 방지)
        try:
            exch_map = {}
            sd = getattr(config.session, 'stock_data', None) or {}
            for key in ("stocks_kr", "etfs_kr"):
                for s in sd.get(key, []):
                    if s.get('code') and s.get('exchange'):
                        exch_map[s['code']] = str(s['exchange']).upper()
            for c, out in result.items():
                if not out.get('rprs_mrkt_kor_name'):
                    out['rprs_mrkt_kor_name'] = exch_map.get(c, '')
        except Exception:
            pass

        return result
    except Exception as e:
        _MULTI_PRICE_DISABLED = True
        _MULTI_PRICE_DISABLED_AT = time.time()
        logger.info(f"[MultiPrice] 관심종목 멀티시세 일시 비활성({_MULTI_PRICE_RETRY_COOLDOWN_SEC}s): {e} → 종목별 현재가 조회로 폴백")
        return None

# [최적화] NXT 멀티시세 비활성 플래그 (KRX 'J' 멀티시세와 분리 — NX 미지원이 J를 끄지 않도록)
#  [Fix] J와 동일하게 쿨다운 일시 비활성: 장전/장후 EGW00201 1회로 NX 병합이 세션 내내 꺼지면
#  현재가가 KRX(전일 종가)로 굳어 '강도만 신선한' stale 증상이 재발하므로 쿨다운 후 재시도한다.
_MULTI_PRICE_NXT_DISABLED = False
_MULTI_PRICE_NXT_DISABLED_AT = 0.0

def _fetch_multi_nxt_raw(codes):
    """NXT(NX) 멀티시세 배치 → {code: {'prpr':int,'vol':int}}. 미지원/오류 시 빈 dict(쿨다운 비활성)."""
    global _MULTI_PRICE_NXT_DISABLED, _MULTI_PRICE_NXT_DISABLED_AT
    if _MULTI_PRICE_NXT_DISABLED:
        if time.time() - _MULTI_PRICE_NXT_DISABLED_AT < _MULTI_PRICE_RETRY_COOLDOWN_SEC:
            return {}
        _MULTI_PRICE_NXT_DISABLED = False  # 쿨다운 경과 → 재시도 허용
    if not codes or config.session.is_simulation:
        return {}
    out = {}
    try:
        for i in range(0, len(codes), 30):
            chunk = codes[i:i + 30]
            params = {}
            for j, c in enumerate(chunk, start=1):
                params[f"FID_COND_MRKT_DIV_CODE_{j}"] = "NX"
                params[f"FID_INPUT_ISCD_{j}"] = c
            res = _api().call_api("uapi/domestic-stock/v1/quotations/intstock-multprice",
                           "domestic", "quotations", "multi_price", params=params,
                           tr_id="FHKST11300006", timeout=5, retries=1)
            if not res or res.get('rt_cd') != '0':
                raise RuntimeError(f"rt_cd={res.get('rt_cd') if res else None} msg={res.get('msg1', '') if res else ''}")
            for row in (res.get('output') or res.get('output1') or []):
                c = str(row.get('inter_shrn_iscd', '')).strip()
                p = _api().safe_int(row.get('inter2_prpr'))
                if c and p > 0:  # nxtSupported=false 종목은 prpr 0 → 제외(KRX 값 유지)
                    out[c] = {'prpr': p, 'vol': _api().safe_int(row.get('acml_vol'))}
        return out
    except Exception as e:
        _MULTI_PRICE_NXT_DISABLED = True
        _MULTI_PRICE_NXT_DISABLED_AT = time.time()
        logger.info(f"[MultiPrice] NXT 멀티시세 일시 비활성({_MULTI_PRICE_RETRY_COOLDOWN_SEC}s): {e} → NXT 병합 생략(KRX 대표가 사용)")
        return {}

def get_multi_current_prices_nxt(codes):
    """KRX(J) 멀티시세에 NXT(NX) 멀티시세를 병합해 반환(장전 08:00~09:00·장후 15:30~20:00용).

    종목별 fetch_nxt_price(NX 단건) 팬아웃이 EGW00201(초당 거래건수 초과)을 유발해 현재가가
    전일종가로 stale 폴백되던 문제를, NX도 30종목/1콜 배치로 바꿔 콜 수를 대폭 줄인다.
    NX에 살아있는 체결가가 있으면 그 값으로 stck_prpr을 교체(등락률은 표시부가 stck_sdpr
    기준으로 재계산). NX 미지원/실패 시 KRX 결과만 반환(세션 내 NXT 병합 자동 비활성).
    """
    base = get_multi_current_prices(codes)  # KRX 'J' (실패 시 None → 종목별 폴백)
    if not base:
        return base
    nxt = _fetch_multi_nxt_raw(codes)
    if not nxt:
        return base
    for c, o in base.items():
        n = nxt.get(c)
        if n and n['prpr'] > 0:
            o['stck_prpr'] = str(n['prpr'])
            if n['vol'] > 0:
                o['acml_vol'] = str(n['vol'])
    return base
