"""정정 주문 경로도 손익 정책 하나를 따라야 한다 — 2026-09-14 수동 매도 수정이 비켜 간 자리.

[무엇이 있었나 · 2026-09-16] 매도 주문을 정정하면 거래소가 **새 주문번호**를 준다. 체결 확인
 (conclusion)은 그 번호의 접수 행을 원 주문으로 읽고, 그 행의 buy_price 로 '실제 체결가' 손익을
 다시 센다. 그런데 trading.modify_order 가 만드는 정정 접수 행에는 buy_price·cost_amt 가 없었고,
 정정가로 다시 세는 추정치는 qty×(정정가−매입가)를 **직접** 계산해 cost_amt 도 적지 않았다.
 결과:
   · 실제 체결가가 아니라 정정가 추정치가 굳는다(매입가 없음 → _recalc_realized 가 물러남).
     외부 매도용 원장 평단 폴백도 profit_amt 가 0 이 아니라 뛰어넘는다.
   · 가상투자에서는 이 행만 순손익이 아니다(실측: 3,460→3,455 ×10주, 정책 −131 vs 직접 −50).
   · cost_amt 가 비어 원금 불변량(profit−cost)이 그 왕복 비용만큼 어긋난다 — 누적이 문턱을 넘는
     날 '출금'으로 오인되는 경로(2026-08-31 사고와 같다).
"""
import inspect
from unittest.mock import patch

import config
from core import trading_cost
from modules import trading


def _revise_src():
    return inspect.getsource(trading.modify_order)


def test_정정_접수_행이_매입가와_비용을_상속한다():
    src = _revise_src()
    i = src.index('insert_trade(\n                    f"{full_action_name}(수동)"')
    call = src[i:src.index(")", src.index("snapshot=inherited_snapshot", i))]
    assert "buy_price=inherited_buy_price" in call, "정정 행에 매입가가 없으면 체결 확인이 정정가 추정치를 굳힌다"
    assert "cost_amt=inherited_cost" in call


def test_정정가_재추정은_정책_함수를_지난다():
    src = _revise_src()
    tail = src[src.index("매도 정정일 경우 새로운 가격으로 예상 손익 재계산"):]
    assert "trading_cost.realized_profit(" in tail, "정정 경로만 손익을 직접 세면 모드별 정책이 여기서 갈린다"
    assert "est_sell_amt - est_buy_amt" not in tail, "총액 직접 계산이 돌아왔다"
    assert "cost_amt=_c" in tail, "재추정한 비용을 함께 적지 않으면 원금 불변량이 그만큼 어긋난다"


def test_정정_재추정의_매입가는_원_주문_행을_먼저_본다():
    """잔고 평단은 정정 사이에 추가 매수가 있었으면 이미 다른 값이다."""
    src = _revise_src()
    tail = src[src.index("매도 정정일 경우 새로운 가격으로 예상 손익 재계산"):]
    assert "buy_price = inherited_buy_price" in tail
    assert tail.index("buy_price = inherited_buy_price") < tail.index("get_domestic_balance")


def test_가상투자에서_직접_계산과_정책이_실제로_갈린다():
    """이 테스트가 참이어야 위 가드들이 지키는 것이 실재한다(문구 가드의 전제)."""
    with patch.object(config.session, 'is_paper', True, create=True):
        amt, _r, cost = trading_cost.realized_profit(3460, 3455, 10)
    direct = int(10 * 3455 - 10 * 3460)
    assert direct == -50 and amt != direct, f"정책({amt})과 직접 계산({direct})이 같다면 이 가드는 빈 것이다"
    assert cost == 0.0
    with patch.object(config.session, 'is_paper', False, create=True):
        amt, _r, cost = trading_cost.realized_profit(3460, 3455, 10)
    assert amt == direct and cost > 0, "실거래는 총차익이 같되 비용이 따로 남아야 한다"
