"""DART 확장 기능(잠정실적 파싱·증자/CB 상세·내부자·재무 스냅샷) 테스트."""
from datetime import date
from unittest.mock import patch

import config
import api
from modules import dart_api
from modules.manage import disclosure, insider, financials
from modules.manage import events as calendar_events


# ---------------------------------------------------------------------------
# 잠정실적 원문 파싱
# ---------------------------------------------------------------------------
def test_parse_earnings_brief_quarterly_layout():
    """잠정실적(5열: 당기/직전/QoQ/전년동기/YoY) 레이아웃 파싱."""
    text = "\n".join([
        "연결재무제표기준영업(잠정)실적", "(단위: 백만원, %)",
        "매출액", "1,000,000", "900,000", "11.1", "800,000", "25.0",
        "영업이익", "100,000", "90,000", "11.1", "50,000", "100.0",
        "당기순이익", "80,000", "70,000", "14.3", "40,000", "100.0",
    ])
    brief = dart_api.parse_earnings_brief(text)
    assert brief["unit"] == 1e6
    cur, _base, pct = brief["rows"]["매출액"]
    assert cur == 1000000 and pct == "25.0"  # 증감률=뒤에서부터 첫 %성 토큰(YoY)


def test_parse_earnings_brief_yearly_layout():
    """손익구조30% 공시(4열: 당기/전기/증감액/증감률) 레이아웃 파싱."""
    text = "\n".join([
        "매출액또는손익구조30%(대규모법인은15%)이상변동", "(단위 : 천원)",
        "매출액", "500,000", "400,000", "100,000", "25.0",
        "영업이익", "50,000", "△10,000", "60,000", "흑자전환",
    ])
    brief = dart_api.parse_earnings_brief(text)
    assert brief["unit"] == 1e3
    assert brief["rows"]["매출액"][0] == 500000
    assert brief["rows"]["영업이익"][2] == "흑자전환"


def test_parse_earnings_brief_trillion_unit_real_samsung_layout():
    """조원 단위 + 빈 셀('-') 포함 실제 삼성전자 잠정실적 레이아웃."""
    # [당해, 직전, QoQ, -, 전년동기, YoY, -] → '-' 제거 후 5열 매핑
    text = "\n".join(["(단위 : 조원, %)", "매출액", "당해실적",
                      "171.00", "133.87", "27.74", "-", "74.57", "129.31", "-"])
    brief = dart_api.parse_earnings_brief(text)
    assert brief["unit"] == 1e12
    assert brief["rows"]["매출액"] == (171.0, 74.57, "129.31")


def test_parse_earnings_brief_two_column_computes_later():
    """[당기, -, 전년동기] 2열 레이아웃: 증감률 셀 없음 → base만 채워짐."""
    text = "\n".join(["(단위: 백만원)", "매출액", "238,297", "-", "207,352"])
    brief = dart_api.parse_earnings_brief(text)
    cur, base, pct = brief["rows"]["매출액"]
    assert cur == 238297 and base == 207352 and pct is None


def test_earnings_note_formats_trillion_and_computed_pct():
    """_earnings_note: 조원 표기 및 증감률 미제공 시 직접 계산."""
    brief = {"unit": 1e12, "rows": {"매출액": (171.0, None, "74.57"),
                                    "영업이익": (15.0, 10.0, None)}}
    with patch.object(api, "get_dart_earnings_brief", return_value=brief):
        note = disclosure._earnings_note({"rcept_no": "RC1"})
    assert "매출 171.0조(+74.6%)" in note
    assert "영업익 15.0조(+50.0%)" in note


def test_parse_earnings_brief_returns_none_on_garbage():
    assert dart_api.parse_earnings_brief(None) is None
    assert dart_api.parse_earnings_brief("아무 관련 없는 텍스트") is None


# ---------------------------------------------------------------------------
# 공시 상세 노트 (유상증자/메자닌)
# ---------------------------------------------------------------------------
def _evt(category, report_nm):
    return {"code": "005930", "name": "삼성전자", "date": "20260707",
            "report_nm": report_nm, "category": category, "rcept_no": "RC1",
            "level": 2, "icon": "🟠"}


def test_paid_increase_note_dilution():
    """유상증자 상세: 신주 수·희석률·방식·자금목적."""
    rows = [{"rcept_no": "RC1", "nstk_ostk_cnt": "10,000,000", "nstk_estk_cnt": "-",
             "bfic_tisstk_ostk": "100,000,000", "bfic_tisstk_estk": "-",
             "ic_mthn": "주주배정증자", "fdpp_fclt": "50,000,000,000",
             "fdpp_bsninh": "-", "fdpp_op": "10,000,000,000", "fdpp_dtrp": "-",
             "fdpp_ocsa": "-", "fdpp_etc": "-"}]
    with patch.object(api, "get_dart_paid_increase_detail", return_value=rows):
        note = disclosure.build_detail_note(_evt("증자·감자", "주요사항보고서(유상증자결정)"))
    assert "신주 10,000,000주" in note
    assert "희석 10.0%" in note
    assert "주주배정증자" in note
    assert "시설자금" in note


def test_bond_note_cb():
    """CB 상세: 권면총액·전환가·발행방법."""
    rows = [{"rcept_no": "RC1", "bd_fta": "50,000,000,000",
             "cv_prc": "12,000", "bdis_mthn": "사모"}]
    with patch.object(api, "get_dart_bond_issue_detail", return_value=rows):
        note = disclosure.build_detail_note(_evt("메자닌(CB/BW)", "주요사항보고서(전환사채권발행결정)"))
    assert "권면 500억" in note
    assert "전환가 12,000원" in note
    assert "사모" in note


def test_detail_note_not_eligible_returns_empty():
    """상세조회 대상이 아닌 공시는 빈 문자열."""
    assert disclosure.build_detail_note(_evt("수주·공급계약", "단일판매ㆍ공급계약체결")) == ""


def test_alert_message_includes_detail(tmp_path):
    """텔레그램 알림에 상세 라인이 포함된다."""
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "삼성전자"}],
                                 "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    config.DART_API_KEY = "DUMMY"
    disc = [{"rcept_no": "RC777", "report_nm": "주요사항보고서(유상증자결정)",
             "flr_nm": "", "rcept_dt": "20260708", "rm": "", "corp_name": ""}]
    detail = [{"rcept_no": "RC777", "nstk_ostk_cnt": "1,000,000", "nstk_estk_cnt": "-",
               "bfic_tisstk_ostk": "10,000,000", "bfic_tisstk_estk": "-",
               "ic_mthn": "제3자배정증자", "fdpp_fclt": "-", "fdpp_bsninh": "-",
               "fdpp_op": "-", "fdpp_dtrp": "-", "fdpp_ocsa": "-", "fdpp_etc": "-"}]
    with patch.object(api, "get_dart_disclosures", return_value=disc), \
         patch.object(api, "get_dart_paid_increase_detail", return_value=detail), \
         patch.object(api, "send_telegram_message") as mock_send:
        sent = disclosure.check_and_alert_disclosures(min_level=2, days=2)
    assert sent == 1
    msg = mock_send.call_args[0][0]
    assert "· 상세: " in msg and "희석 10.0%" in msg


# ---------------------------------------------------------------------------
# 내부자 매매
# ---------------------------------------------------------------------------
def test_insider_collect_filters_by_date():
    """cutoff 이전 보고는 제외된다."""
    ins = [{"rcept_no": "1", "rcept_dt": "20260701", "repror": "홍길동", "ofcps": "대표이사",
            "main_shrholdr": "", "qty": 1000.0, "chg": 500.0, "rate": 0.1, "rate_chg": 0.05},
           {"rcept_no": "2", "rcept_dt": "20250101", "repror": "김과거", "ofcps": "이사",
            "main_shrholdr": "", "qty": 100.0, "chg": -50.0, "rate": 0.01, "rate_chg": None}]
    majs = [{"rcept_no": "3", "rcept_dt": "20260630", "repror": "국민연금", "reason": "단순추가취득",
             "qty": 5e6, "chg": 1e5, "rate": 10.1, "rate_chg": 0.2}]
    with patch.object(api, "get_dart_insider_trades", return_value=ins), \
         patch.object(api, "get_dart_major_holdings", return_value=majs):
        got_ins, got_majs = insider._collect("005930", "삼성전자", "20260401")
    assert len(got_ins) == 1 and got_ins[0]["repror"] == "홍길동"
    assert len(got_majs) == 1


def test_dart_insider_trades_normalizes_fields():
    """elestock 응답 필드 정규화."""
    raw = [{"rcept_no": "R1", "rcept_dt": "2026-07-01", "repror": "홍길동",
            "isu_exctv_ofcps": "대표이사", "isu_main_shrholdr": "-",
            "sp_stock_lmp_cnt": "1,234", "sp_stock_lmp_irds_cnt": "△100",
            "sp_stock_lmp_rate": "0.12", "sp_stock_lmp_irds_rate": "-0.01"}]
    with patch.object(api, "get_dart_corp_map", return_value={"005930": "00126380"}), \
         patch.object(api, "call_dart", return_value=raw):
        rows = api.get_dart_insider_trades("005930")
    assert rows[0]["rcept_dt"] == "20260701"
    assert rows[0]["qty"] == 1234.0
    assert rows[0]["chg"] == -100.0


# ---------------------------------------------------------------------------
# 재무 스냅샷
# ---------------------------------------------------------------------------
def test_report_candidates_mid_year():
    """7월 초: 1분기(당해) → 사업(전년) → 3분기(전년) 순."""
    cands = financials._report_candidates(date(2026, 7, 9))
    assert cands == [(2026, "11013"), (2025, "11011"), (2025, "11014")]


def test_report_candidates_early_year():
    """2월: 전년 3분기 → 전전년 반기... 사업보고서(전년)는 아직 미공시."""
    cands = financials._report_candidates(date(2026, 2, 1))
    assert cands[0] == (2025, "11014")
    assert (2025, "11011") not in cands


def test_extract_is_prefers_cfs():
    """연결(CFS) 우선, 매출/영업이익/순이익 추출."""
    rows = [
        {"fs_div": "OFS", "sj_div": "IS", "account_nm": "매출액",
         "thstrm_amount": "1", "frmtrm_amount": "1", "thstrm_nm": "제1기"},
        {"fs_div": "CFS", "sj_div": "IS", "account_nm": "매출액",
         "thstrm_amount": "100,000,000,000", "frmtrm_amount": "80,000,000,000", "thstrm_nm": "제57기"},
        {"fs_div": "CFS", "sj_div": "IS", "account_nm": "영업이익",
         "thstrm_amount": "10,000,000,000", "frmtrm_amount": "-5,000,000,000", "thstrm_nm": "제57기"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "자산총계",
         "thstrm_amount": "999", "frmtrm_amount": "999", "thstrm_nm": "제57기"},
    ]
    data = financials._extract_is(rows)
    assert data["fs"] == "연결"
    assert data["rev"][0] == 1e11 and data["rev"][1] == 8e10


def test_fmt_cell_yoy_and_turnaround():
    """증감률/흑전·적전 표기."""
    assert "(+25.0%)" in financials._fmt_cell((1e11, 8e10, "제57기"))
    assert "흑전" in financials._fmt_cell((1e10, -5e9, "제57기"))
    assert "적전" in financials._fmt_cell((-1e9, 5e9, "제57기"))
    assert financials._fmt_cell(None) == "-"


# ---------------------------------------------------------------------------
# 캘린더 배당주기
# ---------------------------------------------------------------------------
def test_kr_dividend_plan_monthly():
    """월 10회 이상 지급이면 월배당으로 판정."""
    months, label = calendar_events._kr_dividend_plan(12, "12")
    assert label == "월배당" and months == list(range(1, 13))
