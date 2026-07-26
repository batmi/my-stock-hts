"""경량 운영 관제(/health) 회귀 테스트."""
from datetime import datetime, timedelta

from rich.table import Table

import config
from modules.auto_trade import AutoTrader
from modules.telegram_bot import TelegramCommander


def test_health_message_is_local_and_reports_running_state(monkeypatch):
    trader = AutoTrader()
    trader.is_running = True
    trader.last_cycle_at = datetime.now()
    trader.last_success_at = datetime.now()
    trader.consecutive_errors = 0
    trader.buy_halted = False

    # /health는 잔고·시세 API를 추가 호출하지 않는 관제 화면이다.
    monkeypatch.setattr(trader, "is_market_open", lambda: True)
    monkeypatch.setattr(config, "USE_WEBSOCKET", False, raising=False)

    message = trader.get_health_message()

    assert "[운영 관제 /health: 운영 중]" in message
    assert "REST 폴링 (WebSocket 비활성)" in message
    assert "최근 시작" in message
    assert "주문 감시" in message


def test_health_reports_protective_mode_and_loop_errors(monkeypatch):
    trader = AutoTrader()
    trader.is_running = True
    trader.buy_halted = True
    trader.buy_halt_reason = "일일 손실 한도"
    trader.consecutive_errors = 2
    trader.last_error_at = datetime.now()
    trader.last_error_message = "balance unavailable"

    monkeypatch.setattr(trader, "is_market_open", lambda: True)
    monkeypatch.setattr(config, "USE_WEBSOCKET", False, raising=False)

    message = trader.get_health_message()

    assert "방어 모드" in message
    assert "일일 손실 한도" in message
    assert "자동매매 루프 연속 오류 2/" in message
    assert "balance unavailable" in message


def test_health_message_reports_runtime_and_resources(monkeypatch):
    trader = AutoTrader()
    trader.is_running = True
    trader.start_time = datetime.now() - timedelta(hours=1, minutes=30)

    monkeypatch.setattr(trader, "is_market_open", lambda: True)
    monkeypatch.setattr(config, "USE_WEBSOCKET", False, raising=False)
    # 라즈베리파이 OOM 감시용 항목 — 가용 메모리가 임계 아래면 주의로 승격된다.
    monkeypatch.setattr(AutoTrader, "_health_memory", staticmethod(lambda: (180.0, 90.0)))

    message = trader.get_health_message()

    assert "실행 시간" in message and "(경과 1:30:" in message
    assert "프로세스 메모리 180MB" in message
    assert "가용 메모리 90MB" in message
    assert "가용 메모리 부족" in message


def test_cli_health_rows_escape_markup_and_skip_duplicates():
    trader = AutoTrader()
    trader.get_health_message = lambda: (
        "🟢 [운영 관제 /health: 운영 중]\n"
        "• 실행 시간: 2026-07-26 09:00:00 (경과 1:00:00)\n"
        "• 최근 오류: 09:30:00 — timeout [Errno 60]"
    )

    table = Table()
    table.add_column("구분")
    table.add_column("상세 내용")
    trader._add_health_rows(table, skip_labels={"실행 시간"})

    labels = list(table.columns[0].cells)
    details = list(table.columns[1].cells)

    assert "실행 시간" not in labels
    # 대괄호가 rich 마크업으로 소비되면 'Errno 60'이 통째로 사라진다.
    assert any("Errno 60" in d for d in details)


def test_telegram_health_command_is_registered(monkeypatch):
    commander = TelegramCommander()
    monkeypatch.setattr(commander.trader, "get_health_message", lambda: "health-ok")

    assert commander.command_handlers["/health"]([]) == "health-ok"


def test_cli_health_uses_table_renderer(monkeypatch):
    trader = AutoTrader()
    printed = []
    monkeypatch.setattr(trader, "get_health_message", lambda: (
        "🟢 [운영 관제 /health: 운영 중]\n"
        "• 모드/계좌: KIS 모의 / 12345678\n"
        "• 주문 감시: 미체결 0건\n\n"
        "✅ 관제상 즉시 조치가 필요한 신호가 없습니다."
    ))
    monkeypatch.setattr("modules.auto_trade.trader.utils.clear_screen", lambda: None)
    monkeypatch.setattr("modules.auto_trade.trader.utils.print_breadcrumb", lambda: None)
    monkeypatch.setattr("modules.auto_trade.trader.console.print", lambda value=None: printed.append(value))

    trader.print_health()

    table = next(value for value in printed if isinstance(value, type(__import__("rich.table", fromlist=["Table"]).Table())))
    assert table.columns[0].header == "구분"
    assert table.columns[1].header == "상세 내용"
