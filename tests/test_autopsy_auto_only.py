"""AI 매매 복기는 자동매매(AUTO) 매도에만 나간다.

[2026-09-14] 애프터마켓 테스트로 수동 1주 매매를 했더니 "퀀트 점수 0점 종목의 시간외 비정상 진입 ·
프로세스 결함" 리포트가 왔다. 프롬프트가 모든 거래를 시스템 매매로 전제하기 때문이다. 수동·예약·
외부 매도는 운용자 판단이라 시스템 관점의 판정이 무의미하고, 그런 리포트가 쌓이면 진짜 결함이 묻힌다.
"""
import inspect

from modules.auto_trade import conclusion
from modules.auto_trade.common import is_system_trade


def test_autopsy_gate_is_the_system_trade_predicate():
    src = inspect.getsource(conclusion.ConclusionMonitor._check_conclusions)
    i = src.index("_send_trading_autopsy")
    gate = src[src.rindex("if type_name == \"매도\"", 0, i):i]
    assert "is_system_trade(found_record.get('type'), odno)" in gate


def test_only_auto_types_qualify():
    assert is_system_trade("sell(AUTO)")
    assert not is_system_trade("매도(수동)")
    assert not is_system_trade("매도(예약)")
    assert not is_system_trade("매도(외부)")
