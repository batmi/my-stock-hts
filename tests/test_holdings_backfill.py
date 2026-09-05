"""보유분 거래내역 복원 — 현재 보유수량을 설명하는 만큼만 되살리는가.

[왜] 시스템 DB와 실제 계좌를 대조할 경로가 없었다. HTS·MTS 직접 매수분, 시스템 도입
이전 포지션, DB 이관 중 잃은 기록은 매수 이력이 비어 있어 실현손익·평단·보유일수가
전부 빈 채로 남는다. 실계좌 투입 뒤 무엇이 어긋났는지 볼 기준선이 없다는 뜻이다.

[핵심 판정] 얼마나 거슬러 올라가는가. 현재 포지션이 열린 시점까지만이다.
그보다 과거는 이미 청산된 다른 포지션이라 복원하면 안 된다 — 지금 잔고와 무관한
거래가 섞이면 대조가 오히려 더 어려워진다.
"""
import pytest

import config
from modules import holdings_backfill as hb


def _tx(date, is_buy, qty, price=10_000, odno=None, time="090000", code="005930", name="삼성전자"):
    return {'code': code, 'date': date, 'time': time, 'is_buy': is_buy, 'qty': qty,
            'price': price, 'odno': odno or f"{date}{time}", 'name': name,
            'type_name': "현금매수" if is_buy else "현금매도"}


def _codes(picked):
    return [(t['date'], '매수' if t['is_buy'] else '매도', t['qty']) for t in picked]


# ─────────────────────────────────────────────
# 1. 어디까지 거슬러 올라가는가
# ─────────────────────────────────────────────

def test_stops_at_the_point_the_position_opened():
    """단순 매수 한 건이면 그 한 건만 복원한다."""
    txs = [_tx("20260701", True, 3), _tx("20260728", True, 10)]
    picked, missing = hb.select_explaining_executions(txs, current_qty=10)
    assert _codes(picked) == [("20260728", "매수", 10)]
    assert missing == 0, "이미 청산된 과거 포지션(07-01)까지 끌어왔다"


def test_walks_back_through_an_interleaved_sell():
    """중간에 매도가 끼면 더 과거까지 가야 현재 수량이 설명된다.

    매도를 만나면 '팔기 전에는 더 들고 있었다'는 뜻이므로 설명해야 할 수량이 늘어난다.
    """
    txs = [_tx("20260710", True, 3),      # 이미 청산된 과거 — 대상 아님
           _tx("20260728", True, 10),
           _tx("20260803", False, 4),
           _tx("20260805", True, 4)]
    picked, missing = hb.select_explaining_executions(txs, current_qty=10)
    assert _codes(picked) == [("20260728", "매수", 10),
                              ("20260803", "매도", 4),
                              ("20260805", "매수", 4)]
    assert missing == 0


def test_full_liquidation_resets_the_starting_point():
    """전량 매도 후 재진입이면 재진입 건만 복원한다."""
    txs = [_tx("20260701", True, 10), _tx("20260715", False, 10), _tx("20260801", True, 5)]
    picked, missing = hb.select_explaining_executions(txs, current_qty=5)
    assert _codes(picked) == [("20260801", "매수", 5)]
    assert missing == 0


def test_partial_sell_keeps_the_whole_opening_lot():
    """부분 매도가 있어도 진입 로트 전체를 복원한다(평단 재생에 필요하다)."""
    txs = [_tx("20260701", True, 10), _tx("20260710", True, 5), _tx("20260720", False, 8)]
    picked, missing = hb.select_explaining_executions(txs, current_qty=7)
    assert len(picked) == 3
    assert missing == 0


def test_entry_older_than_the_window_is_reported_not_invented():
    """조회 구간보다 과거에 진입했으면 부분 복원으로 남긴다 — 없는 기록을 지어내지 않는다."""
    txs = [_tx("20260805", True, 4)]
    picked, missing = hb.select_explaining_executions(txs, current_qty=10)
    assert _codes(picked) == [("20260805", "매수", 4)]
    assert missing == 6


@pytest.mark.parametrize("qty,txs", [(0, [_tx("20260801", True, 5)]), (10, [])])
def test_nothing_to_do_cases(qty, txs):
    picked, _ = hb.select_explaining_executions(txs, current_qty=qty)
    assert picked == []


# ─────────────────────────────────────────────
# 2. 기록 형태 · 평단 재생
# ─────────────────────────────────────────────

def test_records_carry_time_and_external_label():
    recs = hb.build_records([_tx("20260728", True, 10, price=70_000, time="093015")])
    r = recs[0]
    assert r['time'] == "2026-07-28 09:30:15"
    assert r['type'].endswith("(외부)"), "시스템이 낸 주문이 아니므로 외부로 표기해야 한다"
    assert r['qty'] == 10 and r['price'] == 70_000


def test_average_price_is_replayed_forward():
    """평단을 0에서 다시 쌓아야 매도 기록의 실현손익이 실제와 맞는다."""
    recs = hb.build_records([
        _tx("20260701", True, 10, price=10_000),
        _tx("20260710", True, 10, price=20_000),
        _tx("20260720", False, 5, price=30_000),
    ])
    sell = recs[-1]
    assert sell['buy_price'] == pytest.approx(15_000)   # (10*1만 + 10*2만)/20
    assert sell['profit_amt'] > 0
    assert sell['profit_rate'] == pytest.approx(
        sell['profit_amt'] / (15_000 * 5) * 100, rel=1e-6)


def test_realized_profit_is_net_of_costs():
    """실현손익은 왕복 비용을 뺀 값이어야 한다(DB의 다른 기록과 같은 자를 쓴다)."""
    from core import trading_cost
    recs = hb.build_records([_tx("20260701", True, 10, price=10_000),
                             _tx("20260720", False, 10, price=11_000)])
    expected, _ = trading_cost.net_realized_profit(10_000, 11_000, 10)
    assert recs[-1]['profit_amt'] == int(expected)


def test_buy_records_have_no_realized_profit():
    recs = hb.build_records([_tx("20260701", True, 10)])
    assert recs[0]['profit_amt'] == 0 and recs[0]['buy_price'] == 0.0


def test_selling_more_than_held_does_not_go_negative():
    """데이터가 어긋나도(매도 > 보유) 음수 평단·유령 손익을 만들지 않는다."""
    recs = hb.build_records([_tx("20260701", True, 5, price=10_000),
                             _tx("20260720", False, 9, price=11_000)])
    assert recs[-1]['profit_amt'] >= 0
    assert recs[-1]['buy_price'] == pytest.approx(10_000)


def test_overseas_uses_overseas_cost_rates():
    from core import trading_cost
    txs = [_tx("20260701", True, 10, price=100), _tx("20260720", False, 10, price=110)]
    dom = hb.build_records(txs)[-1]['profit_amt']
    ovs = hb.build_records(txs, is_overseas=True)[-1]['profit_amt']
    assert ovs < dom, "해외가 국내보다 비용이 커야 한다"
    assert ovs == int(trading_cost.net_realized_profit(100, 110, 10, is_overseas=True)[0])


# ─────────────────────────────────────────────
# 3. 계획 수립 · 중복 방지
# ─────────────────────────────────────────────

def _holding(code="005930", name="삼성전자", qty=10):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': str(qty)}


def test_plan_skips_zero_quantity_rows(monkeypatch):
    monkeypatch.setattr(hb.api, 'get_period_executions', lambda *a, **k: {})
    assert hb.plan([_holding(qty=0)]) == []


def test_plan_counts_records_already_in_the_db(monkeypatch):
    txs = [_tx("20260728", True, 10, odno="A1")]
    monkeypatch.setattr(hb.api, 'get_period_executions', lambda *a, **k: {"005930": txs})
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: odno == "A1")

    plans = hb.plan([_holding()])
    assert plans[0]['already'] == 1, "이미 있는 기록을 신규로 세면 중복 기록된다"


def test_apply_does_not_rewrite_existing_records(monkeypatch):
    """여러 번 실행해도 중복되지 않아야 한다 — 대사 도구가 스스로 오염원이 되면 안 된다."""
    written_calls = []
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: odno == "DUP")
    monkeypatch.setattr(hb.db_manager.db, 'insert_trade',
                        lambda *a, **k: written_calls.append((a, k)) or True)

    plans = [{'code': '005930', 'name': '삼성전자', 'qty': 10, 'missing': 0, 'already': 1,
              'records': hb.build_records([_tx("20260728", True, 10, odno="DUP"),
                                           _tx("20260805", True, 4, odno="NEW")])}]
    written, skipped = hb.apply(plans)
    assert (written, skipped) == (1, 1)
    assert written_calls[0][0][5] == "NEW"


def test_apply_pins_the_account_context(monkeypatch):
    """계좌 귀속이 어긋나면 복원 자체가 무의미하다 — 계좌를 명시 고정해야 한다."""
    from core import context
    seen = []
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'insert_trade',
                        lambda *a, **k: seen.append(
                            getattr(context.trade_context, 'use_auto_account', False)) or True)
    # AccountContext는 모의투자에서 의도적으로 무동작이다(계좌가 하나뿐). 수동/자동이
    # 갈리는 실전(mode 2) 조건을 만들어야 이 가드가 실제로 검증된다.
    monkeypatch.setattr(hb.config.session, 'auto_cano', "44048158", raising=False)
    monkeypatch.setattr(hb.config.session, 'cano', "68029263", raising=False)

    plans = [{'code': '005930', 'name': '삼성전자', 'qty': 10, 'missing': 0, 'already': 0,
              'records': hb.build_records([_tx("20260728", True, 10, odno="X1")])}]
    hb.apply(plans, cano="44048158")
    assert seen == [True], "자동 계좌로 기록해야 하는데 컨텍스트가 서지 않았다"


# ─────────────────────────────────────────────
# 4. 기동 시 동기화 · 자동계좌 제한 등록
# ─────────────────────────────────────────────

def _stub_sync(monkeypatch, plans, restricted_ok=True):
    monkeypatch.setattr(hb, 'plan', lambda *a, **k: plans)
    monkeypatch.setattr(hb, 'apply', lambda *a, **k: (sum(len(p['records']) for p in plans), 0))
    calls = []
    monkeypatch.setattr(hb, '_restrict_external_buys',
                        lambda codes, c, a: calls.append(sorted(codes)) or (
                            [x[0] for x in sorted(codes)] if restricted_ok else []))
    return calls


def _plan_with_buy(code="005930", name="삼성전자"):
    return [{'code': code, 'name': name, 'qty': 10, 'missing': 0, 'already': 0,
             'records': hb.build_records([_tx("20260728", True, 10, odno="N1", code=code)])}]


def test_sync_restricts_external_buys_on_the_auto_account(monkeypatch):
    """자동 계좌의 외부 매수분은 시스템 매매에서 빼야 한다.

    시스템이 꺼진 사이 운용자가 자동 계좌에서 직접 산 종목을 시스템이 '자기 포지션'으로
    알면, 제 손절 기준으로 운용자의 포지션을 청산한다. 실시간 경로는 이미 막고 있는데
    기동 경로에만 이 방어가 없었다.
    """
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    res = hb.sync_account(holdings=[_holding()], register_restrictions=True)
    assert res['restricted'] == ["005930"]
    assert calls and calls[0][0][0] == "005930"


def test_sync_does_not_restrict_on_the_manual_account(monkeypatch):
    """수동 계좌는 시스템이 보지도 팔지도 않으므로 제한이 필요 없다."""
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    res = hb.sync_account(holdings=[_holding()], register_restrictions=False)
    assert res['restricted'] == [] and calls == []


def test_sync_does_not_restrict_records_that_were_already_there(monkeypatch):
    """이미 기록돼 있던 매수는 새 외부 매수가 아니다 — 제한을 새로 걸면 안 된다."""
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: True)
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    res = hb.sync_account(holdings=[_holding()], register_restrictions=True)
    assert res['restricted'] == [] and calls == []


def test_sync_reports_partial_restorations(monkeypatch):
    plans = _plan_with_buy()
    plans[0]['missing'] = 6
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    _stub_sync(monkeypatch, plans)

    res = hb.sync_account(holdings=[_holding()])
    assert res['partial'] == [("005930", "삼성전자", 6)]


def test_sync_swallows_failures(monkeypatch):
    """동기화 실패가 자동매매 기동을 막아선 안 된다 — 기록은 부가 정보다."""
    monkeypatch.setattr(hb, 'plan', lambda *a, **k: (_ for _ in ()).throw(RuntimeError("KIS down")))
    res = hb.sync_account(holdings=[_holding()])
    assert res['error'] and res['written'] == 0


def test_sync_with_no_holdings_does_nothing(monkeypatch):
    monkeypatch.setattr(hb, 'plan', lambda *a, **k: [])
    assert hb.sync_account(holdings=[])['written'] == 0


# ─────────────────────────────────────────────
# 5. 복원 대상이 아닌 모드
# ─────────────────────────────────────────────

def test_paper_mode_has_nothing_to_restore(monkeypatch):
    """가상투자는 증권사에 체결 이력이 없다 — 원장은 paper DB에 따로 있다.

    [관측 2026-08-10] 가드가 없을 때 조회가 빈 값을 돌려주는 것을 '진입이 조회 구간보다
    과거'로 오해해, 보유 4종목이 전부 '부분 복원'으로 표시되고 복원 0건이 나왔다.
    복원할 것도 복원할 곳도 없는 모드에서는 제안 자체를 하지 않아야 한다.
    """
    monkeypatch.setattr(hb.config.session, 'is_paper', True, raising=False)
    assert hb.supports_broker_history() is False

    called = []
    monkeypatch.setattr(hb, 'plan', lambda *a, **k: called.append(1) or [])
    res = hb.sync_account(holdings=[_holding()])
    assert res == {'written': 0, 'skipped': 0, 'restricted': [], 'partial': [], 'error': None}
    assert not called, "가상투자에서 증권사 체결 조회를 시도했다"


def test_toss_mode_has_nothing_to_restore(monkeypatch):
    """토스는 KIS 체결조회 TR 자체가 없다."""
    monkeypatch.setattr(hb.config.session, 'is_toss', True, raising=False)
    assert hb.supports_broker_history() is False


def test_real_and_simulation_modes_are_restorable(monkeypatch):
    monkeypatch.setattr(hb.config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(hb.config.session, 'is_toss', False, raising=False)
    assert hb.supports_broker_history() is True


def test_trade_history_screen_skips_the_offer_in_paper_mode(monkeypatch):
    """메뉴 9-3에서도 제안이 뜨면 안 된다(잔고 조회조차 하지 않는다)."""
    from modules import account
    monkeypatch.setattr(hb.config.session, 'is_paper', True, raising=False)
    monkeypatch.setattr(account, 'fetch_domestic_balance',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("잔고를 조회했다")))
    account.offer_holdings_backfill()      # 예외 없이 조용히 반환해야 한다


# ─────────────────────────────────────────────
# 5. '외부 매수' 판정 — 자기 주문을 남으로 몰지 않는가
#
# [왜 위험한가] 외부로 판정되면 그 종목이 제한 종목(수동매매)으로 올라가 시스템 매도
# 대상에서 빠진다 = 손절·트레일링이 멈춘다. 판정 근거가 '체결 기록이 없다'였는데,
# 체결 기록은 체결 통보를 받은 뒤 따로 INSERT 된다 — 주문을 내고 체결 전에 프로세스가
# 죽으면(라즈베리파이 OOM·재기동) 접수 기록만 남아 **자기 주문이 외부로 판정**됐다.
# ─────────────────────────────────────────────

def _own_accept_row(stop_loss_rate=-9.5, type_str="매수"):
    return {'type': type_str, 'stop_loss_rate': stop_loss_rate, 'strategy_score': 8.0}


def test_an_order_we_placed_is_not_external_even_without_a_fill_record(monkeypatch):
    """[핵심] 접수 기록만 남은 자기 주문 — 제한을 걸면 그 포지션의 손절이 멈춘다."""
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'get_trade_by_odno',
                        lambda odno: _own_accept_row())
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    res = hb.sync_account(holdings=[_holding()], register_restrictions=True)
    assert res['restricted'] == [] and calls == [], \
        "시스템이 낸 주문을 외부 매수로 보고 자기 포지션을 제한 종목으로 올렸다"


def test_an_external_app_order_is_still_external(monkeypatch):
    """[대조군] 외부 앱(MTS/HTS) 주문도 접수 기록이 생긴다 — 그건 우리 주문이 아니다."""
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'get_trade_by_odno',
                        lambda odno: _own_accept_row(type_str="매수(외부)"))
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    res = hb.sync_account(holdings=[_holding()], register_restrictions=True)
    assert res['restricted'] == ["005930"], "외부 매수 방어가 풀렸다"


def test_an_unknown_order_is_external(monkeypatch):
    """[대조군] 주문 기록이 아예 없으면 외부다(종전 판정과 같다)."""
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'get_trade_by_odno', lambda odno: None)
    calls = _stub_sync(monkeypatch, _plan_with_buy())

    assert hb.sync_account(holdings=[_holding()], register_restrictions=True)['restricted'] == ["005930"]


def test_recovered_own_fill_keeps_its_stop_loss_rate(monkeypatch):
    """복원 기록이 '(외부)'로 남으면 진입 시 손절률이 사라져 청산 기준이 폴백으로 내려간다."""
    written = []
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'get_trade_by_odno',
                        lambda odno: _own_accept_row(stop_loss_rate=-9.5))
    monkeypatch.setattr(hb.db_manager.db, 'insert_trade',
                        lambda *a, **k: written.append((a, k)) or True)

    hb.apply([{'code': '005930', 'name': '삼성전자', 'qty': 10, 'missing': 0, 'already': 0,
               'records': hb.build_records([_tx("20260728", True, 10, odno="MINE")])}])

    args, kwargs = written[0]
    assert "(외부)" not in args[0], f"자기 체결을 외부로 기록했다: {args[0]}"
    assert kwargs['stop_loss_rate'] == -9.5, "진입 시 손절률이 복원되지 않았다"
    assert kwargs['reason'] == hb.OWN_FILL_REASON


def test_recovered_external_fill_stays_labelled_external(monkeypatch):
    """[대조군] 진짜 외부 체결은 '(외부)' 딱지와 복원 사유를 그대로 유지한다."""
    written = []
    monkeypatch.setattr(hb, '_exists', lambda odno, on_date=None: False)
    monkeypatch.setattr(hb.db_manager.db, 'get_trade_by_odno', lambda odno: None)
    monkeypatch.setattr(hb.db_manager.db, 'insert_trade',
                        lambda *a, **k: written.append((a, k)) or True)

    hb.apply([{'code': '005930', 'name': '삼성전자', 'qty': 10, 'missing': 0, 'already': 0,
               'records': hb.build_records([_tx("20260728", True, 10, odno="THEIRS")])}])

    args, kwargs = written[0]
    assert "(외부)" in args[0]
    assert kwargs['reason'] == hb.BACKFILL_REASON
    assert kwargs['stop_loss_rate'] == 0.0


# ─────────────────────────────────────────────
# 6. 조회 실패는 '체결 없음'이 아니다 (2026-09-05)
#
# api.get_period_executions 는 실패해도 빈 dict 를 돌려줬고, 그 아래
# _fetch_period_executions 는 첫 구간 조회가 실패하면 조용히 break 해서
# {code: []} 를 돌려줬다. 그러면 이 모듈은 그것을 '이 기간에 체결이 없다'로 읽는다:
#
#   · 보유분 전체가 '진입이 조회 구간(12개월)보다 과거 = 부분 복원'으로 보고된다.
#     이 모듈의 다짐은 "없는 기록을 지어내지 않는다"인데, 없는 진단을 지어냈다.
#   · 외부 매수를 하나도 못 찾아 제한 등록을 건너뛴다 — 운용자가 직접 산 종목을
#     시스템이 자기 포지션으로 알고 관리한다(손절·트레일링이 그 종목에 걸린다).
#   · summary['error'] 는 None 이라 정상 실행처럼 보인다.
# ─────────────────────────────────────────────
def test_조회_실패로는_계획을_세우지_않는다(monkeypatch):
    monkeypatch.setattr(hb.api, 'get_period_executions', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="체결 없음"):
        hb.plan([_holding()])


def test_체결이_정말_없으면_계획은_빈_기록이다(monkeypatch):
    """대조군 — '없음'은 실패가 아니다. 이 경우는 부분 복원이 맞다."""
    monkeypatch.setattr(hb.api, 'get_period_executions', lambda *a, **k: {"005930": []})
    plans = hb.plan([_holding(qty=10)])
    assert plans[0]['records'] == []
    assert plans[0]['missing'] == 10, "설명 못 한 수량은 그대로 보고해야 한다"


def test_동기화_실패는_부분복원으로_둔갑하지_않는다(monkeypatch):
    monkeypatch.setattr(hb, 'supports_broker_history', lambda: True)
    monkeypatch.setattr(hb.api, 'get_domestic_balance', lambda *a, **k: ([_holding()], []))
    monkeypatch.setattr(hb.api, 'get_period_executions', lambda *a, **k: None)

    summary = hb.sync_account(register_restrictions=True)
    assert summary['error'], "실패가 흔적 없이 지나갔다"
    assert summary['partial'] == [], (
        "조회 실패가 '진입이 12개월보다 과거'라는 진단으로 보고됐다")
    assert summary['restricted'] == []
    assert summary['written'] == 0


def test_첫_구간_조회_실패는_ok_False로_돌아온다(monkeypatch):
    """아래 계층에서 '못 읽었다'를 만들어 준다 — 위에서 가를 수 있게."""
    import api as api_mod
    from api import account as acct

    monkeypatch.setattr(acct, "_paper_active", lambda: False)
    monkeypatch.setattr(api_mod, "call_api",
                        lambda *a, **k: {'rt_cd': '1', 'msg1': 'EGW00201'})
    rows, win, ok = acct._fetch_period_executions(["005930"], cano="1", acnt_prdt_cd="01")
    assert ok is False, "실패인데 '읽었다'로 답했다"
    assert rows == {"005930": []} and win is None

    monkeypatch.setattr(api_mod, "call_api", lambda *a, **k: {'rt_cd': '0', 'output1': []})
    _rows, _win, ok2 = acct._fetch_period_executions(["005930"], cano="1", acnt_prdt_cd="01")
    assert ok2 is True, "정상 응답인데 실패로 봤다"


def test_실패한_진입일_조회는_캐시에_굳지_않는다(monkeypatch):
    """굳으면 15분 동안 '증권사 이력 없음'이 되어 보유일수가 0일로 남는다."""
    import api as api_mod
    from api import account as acct

    monkeypatch.setattr(acct, "_paper_active", lambda: False)
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    stored = []
    monkeypatch.setattr(api_mod, "_set_micro_cache",
                        lambda k, v, *a, **kw: stored.append((k, v)))
    monkeypatch.setattr(api_mod, "_get_micro_cache", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "call_api", lambda *a, **k: {'rt_cd': '1', 'msg1': 'boom'})

    assert acct.get_period_entry_dates(["005930"], qty_map={"005930": 10}) == {}
    assert not stored, "조회 실패로 나온 빈 답이 캐시에 굳었다"


def test_공개_진입점이_실패를_None으로_올린다(monkeypatch):
    """[실제 계층] plan() 이 가르려면 그 아래가 먼저 갈라 줘야 한다."""
    import api as api_mod
    from api import account as acct

    monkeypatch.setattr(acct, "_paper_active", lambda: False)
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)

    monkeypatch.setattr(api_mod, "call_api", lambda *a, **k: {'rt_cd': '1', 'msg1': 'boom'})
    assert acct.get_period_executions(["005930"], cano="1", acnt_prdt_cd="01") is None, (
        "조회 실패를 빈 dict 로 답했다 — 호출부가 '체결 없음'으로 읽는다")

    monkeypatch.setattr(api_mod, "call_api", lambda *a, **k: {'rt_cd': '0', 'output1': []})
    got = acct.get_period_executions(["005930"], cano="1", acnt_prdt_cd="01")
    assert got == {"005930": []}, f"정상 응답인데 실패로 봤다: {got}"


def test_지원하지_않는_모드는_실패가_아니다(monkeypatch):
    """가상투자·토스는 '해당 없음'이라 빈 dict 다 — 그래야 복원이 조용히 넘어간다."""
    from api import account as acct

    monkeypatch.setattr(acct, "_paper_active", lambda: True)
    assert acct.get_period_executions(["005930"]) == {}
