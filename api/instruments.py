"""종목 속성 판별 — NXT(대체거래소) 취급 여부와 국내 ETF/ETN 구분.

주문 경로(SOR)와 시세 경로가 종목 성격에 따라 갈리므로, 그 판별을 한곳에 모은다.
NXT 마스터는 파일에서 읽어 캐시하며, 로드 실패 시에는 쿨다운을 두고 재시도한다
(주문마다 파일 접근으로 붙잡히지 않게).
"""
import logging
import threading
import time
import requests
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

# [추가] 대체거래소(NXT) 관련 마스터 파일 캐시
_NXT_TRADEABLE_CACHE = set()
_NXT_REJECTED_CACHE = set()           # SOR 주문이 실제로 거부된 종목(증권사 응답으로 학습)
_NXT_MASTER_LOADED = False
_NXT_MASTER_LOCK = threading.RLock()
_NXT_MASTER_RETRY_AT = 0.0            # 로드 실패 시 다음 재시도가 허용되는 시각(epoch)
NXT_MASTER_RETRY_COOLDOWN = 300.0     # 실패 후 재시도 간격(초). 주문마다 5초씩 붙잡히지 않게 한다.

def load_nxt_master():
    """KIS API의 NXT 종목 마스터 파일을 다운로드하여 거래 가능 종목 코드를 추출합니다.

    [실패 처리] 종전에는 실패해도 finally에서 _NXT_MASTER_LOADED를 True로 못 박아
     프로세스 수명 내내 재시도하지 않았고, 실패 로그도 debug 레벨이라 기본 설정
     (FILE_DEBUG_LEVEL=INFO)에서는 파일에 남지도 않았다. 캐시가 빈 상태에서
     is_nxt_tradeable은 전 종목 True를 돌려주므로 NXT 미지원 종목(ETF 등)에도 SOR이
     붙어 주문이 APBK3026으로 거부된다 — 매수뿐 아니라 **보유 종목의 매도까지** 같은
     경로라 청산이 막힌다. 기동 시 5초 타임아웃 한 번으로 그 상태가 결정되는데 아무도
     모른다는 것이 문제였다(라즈베리파이는 패키지 적용으로 수시로 재시작된다).
     → 성공했을 때만 완료로 표시하고, 실패는 경고로 남긴 뒤 쿨다운 후 다시 시도한다.
    """
    global _NXT_MASTER_LOADED, _NXT_MASTER_RETRY_AT
    with _NXT_MASTER_LOCK:
        if _NXT_MASTER_LOADED: return
        if time.time() < _NXT_MASTER_RETRY_AT: return   # 쿨다운 중

        try:
            # NXT 마스터 파일 다운로드 및 파싱을 시도합니다.
            base_url = config.REAL_URL
            # KIS OpenAPI 대체거래소 종목정보 다운로드 API 경로
            url_path = "uapi/domestic-stock/v1/quotations/nxt-master" 
            
            token = _api().get_current_token()
            key = config.session.real_app_key
            secret = config.session.real_app_secret
            
            headers = {
                "authorization": f"Bearer {token}",
                "appKey": key,
                "appSecret": secret,
                "tr_id": "CTCA0703C", # 대체거래소 마스터 조회 TR_ID
                "custtype": "P"
            }
            
            res = requests.get(f"{base_url}/{url_path}", headers=headers, timeout=5)
            if res.status_code == 200:
                # 마스터 파일 파싱 (한 줄씩 파이프(|) 구분되어 있다고 가정)
                lines = res.text.splitlines()
                for line in lines:
                    parts = line.split('|')
                    if len(parts) > 0 and len(parts[0]) == 6 and parts[0][0].isdigit():
                        _NXT_TRADEABLE_CACHE.add(parts[0])
                logger.info(f"NXT 거래 가능 종목 마스터 파일 로드 완료 ({len(_NXT_TRADEABLE_CACHE)}종목)")
            else:
                raise RuntimeError(f"HTTP {res.status_code}")
        except Exception as e:
            reason = e
        else:
            # 파싱까지 마쳤는데 캐시가 비었다면 성공으로 볼 수 없다(스펙 변경·빈 응답).
            reason = None if _NXT_TRADEABLE_CACHE else "응답에 종목 코드가 없음"

        if reason is None:
            _NXT_MASTER_LOADED = True
            _NXT_MASTER_RETRY_AT = 0.0
        else:
            # [가시화] debug가 아니라 warning — 이 상태에서는 NXT 미지원 종목의 주문(매도 포함)이
            #  거래소 코드 오배정으로 거부될 수 있으므로 운영자가 로그에서 볼 수 있어야 한다.
            _NXT_MASTER_RETRY_AT = time.time() + NXT_MASTER_RETRY_COOLDOWN
            logger.warning(
                f"NXT 마스터 파일 로드 실패 ({reason}) — 거래소 코드를 SOR로 낙관 배정합니다. "
                f"미지원 종목은 주문 거부 후 KRX로 자동 재시도됩니다. "
                f"{int(NXT_MASTER_RETRY_COOLDOWN)}초 뒤 마스터를 다시 받습니다.")

def is_nxt_tradeable(code):
    """NXT 거래 대상 종목 여부를 확인합니다."""
    if not _NXT_MASTER_LOADED:
        load_nxt_master()

    # 0. SOR 주문이 실제로 거부됐던 종목은 마스터보다 증권사 응답을 믿는다.
    if code in _NXT_REJECTED_CACHE:
        return False

    # 1. 마스터 파일이 정상 로드되어 캐시에 종목이 있는 경우
    if _NXT_TRADEABLE_CACHE:
        return code in _NXT_TRADEABLE_CACHE
        
    # 2. 마스터 로드에 실패했거나 미지원 상태일 경우
    # 안전장치로 일단 일반 주식은 모두 통과시킵니다 (오류로 매매 못하는 것 방지).
    # 이 낙관 배정으로 NXT 미지원 종목이 거부되면 place_order가 KRX로 1회 재시도한다.
    return True

# [추가] 국내 ETF/ETN 판정용 캐시 및 브랜드/키워드 목록
#  - 관심목록(etfs_kr)에 없더라도 보유 중인 ETF/ETN을 식별하기 위함.
#  - 1GB 라즈베리파이 운영 및 모의투자 API 한계를 고려해 매 주기 API 호출 대신
#    관심목록 + 종목명 브랜드/키워드 휴리스틱으로 판정하고 코드 단위로 캐시한다.
_ETF_ETN_CACHE = {}
_KR_ETF_BRANDS = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF", "HANARO", "ACE", "SOL",
    "PLUS", "RISE", "TIMEFOLIO", "KOACT", "WOORI", "BNK", "FOCUS", "TREX",
    "KCGI", "VITA", "KINDEX", "에셋플러스", "마이다스", "히어로즈", "마이티",
)
_KR_ETF_ETN_KEYWORDS = ("레버리지", "인버스", "ETN", "ETF", "선물")

def is_domestic_etf_etn(code, name=""):
    """국내 보유/관심 종목이 ETF/ETN인지 판정한다.
    1) 관심목록(etfs_kr) 등록 여부, 2) 종목명 브랜드 프리픽스/키워드 휴리스틱.
    결과는 코드 단위로 캐시한다. (해외 종목은 호출 측에서 사전 제외)"""
    if not code:
        return False
    if code in _ETF_ETN_CACHE:
        return _ETF_ETN_CACHE[code]

    result = False
    try:
        # 1) 관심목록(국내 ETF)에 등록된 경우
        sd = getattr(config.session, 'stock_data', None) if config.session else None
        etfs = sd.get('etfs_kr', []) if sd else []
        if any(e.get('code') == code for e in etfs):
            result = True
        else:
            # 2) 종목명 기반 휴리스틱 (브랜드 프리픽스 또는 ETF/ETN/레버리지 등 키워드)
            nm = (name or "").upper().replace(" ", "")
            if nm and (any(nm.startswith(b) for b in _KR_ETF_BRANDS)
                       or any(k in nm for k in _KR_ETF_ETN_KEYWORDS)):
                result = True
    except Exception:
        result = False

    _ETF_ETN_CACHE[code] = result
    return result
