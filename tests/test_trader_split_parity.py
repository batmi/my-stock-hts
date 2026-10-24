"""AutoTrader 분리(C1)의 연결부를 지킨다 — 나눈 조각이 '한 클래스였을 때'와 같이 맞물려야 한다.

[2026-10-24 1단계] 화면·보고 메서드 22개를 modules/auto_trade/reporting.TraderReportingMixin 으로 옮겼다.
 본문은 그대로이고 AutoTrader 가 믹스인을 상속한다. 옮길 때 메서드마다 AST 가 같은 것과, 옮긴 코드가
 쓰는 전역 59개가 trader 와 같은 객체인 것을 대조했다. 이 파일은 그 뒤에 어긋나는 것을 막는다.
  · 같은 이름의 메서드가 AutoTrader 와 믹스인 양쪽에 생기면 AutoTrader 쪽이 조용히 이긴다(MRO) —
    화면을 고쳤는데 화면이 안 바뀌거나, 매매 메서드가 화면 메서드에 가려진다.
  · 두 모듈이 같은 이름(datetime·console·api …)을 서로 다른 객체로 들면, 한쪽만 바꿔 끼운 테스트나
    한쪽만 고친 import 가 화면과 매매를 다른 세계에서 돌게 한다.
 바깥 동작의 동등성은 주기 재생 기록(tests/test_cycle_replay.py)이 본다.
"""
import inspect

from modules.auto_trade import reporting, trader


def _own_methods(cls):
    return {n for n, v in vars(cls).items() if inspect.isfunction(v) or isinstance(v, (staticmethod, classmethod))}


def test_AutoTrader_는_화면_보고_믹스인을_상속한다():
    assert issubclass(trader.AutoTrader, reporting.TraderReportingMixin)
    for name in ("print_status", "get_status_message", "get_health_message", "print_report", "view_log_file",
                 "get_performance_report", "get_recent_logs", "_get_holdings_message"):
        assert hasattr(trader.AutoTrader, name), name
        assert name in vars(reporting.TraderReportingMixin), f"{name} 이 믹스인에 없다"


def test_같은_이름의_메서드가_양쪽에_없다():
    both = _own_methods(trader.AutoTrader) & _own_methods(reporting.TraderReportingMixin)
    assert not both, f"AutoTrader 가 믹스인 메서드를 가린다: {sorted(both)}"


def test_두_모듈이_함께_쓰는_전역은_같은_객체다():
    #  _pkg 는 모듈마다 따로 두는 접근자다(결과가 같은 패키지인지는 아래에서 본다).
    shared = (set(vars(trader)) & set(vars(reporting))) - {"_pkg"}
    shared = {n for n in shared if not n.startswith("__")}
    assert {"config", "api", "console", "datetime", "db_manager", "utils", "logger"} <= shared
    diff = sorted(n for n in shared if vars(trader)[n] is not vars(reporting)[n])
    assert not diff, f"trader 와 reporting 이 같은 이름을 다른 객체로 든다: {diff}"
    assert reporting._pkg() is trader._pkg()


def test_화면_보고_로그는_트레이더_로그로_남는다():
    assert reporting.logger is trader.logger
    assert reporting.logger.name == "modules.auto_trade.trader"
