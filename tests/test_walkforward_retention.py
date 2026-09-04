"""Walk-Forward 의 '성과 유지율'이 길이 차이를 성과 저하로 착각하지 않는가.

[구조] IS 는 anchored/expanding 이라 폴드가 진행될수록 길어지고(분석 구간의 40%→85%),
OOS 는 고정 길이(15%)다. 즉 폴드마다 IS 가 OOS 의 2.7~5.6배다. 누적 수익률끼리 나누면
**과최적화가 전혀 없어도** 유지율이 20%대로 찍힌다. 이 도구가 내놓는 결론이 그 숫자
하나이므로, 그러면 판정 문턱(40%/70%)이 닿을 수 없는 자리에 놓인다.
"""
import pytest

from modules.backtest import annualized_return as ann


def _fold_windows(start_idx=250, n=1000, n_splits=4):
    """run_walk_forward 의 구간 산술을 그대로 옮긴 것."""
    analysis_len = n - start_idx
    oos_region_start = start_idx + int(analysis_len * 0.4)
    fold_size = (n - oos_region_start) // n_splits
    out = []
    for i in range(n_splits):
        oos_start = oos_region_start + i * fold_size
        oos_end = n if i == n_splits - 1 else oos_start + fold_size
        out.append((oos_start - start_idx, oos_end - oos_start))
    return out


def test_the_is_window_really_is_several_times_longer():
    """전제 확인 — 이게 사실이 아니면 아래 테스트는 의미가 없다."""
    ratios = [is_len / oos_len for is_len, oos_len in _fold_windows()]
    assert min(ratios) > 2.5 and max(ratios) > 5.0, ratios


def test_a_strategy_with_no_decay_scores_a_hundred_percent():
    """하루 수익률이 IS·OOS 완전히 동일한 전략 = 과최적화 0. 유지율은 100% 여야 한다."""
    d = 0.0005
    is_ann, oos_ann = [], []
    for is_len, oos_len in _fold_windows():
        is_ann.append(ann(((1 + d) ** is_len - 1) * 100, is_len))
        oos_ann.append(ann(((1 + d) ** oos_len - 1) * 100, oos_len))
    retention = sum(oos_ann) / sum(is_ann) * 100
    assert 99.0 <= retention <= 101.0, retention


def test_the_old_cumulative_comparison_was_structurally_wrong():
    """종전 산식이 같은 전략을 어떻게 깎았는지 남겨 둔다(회귀 방지용 기준선)."""
    d = 0.0005
    is_c = [((1 + d) ** a - 1) * 100 for a, _ in _fold_windows()]
    oos_c = [((1 + d) ** b - 1) * 100 for _, b in _fold_windows()]
    assert sum(oos_c) / sum(is_c) * 100 < 30, "이 값이 100 에 가까웠다면 결함이 아니었다"


def test_real_decay_still_shows_up():
    """길이 보정이 과최적화를 가려 버리면 도구가 죽는다 — 절반으로 떨어지면 절반이 나와야."""
    is_ann, oos_ann = [], []
    for is_len, oos_len in _fold_windows():
        is_ann.append(ann(((1 + 0.0005) ** is_len - 1) * 100, is_len))
        oos_ann.append(ann(((1 + 0.00025) ** oos_len - 1) * 100, oos_len))
    retention = sum(oos_ann) / sum(is_ann) * 100
    assert 45 <= retention <= 55, retention


@pytest.mark.parametrize("ret, bars, expected", [
    (0.0, 252, 0.0),
    (10.0, 252, 10.0),          # 딱 1년이면 그대로
    (-100.0, 60, -100.0),       # 전액 손실 — 환산이 정의되지 않는다
    (5.0, 0, 0.0),              # 길이 0 은 판정 불가
])
def test_edges(ret, bars, expected):
    assert ann(ret, bars) == pytest.approx(expected, abs=1e-6)


def test_a_loss_annualizes_to_a_worse_loss_over_a_short_window():
    """짧은 구간의 -10% 는 1년으로 늘리면 훨씬 큰 손실이다 — 부호와 방향이 뒤집히면 안 된다."""
    assert ann(-10.0, 60) < -10.0
    assert ann(-10.0, 500) > -10.0
