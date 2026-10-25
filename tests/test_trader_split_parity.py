"""AutoTrader 분리(C1)의 연결부를 지킨다 — 나눈 조각이 '한 클래스였을 때'와 같이 맞물려야 한다.

[2026-10-24 1단계] 화면·보고 메서드 22개를 modules/auto_trade/reporting.TraderReportingMixin 으로 옮겼다.
 본문은 그대로이고 AutoTrader 가 믹스인을 상속한다. 옮길 때 메서드마다 AST 가 같은 것과, 옮긴 코드가
 쓰는 전역 59개가 trader 와 같은 객체인 것을 대조했다. 이 파일은 그 뒤에 어긋나는 것을 막는다.
  · 같은 이름의 메서드가 AutoTrader 와 믹스인 양쪽에 생기면 AutoTrader 쪽이 조용히 이긴다(MRO) —
    화면을 고쳤는데 화면이 안 바뀌거나, 매매 메서드가 화면 메서드에 가려진다.
  · 두 모듈이 같은 이름(datetime·console·api …)을 서로 다른 객체로 들면, 한쪽만 바꿔 끼운 테스트나
    한쪽만 고친 import 가 화면과 매매를 다른 세계에서 돌게 한다.
[2026-10-24 2단계] 시장·리스크 메서드 10개를 modules/auto_trade/market_risk.TraderMarketRiskMixin 으로 옮겼다
 (같은 방법 — AST 동일, 전역 26개 동일 객체). 믹스인끼리도 같은 이름을 들면 먼저 상속한 쪽이 조용히 이긴다.
[2026-10-25 3단계] 계좌 동기화 메서드 12개를 modules/auto_trade/account_sync.TraderAccountSyncMixin 으로 옮겼다
 (같은 방법 — AST 동일, 전역 36개 중 남는 이름 동일 객체, 그 메서드들만 쓰던 상수 4개도 함께).
 바깥 동작의 동등성은 주기 재생 기록 세 시나리오(tests/test_cycle_replay.py — base·market_stress·account_sync)가 본다.
[2026-10-25 4단계] 매도 메서드 12개를 modules/auto_trade/selling.TraderSellingMixin 으로 옮겼다(같은 방법 — AST 동일,
 전역 40개 중 남는 이름 동일 객체, engine 의 미관리 사유·경보 횟수 상수는 engine 에서 그대로 import). 동등성은
 네 시나리오(sell_paths 추가)가 본다.
[2026-10-25 5단계] 매수 메서드 9개와 클래스 속성 2개를 modules/auto_trade/buying.TraderBuyingMixin 으로 옮겼다(같은
 방법 — AST 동일, 바꾼 곳은 정적 메서드의 클래스 이름 한 곳, 전역 18개 동일 객체). 매수만 쓰던 모듈 함수 둘(후보
 정렬 키·오염 일봉 판정)도 함께 옮겼고 trader 가 다시 import 한다. 동등성은 다섯 시나리오(buy_paths 추가)가 본다.
"""
import inspect
import itertools

import pytest

from modules.auto_trade import account_sync, buying, market_risk, reporting, selling, trader

MIXINS = {
    reporting: (reporting.TraderReportingMixin,
                ("print_status", "get_status_message", "get_health_message", "print_report", "view_log_file",
                 "get_performance_report", "get_recent_logs", "_get_holdings_message"),
                {"config", "api", "console", "datetime", "db_manager", "utils", "logger"}),
    market_risk: (market_risk.TraderMarketRiskMixin,
                  ("_update_market_indices_status", "_format_market_filter_basis", "_notify_market_unknown",
                   "_whipsaw_risk_scale", "_update_vol_regime", "_update_risk_scale", "_risk_floor_day",
                   "_hold_intraday_floor", "_save_risk_floor", "_get_account_drawdown_pct"),
                  {"config", "api", "datetime", "db_manager", "logger",
                   "_get_trade_account", "trade_account_key"}),     # timedelta 는 3단계, analysis 는 5단계로 trader 가 안 쓴다
    account_sync: (account_sync.TraderAccountSyncMixin,
                   ("effective_baseline", "daily_pnl_base", "transfer_note", "_offline_transfer_threshold",
                    "_realized_matches_broker", "_reconcile_offline_transfer", "_reevaluate_buy_halt_after_transfer",
                    "_sync_external_fills", "_run_account_circuit_breaker", "_monitor_account_status",
                    "_get_toss_open_buy_reserved"),
                   {"config", "api", "account", "datetime", "db_manager", "logger", "time", "_get_trade_account",
                    "is_plausible_baseline", "load_daily_initial_asset", "save_daily_initial_asset"}),
    selling: (selling.TraderSellingMixin,
              ("_live_risk_map", "_last_known_sl_rate", "_effective_stop_loss_rate", "_cancel_pending_buy_on_stop_loss",
               "_apply_corporate_action", "_already_half_sold", "_cached_anchor", "_restore_trailing_anchor",
               "_alert_unmanaged_stop", "_scan_after_hours_sell_signals", "_alert_after_hours_sell",
               "_check_sell_conditions"),
              {"config", "api", "concurrent", "datetime", "db_manager", "logger", "time",
               "utils", "_enrich_rules_with_weights", "_get_trade_account", "get_restricted_stocks"}),
    #  ↑ analysis·indicators 는 5단계로 trader 가 안 쓴다(buying 이 가져갔다)
    buying: (buying.TraderBuyingMixin,
             ("_try_pyramid_buy", "occupied_slot_codes", "_check_buy_conditions", "_collect_stop_exit_prices",
              "_analyze_candidate_worker", "_note_score_boundary", "_drain_boundary_rows", "_analyze_candidates",
              "_execute_buy_orders"),
             {"config", "api", "concurrent", "context", "datetime", "db_manager", "logger", "time", "utils",
              "_enrich_rules_with_weights", "_get_trade_account", "get_restricted_stocks",
              "candidate_priority_key", "contaminated_chart_reason"}),     # 둘은 trader 가 다시 import 한다
}
_IDS = [m.__name__.rsplit(".", 1)[-1] for m in MIXINS]


def _own_methods(cls):
    #  이중 밑줄 이름은 뺀다 — 파이썬 3.14 는 클래스 주석(믹스인의 상태 선언)마다 __annotate_func__ 함수를 만든다.
    return {n for n, v in vars(cls).items()
            if not n.startswith("__") and (inspect.isfunction(v) or isinstance(v, (staticmethod, classmethod)))}


@pytest.mark.parametrize("mod", MIXINS, ids=_IDS)
def test_AutoTrader_는_믹스인을_상속한다(mod):
    mixin, names, _ = MIXINS[mod]
    assert issubclass(trader.AutoTrader, mixin)
    for name in names:
        assert hasattr(trader.AutoTrader, name), name
        assert name in vars(mixin), f"{name} 이 {mixin.__name__} 에 없다"


@pytest.mark.parametrize("mod", MIXINS, ids=_IDS)
def test_같은_이름의_메서드가_양쪽에_없다(mod):
    mixin = MIXINS[mod][0]
    both = _own_methods(trader.AutoTrader) & _own_methods(mixin)
    assert not both, f"AutoTrader 가 {mixin.__name__} 메서드를 가린다: {sorted(both)}"


def test_믹스인끼리_같은_이름의_메서드가_없다():
    for (a, *_), (b, *_) in itertools.combinations(MIXINS.values(), 2):
        both = _own_methods(a) & _own_methods(b)
        assert not both, f"먼저 상속한 {a.__name__} 가 {b.__name__} 의 메서드를 가린다: {sorted(both)}"


@pytest.mark.parametrize("mod", MIXINS, ids=_IDS)
def test_두_모듈이_함께_쓰는_전역은_같은_객체다(mod):
    #  _pkg 는 모듈마다 따로 두는 접근자다(결과가 같은 패키지인지는 아래에서 본다).
    required = MIXINS[mod][2]
    shared = (set(vars(trader)) & set(vars(mod))) - {"_pkg"}
    shared = {n for n in shared if not n.startswith("__")}
    assert required <= shared, sorted(required - shared)
    diff = sorted(n for n in shared if vars(trader)[n] is not vars(mod)[n])
    assert not diff, f"trader 와 {mod.__name__} 가 같은 이름을 다른 객체로 든다: {diff}"
    assert mod._pkg() is trader._pkg()


@pytest.mark.parametrize("mod", MIXINS, ids=_IDS)
def test_옮긴_코드의_로그는_트레이더_로그로_남는다(mod):
    assert mod.logger is trader.logger
    assert mod.logger.name == "modules.auto_trade.trader"
