"""곱셈 감소의 기준 — 한도가 구속하지 않았는데 한도를 무너뜨리지 않는다.

[왜 · 2026-09-08] 종전 규칙은 조건 없이 `ref = min(한도, 직전 1초 전송건수)` 였다.
 의도는 옳았다 — 한도가 20인데 실제로 8/s 를 보내는 중이면 20×0.95=19 는 아무것도 바꾸지
 못하는 헛걸음이다. 그런데 그 대안이 과교정이었다. 전송건수가 낮으면 새 기준도 낮아지고,
 0.95 를 곱한 값이 하한 아래로 떨어져 **클램프**된다.

 실측(수정 전, 한도 20에서 거부 한 번):
     직전 1초 전송  1 →  15.00 (하한)      ← 한 번의 거부로 20 에서 바닥까지
     직전 1초 전송 15 →  15.00 (하한)
     직전 1초 전송 18 →  17.10
     직전 1초 전송 20 →  19.00
 **덜 밀수록 더 크게 무너지는** 뒤집힌 응답 곡선이다.

 운영 로그 2,569건 실측: 거부의 45.9%(1,179건)가 하한에 착지했다. 이것은 이 코드의 주석이
 종전 밴드 [17, 19.6] 을 폐기하며 적어 둔 증상과 같다("495건 중 100%가 하한 도달") —
 밴드를 [15, 20] 으로 넓혀도 증상이 남았다. 밴드가 아니라 **기준 규칙**이 원인이었다.
 그리고 집계 창 1,208개 중 전송률이 한도의 절반이라도 된 창은 20개(1.7%)뿐이다.
 대부분의 거부에서 한도는 구속하고 있지 않았고, 한도를 낮추는 것으로는 거부가 줄지 않는다.
"""
from unittest.mock import patch

import pytest

import config
from api import ThrottledSession

NOW = 10000.0


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(config, "REAL_TX_PER_SECOND", 20)
    monkeypatch.setattr(config, "REAL_TPS_SAFETY", 1.0)
    monkeypatch.setattr(config, "REAL_TPS_SAFETY_MIN", 0.75)   # 하한 15
    monkeypatch.setattr(config, "REAL_TPS_SAFETY_MAX", 1.0)    # 상한 20
    return ThrottledSession()


def _reject(session, *, limit_before, sent_1s, now=NOW):
    b = session._real_buckets[session.BUCKET_MANUAL]
    b.adaptive_limit = limit_before
    b.last_drop = 0.0                      # 백오프 창을 열어 둔다
    b.history.clear()
    for _ in range(sent_1s):
        b.history.append(now - 0.5)
    with patch("time.time", return_value=now):
        session._tps_on_rate_limit_real(url="https://x/uapi/y", tr_id="T")
    return b.adaptive_limit


FLOOR = 15.0


@pytest.mark.parametrize("sent_1s", [1, 2, 5, 10, 14, 15])
def test_a_rejection_far_below_the_limit_does_not_collapse_it(gate, sent_1s):
    """한도 20 을 다 쓰지도 않은 상태의 거부 하나가 바닥까지 끌어내리면 안 된다."""
    after = _reject(gate, limit_before=20.0, sent_1s=sent_1s)
    assert after > FLOOR + 0.01, f"직전 1초 {sent_1s}건인데 한 번에 하한({after})으로 갔다"
    assert after == pytest.approx(19.0), after


@pytest.mark.parametrize("sent_1s, expected", [(16, 15.20), (18, 17.10), (20, 19.00)])
def test_a_rejection_at_saturation_still_backs_off_to_the_measured_rate(gate, sent_1s, expected):
    """한도가 실제로 구속했으면 종전대로 **실측 전송률**에서 물러난다.

    이것이 원래 의도이고, 고치면서 잃으면 안 되는 성질이다.
    """
    assert _reject(gate, limit_before=20.0, sent_1s=sent_1s) == pytest.approx(expected)


def test_the_response_is_monotone_in_the_wrong_direction_no_more(gate):
    """'덜 밀수록 더 크게 무너진다'가 사라졌는가 — 곡선 전체를 한 번에 본다."""
    curve = {s: _reject(gate, limit_before=20.0, sent_1s=s)
             for s in (0, 1, 5, 10, 15, 16, 18, 20)}
    #  한도 근처도 아닌 구간(0~15)에서는 전부 같은 한 눈금이어야 한다
    light = {curve[s] for s in (0, 1, 5, 10, 15)}
    assert len(light) == 1, f"구속하지 않은 구간의 반응이 전송량에 따라 갈린다: {curve}"
    #  그리고 그 한 눈금은 포화 구간의 어떤 반응보다도 완만하다
    assert min(light) > min(curve[s] for s in (16, 18)), curve


def test_persistent_rejection_still_reaches_the_floor(gate):
    """계속 거부당하면 여전히 하한까지 내려간다 — 한 번이 아니라 증거로."""
    b = gate._real_buckets[gate.BUCKET_MANUAL]
    b.adaptive_limit = 20.0
    traj = []
    for i in range(8):
        b.last_drop = 0.0
        b.history.clear()
        b.history.append(NOW + i * 2 - 0.5)          # 계속 1건/s
        with patch("time.time", return_value=NOW + i * 2):
            gate._tps_on_rate_limit_real(url="https://x/uapi/y", tr_id="T")
        traj.append(round(b.adaptive_limit, 2))
    assert traj[-1] == pytest.approx(FLOOR), traj
    assert traj[0] > traj[1] > traj[2], f"내려가지 않는다: {traj}"
    assert len([t for t in traj if t > FLOOR]) >= 4, \
        f"너무 빨리 바닥에 닿는다(증거 없이 무너진다): {traj}"


def test_the_backoff_window_still_suppresses_a_second_drop(gate):
    """한 혼잡에 여러 스레드가 동시에 거부돼도 곱셈 감소는 창당 한 번이다."""
    b = gate._real_buckets[gate.BUCKET_MANUAL]
    b.adaptive_limit = 20.0
    b.last_drop = 0.0
    b.history.clear()
    with patch("time.time", return_value=NOW):
        gate._tps_on_rate_limit_real(url="https://x/uapi/y", tr_id="T")
        first = b.adaptive_limit
        for _ in range(5):
            gate._tps_on_rate_limit_real(url="https://x/uapi/y", tr_id="T")
    assert b.adaptive_limit == pytest.approx(first), "같은 창에서 여러 번 내려갔다"


def test_the_floor_and_ceiling_come_from_config_not_from_a_literal(gate, monkeypatch):
    """밴드를 옮기면 반응도 따라 움직여야 한다(폐기된 밴드가 코드에 남지 않게)."""
    monkeypatch.setattr(config, "REAL_TPS_SAFETY_MIN", 0.5)      # 하한 10
    after = _reject(gate, limit_before=20.0, sent_1s=18)
    assert after == pytest.approx(17.10)
    #  구속 구간에서 실측치가 아주 낮으면 새 하한까지는 갈 수 있다
    monkeypatch.setattr(config, "TPS_BACKOFF_BINDING_RATIO", 0.0)
    assert _reject(gate, limit_before=20.0, sent_1s=1) == pytest.approx(10.0)
