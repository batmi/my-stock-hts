"""성과 보고 계산(modules/auto_trade/performance) — 트레이더 인스턴스 없이 규칙을 고정한다."""
from modules.auto_trade import performance as perf


def _r(typ, time, code="005930", **kw):
    d = {"type": typ, "time": time, "code": code, "qty": 1, "price": 1000.0, "reason": ""}
    d.update(kw)
    return d


def test_refine_merges_same_day_same_odno_but_not_across_days():
    recs = [_r("buy", "2026-01-02 09:00:00", odno="0001", price=0, reason="시스템 매수"),
            _r("buy", "2026-01-02 09:00:05", odno="0001", price=1000.0, reason="체결 확인",
               order_status="체결"),
            _r("buy", "2026-01-05 09:00:00", odno="0001", reason="다른 날 같은 번호")]
    out = perf.refine_trade_records(recs)
    assert len(out) == 2                                   # 주문번호는 당일 채번 — 날짜가 키에 들어간다
    merged = out[0]
    assert merged["price"] == 1000.0 and merged["reason"] == "시스템 매수"   # 구체적 사유 유지
    assert merged["order_status"] == "체결"


def test_statistics_fifo_holding_and_reason_buckets():
    recs = [_r("buy", "2026-01-02 09:00:00", reason="조건 만족"),
            _r("buy", "2026-01-02 10:00:00", reason="역매수"),
            _r("sell", "2026-01-02 09:30:00", profit_amt=500, profit_rate=5.0, reason="트레일링스탑"),
            _r("sell", "2026-01-02 11:00:00", profit_amt=-200, profit_rate=-2.0, reason="ATR손절")]
    st = perf.calculate_statistics(recs)
    assert (st["buy_count"], st["sell_count"], st["win_trades"], st["loss_trades"]) == (2, 2, 1, 1)
    assert st["total_profit"] == 300
    assert st["avg_holding_str"] == "45분 0초"            # FIFO: 30분, 60분
    assert st["sell_reasons"] == {"트레일링스탑": 1, "ATR손절": 1}
    assert st["buy_reasons"] == {"스코어 진입 (조건 만족)": 1, "역추세 매수 (낙폭과대)": 1}
    assert st["best_trade"]["profit_amt"] == 500 and st["worst_trade"]["profit_amt"] == -200


def test_statistics_looks_up_buys_outside_the_window_only_when_needed():
    calls = []

    def past(code):
        calls.append(code)
        return [{"type": "buy(AUTO)", "time": "2026-01-01 09:00:00"}]
    sell = _r("sell", "2026-01-02 09:00:00", profit_amt=0)
    st = perf.calculate_statistics([sell], past_trades_of=past)
    assert calls == ["005930"] and st["avg_holding_str"] == "24시간 0분"
    calls.clear()
    perf.calculate_statistics([_r("buy", "2026-01-02 08:00:00"), sell], past_trades_of=past)
    assert calls == []                                     # 기간 안에 매수가 있으면 조회하지 않는다


def test_statistics_survives_lookup_failure_and_missing_lookup():
    def boom(code):
        raise RuntimeError("DB down")
    sell = _r("sell", "2026-01-02 09:00:00", profit_amt=100)
    for lookup in (boom, None):
        st = perf.calculate_statistics([sell], past_trades_of=lookup)
        assert st["sell_count"] == 1 and st["avg_holding_str"] == "-"
