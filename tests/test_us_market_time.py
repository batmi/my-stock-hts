"""미국 주식 수동 주문이 지금이 어떤 세션인지를 어디서 알아내는가.

[배경 · 2026-09-04 감사] send_order 는 UTC→ET 서머타임 계산과 구간 판정을 **직접** 했다.
api 쪽(us_session_phase / now_us_eastern)의 주석은 "trading.py 의 주문 세션 판별과 동일
규칙"이라고 적고 있었지만 실제로는 별개 구현이었고, 결정적으로 **휴장일을 보지 않았다**
(api 는 XNYS 거래소 달력을 본다).

실측 대조:
  2026-11-26 ET 10:00 (추수감사절) → send_order '정규장'(ord_dvsn=00) / api '휴장'
  2026-09-05 KST 낮  (토요일)     → send_order '데이마켓'(31)        / api '휴장'
잘못된 세션 코드로 나간 주문은 증권사가 거부하는데, 화면은 '정규장 접수'라고 말하므로
왜 안 됐는지를 사람이 알 수 없다.

세션 구간·서머타임 자체의 검증은 api 쪽(test_market_session_label)에 있다. 여기서는
**주문 화면이 그 판정을 그대로 쓰는가**만 본다 — 그게 이번 수정의 계약이다.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

import config
from modules import trading


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    original_level = config.FILE_DEBUG_LEVEL
    config.FILE_DEBUG_LEVEL = "OFF"
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(config.console, "print", MagicMock())
    monkeypatch.setattr(config.console, "log", MagicMock())
    progress = MagicMock()
    progress.__enter__.return_value = progress
    monkeypatch.setattr("modules.trading.Progress", lambda *a, **k: progress)
    monkeypatch.setattr("builtins.print", MagicMock())
    yield
    logging.disable(logging.NOTSET)
    config.FILE_DEBUG_LEVEL = original_level


def _run_buy(phase, answers=("AAPL", "1", "150", "y"), et_hm="11:00"):
    """해외 매수 화면을 한 번 태우고 place_order 호출을 돌려준다(없으면 None)."""
    import datetime as dt

    with patch('modules.trading.api.place_order') as place, \
         patch('modules.trading.api.send_telegram_message'), \
         patch('modules.trading.api.get_chart_data', return_value=None), \
         patch('modules.trading.api.get_current_price', return_value=150.0), \
         patch('modules.trading.api.get_stock_name_by_code', return_value="Apple Inc."), \
         patch('modules.trading.api.find_best_exchange_code', return_value="NAS"), \
         patch('modules.trading.api.fetch_overseas_buyable_quantity', return_value=10), \
         patch('modules.trading.api.us_session_phase', return_value=phase), \
         patch('modules.trading.api.now_us_eastern',
               return_value=dt.datetime(2026, 9, 4, int(et_hm[:2]), int(et_hm[3:]))), \
         patch('modules.trading.analysis.print_table'), \
         patch('modules.trading.Prompt.ask') as ask, \
         patch('modules.trading.utils.validate_and_confirm_stock', return_value=True), \
         patch('modules.trading.utils.show_menu', return_value="5"), \
         patch('modules.trading.select_account',
               return_value=("12345678", "01", "실전투자")), \
         patch('modules.trading.db_manager.db'), \
         patch('modules.trading.auto_trade.AutoTrader'), \
         patch('modules.trading.auto_trade.ConclusionMonitor'):
        ask.side_effect = list(answers)
        place.return_value = {'rt_cd': '0', 'output': {'ODNO': '000123'}}
        trading.send_order("buy")
        return place.call_args if place.called else None


# ─────────────────────────────────────────────
# 1. api 의 세션 판정이 주문 구분으로 이어지는가
# ─────────────────────────────────────────────

@pytest.mark.parametrize("phase,ord_dvsn", [
    ("pre", "32"),
    ("regular", "00"),
    ("after", "34"),
    ("day", "31"),
])
def test_session_phase_decides_the_order_division(phase, ord_dvsn):
    call = _run_buy(phase)
    assert call is not None, f"{phase}: 주문이 나가지 않았다"
    args, _ = call
    assert args[0] == "overseas"
    assert args[5] == ord_dvsn, f"{phase} 인데 ord_dvsn={args[5]}"


# ─────────────────────────────────────────────
# 2. 휴장일 — 종전에는 '정규장'이라 말하며 그대로 보냈다
# ─────────────────────────────────────────────

def test_a_closed_market_is_not_silently_treated_as_regular():
    """[핵심] 추수감사절 ET 10:00 이 '정규장'으로 나가던 자리."""
    call = _run_buy("closed", answers=("AAPL", "1", "150", "n"))   # 휴장 진행? → n
    assert call is None, "휴장인데 주문이 그대로 나갔다"


def test_the_user_can_still_force_an_order_when_closed():
    """판단은 사람 몫이다 — 막지 않고 묻는다(증권사가 받아 주는 경우도 있다)."""
    # 프롬프트 순서: 종목코드 → 수량 → 단가 → [휴장 진행?] → 최종확인
    call = _run_buy("closed", answers=("AAPL", "1", "150", "y", "y"), et_hm="11:00")
    assert call is not None, "진행을 택했는데 주문이 나가지 않았다"
    assert call[0][5] == "00", "강행 시에는 시간대로 가장 가까운 세션을 고른다"


# ─────────────────────────────────────────────
# 3. 판별이 다시 갈라지지 않는가
# ─────────────────────────────────────────────

def test_trading_no_longer_computes_dst_itself():
    """세 곳에 흩어져 있던 서머타임 계산을 api 하나로 모았다."""
    src = _code_only(trading.send_order)
    assert "dst_start_utc" not in src and "march_second_sunday" not in src
    assert "api.us_session_phase()" in src


def _code_only(fn):
    """주석을 뺀 실행 코드 — 왜 고쳤는지 적은 주석에 옛 구간이 남아 있어도 되게."""
    import inspect

    return "\n".join(l.split("  #")[0] for l in inspect.getsource(fn).splitlines()
                     if not l.strip().startswith("#"))


@pytest.mark.parametrize("fn", [trading.send_order, trading.modify_order])
def test_domestic_nxt_window_comes_from_the_session_helper(fn):
    """종전 구간('0800'~'0850')은 정본(08:00~09:00)과 10분 어긋나 있었다 —
    08:50~09:00 에 낸 시장가가 NXT 로 ord_dvsn='01' 로 나갔다(NXT 는 시장가 미지원).
    발주와 정정 두 화면에 같은 구간이 복사돼 있었다."""
    src = _code_only(fn)
    assert "api.domestic_session_phase()" in src
    assert '"0850"' not in src
