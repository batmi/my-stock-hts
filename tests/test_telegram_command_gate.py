"""텔레그램 명령 수신 관문 — 실주문을 낼 수 있는 유일한 원격 입구다.

[왜 이 파일인가] /stop 한 단어가 보유 종목의 손절·트레일링 감시를 통째로 멈추고,
/addrestrict 는 종목을 전역 차단하며, /start 는 자동매매를 켠다. 이 경로에 들어오는
것은 전부 인터넷에서 온 남의 문자열이다. 그런데 수신 루프(`_run_loop`)와 관문
(`_handle_message`)의 상당 부분이 검증되지 않은 채였다(2026-08-30 커버리지 실측
modules/telegram_bot.py 71%, _run_loop 4%).

[여기서 고정하는 것]
  ① 인증은 fail-closed — 수신자를 모르면 아무 명령도 받지 않는다.
  ② 명령에는 유효기간이 있다 — 텔레그램은 봇이 죽은 동안의 메시지를 24시간 쌓아
     두었다가 재기동 첫 폴링에 쏟아붓는다. 어젯밤 /stop 이 오늘 아침 되살아나면 안 된다.
  ③ 버린 사실은 알린다 — 원격 제어 채널에서 침묵은 '봇이 죽었다'로 읽힌다.
  ④ 명령 하나가 터져도 수신 루프는 살아 있어야 한다.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

import config
from modules import telegram_bot as tb

CHAT = "987654321"


@pytest.fixture
def bot(monkeypatch):
    """싱글톤이라 테스트 간 상태가 샌다 — 매번 초기화하고 전송을 막는다."""
    tb.TelegramCommander._instance = None
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", CHAT, raising=False)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "T", raising=False)
    with patch.object(tb, "AutoTrader", MagicMock()):
        b = tb.TelegramCommander()
    b.command_handlers = dict(b.command_handlers)
    monkeypatch.setattr(b, "_send_reply", MagicMock())
    yield b
    tb.TelegramCommander._instance = None


def _msg(text, chat=CHAT, age=0):
    return {"text": text, "chat": {"id": chat}, "date": time.time() - age}


def _dispatch(bot, message):
    """핸들러를 스레드풀이 아니라 그 자리에서 돌린다(순서·예외를 눈으로 보기 위해)."""
    submitted = []
    with patch.object(tb.bot_executor, "submit", lambda fn: submitted.append(fn) or fn()):
        out = bot._handle_message(message)
    return out, submitted


# ───────────────────── ① 인증 ─────────────────────

def test_a_stranger_gets_no_command_and_no_reply(bot):
    handler = MagicMock()
    bot.command_handlers["/stop"] = handler
    _dispatch(bot, _msg("/stop", chat="111"))
    handler.assert_not_called()
    bot._send_reply.assert_not_called(), "남에게 응답을 돌려주면 봇의 존재가 드러난다"


def test_an_unset_chat_id_accepts_nothing(bot, monkeypatch):
    """[핵심] 환경변수를 빠뜨린 채 실계좌 봇이 돌 때 토큰만 알면 조종되면 안 된다."""
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "", raising=False)
    handler = MagicMock()
    bot.command_handlers["/stop"] = handler
    _dispatch(bot, _msg("/stop"))
    handler.assert_not_called()


# ───────────────────── ② 명령의 유효기간 ─────────────────────

def test_a_command_sent_while_the_bot_was_down_does_not_execute(bot):
    """[핵심] 텔레그램은 미수신 메시지를 24시간 보관했다가 재기동 첫 폴링에 내려준다.

    어젯밤 /stop 으로 자동매매를 끄고 오늘 아침 재기동하면, 그 /stop 이 다시 실행돼
    개장과 함께 손절 감시가 꺼진 채로 하루가 시작된다.
    """
    handler = MagicMock()
    bot.command_handlers["/stop"] = handler
    out, _ = _dispatch(bot, _msg("/stop", age=12 * 3600))
    assert out == "stale"
    handler.assert_not_called()


def test_a_fresh_command_still_runs(bot):
    handler = MagicMock(return_value="ok")
    bot.command_handlers["/stop"] = handler
    out, _ = _dispatch(bot, _msg("/stop", age=5))
    assert out != "stale"
    handler.assert_called_once()


def test_the_age_limit_is_configurable(bot, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_COMMAND_MAX_AGE_SEC", 10, raising=False)
    handler = MagicMock()
    bot.command_handlers["/stop"] = handler
    assert _dispatch(bot, _msg("/stop", age=60))[0] == "stale"
    monkeypatch.setattr(config, "TELEGRAM_COMMAND_MAX_AGE_SEC", 3600, raising=False)
    _dispatch(bot, _msg("/stop", age=60))
    handler.assert_called_once()


def test_a_message_without_a_timestamp_is_not_dropped(bot):
    """date 가 없는 형태(테스트 픽스처·미래 API 변화)를 만료로 오판해 명령을 잃지 않는다."""
    handler = MagicMock(return_value="ok")
    bot.command_handlers["/status"] = handler
    _dispatch(bot, {"text": "/status", "chat": {"id": CHAT}})
    handler.assert_called_once()


def test_expiry_is_checked_only_after_authentication(bot):
    """만료 통지가 남의 메시지에 답장으로 나가면 봇의 존재가 드러난다."""
    out, _ = _dispatch(bot, _msg("/stop", chat="111", age=12 * 3600))
    assert out != "stale"
    bot._send_reply.assert_not_called()


# ───────────────────── ③ 수신 루프 ─────────────────────

def _poll_once(bot, results):
    """_run_loop 를 한 바퀴만 돌린다."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": results}

    def _get(*a, **kw):
        bot.is_running = False          # 한 바퀴 뒤 종료
        return resp

    bot.is_running = True
    bot.thread = __import__("threading").current_thread()
    with patch.object(bot.session, "get", side_effect=_get):
        bot._run_loop()


def test_the_loop_reports_dropped_commands_once(bot):
    """[핵심] 조용히 버리면 '왜 /stop 이 안 먹었지'가 된다 — 배치당 한 번만 알린다."""
    handler = MagicMock()
    bot.command_handlers["/stop"] = handler
    results = [{"update_id": i, "message": _msg("/stop", age=9999)} for i in range(3)]
    with patch.object(tb.bot_executor, "submit", lambda fn: fn()):
        _poll_once(bot, results)

    handler.assert_not_called()
    replies = [c.args[0] for c in bot._send_reply.call_args_list]
    assert len(replies) == 1, f"만료 통지가 {len(replies)}번 나갔다(도배)"
    assert "3건" in replies[0]


def test_the_loop_advances_the_offset_even_for_dropped_commands(bot):
    """오프셋이 안 오르면 같은 백로그를 영원히 다시 받는다(무한 루프)."""
    results = [{"update_id": 41, "message": _msg("/stop", age=9999)},
               {"update_id": 42, "message": _msg("/stop", age=9999)}]
    with patch.object(tb.bot_executor, "submit", lambda fn: fn()):
        _poll_once(bot, results)
    assert bot.last_update_id == 42


def test_a_handler_that_raises_does_not_kill_the_receiver(bot):
    """명령 하나가 터져서 수신 루프가 죽으면 원격 제어가 통째로 사라진다."""
    bot.command_handlers["/boom"] = MagicMock(side_effect=RuntimeError("boom"))
    later = MagicMock(return_value="ok")
    bot.command_handlers["/status"] = later
    with patch.object(tb.bot_executor, "submit", lambda fn: fn()):
        _poll_once(bot, [{"update_id": 1, "message": _msg("/boom")},
                         {"update_id": 2, "message": _msg("/status")}])
    later.assert_called_once()
    assert any("오류" in c.args[0] for c in bot._send_reply.call_args_list)


# ───────────────────── ④ 폐지된 레버 ─────────────────────

def test_the_retired_strategy_preset_has_no_way_in(bot):
    """[회귀 방지] 전략 프리셋은 2026-07-20 폐지됐다(약세 프리셋은 수익 1/3, 횡보는 3.8년 0매매).

    진입 임계값·손절·TS 폭·스코어 가중치를 통째로 덮어쓰는 레버라, 원격 명령으로
    되살아나면 설정 메뉴에서 그 키들을 봉인한 취지가 무의미해진다.
    """
    assert "/preset" not in bot.command_handlers
    assert not hasattr(bot, "_cmd_preset")
    assert not hasattr(bot, "_get_preset_status")
    code = [ln for ln in open(tb.__file__, encoding="utf-8")
            if not ln.lstrip().startswith("#")]
    assert not any("apply_strategy_preset" in ln for ln in code), \
        "폐지된 프리셋 적용 함수를 아직 부른다"


def test_an_unknown_command_is_answered_not_ignored(bot):
    _dispatch(bot, _msg("/nope"))
    assert any("지원하지 않는" in c.args[0] for c in bot._send_reply.call_args_list)


def test_plain_text_is_not_a_command(bot):
    bot.command_handlers["/stop"] = MagicMock()
    _dispatch(bot, _msg("stop"))
    bot.command_handlers["/stop"].assert_not_called()
    bot._send_reply.assert_not_called()
