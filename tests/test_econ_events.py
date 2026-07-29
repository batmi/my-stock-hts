from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

import config
from modules.manage import econ_events


def _fake_response(payload, encoding_holder=None):
    res = MagicMock()
    res.json.return_value = payload
    res.raise_for_status.return_value = None
    return res


def _expiry_dates(out, country):
    return [e["date"] for e in out if e["country"] == country]


def test_kr_option_expiry_is_second_thursday():
    """국내 동시만기는 3·6·9·12월 둘째 목요일이다."""
    out = econ_events._option_expiry(date(2026, 1, 1), date(2026, 12, 31))
    assert _expiry_dates(out, "KR") == ["2026-03-12", "2026-06-11", "2026-09-10", "2026-12-10"]
    assert all(e["source"] == "계산" for e in out)


def test_us_option_expiry_is_third_friday():
    """미국 동시만기(quadruple witching)는 3·6·9·12월 셋째 금요일이다."""
    out = econ_events._option_expiry(date(2025, 1, 1), date(2025, 12, 31))
    assert _expiry_dates(out, "US") == ["2025-03-21", "2025-06-20", "2025-09-19", "2025-12-19"]


def test_us_expiry_rolls_back_off_juneteenth():
    """미국 6월 셋째 금요일이 준틴스데이면 만기가 직전 영업일로 앞당겨진다."""
    # 2026-06-19(금)은 준틴스데이 → 06-18(목)
    out = econ_events._option_expiry(date(2026, 6, 1), date(2026, 6, 30))
    assert _expiry_dates(out, "US") == ["2026-06-18"]
    # 2027-06-19(토) 대체휴일이 06-18(금) → 06-17(목)
    out = econ_events._option_expiry(date(2027, 6, 1), date(2027, 6, 30))
    assert _expiry_dates(out, "US") == ["2027-06-17"]


def test_option_expiry_respects_window():
    """조회 구간을 벗어난 만기일은 제외된다."""
    out = econ_events._option_expiry(date(2026, 4, 1), date(2026, 10, 1))
    assert _expiry_dates(out, "KR") == ["2026-06-11", "2026-09-10"]
    assert _expiry_dates(out, "US") == ["2026-06-18", "2026-09-18"]


def test_nth_weekday():
    """n번째 요일 계산 (월=0 … 일=6)."""
    assert econ_events._nth_weekday(2026, 3, 3, 2) == date(2026, 3, 12)   # 둘째 목요일
    assert econ_events._nth_weekday(2026, 3, 4, 3) == date(2026, 3, 20)   # 셋째 금요일
    assert econ_events._nth_weekday(2026, 5, 4, 1) == date(2026, 5, 1)    # 1일이 곧 첫 금요일


def test_fetch_fred_skipped_without_key():
    """FRED 키가 없으면 네트워크를 타지 않고 빈 결과를 돌려준다."""
    with patch.object(config, "FRED_API_KEY", ""), \
         patch.object(econ_events.requests, "get") as mock_get:
        assert econ_events._fetch_fred(date(2026, 7, 29), date(2026, 9, 12)) == ([], True)
    mock_get.assert_not_called()


def test_fetch_fred_release_parses_and_filters():
    """FRED 응답에서 조회 구간 안의 발표일만 뽑아 표시명을 붙인다."""
    payload = {"release_dates": [
        {"release_id": 10, "date": "2026-08-12"},
        {"release_id": 10, "date": "2026-09-11"},
        {"release_id": 10, "date": "2027-01-13"},  # 구간 밖 → 제외
    ]}
    with patch.object(config, "FRED_API_KEY", "DUMMY"), \
         patch.object(econ_events.requests, "get", return_value=_fake_response(payload)):
        out = econ_events._fetch_fred_release(10, date(2026, 7, 29), date(2026, 9, 12))

    assert [e["date"] for e in out] == ["2026-08-12", "2026-09-11"]
    assert out[0]["name"] == "미국 CPI" and out[0]["weight"] == 1


def test_fetch_fed_extracts_fomc_and_skips_undated():
    """연준 캘린더에서 FOMC 항목만 뽑고, month가 빈 레코드는 버린다."""
    payload = {"events": [
        {"type": "FOMC", "title": "FOMC Meeting", "month": "2026-07", "days": "29", "time": "2:00 p.m."},
        {"type": "FOMC", "title": " FOMC Minutes", "month": "2026-08", "days": "19"},  # 앞 공백
        {"type": "events", "title": "FOMC Meeting", "month": "", "days": "3"},         # 날짜 없음
        {"type": "Speeches", "title": "Speech - Governor", "month": "2026-08", "days": "5"},
    ]}
    with patch.object(econ_events.requests, "get", return_value=_fake_response(payload)):
        out, ok = econ_events._fetch_fed(date(2026, 7, 29), date(2026, 9, 12))

    assert ok
    assert [(e["date"], e["name"]) for e in out] == [
        ("2026-07-29", "FOMC 금리결정"), ("2026-08-19", "FOMC 의사록")]


def test_fetch_fed_handles_multi_day_meeting():
    """2일 회의가 '27-28'로 들어오면 결정일인 마지막 날을 쓴다."""
    payload = {"events": [
        {"type": "FOMC", "title": "FOMC Meeting", "month": "2026-10", "days": "27-28"},
    ]}
    with patch.object(econ_events.requests, "get", return_value=_fake_response(payload)):
        out, _ = econ_events._fetch_fed(date(2026, 10, 1), date(2026, 11, 1))
    assert out[0]["date"] == "2026-10-28"


def test_fetch_fed_survives_network_failure():
    """연준 캘린더가 죽어도 예외를 밖으로 내지 않는다(부분 실패 허용)."""
    with patch.object(econ_events.requests, "get", side_effect=OSError("boom")):
        assert econ_events._fetch_fed(date(2026, 7, 29), date(2026, 9, 12)) == ([], False)


def test_collect_dedupes_same_day_same_name():
    """소스가 겹쳐 같은 날 같은 이벤트가 중복돼도 하나로 합쳐진다."""
    dup = [{"date": "2026-08-12", "name": "미국 CPI", "country": "US", "weight": 1, "source": "FRED"}]
    with patch.object(econ_events, "_fetch_fred", return_value=(list(dup), True)), \
         patch.object(econ_events, "_fetch_fed", return_value=(list(dup), True)), \
         patch.object(econ_events, "_option_expiry", return_value=[]), \
         patch.object(econ_events, "_load_seed", return_value=[]):
        out, complete = econ_events._collect(date(2026, 7, 29), date(2026, 9, 12))
    assert len(out) == 1 and complete


def test_get_events_always_refetches(tmp_path):
    """당일 캐시가 있어도 매번 새로 수집한다 (캐시는 읽기용이 아니라 폴백용)."""
    today = datetime.now().date()
    cache = {
        "fetched": today.strftime("%Y-%m-%d"),
        "covers_until": (today + timedelta(days=60)).strftime("%Y-%m-%d"),
        "complete": True,
        "events": [{"date": (today + timedelta(days=5)).strftime("%Y-%m-%d"),
                    "name": "묵은 CPI", "country": "US", "weight": 1, "source": "FRED"}],
    }
    fresh = [{"date": (today + timedelta(days=5)).strftime("%Y-%m-%d"),
              "name": "미국 CPI", "country": "US", "weight": 1, "source": "FRED"}]

    with patch.object(econ_events, "CACHE_FILE", str(tmp_path / "c.json")), \
         patch.object(econ_events.jsonio, "load_json", return_value=cache), \
         patch.object(econ_events.jsonio, "save_json") as mock_save, \
         patch.object(econ_events, "_collect", return_value=(fresh, True)) as mock_collect:
        out, status = econ_events.get_events(days=30)

    mock_collect.assert_called_once()
    assert [e["name"] for e in out] == ["미국 CPI"]     # 캐시가 아니라 새로 받은 값
    assert status == {"stale_since": None, "complete": True}
    assert mock_save.call_args.args[1]["complete"] is True


def test_get_events_flags_partial_collection(tmp_path):
    """일부 소스가 실패하면 받은 것만 쓰되 complete=False로 알린다."""
    today = datetime.now().date()
    partial = [{"date": (today + timedelta(days=5)).strftime("%Y-%m-%d"),
                "name": "FOMC 금리결정", "country": "US", "weight": 1, "source": "Fed"}]

    with patch.object(econ_events, "CACHE_FILE", str(tmp_path / "c.json")), \
         patch.object(econ_events.jsonio, "save_json") as mock_save, \
         patch.object(econ_events, "_collect", return_value=(partial, False)):
        out, status = econ_events.get_events(days=30)

    assert [e["name"] for e in out] == ["FOMC 금리결정"]
    assert status["stale_since"] is None and status["complete"] is False
    assert mock_save.call_args.args[1]["complete"] is False


def test_get_events_falls_back_to_stale_cache(tmp_path):
    """수집이 전부 실패하면 날짜 지난 캐시라도 살려 쓰고, 언제 기준인지 알려준다."""
    today = datetime.now().date()
    stale = {
        "fetched": "2000-01-01",
        "covers_until": "2000-03-01",
        "events": [{"date": (today + timedelta(days=3)).strftime("%Y-%m-%d"),
                    "name": "FOMC 금리결정", "country": "US", "weight": 1, "source": "Fed"}],
    }
    with patch.object(econ_events, "CACHE_FILE", str(tmp_path / "c.json")), \
         patch.object(econ_events.jsonio, "load_json", return_value=stale), \
         patch.object(econ_events, "_collect", return_value=([], False)):
        out, status = econ_events.get_events(days=30)

    assert len(out) == 1 and out[0]["name"] == "FOMC 금리결정"
    assert status["stale_since"] == "2000-01-01"


def test_get_events_empty_when_collect_fails_without_cache(tmp_path):
    """수집도 실패하고 폴백할 캐시도 없으면 빈 목록 — stale 표시는 붙이지 않는다."""
    with patch.object(econ_events, "CACHE_FILE", str(tmp_path / "c.json")), \
         patch.object(econ_events.jsonio, "load_json", return_value={}), \
         patch.object(econ_events, "_collect", return_value=([], False)):
        out, status = econ_events.get_events(days=30)

    assert out == [] and status["stale_since"] is None


def test_get_events_drops_past_dates(tmp_path):
    """이미 지난 이벤트는 결과에서 빠진다."""
    today = datetime.now().date()
    collected = [
        {"date": (today - timedelta(days=1)).strftime("%Y-%m-%d"), "name": "지난 CPI",
         "country": "US", "weight": 1, "source": "FRED"},
        {"date": today.strftime("%Y-%m-%d"), "name": "오늘 FOMC",
         "country": "US", "weight": 1, "source": "Fed"},
    ]
    with patch.object(econ_events, "CACHE_FILE", str(tmp_path / "c.json")), \
         patch.object(econ_events.jsonio, "save_json"), \
         patch.object(econ_events, "_collect", return_value=(collected, True)):
        out, _ = econ_events.get_events(days=30)

    assert [e["name"] for e in out] == ["오늘 FOMC"]


def test_load_seed_missing_file_is_harmless(tmp_path):
    """시드 파일이 없어도 조용히 빈 목록을 돌려준다."""
    with patch.object(econ_events, "SEED_FILE", str(tmp_path / "nope.json")):
        assert econ_events._load_seed(date(2026, 7, 29), date(2026, 9, 12)) == []


def test_render_warns_when_no_fred_key():
    """FRED 키가 없으면 발급 안내를 띄운다."""
    with patch.object(config, "FRED_API_KEY", ""), \
         patch.object(econ_events, "get_events",
                      return_value=([], {"stale_since": None, "complete": True})), \
         patch("config.console.print") as mock_print:
        econ_events.render()

    out = " ".join(str(c.args) for c in mock_print.call_args_list)
    assert "FRED API 키가 없어" in out


def test_render_warns_when_showing_stale_events():
    """묵은 저장분을 보여줄 때는 기준일을 화면에 밝힌다."""
    today = datetime.now().date()
    ev = [{"date": today.strftime("%Y-%m-%d"), "name": "FOMC 금리결정",
           "country": "US", "weight": 1, "source": "Fed"}]
    with patch.object(config, "FRED_API_KEY", "DUMMY"), \
         patch.object(econ_events, "get_events",
                      return_value=(ev, {"stale_since": "2026-07-20", "complete": False})), \
         patch("config.console.print") as mock_print:
        econ_events.render()

    out = " ".join(str(c.args) for c in mock_print.call_args_list)
    assert "2026-07-20 기준 저장분" in out


def test_render_marks_dday():
    """당일 이벤트는 D-DAY로 표기된다."""
    today = datetime.now().date()
    ev = [{"date": today.strftime("%Y-%m-%d"), "name": "FOMC 금리결정",
           "country": "US", "weight": 1, "source": "Fed"}]
    with patch.object(config, "FRED_API_KEY", "DUMMY"), \
         patch.object(econ_events, "get_events",
                      return_value=(ev, {"stale_since": None, "complete": True})), \
         patch("config.console.print") as mock_print:
        econ_events.render()

    rendered = [c.args[0] for c in mock_print.call_args_list if c.args and hasattr(c.args[0], "columns")]
    assert rendered, "테이블이 렌더링되지 않았다"
    cells = [str(c) for col in rendered[0].columns for c in col._cells]
    assert any("D-DAY" in c for c in cells)
    assert any("FOMC 금리결정" in c for c in cells)


def test_build_lines_marks_important_events():
    """텍스트 출력에도 D-day가 붙고, 중요도 1 이벤트는 표식(❗)이 달린다."""
    today = datetime.now().date()
    ev = [{"date": today.strftime("%Y-%m-%d"), "name": "FOMC 금리결정",
           "country": "US", "weight": 1, "source": "Fed"},
          {"date": (today + timedelta(days=3)).strftime("%Y-%m-%d"), "name": "미국 JOLTS",
           "country": "US", "weight": 3, "source": "FRED"}]
    with patch.object(config, "FRED_API_KEY", "DUMMY"), \
         patch.object(econ_events, "get_events",
                      return_value=(ev, {"stale_since": None, "complete": True})):
        lines = econ_events.build_lines(days=45)

    text = "\n".join(lines)
    assert "▸ 주요 경제 이벤트" in text
    assert "❗" in text and "D-DAY" in text and "FOMC 금리결정" in text
    assert "• " in text and "D-3" in text and "미국 JOLTS" in text


def test_build_lines_reports_stale_cache():
    """묵은 저장분이면 텍스트에도 기준일을 밝힌다."""
    with patch.object(config, "FRED_API_KEY", "DUMMY"), \
         patch.object(econ_events, "get_events",
                      return_value=([], {"stale_since": "2026-07-20", "complete": False})):
        text = "\n".join(econ_events.build_lines())
    assert "2026-07-20 기준 저장분" in text
