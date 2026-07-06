from datetime import date
from unittest.mock import patch

import config
import api
from modules.manage import disclosure
from modules import db_manager


def _set_watchlist(kr=None):
    config.session.stock_data = {
        "stocks_kr": kr or [], "etfs_kr": [],
        "stocks_us": [], "etfs_us": [],
    }


def test_classify_disclosure_levels():
    """공시 제목 -> 중요도/카테고리 분류."""
    assert disclosure.classify_disclosure("불성실공시법인지정")[0] == 2
    assert disclosure.classify_disclosure("유상증자결정")[0] == 2
    assert disclosure.classify_disclosure("횡령ㆍ배임혐의발생")[2] == "횡령·배임"
    assert disclosure.classify_disclosure("자기주식취득결정")[2] == "자기주식"
    assert disclosure.classify_disclosure("전환사채권발행결정")[2] == "메자닌(CB/BW)"
    assert disclosure.classify_disclosure("단일판매ㆍ공급계약체결")[2] == "수주·공급계약"
    assert disclosure.classify_disclosure("현금ㆍ현물배당결정")[2] == "배당"
    # 임원·지분 등 일상 공시는 일반(0)
    assert disclosure.classify_disclosure("임원ㆍ주요주주특정증권등소유상황보고서")[0] == 0
    # 미분류는 기타(0)
    assert disclosure.classify_disclosure("무언가알수없는보고서")[2] == "기타"


def test_next_earnings_deadline_dec_fiscal():
    """12월 결산: 6/21 기준 다음은 반기보고서(6월말+45일=8/14)."""
    dl, label = disclosure.next_earnings_deadline("12", date(2026, 6, 21))
    assert dl == date(2026, 8, 14)
    assert label == "반기보고서"


def test_show_disclosures_empty_and_nokey():
    """관심종목 없음/키 없음 경로는 조용히 안내."""
    _set_watchlist([])
    with patch("config.console.print") as mock_print:
        disclosure.show_disclosures()
    assert any("관심종목이 없습니다" in str(c.args) for c in mock_print.call_args_list)


def test_collect_disclosures_filters_min_level():
    """min_level 미만 공시는 제외된다."""
    rows = [
        {"rcept_no": "1", "report_nm": "유상증자결정", "flr_nm": "", "rcept_dt": "20260620", "rm": "", "corp_name": ""},
        {"rcept_no": "2", "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "flr_nm": "", "rcept_dt": "20260619", "rm": "", "corp_name": ""},
    ]
    with patch.object(api, "get_dart_disclosures", return_value=rows):
        out = disclosure.collect_disclosures("005930", "삼성전자", days=14, min_level=1)
    assert len(out) == 1 and out[0]["rcept_no"] == "1"


def test_check_and_alert_dedup(tmp_path):
    """중대 공시 알림은 텔레그램 발송 후 DB로 중복방지된다."""
    _set_watchlist([{"code": "005930", "name": "삼성전자"}])
    config.DART_API_KEY = "DUMMY"
    rows = [{"rcept_no": "RC100", "report_nm": "유상증자결정", "flr_nm": "", "rcept_dt": "20260620", "rm": "", "corp_name": ""}]

    with patch.object(api, "get_dart_disclosures", return_value=rows), \
         patch.object(api, "send_telegram_message") as mock_send:
        # 1회차: 발송
        sent1 = disclosure.check_and_alert_disclosures(min_level=2, days=2)
        # 2회차: 중복 → 미발송
        sent2 = disclosure.check_and_alert_disclosures(min_level=2, days=2)

    assert sent1 == 1
    assert sent2 == 0
    assert mock_send.call_count == 1
    assert db_manager.db.is_disclosure_notified("RC100") is True


def test_db_disclosure_notified_roundtrip():
    """DB 중복방지 저장/조회."""
    assert db_manager.db.is_disclosure_notified("RC-XYZ") is False
    db_manager.db.mark_disclosure_notified("RC-XYZ")
    assert db_manager.db.is_disclosure_notified("RC-XYZ") is True
