"""/disclosure — 메뉴 6-6(공시 모니터링)의 텔레그램 경로.

화면과 같은 소스(_gather → _enrich_details)를 쓰되 두 가지가 달라야 한다.
  · 진행바를 만들지 않는다 — config.console에 그려지므로 봇 스레드에서 띄우면
    운용자가 보던 화면 위에 남의 진행바가 끼어든다.
  · 건수를 끊는다 — 전송 계층이 4000자마다 쪼개므로 전건을 보내면 조각 메시지가
    줄줄이 오고 정작 중요한 위쪽이 묻힌다.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import telegram_bot  # noqa: E402
from modules.auto_trade import AutoTrader  # noqa: E402
from modules.manage import disclosure  # noqa: E402


@pytest.fixture
def commander():
    telegram_bot.TelegramCommander._instance = None
    cmd = telegram_bot.TelegramCommander()
    cmd.trader = MagicMock(spec=AutoTrader)
    cmd._send_reply = MagicMock()
    return cmd


def _event(level, date, name="테스트", code="005930", cat="실적·IR", rcept="20260821000001"):
    return {"level": level, "date": date, "name": name, "code": code, "category": cat,
            "report_nm": "기업설명회(IR)개최(안내공시)", "icon": "🔵", "rcept_no": rcept}


def test_명령어가_등록되어_있다(commander):
    assert "/disclosure" in commander.command_handlers
    assert commander.command_handlers["/disclosure"] == commander._cmd_disclosure
    assert "/disclosure" in commander._cmd_help([])


def test_인자_파싱_일수와_전체(commander):
    with patch.object(disclosure, "build_telegram_message", return_value="ok") as m:
        commander._cmd_disclosure([])
        assert m.call_args.kwargs == {"days": 14, "min_level": 1}      # 기본은 화면 6-6과 같다

        commander._cmd_disclosure(["7"])
        assert m.call_args.kwargs["days"] == 7

        commander._cmd_disclosure(["30", "all"])
        assert m.call_args.kwargs == {"days": 30, "min_level": 0}

        commander._cmd_disclosure(["전체"])
        assert m.call_args.kwargs["min_level"] == 0


def test_일수는_범위를_벗어나지_않는다(commander):
    with patch.object(disclosure, "build_telegram_message", return_value="ok") as m:
        commander._cmd_disclosure(["0"])
        assert m.call_args.kwargs["days"] == 1
        commander._cmd_disclosure(["9999"])
        assert m.call_args.kwargs["days"] == 90


def test_조회_실패해도_봇은_죽지_않는다(commander):
    with patch.object(disclosure, "build_telegram_message", side_effect=RuntimeError("DART down")):
        res = commander._cmd_disclosure([])
    assert "오류" in res


def test_진행바를_만들지_않는다():
    """봇 경로가 콘솔 진행바를 띄우면 운영 화면이 오염된다."""
    with patch.object(disclosure, "_kr_watchlist", return_value=[("005930", "삼성전자")]), \
         patch.object(config, "DART_API_KEY", "dummy"), \
         patch.object(disclosure, "collect_disclosures", return_value=[_event(2, "20260821")]), \
         patch.object(disclosure, "_make_progress") as prog:
        disclosure.build_telegram_message(days=7)
    prog.assert_not_called()


def test_중요도순_상위만_보내고_초과를_알린다():
    events = [_event(0, f"202608{10 + i:02d}", rcept=f"2026082100{i:04d}") for i in range(25)]
    events.append(_event(2, "20260801", name="중대건", rcept="20260801000099"))

    with patch.object(disclosure, "_kr_watchlist", return_value=[("005930", "삼성전자")]), \
         patch.object(config, "DART_API_KEY", "dummy"), \
         patch.object(disclosure, "_gather", return_value=events), \
         patch.object(disclosure, "_enrich_details"):
        msg = disclosure.build_telegram_message(days=14, limit=20)

    assert "전체 26건" in msg
    assert "상위 20건" in msg
    # 중요도(level 2)가 날짜상 가장 오래됐어도 맨 위에 온다
    body = msg.split("\n\n")[1]
    assert "중대건" in body


def test_관심종목_없거나_키_없으면_안내만(monkeypatch):
    with patch.object(disclosure, "_kr_watchlist", return_value=[]):
        assert "관심종목이 없습니다" in disclosure.build_telegram_message()

    with patch.object(disclosure, "_kr_watchlist", return_value=[("005930", "삼성전자")]), \
         patch.object(config, "DART_API_KEY", ""):
        assert "DART API 키" in disclosure.build_telegram_message()


def test_화면용_rich_태그가_새어나가지_않는다():
    """공시 상세는 화면용 색 마크업을 달고 온다([red]매출대비 5.4%[/]).

    닫기 태그는 이름이 없을 수 있어([/]) 종전 제거 패턴이 놓쳤고, 꼬리에 '[/]'가
    그대로 노출됐다. 이 경로(조회)와 자동 알림(check_and_alert_disclosures)이 같은
    note를 쓰므로 회귀하면 양쪽이 함께 깨진다.
    """
    from unittest.mock import MagicMock
    from modules import telegram_notify

    sent = []

    def fake_post(url, data=None, timeout=None, **kw):
        sent.append(data)
        res = MagicMock()
        res.status_code = 200
        return res

    body = "· 상세: 계약 9,281억 · [red]매출대비 5.4%[/] · ~2029-04-01\n[시스템] 유지"
    with patch.object(telegram_notify.requests, "post", side_effect=fake_post), \
         patch.object(config, "TELEGRAM_BOT_TOKEN", "T"), \
         patch.object(config, "TELEGRAM_CHAT_ID", "1"):
        telegram_notify.send_telegram_message(body, sync=True)

    text = sent[-1]["text"]
    assert "[/]" not in text and "[red]" not in text
    assert "매출대비 5.4%" in text
    assert "[시스템]" in text, "대괄호 표기까지 지워서는 안 된다"


def test_공시_제목의_IR이_티커로_오인되지_않는다():
    """'기업설명회(IR)개최'는 공시마다 나온다 — 매 건 트레이딩뷰 링크가 걸렸다."""
    from unittest.mock import MagicMock
    from modules import telegram_notify

    sent = []

    def fake_post(url, data=None, timeout=None, **kw):
        sent.append(data)
        res = MagicMock()
        res.status_code = 200
        return res

    with patch.object(telegram_notify.requests, "post", side_effect=fake_post), \
         patch.object(config, "TELEGRAM_BOT_TOKEN", "T"), \
         patch.object(config, "TELEGRAM_CHAT_ID", "1"):
        telegram_notify.send_telegram_message("기업설명회(IR)개최 · 카카오 (035720)", sync=True)

    text = sent[-1]["text"]
    assert "symbols/IR" not in text
    assert "KRX-035720" in text, "실제 종목코드 링크는 계속 걸려야 한다"
