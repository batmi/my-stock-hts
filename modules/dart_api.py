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


def _rcept_date(row):
    """접수일자(YYYYMMDD). rcept_dt가 없으면 접수번호 앞 8자리로 복원한다.

    DART API는 계열에 따라 날짜 필드 제공 여부가 다르다(실측 2026-07-22, 삼성전자):
      - 공시목록 계열(list/elestock/majorstock): rcept_dt 제공 ✅
      - 주요사항보고서 '결정' 계열(자기주식·메자닌·무상증자·감자): rcept_dt **미제공** ❌
        → 응답 키에 아예 없고 접수번호(14자리) 앞 8자리가 접수일자다.
          예: rcept_no=20260713000395 → 2026-07-13
    이 복원이 없으면 화면의 '일자' 칸이 공백이 되고, rcept_dt 기준 정렬도 전부 빈 문자열
    비교가 되어 최신순 정렬이 무효화된다.
    """
    dt = str(row.get("rcept_dt") or "").replace("-", "").strip()
    if len(dt) == 8 and dt.isdigit():
        return dt
    head = str(row.get("rcept_no") or "").strip()[:8]
    return head if len(head) == 8 and head.isdigit() else ""


def _fill_rcept_dt(rows):
    """결정 계열 응답에 rcept_dt를 주입해 호출측이 날짜 필드를 그대로 쓰게 한다."""
    if not isinstance(rows, list):
        return []
    for r in rows:
        if isinstance(r, dict):
            r["rcept_dt"] = _rcept_date(r)
    return rows


def _dart_num(s):
    """DART 숫자 문자열('1,234', '△12' 등) -> float. 파싱 불가 시 None."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("△", "-").replace("▲", "-").strip()
    if t in ("", "-", "0-"):
        return None
    try:
        return float(t)
    except Exception:
        return None


def get_dart_insider_trades(stock_code, since=None):
    """임원·주요주주 특정증권등 소유상황 보고 (elestock.json, 최신순).

    반환: [{rcept_no, rcept_dt, repror, ofcps, main_shrholdr, qty, chg, rate, rate_chg}, ...]
    qty=보유 특정증권 수, chg=증감 수량(+매수/-매도), rate=보유비율(%).
    since: 'YYYYMMDD' — 응답이 전체 이력(수천 건)이라 이 날짜 이전은 정규화 전에 버려
           저사양 환경의 메모리 사용을 줄인다.
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("elestock.json", {"corp_code": corp})
    if not rows or not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if since and (r.get("rcept_dt") or "").replace("-", "") < since:
            continue
        out.append({
            "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "repror": (r.get("repror") or "").strip(),
            "ofcps": (r.get("isu_exctv_ofcps") or "").strip(),
            "main_shrholdr": (r.get("isu_main_shrholdr") or "").strip(),
            "qty": _dart_num(r.get("sp_stock_lmp_cnt")),
            "chg": _dart_num(r.get("sp_stock_lmp_irds_cnt")),
            "rate": _dart_num(r.get("sp_stock_lmp_rate")),
            "rate_chg": _dart_num(r.get("sp_stock_lmp_irds_rate")),
        })
    return out


def get_dart_major_holdings(stock_code):
    """대량보유(5%) 상황 보고 (majorstock.json, 최신순).

    반환: [{rcept_no, rcept_dt, repror, reason, qty, chg, rate, rate_chg}, ...]
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("majorstock.json", {"corp_code": corp})
    if not rows or not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        out.append({
            "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "repror": (r.get("repror") or "").strip(),
            "reason": " ".join((r.get("report_resn") or "").split()),
            "qty": _dart_num(r.get("stkqy")),
            "chg": _dart_num(r.get("stkqy_irds")),
            "rate": _dart_num(r.get("stkrt")),
            "rate_chg": _dart_num(r.get("stkrt_irds")),
        })
    return out


def get_dart_financials(stock_code, year, reprt_code):
    """단일회사 주요계정 (fnlttSinglAcnt.json) 원본 rows. 없으면 None.

    reprt_code: 11011=사업, 11012=반기, 11013=1분기, 11014=3분기.
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return None
    rows = _api().call_dart("fnlttSinglAcnt.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code
    })
    return rows if isinstance(rows, list) else None


def get_dart_paid_increase_detail(stock_code, bgn_de, end_de):
    """유상증자 결정 세부내역 (piicDecsn.json). 없으면 [].

    주요 필드: nstk_ostk_cnt(신주 보통주), nstk_estk_cnt(신주 기타주),
    bfic_tisstk_ostk(증자 전 발행주식총수), ic_mthn(증자방식), fdpp_*(자금 목적).
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("piicDecsn.json", {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    return rows if isinstance(rows, list) else []


_BOND_ENDPOINTS = {
    "CB": "cvbdIsDecsn.json",   # 전환사채
    # [Fix] 신주인수권부사채는 bdwtIsDecsn. 기존 'bwbdIsDecsn'는 존재하지 않는 URL이라
    #  DART가 status 101(잘못된 URL)을 돌려주었고, BW 오버행이 조회 자체가 되지 않았다.
    #  (실측 2026-07-22: bwbdIsDecsn→101 / bdwtIsDecsn→013 '조회된 데이타가 없습니다')
    "BW": "bdwtIsDecsn.json",   # 신주인수권부사채
    "EB": "exbdIsDecsn.json",   # 교환사채
}


def get_dart_bond_issue_detail(stock_code, bgn_de, end_de, kind="CB"):
    """메자닌(CB/BW/EB) 발행 결정 세부내역. 없으면 [].

    주요 필드: bd_fta(권면총액), cv_prc/ex_prc(전환/행사가액), bdis_mthn(발행방법).
    """
    endpoint = _BOND_ENDPOINTS.get(kind)
    if not endpoint:
        return []
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    rows = _api().call_dart(endpoint, {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    # [Fix] 이 계열은 rcept_dt를 주지 않으므로 접수번호에서 복원해 주입한다(_rcept_date 참조)
    return _fill_rcept_dt(rows)


def _decsn_rows(stock_code, endpoint, bgn_de, end_de):
    """주요사항보고서 결정 계열(기간 조회) 공통 래퍼. 없으면 [].

    이 계열(자기주식·무상증자·감자 등)은 rcept_dt를 주지 않아 접수번호에서 복원해 주입한다.
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    rows = _api().call_dart(endpoint, {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    return _fill_rcept_dt(rows)


def get_dart_treasury_decisions(stock_code, bgn_de, end_de):
    """자기주식 취득/처분/신탁계약 체결 결정 (수급 신호 — 회사 단위 매수는 내부자 개인 매매보다 강함).

    반환: [{kind, rcept_no, rcept_dt, qty, amount, bgd, edd, note}, ...] (최신순)
    kind: '취득'|'처분'|'신탁체결'. amount=예정금액(원), qty=예정주식수(보통주).
    """
    out = []
    # 1) 직접 취득 결정
    for r in _decsn_rows(stock_code, "tsstkAqDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "취득", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": _dart_num(r.get("aqpln_stk_ostk")),
            "amount": _dart_num(r.get("aqpln_prc_ostk")),
            "bgd": (r.get("aqexpd_bgd") or "").strip(),
            "edd": (r.get("aqexpd_edd") or "").strip(),
            "note": " ".join((r.get("aq_pp") or "").split()),  # 취득목적
        })
    # 2) 처분 결정
    for r in _decsn_rows(stock_code, "tsstkDpDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "처분", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": _dart_num(r.get("dppln_stk_ostk")),
            "amount": _dart_num(r.get("dppln_prc_ostk")),
            "bgd": (r.get("dpprpd_bgd") or "").strip(),
            "edd": (r.get("dpprpd_edd") or "").strip(),
            "note": " ".join((r.get("dp_pp") or "").split()),  # 처분목적
        })
    # 3) 신탁계약 체결 결정 (간접 취득)
    for r in _decsn_rows(stock_code, "tsstkAqTrctrCnsDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "신탁체결", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": None,
            "amount": _dart_num(r.get("ctr_prc")),
            "bgd": (r.get("ctr_pd_bgd") or "").strip(),
            "edd": (r.get("ctr_pd_edd") or "").strip(),
            "note": "신탁계약",
        })
    out.sort(key=lambda r: r["rcept_dt"], reverse=True)
    return out


def get_dart_free_increase_detail(stock_code, bgn_de, end_de):
    """무상증자 결정 세부내역 (fricDecsn.json). 없으면 [].

    주요 필드: nstk_ostk_cnt(신주 보통주 수), nstk_ascnt_ps_ostk(1주당 배정 주식수),
    nstk_asstd(신주배정기준일), bfic_tisstk_ostk(증자 전 발행주식총수).
    """
    return _decsn_rows(stock_code, "fricDecsn.json", bgn_de, end_de)


def get_dart_capital_reduction_detail(stock_code, bgn_de, end_de):
    """감자 결정 세부내역 (crDecsn.json). 없으면 [].

    주요 필드: cr_rt_ostk(감자비율 %), cr_std(감자기준일), cr_mth(감자방법), cr_rs(감자사유).
    """
    return _decsn_rows(stock_code, "crDecsn.json", bgn_de, end_de)


_dart_shares_cache = {}  # 종목코드 -> (발행주식총수, 유통주식수) — 정기보고서 기준이라 프로세스 캐시로 충분


def get_dart_shares_outstanding(stock_code):
    """주식의 총수 현황 (stockTotqySttus.json) — (발행주식총수, 유통주식수) 또는 (None, None).

    최근 사업/분기보고서 순으로 조회. 오버행(전환물량) 비중 계산 등에 사용.
    """
    if stock_code in _dart_shares_cache:
        return _dart_shares_cache[stock_code]
    corp = _api().get_dart_corp_map().get(stock_code)
    result = (None, None)
    if corp:
        y = datetime.now().year
        for year, reprt in ((y - 1, "11011"), (y - 2, "11011")):
            rows = _api().call_dart("stockTotqySttus.json", {
                "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt})
            if not isinstance(rows, list):
                continue
            for r in rows:
                se = (r.get("se") or "").replace(" ", "")
                if "보통주" in se or "합계" in se:
                    tot = _dart_num(r.get("istc_totqy"))
                    distb = _dart_num(r.get("distb_stock_co"))
                    if tot:
                        result = (tot, distb)
                        break
            if result[0]:
                break
    _dart_shares_cache[stock_code] = result
    return result


# 재무지표 분류코드 (fnlttSinglIndx.json idx_cl_code)
DART_INDEX_CLASSES = {
    "M210000": "수익성", "M220000": "안정성", "M230000": "성장성", "M240000": "활동성",
}


def get_dart_financial_index(stock_code, year, reprt_code, idx_cl_code):
    """단일회사 주요 재무지표 (fnlttSinglIndx.json) — DART가 계산한 지표 원본 rows.

    idx_cl_code: M210000 수익성 / M220000 안정성 / M230000 성장성 / M240000 활동성.
    row 필드: idx_nm(지표명), idx_val(값), bsns_year, stlm_dt. 없으면 None.
    """
    corp = _api().get_dart_corp_map().get(stock_code)
    if not corp:
        return None
    rows = _api().call_dart("fnlttSinglIndx.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code,
        "idx_cl_code": idx_cl_code,
    })
    return rows if isinstance(rows, list) else None


# ---------------------------------------------------------------------------
# 공시 원문(document.xml) 기반 잠정실적 파싱
# ---------------------------------------------------------------------------
def get_dart_document_text(rcept_no):
    """공시 원문(document.xml ZIP)을 내려받아 태그를 제거한 텍스트 반환. 실패 시 None."""
    if not config.DART_API_KEY or not rcept_no:
        return None
    try:
        import io
        import re
        import zipfile
        res = requests.get(f"{DART_BASE_URL}/document.xml",
                           params={"crtfc_key": config.DART_API_KEY, "rcept_no": rcept_no},
                           timeout=15)
        if not res.content.startswith(b"PK"):  # ZIP이 아니면 오류 JSON
            return None
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            raw = zf.read(zf.namelist()[0])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp949", errors="replace")
        text = re.sub(r"<[^>]+>", "\n", text)
        import html as _html
        return _html.unescape(text)
    except Exception as e:
        logger.debug(f"[DART] document.xml({rcept_no}) 조회 실패: {e}")
        return None


# 잠정실적 표의 숫자/증감 셀 토큰 (숫자, %, 흑전·적전 등)
_EARNINGS_TOKEN = None  # 모듈 임포트 시점 컴파일 비용 회피 (지연 컴파일)


def _earnings_token_re():
    global _EARNINGS_TOKEN
    if _EARNINGS_TOKEN is None:
        import re
        _EARNINGS_TOKEN = re.compile(
            r"^[-+△▲(]?\s*[\d,]+(?:\.\d+)?\s*[)%]?$|^(?:-|흑전|적전|흑자전환|적자전환|적자지속|흑자지속)$")
    return _EARNINGS_TOKEN


def parse_earnings_brief(text):
    """잠정실적/손익구조변동 공시 텍스트에서 매출·영업이익·순이익을 추출 (best-effort).

    반환: {"unit": 배수(원), "rows": {지표명: (당기, 전년동기/전기, 증감률str|None)}} 또는 None.
    '-'(빈 셀) 제거 후 남은 열 수로 레이아웃 판별:
      5+열=[당기,직전,QoQ,전년동기,YoY], 4열=[당기,전기,증감액,증감률],
      3열=[당기,전기,증감률], 2열=[당기,비교값(증감률은 직접 계산)].
    """
    if not text:
        return None
    import re
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    unit = 1.0
    for ln in lines[:400]:
        m = re.search(r"단위\s*[::]?\s*(조원|백만원|천만원|억원|천원|원)", ln.replace(" ", ""))
        if m:
            unit = {"조원": 1e12, "백만원": 1e6, "천만원": 1e7,
                    "억원": 1e8, "천원": 1e3, "원": 1.0}[m.group(1)]
            break

    token_re = _earnings_token_re()
    metrics = (("매출액", "매출액"), ("영업이익", "영업이익"), ("당기순이익", "당기순이익"))
    rows = {}
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        for key, label in metrics:
            if label in rows:
                continue
            # 셀 라벨 형태만 매칭 (문장 속 언급 제외)
            if compact == key or (compact.startswith(key) and len(compact) <= len(key) + 6
                                  and "율" not in compact and "액또는" not in compact):
                toks, skipped = [], 0
                for nxt in lines[i + 1:i + 12]:
                    t = nxt.replace(" ", "")
                    if token_re.match(t):
                        toks.append(t)
                    elif toks:
                        break
                    else:  # 라벨과 숫자 사이 헤더 셀 등은 소량 허용
                        skipped += 1
                        if skipped > 2:
                            break
                toks = [t for t in toks if t != "-"]
                if not toks:
                    continue
                cur = _dart_num(toks[0])
                base = pct = None
                if len(toks) >= 5:
                    base, pct = _dart_num(toks[3]), toks[4]
                elif len(toks) == 4:
                    base, pct = _dart_num(toks[1]), toks[3]
                elif len(toks) == 3:
                    base, pct = _dart_num(toks[1]), toks[2]
                elif len(toks) == 2:
                    base = _dart_num(toks[1])
                if cur is not None:
                    rows[label] = (cur, base, pct)
    if not rows:
        return None
    return {"unit": unit, "rows": rows}


def get_dart_earnings_brief(rcept_no):
    """잠정실적 공시 원문에서 주요 수치 추출. 실패 시 None."""
    return parse_earnings_brief(_api().get_dart_document_text(rcept_no))


# ---------------------------------------------------------------------------
# 배당 결정 공시(현금ㆍ현물배당결정 — 거래소 수시공시) 원문 파싱
#  주요사항보고서 구조화 API가 없어(DS005 36종에 배당 없음) 원문에서 추출한다.
# ---------------------------------------------------------------------------
_DIV_DECISION_TITLE = ("현금ㆍ현물배당", "현금·현물배당", "현금배당", "현물배당")


def _find_date_after(lines, i, limit=6):
    """라벨 라인 이후 limit줄 안에서 날짜(YYYY-MM-DD류) 탐색 → 'YYYYMMDD'."""
    import re
    for nxt in lines[i + 1:i + 1 + limit]:
        m = re.search(r"(20\d{2})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", nxt)
        if m:
            return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return None


def parse_dividend_decision(text):
    """배당결정 공시 텍스트에서 1주당 배당금·배당기준일·지급예정일 추출 (best-effort).

    반환: {"dps": float|None, "record_date": 'YYYYMMDD'|None,
           "pay_date": 'YYYYMMDD'|None, "yield": float|None} 또는 None.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {"dps": None, "record_date": None, "pay_date": None, "yield": None}
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        if out["dps"] is None and "1주당배당금" in compact:
            for nxt in lines[i + 1:i + 7]:
                num = _dart_num(nxt.replace(" ", ""))
                if num is not None and num > 0:
                    out["dps"] = num
                    break
        elif out["record_date"] is None and "배당기준일" in compact:
            out["record_date"] = _find_date_after(lines, i)
        elif out["pay_date"] is None and ("지급예정" in compact and "일" in compact):
            out["pay_date"] = _find_date_after(lines, i)
        elif out["yield"] is None and "시가배당" in compact:
            for nxt in lines[i + 1:i + 5]:
                num = _dart_num(nxt.replace(" ", "").replace("%", ""))
                if num is not None and 0 < num < 100:
                    out["yield"] = num
                    break
    if out["dps"] is None and out["record_date"] is None:
        return None
    return out


def get_dart_dividend_decision(stock_code, days=200):
    """최근 배당결정 공시(현금ㆍ현물배당결정)를 찾아 원문에서 확정 배당 정보를 추출.

    반환: {"dps", "record_date", "pay_date", "yield", "rcept_dt", "rcept_no"} 또는 None.
    분기·결산 배당의 '확정' 기준일을 제공한다 (캘린더의 추정 배당락일을 확정값으로 대체).
    """
    rows = _api().get_dart_disclosures(stock_code, days=days)
    for r in rows:  # 최신순 — 첫 매칭이 최근 결정
        nm = r.get("report_nm", "")
        if not any(k in nm for k in _DIV_DECISION_TITLE):
            continue
        parsed = parse_dividend_decision(_api().get_dart_document_text(r["rcept_no"]))
        if parsed:
            parsed["rcept_dt"] = r.get("rcept_dt", "")
            parsed["rcept_no"] = r.get("rcept_no", "")
            return parsed
    return None
