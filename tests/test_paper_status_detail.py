"""가상투자 성과 현황(9-6-5)이 '지금 무엇이 돌고 있는지'를 보여주는가.

[왜 이 화면에 상세가 필요한가] 위쪽 요약표는 **청산이 쌓여야** 채워진다(PF·승률·연속손절).
운용 초기에는 전부 0이라 화면만 봐서는 시스템이 판정을 하고 있는지 멈춰 있는지 구분되지
않는다. 그런데 이 모드의 존재 이유가 '실매매와 같은 판정을 하는가'를 확인하는 것이므로,
판정의 산출물인 포지션 상태(보유일·증액 차수·TS 무장·청산선·시간청산 잔여)를 드러낸다.

[가장 중요한 계약] TS 청산선은 **실제 청산 판정과 같은 함수**로 계산해야 한다. 표시용으로
식을 다시 쓰면 화면의 선과 실제로 팔리는 선이 갈라진다 — 그 갈라짐은 조용하고, 사용자는
화면을 믿는다.
"""
from unittest.mock import patch

import pytest

import config
from modules import paper_broker, paper_report


PERF = {
    "seed": 10_000_000, "total": 9_930_320, "total_return": -0.70, "cash": 6_138_271,
    "positions": 1, "sell_count": 0, "win": 0, "loss": 0, "win_rate": 0.0, "pf": 0.0,
    "gross_profit": 0, "gross_loss": 0, "mdd": -1.28, "max_loss_streak": 0,
    "started_at": "2026-08-05 21:00:02",
}


def _position(code="005930", name="삼성전자", avg=70000.0, first="2026-08-06 09:10:00"):
    return {"code": code, "name": name, "qty": 10, "avg_price": avg,
            "first_buy_at": first, "last_buy_at": first}


def _render(capsys, positions, highs, price=77000.0, perf=None):
    # 이 화면은 넓은 터미널을 전제로 한다(열이 10개). 좁은 폭에서는 rich가 열을 줄여
    #  '삼성 …'처럼 잘라내므로, 폭을 고정하지 않으면 문자열 단언이 폭에 따라 흔들린다.
    from rich.console import Console

    curve = [{"date": f"2026-08-{d:02d}", "cash": 1, "stock_value": 1, "total": 9_900_000}
             for d in range(6, 20)]
    with patch.object(config, "console", Console(width=200)), \
         patch.object(paper_broker, "get_performance", return_value=dict(perf or PERF)), \
         patch.object(paper_broker, "get_positions", return_value=positions), \
         patch.object(paper_broker, "get_equity_curve", return_value=curve), \
         patch.object(paper_broker, "_current_price", return_value=price), \
         patch("modules.db_manager.db.get_all_trailing_stops", return_value=highs), \
         patch("modules.db_manager.db.get_pyramid_count", return_value=0):
        paper_report._print_status()
    return capsys.readouterr().out


def test_보유가_있으면_판정_상태를_보여준다(capsys):
    out = _render(capsys, [_position()], {"005930": 82000.0})
    assert "보유 포지션 상세" in out
    assert "삼성전자" in out
    assert "무장" in out, "TS가 발동선을 넘었는데 상태가 표시되지 않았다"


def test_TS_청산선은_실제_청산_함수의_값과_같다(capsys):
    """[핵심] 화면의 선과 실제로 팔리는 선이 같아야 한다."""
    from modules.auto_trade import engine

    high, avg, cur = 82000.0, 70000.0, 77000.0
    expected = engine.compute_trailing_stop(high, avg, cur)
    assert expected and expected["armed"], "표본이 무효다 — 무장 상태를 만들지 못했다"

    out = _render(capsys, [_position(avg=avg)], {"005930": high}, price=cur)
    assert f"{expected['stop_price']:,.0f}" in out, (
        f"화면의 청산선이 engine.compute_trailing_stop({expected['stop_price']:,.0f})과 다르다")


def test_최고가_기록이_없으면_추적_전으로_밝힌다(capsys):
    """모르는 것을 '대기'로 적으면 안 된다 — 아직 추적이 시작되지 않은 것이다."""
    out = _render(capsys, [_position()], {})
    assert "추적 전" in out


def test_시간청산_잔여일이_보인다(capsys):
    out = _render(capsys, [_position(first="2026-08-06 09:10:00")], {})
    days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 15)
    assert "D-" in out or "도달" in out, f"시간청산({days}일) 잔여 표기가 없다"


def test_표본이_적으면_백분위를_성과로_읽지_말라고_경고한다(capsys):
    out = _render(capsys, [_position()], {})
    assert "성과 판정으로 읽지 마세요" in out, (
        "2주짜리 표본을 3년 분포와 나란히 놓고 경고가 없으면 오독을 부른다")


def test_청산이_충분히_쌓이면_경고를_거둔다(capsys):
    perf = dict(PERF, sell_count=40, started_at="2024-01-02 09:00:00")
    out = _render(capsys, [_position()], {}, perf=perf)
    assert "성과 판정으로 읽지 마세요" not in out


def test_보유가_없어도_화면이_깨지지_않는다(capsys):
    out = _render(capsys, [], {})
    assert "운용 표본" in out
    assert "보유 포지션 상세" not in out


def test_평가금액과_수량이_함께_보인다(capsys):
    """수량 × 현재가 = 평가금액. 둘 다 없으면 '이 종목이 계좌에서 얼마인가'를 알 수 없다."""
    out = _render(capsys, [_position()], {"005930": 82000.0}, price=77000.0)
    assert "수량" in out and "평가금액" in out
    assert "770,000" in out, "평가금액(10주 × 77,000원)이 표에 없다"


def test_표_폭이_상한을_넘지_않는다(capsys):
    """표 폭 상한은 메뉴 2-1 출력 폭(실측 135열)이다.

    열을 더할 때마다 조용히 넘어가면 좁은 터미널에서 rich 가 값을 '…'로 잘라낸다 —
    잘리는 것은 대개 오른쪽 끝의 손절선·리스크, 즉 가장 봐야 할 열이다. 넓은 폭으로
    렌더해서 실제 필요 폭을 재고, 상한을 넘으면 여기서 깨뜨린다.

    자릿수가 큰 종목(6자리 단가·네 자리 수량)을 넣는다 — 폭은 데이터가 정한다.
    """
    pos = _position(name="SK이노베이션", avg=522_000.0)
    pos["qty"] = 1_270
    out = _render(capsys, [pos], {"005930": 600_000.0}, price=548_000.0)
    rules = [ln.rstrip() for ln in out.splitlines() if ln.strip() and set(ln.strip()) <= {"─"}]
    assert rules, "표가 렌더되지 않았다"
    assert max(len(ln) for ln in rules) <= 135, \
        f"보유 포지션 상세 표가 {max(len(ln) for ln in rules)}열 — 상한 135열을 넘었다"
