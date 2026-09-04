"""가상 계좌 성과 화면의 낙폭이 입출금을 손실로 세지 않는가.

[배경] 실계좌 쪽은 2026-08-30 에 같은 문제를 정리했다 — 자산 이력을 옮기지 않고
`net_transfer` 로 읽을 때 환산한다([[daily-asset-baseline-transfers]] 의 2026-08-23 사고).
그런데 5-8 성과 화면이 보는 `paper_equity` 곡선만 총자산 그대로 낙폭을 재고 있었다.
출금은 현금을 계단처럼 깎으므로, 매매가 조금도 나빠지지 않아도 그만큼이 드로다운으로
찍힌다. 드로다운은 리스크 한도를 조이는 값이라 '가짜 낙폭'은 그냥 표시 오류가 아니다.
"""
import pytest

from modules.paper_broker import equity_index, max_drawdown


def _rows(pairs):
    return [{"total": t, "seed": s} for t, s in pairs]


def test_a_withdrawal_is_not_a_drawdown():
    """자산이 한 번도 줄지 않은 계좌에서 300만원을 뺐다."""
    rows = _rows([(10_000_000, 10_000_000),
                  (10_200_000, 10_000_000),
                  (7_400_000, 7_000_000),      # 출금 300만 (총자산·시드 동시 감소)
                  (7_600_000, 7_000_000)])
    assert max_drawdown(equity_index(rows)) == pytest.approx(0.0, abs=1e-9)


def test_the_raw_curve_would_have_called_it_a_27_percent_crash():
    """고치기 전 산식이 무엇을 보고했는지 남겨 둔다 — 이 값이 결함의 크기다."""
    totals = [10_000_000, 10_200_000, 7_400_000, 7_600_000]
    peak, mdd = totals[0], 0.0
    for v in totals:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    assert mdd < -25


def test_a_deposit_does_not_inflate_the_peak():
    """입금은 고점을 올려 뒤이은 진짜 낙폭을 부풀린다 — 반대 방향의 같은 결함."""
    rows = _rows([(10_000_000, 10_000_000),
                  (15_000_000, 15_000_000),    # 500만 입금
                  (14_700_000, 15_000_000)])   # 그 뒤 -2%
    assert max_drawdown(equity_index(rows)) == pytest.approx(-2.0, abs=0.01)


def test_a_real_loss_still_shows_up():
    """중립화가 진짜 손실까지 지워 버리면 지표가 죽는다."""
    rows = _rows([(10_000_000, 10_000_000),
                  (10_200_000, 10_000_000),
                  (7_000_000, 7_000_000),      # 출금 300만 + 실제 20만 손실
                  (7_200_000, 7_000_000)])
    assert max_drawdown(equity_index(rows)) == pytest.approx(-1.96, abs=0.05)


def test_a_loss_then_recovery_keeps_the_worst_point():
    rows = _rows([(100.0, 100.0), (80.0, 100.0), (120.0, 100.0)])
    assert max_drawdown(equity_index(rows)) == pytest.approx(-20.0, abs=1e-9)


def test_the_live_value_is_appended():
    """오늘의 하락은 다음 스냅샷을 기다리지 않고 곧바로 낙폭에 들어가야 한다."""
    rows = _rows([(100.0, 100.0), (110.0, 100.0)])
    assert max_drawdown(equity_index(rows)) == pytest.approx(0.0, abs=1e-9)
    with_live = equity_index(rows, live_total=90.0, live_seed=100.0)
    assert max_drawdown(with_live) < -18


def test_old_rows_without_a_seed_are_treated_as_transfer_free():
    """seed 컬럼이 생기기 전 행 — 추정하느니 입출금 없음으로 본다."""
    rows = [{"total": 100.0, "seed": None},
            {"total": 90.0, "seed": None},
            {"total": 95.0, "seed": 95.0}]
    assert max_drawdown(equity_index(rows)) == pytest.approx(-10.0, abs=1e-9)


@pytest.mark.parametrize("rows", [[], [{"total": 100.0, "seed": 100.0}]])
def test_degenerate_curves_do_not_crash(rows):
    assert max_drawdown(equity_index(rows)) == 0.0


def test_a_zero_total_does_not_divide_by_zero():
    rows = _rows([(0.0, 100.0), (50.0, 100.0)])
    max_drawdown(equity_index(rows))     # 예외가 안 나면 통과
