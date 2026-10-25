"""원격 기동 명령은 **실제로 떴는지**를 보고 답한다.

[왜] AutoTrader.start() 는 여섯 갈래로 **조용히 되돌아간다** — 방어 모드(buy_halted),
 자격증명 없음(토스/실전), 계좌 잠금 거부(같은 계좌로 다른 엔진이 이미 매매 중),
 DB 건강검진 실패(손절 기준이 든 DB 손상), 초기화 실패. 반환값이 없어 호출부는
 그것을 알 수 없다. 그런데 종전 응답은 그 전부에 대해 "🚀 시작했습니다" 였다.

 실측: start() 가 아무것도 하지 않고 돌아와도 is_running=False 인 채로
 '🚀 시스템 트레이딩을 시작했습니다.' 가 나갔다.

 이 명령을 쓰는 사람은 화면 앞에 없다 — 그래서 텔레그램이다. 자동매매가 돌고
 손절·트레일링이 감시 중이라고 믿게 만드는 거짓 확인은 가장 비싼 종류다.
 /restart 는 더 나쁘다: 멈추는 데는 성공하고 켜는 데 실패하면 **꺼진 채로 돌고 있다고**
 믿게 된다.
"""
from unittest.mock import patch

import pytest

from modules.auto_trade import AutoTrader
from modules.telegram_bot import TelegramCommander


@pytest.fixture
def bot():
    TelegramCommander._instance = None
    AutoTrader._instance = None
    b = TelegramCommander.__new__(TelegramCommander)
    b.trader = AutoTrader()
    b.trader.is_running = False
    b.trader.start_block_reason = ""
    yield b
    TelegramCommander._instance = None
    AutoTrader._instance = None


def _refuse(_self, interactive=True):
    """start() 의 여섯 조기 반환을 흉내 낸다 — 아무것도 하지 않고 돌아온다."""
    return


def _succeed(_self, interactive=True):
    _self.is_running = True


def test_기동에_실패하면_실패라고_답한다(bot):
    with patch.object(type(bot.trader), 'start', _refuse):
        reply = bot._cmd_start(None)
    assert "시작했습니다" not in reply, f"거짓 확인이 나갔다: {reply}"
    assert "시작하지 못했습니다" in reply
    assert "손절" in reply, "무엇이 멈춰 있는지 말하지 않으면 경고가 아니다"


def test_기동에_성공하면_성공이라고_답한다(bot):
    with patch.object(type(bot.trader), 'start', _succeed):
        reply = bot._cmd_start(None)
    assert "시작했습니다" in reply


def test_이미_실행_중이면_종전대로_알린다(bot):
    bot.trader.is_running = True
    assert "이미" in bot._cmd_start(None)


def test_잠금_거부_사유가_원격까지_간다(bot):
    """사유가 로그에만 남으면 화면 앞에 없는 운용자는 왜 안 떴는지 모른다."""
    def _blocked(_self, interactive=True):
        _self.start_block_reason = "같은 계좌로 다른 프로세스가 이미 매매 중입니다 (pid=1234)"

    with patch.object(type(bot.trader), 'start', _blocked):
        reply = bot._cmd_start(None)
    assert "pid=1234" in reply and "다른 프로세스" in reply


def test_방어모드_사유도_전달된다(bot):
    def _halted(_self, interactive=True):
        _self.buy_halted = True
        _self.buy_halt_reason = "일일 손실 한도 도달"

    with patch.object(type(bot.trader), 'start', _halted):
        reply = bot._cmd_start(None)
    assert "방어 모드" in reply and "일일 손실 한도" in reply


def test_재시작이_실패하면_꺼진_상태임을_밝힌다(bot):
    """멈추는 데는 성공했다 — 그 사실을 감추면 '돌고 있다'로 읽힌다."""
    bot.trader.is_running = True

    def _stop(_self, use_status=True):
        _self.is_running = False

    with patch.object(type(bot.trader), 'stop', _stop), \
         patch.object(type(bot.trader), 'start', _refuse), \
         patch('modules.telegram_bot.time.sleep', lambda *_a: None):
        reply = bot._cmd_restart(None)
    assert "재시작했습니다" not in reply, f"거짓 확인이 나갔다: {reply}"
    assert "중단된 상태로 남아 있습니다" in reply


def test_재시작_성공은_그대로_알린다(bot):
    bot.trader.is_running = True

    def _stop(_self, use_status=True):
        _self.is_running = False

    with patch.object(type(bot.trader), 'stop', _stop), \
         patch.object(type(bot.trader), 'start', _succeed), \
         patch('modules.telegram_bot.time.sleep', lambda *_a: None):
        reply = bot._cmd_restart(None)
    assert "재시작했습니다" in reply


def test_실제_잠금_거부가_사유를_남긴다(bot, monkeypatch):
    """위 테스트는 스텁이 사유를 심는다 — 사유를 만드는 쪽도 함께 못 박는다."""
    from modules.auto_trade import lifecycle as tr     # 중복 실행 잠금(_acquire_instance_lock)은 C1 6단계로 lifecycle.py

    class _HeldLock:
        holder = "pid=1234 account=1111"

        def __init__(self, *a, **k):
            pass

        def acquire(self):
            return False

    monkeypatch.setattr(tr.instance_lock, 'InstanceLock', _HeldLock)
    monkeypatch.setattr(tr.api, '_is_screen_output_allowed', lambda: False)
    monkeypatch.setattr(type(bot.trader), 'log', lambda self, *a, **k: None, raising=False)

    assert bot.trader._acquire_instance_lock() is False
    assert "pid=1234" in getattr(bot.trader, 'start_block_reason', ''), \
        "잠금 거부 사유가 로그에만 남는다 — 원격 운용자는 왜 안 떴는지 모른다"
    assert "pid=1234" in bot._start_failure_reason()


def test_잠금에_성공하면_사유가_비워진다(bot, monkeypatch):
    """앞 시도의 사유가 남아 다음 성공을 오염시키면 안 된다."""
    from modules.auto_trade import lifecycle as tr     # 중복 실행 잠금(_acquire_instance_lock)은 C1 6단계로 lifecycle.py

    class _FreeLock:
        holder = ""

        def __init__(self, *a, **k):
            pass

        def acquire(self):
            return True

    bot.trader.start_block_reason = "옛 사유"
    monkeypatch.setattr(tr.instance_lock, 'InstanceLock', _FreeLock)
    assert bot.trader._acquire_instance_lock() is True
    assert bot.trader.start_block_reason == ""


def test_초기화_예외도_사유를_남기고_텔레그램으로_알린다(bot, monkeypatch):
    """[2026-10-25] 초기화 예외는 mystock.log 와 콘솔에만 남았다 — /start 응답의 사유가 비었고, `--auto` 기동은
    실패 자체를 아무도 몰랐다(부팅 직후 기동 잔고 조회 실패 → 프로세스·하트비트는 정상인 채 자동매매만 꺼짐)."""
    import config
    from modules.auto_trade import lifecycle as tr     # 기동(start)은 C1 6단계로 lifecycle.py

    sent, logged, released = [], [], []
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(tr.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(tr.api, '_is_screen_output_allowed', lambda: False)
    monkeypatch.setattr(type(bot.trader), 'log', lambda self, m, *a, **k: logged.append(m), raising=False)
    monkeypatch.setattr(type(bot.trader), '_acquire_instance_lock', lambda self: True)
    monkeypatch.setattr(type(bot.trader), '_release_instance_lock', lambda self: released.append(1))
    monkeypatch.setattr(type(bot.trader), '_check_db_health', lambda self: True)

    def _boom(self):
        raise Exception("자산/예수금 조회 실패 (API 응답 없음)")
    monkeypatch.setattr(type(bot.trader), 'initialize', _boom)
    bot.trader.initialized = False
    bot.trader.initial_asset = 0

    reply = bot._cmd_start(None)
    assert bot.trader.is_running is False
    assert released, "루프가 뜨지 못했는데 잠금을 붙들고 있다 — 다음 시도가 '이미 실행 중'으로 거부된다"
    assert "시작하지 못했습니다" in reply and "자산/예수금 조회 실패" in reply, reply
    assert len(sent) == 1 and sent[0].startswith("⛔ [기동 실패]"), sent
    assert "자산/예수금 조회 실패" in sent[0] and "손절·트레일링" in sent[0]
    assert any(m.startswith("[기동 실패]") for m in logged), "화면 로그(트레이더 로그 파일)에 남지 않았다"


def test_초기화가_거짓을_돌려줘도_같은_실패_경로를_탄다(bot, monkeypatch):
    """[2026-10-25] initialize 는 True 를 돌려주거나 예외를 던진다. 거짓 반환을 따로 받던 분기는 닿지 않았고, 예외 경로와
    알림·잠금 해제를 따로 들고 있었다 — 이제 예외로 바꿔 한 경로로 모은다. 거짓이 와도 매매 루프를 띄우면 안 된다."""
    import config
    from modules.auto_trade import lifecycle as tr

    sent, released = [], []
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(tr.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(tr.api, '_is_screen_output_allowed', lambda: False)
    monkeypatch.setattr(type(bot.trader), 'log', lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(type(bot.trader), '_acquire_instance_lock', lambda self: True)
    monkeypatch.setattr(type(bot.trader), '_release_instance_lock', lambda self: released.append(1))
    monkeypatch.setattr(type(bot.trader), '_check_db_health', lambda self: True)
    monkeypatch.setattr(type(bot.trader), 'initialize', lambda self: False)
    bot.trader.initialized = False
    bot.trader.initial_asset = 0

    reply = bot._cmd_start(None)
    assert bot.trader.is_running is False
    assert released, "루프가 뜨지 못했는데 잠금을 붙들고 있다"
    assert "초기화가 완료되지 않았습니다" in reply, reply
    assert len(sent) == 1 and sent[0].startswith("⛔ [기동 실패]"), sent


def test_DB_손상_사유도_원격까지_간다(bot, monkeypatch):
    """DB 이상은 텔레그램을 따로 보내지만 /start 응답의 사유는 비어 있었다 — 잠금 거부와 같이 사유를 남긴다."""
    from modules.auto_trade import lifecycle as tr

    monkeypatch.setattr(tr.db_manager.db, 'check_integrity',
                        lambda: (False, "Page 7: btreeInitPage() returns error code 11"))
    monkeypatch.setattr(tr.api, 'send_telegram_message', lambda *a, **k: None)
    monkeypatch.setattr(tr.api, '_is_screen_output_allowed', lambda: False)
    monkeypatch.setattr(type(bot.trader), 'log', lambda self, *a, **k: None, raising=False)

    assert bot.trader._check_db_health() is False
    assert "DB 무결성 검사 실패" in bot._start_failure_reason()
