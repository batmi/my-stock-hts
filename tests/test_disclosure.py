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
         patch.object(api, "get_dart_paid_increase_detail", return_value=[]), \
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


# ---------------------------------------------------------------------------
# 진행바 통합 (조회 → 상세를 하나의 막대로)
# ---------------------------------------------------------------------------
def _fake_collect(code, name, days, min_level):
    return [{
        "code": code, "name": name, "date": "20260720",
        "report_nm": "주요사항보고서(자기주식취득결정)",
        "level": 2, "icon": "🔴", "category": "자기주식",
        "rcept_no": "20260720000001",
    }]


def _run_show_disclosures(n_codes=20):
    """show_disclosures를 실행하고 생성된 Progress 인스턴스 목록을 돌려준다."""
    created = []
    orig = disclosure._make_progress

    def spy():
        p = orig()
        created.append(p)
        return p

    codes = [(f"{i:06d}", f"종목{i}") for i in range(1, n_codes + 1)]
    with patch.object(disclosure, "_make_progress", spy), \
         patch.object(disclosure, "_kr_watchlist", return_value=codes), \
         patch.object(disclosure, "collect_disclosures", side_effect=_fake_collect), \
         patch.object(disclosure, "build_detail_note", return_value="예정금액 100억"), \
         patch.object(disclosure, "_maybe_ai_summary"), \
         patch.object(config, "DART_API_KEY", "x" * 40), \
         patch("core.utils.clear_screen"), patch.object(config.console, "print"):
        disclosure.show_disclosures(days=14)
    return created, len(codes)


def test_show_disclosures_uses_single_progress_bar():
    """공시 조회와 상세 조회가 진행바 하나를 공유해야 한다.

    (기존에는 단계마다 Progress를 새로 만들어 진행바가 순차로 두 개 보였다.)
    """
    created, _ = _run_show_disclosures()
    assert len(created) == 1, "진행바는 하나만 생성되어야 한다"
    assert len(created[0].tasks) == 1, "막대(task)도 하나여야 한다"


def test_progress_total_covers_both_phases():
    """조회·상세 두 단계가 끊김 없이 하나의 막대로 100%까지 이어져야 한다."""
    created, n_codes = _run_show_disclosures()
    task = created[0].tasks[0]
    # 조회 n건 + 상세 대상(최대 _DETAIL_LIMIT건)까지 한 막대로 처리된다
    assert n_codes < task.completed <= n_codes + disclosure._DETAIL_LIMIT
    assert task.completed == task.total        # 예약분이 정리되어 100%로 끝난다
    # 라벨이 단계마다 바뀌면 사용자에게 진행바가 새로 뜬 것처럼 보인다 → 고정이어야 한다
    assert task.description == disclosure._PROGRESS_LABEL


def test_progress_percentage_never_rewinds():
    """진행률이 뒤로 되감기면 새 진행바가 뜬 것처럼 보인다 → 단조 증가여야 한다."""
    seen = []

    class _Recorder(disclosure.Progress):
        def advance(self, task_id, advance=1):
            super().advance(task_id, advance)
            seen.append(self.tasks[0].percentage)

        def update(self, task_id, **kw):
            super().update(task_id, **kw)
            seen.append(self.tasks[0].percentage)

    with patch.object(disclosure, "Progress", _Recorder):
        _run_show_disclosures()
    assert seen, "진행률이 기록되지 않았다"
    assert seen == sorted(seen), f"진행률이 되감겼다: {seen}"
    assert seen[-1] == 100.0


def test_gather_and_enrich_still_standalone():
    """진행바를 안 넘기면 각자 자체 진행바를 만들어 단독 호출도 계속 동작한다."""
    codes = [("005930", "삼성전자")]
    with patch.object(disclosure, "collect_disclosures", side_effect=_fake_collect):
        events = disclosure._gather(codes, 14, 1)
    assert len(events) == 1

    with patch.object(disclosure, "build_detail_note", return_value="비고"):
        disclosure._enrich_details(events)
    assert events[0]["note"] == "비고"
