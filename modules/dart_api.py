# modules/dart_api.py
"""OpenDART (전자공시) 연동 계층 - 국내 배당/실적/공시 조회.

api.py에서 분리된 구현. 기존 호출부와의 호환을 위해 api.py가 동일 이름으로
재수출(re-export)하므로, 호출·테스트 patch 는 계속 api.call_dart 방식으로 동작한다.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger(__name__)


def _api():
    """호출 시점의 api 모듈 반환.

    내부 상호 호출(get_dart_dividend → call_dart 등)도 api.* 네임스페이스를
    경유시켜, 테스트의 patch.object(api, "call_dart") 등이 분리 전과 동일하게
    내부 호출까지 적용되도록 유지하기 위한 접근자다. (지연 import라 순환 없음)
    """
    import api
    return api


DART_BASE_URL = "https://opendart.fss.or.kr/api"
_dart_corp_map_cache = None  # 프로세스 메모리 캐시
_dart_corp_map_lock = threading.Lock()  # [중요] 동시 다운로드 방지용 락


def call_dart(endpoint, params, timeout=10):
    """OpenDART OpenAPI 공통 호출 래퍼.

    반환: 성공 시 응답 JSON의 'list'(없으면 dict 전체), 실패/데이터없음 시 None.
    status: 000=정상, 013=데이터없음, 020/021=한도초과/오류.
    """
    if not config.DART_API_KEY:
        return None
    try:
        p = dict(params)
        p["crtfc_key"] = config.DART_API_KEY
        res = requests.get(f"{DART_BASE_URL}/{endpoint}", params=p, timeout=timeout)
        data = res.json()
        status = data.get("status")
        if status == "000":
            return data.get("list", data)
        if status == "013":  # 조회된 데이터 없음 (정상 케이스)
            return None
        logger.warning(f"[DART] {endpoint} 응답 코드 {status}: {data.get('message')}")
        return None
    except Exception as e:
        logger.error(f"[DART] {endpoint} 호출 오류: {e}")
        return None


def get_dart_corp_map(force_refresh=False):
    """종목코드(6자리) -> DART 고유번호(corp_code, 8자리) 매핑.

    corpCode.xml(ZIP) 1회 다운로드 후 json 파일로 캐시(30일 TTL).
    """
    global _dart_corp_map_cache
    if _dart_corp_map_cache is not None and not force_refresh:
        return _dart_corp_map_cache

    if not config.DART_API_KEY:
        return {}

    # [중요] 동시 다운로드 방지: 여러 워커 스레드(공시 수집 등)가 동시에 진입하면
    # 각자 DART 기업코드 ZIP(수십 MB XML+10만건 dict)을 중복 다운로드/파싱해 메모리가
    # 수배로 폭증(OOM)한다. 락으로 직렬화하여 한 스레드만 받고 나머지는 캐시를 재사용한다.
    with _dart_corp_map_lock:
        # 락 획득 후 재확인 (대기 중 다른 스레드가 이미 채웠을 수 있음)
        if _dart_corp_map_cache is not None and not force_refresh:
            return _dart_corp_map_cache

        return _load_dart_corp_map_locked(force_refresh)


def _load_dart_corp_map_locked(force_refresh):
    """락 보유 상태에서 DART 기업코드 맵을 파일캐시/다운로드로 로드한다."""
    global _dart_corp_map_cache
    cache_path = os.path.join(config.JSON_DIR, "dart_corp_map.json")

    # 파일 캐시 확인 (30일 이내면 재사용)
    if not force_refresh and os.path.exists(cache_path):
        try:
            age_days = (time.time() - os.path.getmtime(cache_path)) / 86400.0
            if age_days < 30:
                with open(cache_path, "r", encoding="utf-8") as f:
                    _dart_corp_map_cache = json.load(f)
                return _dart_corp_map_cache
        except Exception:
            pass

    # 신규 다운로드 (ZIP 안에 CORPCODE.xml)
    try:
        import zipfile, io
        import xml.etree.ElementTree as ET
        res = requests.get(f"{DART_BASE_URL}/corpCode.xml",
                           params={"crtfc_key": config.DART_API_KEY}, timeout=20)

        # [메모리 최적화] 전체 XML(수십 MB)을 트리로 올리지 않고 스트리밍 파싱(iterparse)으로
        # <list> 요소를 하나씩 처리 후 즉시 비워(clear) 메모리 피크를 최소화한다. (저사양 보호)
        corp_map = {}
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            with zf.open(zf.namelist()[0]) as xmlf:
                for _evt, item in ET.iterparse(xmlf, events=("end",)):
                    if item.tag != "list":
                        continue
                    stock_code = (item.findtext("stock_code") or "").strip()
                    corp_code = (item.findtext("corp_code") or "").strip()
                    if stock_code and corp_code:  # 상장사만 (비상장은 stock_code 공란)
                        corp_map[stock_code] = corp_code
                    item.clear()  # 처리한 요소 즉시 해제

        if corp_map:
            _dart_corp_map_cache = corp_map
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(corp_map, f)
            except Exception as e:
                logger.warning(f"[DART] corp_map 캐시 저장 실패: {e}")
            return corp_map
    except Exception as e:
        logger.error(f"[DART] corp_map 다운로드 오류: {e}")

    return _dart_corp_map_cache or {}


def get_dart_dividend(stock_code, year=None, reprt_code="11011"):
    """국내 종목의 '배당에 관한 사항' 조회 (정기보고서 기준).

    반환: {'주당배당금': float, '시가배당률': float, '결산월': str, 'year': str} 또는 None.
    reprt_code: 11011=사업보고서(연간), 11012=반기, 11013=1분기, 11014=3분기.
    """
    if year is None:
        # 사업보고서는 다음 해 3월경 공시되므로 직전 회계연도를 우선 조회
        year = datetime.now().year - 1

    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return None

    rows = _api().call_dart("alotMatter.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code
    })
    if not rows or not isinstance(rows, list):
        return None

    def _to_num(s):
        try:
            return float(str(s).replace(",", "").strip())
        except Exception:
            return 0.0

    result = {"year": str(year), "주당배당금": 0.0, "시가배당률": 0.0}
    for row in rows:
        se = (row.get("se") or "").strip()          # 항목명
        val = row.get("thstrm")                       # 당기 값
        # 주당 현금배당금(원) / 현금배당수익률(%) 추출 (보통주 기준)
        if "주당 현금배당금" in se or ("주당배당금" in se and "현금" in se):
            num = _to_num(val)
            if num > result["주당배당금"]:
                result["주당배당금"] = num
        elif "현금배당수익률" in se or "시가배당" in se:
            num = _to_num(val)
            if num > result["시가배당률"]:
                result["시가배당률"] = num

    if result["주당배당금"] <= 0 and result["시가배당률"] <= 0:
        return None
    return result


_dart_acc_month_cache = {}  # 종목코드 -> 결산월


def get_dart_acc_month(stock_code):
    """종목의 결산월('12' 등) 조회 (company.json). 프로세스 메모리 캐시."""
    if stock_code in _dart_acc_month_cache:
        return _dart_acc_month_cache[stock_code]

    acc = None
    corp = _api().get_dart_corp_map().get(stock_code)
    if corp:
        data = _api().call_dart("company.json", {"corp_code": corp})
        if isinstance(data, dict):
            acc = (data.get("acc_mt") or "").strip() or None
    _dart_acc_month_cache[stock_code] = acc
    return acc


def get_dart_disclosures(stock_code, days=30, pblntf_ty=None, page_count=100):
    """종목의 최근 공시 목록 조회 (list.json).

    반환: [{rcept_no, report_nm, flr_nm, rcept_dt, rm, corp_name}, ...] (최신순). 실패 시 [].
    pblntf_ty: 공시유형 코드(A정기/B주요사항/C발행/D지분 등). None이면 전체.
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    end = datetime.now()
    bgn = end - timedelta(days=int(days))
    params = {
        "corp_code": corp,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": str(page_count),
        "sort": "date", "sort_mth": "desc",
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    rows = _api().call_dart("list.json", params)
    if not rows or not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        out.append({
            "rcept_no": r.get("rcept_no", ""),
            "report_nm": (r.get("report_nm") or "").strip(),
            "flr_nm": (r.get("flr_nm") or "").strip(),
            "rcept_dt": (r.get("rcept_dt") or "").strip(),
            "rm": (r.get("rm") or "").strip(),
            "corp_name": (r.get("corp_name") or "").strip(),
        })
    return out
