"""배당 캘린더 — '확정 공시를 못 읽음'과 '확정 공시가 없음'을 나눈다.

[왜 · 2026-09-08] 이 화면의 요지는 '패턴 추정을 배당결정 공시의 확정 기준일로 대체한다'
 는 것이다. 그런데 그 확정 조회가 `try/except: pass` 안에 있어, DART 한도 초과나 순단으로
 못 읽으면 조용히 추정으로 되돌아갔다. 비고에는 '연1회·추정'만 남아, 애초에 확정 공시가
 없는 종목과 글자가 완전히 같다.

 실측: 확정 기준일이 있는 종목에서 그 조회 하나만 실패시키자 배당락일이 9/23(확정) →
 12/29(결산월 일반규칙 추정)로 3개월 움직였고 화면에는 흔적이 없었다. 배당락일은 그날
 가격이 배당만큼 내리는 날이라 매수·매도 시점 판단에 직접 쓰인다.
"""
from datetime import date, timedelta
from unittest.mock import patch

from modules.manage import events as E

_REC = (date.today() + timedelta(days=20)).strftime("%Y%m%d")


def _collect(decision):
    """decision: 호출 결과를 돌려줄 함수(예외를 던지면 조회 실패)."""
    with patch("api.get_dart_dividend",
               return_value={"year": "2025", "주당배당금": 361.0, "시가배당률": 1.5}), \
         patch("api.get_dart_acc_month", return_value="12"), \
         patch("api.get_dart_dividend_decision", side_effect=decision), \
         patch.object(E, "_kr_yf_dividends", return_value=None):
        return E._collect_kr("005930", "삼성전자")


def _confirmed(code, days=200):
    return {"record_date": _REC, "dps": 361}


def _none(code, days=200):
    return None


def _fails(code, days=200):
    raise RuntimeError("dividendDecsn: 응답 코드 020 (사용한도를 초과하였습니다)")


def test_a_failed_decision_query_is_marked_unread():
    r = _collect(_fails)
    assert r is not None, "행 전체를 잃어서는 안 된다 — 배당 정보 본체는 읽혔다"
    assert r["confirmed"] is False
    assert r["decision_unread"] is True


def test_a_company_with_no_decision_is_not_marked_unread():
    """확정 공시가 아직 없는 것은 실패가 아니다."""
    r = _collect(_none)
    assert r["confirmed"] is False
    assert r["decision_unread"] is False


def test_a_successful_decision_still_wins_over_the_estimate():
    """게이트가 정상 경로를 건드리면 안 된다."""
    r = _collect(_confirmed)
    assert r["confirmed"] is True
    assert r["decision_unread"] is False


def test_the_estimate_and_the_confirmation_really_differ():
    """이 결함이 '표시만의 문제'가 아님을 고정한다 — 날짜 자체가 달라진다."""
    ok = _collect(_confirmed)
    bad = _collect(_fails)
    assert ok["ex_date"] != bad["ex_date"], (ok["ex_date"], bad["ex_date"])


# ---------------------------------------------------------------------------
# 비고 문구까지 실제로 갈라지는가
# ---------------------------------------------------------------------------

def _note(**over):
    e = {"estimated": True, "confirmed": False, "exact": False, "freq": "연1회"}
    e.update(over)
    return E._upcoming_note(e)[0]


def test_the_note_says_the_confirmation_could_not_be_read():
    assert "조회실패" in _note(decision_unread=True), _note(decision_unread=True)


def test_the_note_stays_plain_when_there_is_simply_no_confirmation():
    assert "조회실패" not in _note(decision_unread=False)
    assert _note(decision_unread=False) == "연1회·추정"


def test_the_mark_survives_the_hand_off_to_the_upcoming_table():
    """_collect_kr 이 남긴 표식이 예정 일정 행까지 전달돼야 화면에 닿는다."""
    r = _collect(_fails)
    events = []
    for row in [r]:
        if row.get("ex_date"):
            events.append({"code": row["code"], "estimated": True,
                           "exact": row.get("exact", False),
                           "confirmed": row.get("confirmed", False),
                           "decision_unread": row.get("decision_unread", False)})
    #  위 조립은 events.py 의 kr_rows→events 합류부와 같은 키를 쓴다. 그 합류부가
    #  decision_unread 를 빠뜨리면 아래 소스 검사가 잡는다.
    src = open("modules/manage/events.py", encoding="utf-8").read()
    assert '"decision_unread": r.get("decision_unread", False),' in src, \
        "예정 일정 합류부가 표식을 떨어뜨리면 화면에 닿지 않는다"
    assert "조회실패" in E._upcoming_note(events[0])[0]
