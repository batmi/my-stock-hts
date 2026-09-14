"""KRX 애프터마켓 2차 감사(2026-09-14 저녁)에서 고친 네 자리를 고정한다.

1. VI 감시는 정규장+애프터(운용자 확인: 애프터에도 VI 적용), CB 는 정규장만(애프터엔 CB 없음).
2. 체결 감시는 살아있는 국내 시장이 있으면 폴링한다 — 거래 시간 설정(자동매매 운용 범위)에 묶이면
   앱/HTS 의 프리·애프터 체결을 놓치고, 주문번호가 당일 채번이라 다음날엔 못 찾는다.
3. account.sync_today_trades 도 conclusion 과 같은 손익 규칙(재계산·매입가·비용·외부 매도 평단)을 쓴다.
4. 세션 전환 알림·예약 '당일' 경고 문구가 애프터마켓을 안다.
"""
import inspect

import pytest
from unittest.mock import patch

import api
import config
from modules import account, market_halt, trading
from modules.auto_trade import conclusion, trader


def _monitor():
    market_halt.MarketHaltMonitor._instance = None
    return market_halt.MarketHaltMonitor()


def test_vi_window_covers_after_market_and_cb_stays_regular(monkeypatch):
    m = _monitor()
    calls = []
    monkeypatch.setattr(config, "MARKET_HALT_ALERT_USE", True, raising=False)
    monkeypatch.setattr(config, "MARKET_HALT_VI_USE", True, raising=False)
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(m, "_check_cb_kis", lambda: calls.append("cb"))
    monkeypatch.setattr(m, "_check_vi_kis", lambda: (calls.append("vi") or ({}, set())))
    monkeypatch.setattr(m, "_diff_vi_alerts", lambda cur, chk: None)

    for phase, expect in (("krx", {"cb", "vi"}), ("krx_after", {"vi"}), ("nxt_after", set()), ("closed", set()), ("nxt_pre", set())):
        calls.clear()
        m.last_cb_check = m.last_vi_check = 0
        with patch.object(api, "domestic_session_phase", lambda: phase):
            m.check()
        assert set(calls) == expect, f"{phase}: {calls}"


def test_conclusion_polls_while_any_domestic_market_is_open():
    m = conclusion.ConclusionMonitor.__new__(conclusion.ConclusionMonitor)
    with patch.object(api, "domestic_trading_session_open", lambda: True), \
            patch.object(conclusion, "is_system_market_open", lambda: False):
        assert m._is_market_open() is True, "설정이 15:30 이라도 애프터에 외부 체결이 생긴다"
    with patch.object(api, "domestic_trading_session_open", lambda: False), \
            patch.object(conclusion, "is_system_market_open", lambda: False):
        assert m._is_market_open() is False


def test_history_sync_shares_conclusion_profit_rules():
    src = inspect.getsource(account.sync_today_trades)
    assert "_concl._recalc_realized(" in src
    assert "_concl._ledger_buy_price(" in src
    i = src.index("db_manager.db.insert_trade(")
    call = src[i:i + 700]
    assert "buy_price=_bp" in call and "cost_amt=_cost_amt" in call


def test_session_change_message_knows_after_market():
    full = inspect.getsource(trader)
    assert '"1600" <= now_time_str < "1610"' in full and "애프터마켓 개시" in full
    assert '"1530" <= now_time_str < "1540"' not in full, "휴게(15:30~16:00)엔 시장이 없어 도달 불가한 분기"


def test_reserved_today_warning_boundary_is_2000():
    src = inspect.getsource(trading)
    assert '_hm_now > "2000"' in src
    assert '"1530" <= _hm_now < "1600"' in src


# ==========================================================
# 5. 잔고 AFHR_FLPR_YN — 확장 세션에선 Y(통합 최종가), 아니면 N(정규장 종가)
#    실측 2026-09-14: N/X → 16,140(정규장 종가), Y → 16,160(애프터 최종가)
# ==========================================================

@pytest.mark.parametrize("phase, fixed_on, expect", [
    ("active", True, "Y"),      # NXT 프리 — N 이면 전일 종가로 손절 판정
    ("krx_after", True, "Y"),   # KRX 애프터 — N 이면 15:30 가격으로 손절 판정
    ("skip", True, "N"),        # 정규장 — 같은 값, 종전 유지
    ("break", True, "N"),       # 휴게 — 시장 없음
    ("offhours", True, "N"),    # 야간·정규장 종가 고정
    ("offhours", False, "Y"),   # 야간·마지막 실거래가 표시 — 라벨 'KRX 애프터 최종가'와 일치
])
def test_balance_afhr_flag_follows_session(phase, fixed_on, expect):
    with patch.object(api, "_nxt_quote_phase", lambda: phase), \
            patch.object(config, "USE_KRX_CLOSE_AFTER_HOURS", fixed_on, create=True):
        assert api.balance_afhr_flpr_yn() == expect


def test_domestic_balance_sends_session_flag():
    from api import account as acct
    src = inspect.getsource(acct.get_domestic_balance)
    assert '"AFHR_FLPR_YN": _api().balance_afhr_flpr_yn()' in src
    assert '"AFHR_FLPR_YN": "N"' not in src
