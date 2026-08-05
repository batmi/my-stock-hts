"""AIMD 가산 증가는 요청당이 아니라 윈도우당 한 번이다.

[관측 2026-08-05 · 라즈베리파이 실전 시세] EGW00201(초당 거래건수 초과)이 30분간
100건 넘게 났다. 우선순위 게이트를 넣은 뒤에도 cand_io_*·at_cand_* 같은 시스템
스레드가 최종 실패했다 — 우선순위 배분 이전에 전역 총량이 서버 한도를 넘고 있었다.

원인은 AIMD의 증가 주기다. 성공 1건마다 TPS_ADAPT_STEP(0.05)을 더하면, 초당 ~18건을
쏘는 동안 실효 상승률이 초당 0.9 TPS가 된다. 바닥(17)→천장(19.6)이 2.6 TPS이므로
백오프 후 약 3초면 천장에 복귀하고 다시 걸린다. 평형점이 천장(19.6)인데 서버 한도가
20이라, 레이트리밋이 '정상 상태'가 되어 버린다.
"""
import time

import pytest

import api
import config


@pytest.fixture
def sess():
    from collections import deque
    s = api.ThrottledSession.__new__(api.ThrottledSession)
    import threading
    s.lock = threading.Lock()
    s.adaptive_limit_real = None
    s._last_tps_raise = 0.0
    s.request_history_real = deque()
    s.request_history_sim = deque()
    return s


def test_increase_happens_at_most_once_per_window(sess):
    """[회귀 방지] 성공이 쏟아져도 1초 안에는 한 번만 올린다."""
    sess.adaptive_limit_real = 17.0
    for _ in range(200):
        sess._tps_on_success_real()

    step = getattr(config, 'TPS_ADAPT_STEP', 0.05)
    assert sess.adaptive_limit_real == pytest.approx(17.0 + step), (
        f"성공 200건에 {sess.adaptive_limit_real - 17.0:.2f} TPS 올랐다 — "
        "요청당 증가로 되돌아가면 평형점이 천장에 고정되어 EGW00201이 상시 발생한다")


def test_next_window_allows_another_increase(sess):
    """윈도우가 지나면 다시 한 단계 올린다(상승 자체를 막는 게 아니다)."""
    sess.adaptive_limit_real = 17.0
    sess._tps_on_success_real()
    sess._last_tps_raise -= 1.01          # 한 윈도우 경과를 흉내
    sess._tps_on_success_real()

    step = getattr(config, 'TPS_ADAPT_STEP', 0.05)
    assert sess.adaptive_limit_real == pytest.approx(17.0 + 2 * step)


def test_backoff_is_immediate_and_multiplicative(sess):
    """감소는 즉시·곱셈이다(요청당 억제 없음) — 물러날 때는 빨라야 한다."""
    sess.adaptive_limit_real = 19.6
    sess._tps_on_rate_limit_real()
    assert sess.adaptive_limit_real == pytest.approx(19.6 * config.TPS_ADAPT_BACKOFF)


def test_backoff_holds_for_one_window(sess):
    """물러난 직후 곧바로 되올리지 않는다 — 낮춘 값으로 한 윈도우는 관찰한다."""
    sess.adaptive_limit_real = 19.6
    sess._tps_on_rate_limit_real()
    lowered = sess.adaptive_limit_real
    for _ in range(50):
        sess._tps_on_success_real()
    assert sess.adaptive_limit_real == pytest.approx(lowered)


def test_recovery_to_ceiling_takes_realistic_time(sess):
    """천장 복귀에 걸리는 시간이 '초 단위'가 아니라 '분 단위'여야 한다.

    이 여유가 있어야 컨트롤러가 천장이 아니라 실제 한도 아래에서 수렴한다.
    """
    lo, hi, _start = sess._real_tps_bounds()
    step = getattr(config, 'TPS_ADAPT_STEP', 0.05)
    windows_needed = (hi - lo) / step
    assert windows_needed >= 30, (
        f"바닥에서 천장까지 {windows_needed:.0f}초 — 너무 빨라 천장에 상시 붙는다")


def test_bounds_stay_below_nominal_limit(sess):
    """실효 한도는 명목 한도(20 TPS)를 넘지 않는다."""
    lo, hi, start = sess._real_tps_bounds()
    assert lo < start <= hi < config.REAL_TX_PER_SECOND


def test_rate_limit_logs_observed_send_rate(sess, caplog):
    """[진단] 거부 시점의 클라이언트 실제 전송률을 남긴다.

    게이트는 한 프로세스가 실효 한도를 넘길 수 없게 만든다. 그런데도 서버가 거부하면
    원인은 게이트 밖(다른 프로세스·낮은 계정 한도)이며, 둘은 '그 순간 우리가 실제로
    몇 건을 보냈는가'로만 갈린다. 추측하지 않으려면 그 값이 로그에 있어야 한다.
    """
    import logging
    now = time.time()
    sess.request_history_real = [now - 0.1, now - 0.2, now - 0.3, now - 1.05]
    sess.adaptive_limit_real = 18.0

    with caplog.at_level(logging.WARNING, logger="api"):
        sess._tps_on_rate_limit_real()

    msg = "\n".join(r.message for r in caplog.records)
    assert "EGW00201" in msg
    assert "직전 1초 전송 3건" in msg, f"1초 내 전송 건수가 없거나 틀리다: {msg}"
    assert "1.1초 창 4건" in msg
    assert "TPS" in msg
