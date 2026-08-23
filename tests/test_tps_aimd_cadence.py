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
    # [수정 2026-08-09] __new__ 로 껍데기만 만들고 필요한 속성을 손으로 채우던 방식은,
    #  세션에 진단용 필드가 하나 늘 때마다 AttributeError 로 무너졌다(실제로 무너졌다).
    #  생성자는 네트워크를 타지 않으므로 그냥 정상 생성한다.
    return api.ThrottledSession()


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


def test_recovery_to_ceiling_is_fast(sess):
    """[정책 2026-08-09] 천장 복귀는 빨라야 한다 — 속도 최우선.

    종전에는 '천장에 상시 붙지 않도록' 복귀를 분 단위로 늦췄다. 그 전제는 천장이 실제
    한도보다 위에 있어서 붙으면 손해라는 것이었는데, 실측은 반대였다 — 성공 처리량이
    목표 TPS에 단조 증가한다(6 TPS 5.92/s → 20 TPS 15.58/s). 물러나 있는 시간이 곧 손해다.
    한 번 물러났으면 수 초 안에 명목 한도로 돌아와야 한다.
    """
    lo, hi, _start = sess._real_tps_bounds()
    step = getattr(config, 'TPS_ADAPT_STEP', 0.5)
    seconds_needed = (hi - lo) / step
    assert seconds_needed <= 15, (
        f"바닥에서 천장까지 {seconds_needed:.0f}초 — 속도 우선 정책에서 너무 오래 물러나 있다")


def test_bounds_stay_within_nominal_limit(sess):
    """실효 한도는 명목 한도(20 TPS)를 넘지 않는다.

    [변경 2026-08-09] 종전에는 천장을 명목보다 낮게(0.98) 두었으나, 실측 무릎이 6 TPS라
    천장이 어디든 도달할 일이 없었다. 시작·천장을 명목에 맞추고 컨트롤러가 내려오게 한다.
    """
    lo, hi, start = sess._real_tps_bounds()
    assert lo < start <= hi <= config.REAL_TX_PER_SECOND


def test_rate_limit_log_is_aggregated_and_diagnostic(sess, caplog):
    """[핵심] 거부 로그는 집계 한 줄이되, 원인 후보를 가르는 값은 전부 담는다.

    거부는 초당 수 건까지 나므로 건건이 WARNING을 남기면 로그가 그것만으로 찬다.
    그렇다고 값을 빼면 사후 분석이 다시 추측으로 돌아간다 — 실제로 그래서 운영 로그
    495건을 재분석해야 했다. 첫 거부는 즉시, 이후는 주기마다 한 줄로 묶는다.
    """
    import logging

    sess.adaptive_limit_real = 18.0
    sess.gate_grants_real = 500

    with caplog.at_level(logging.WARNING, logger="api"):
        sess._tps_on_rate_limit_real(
            url="https://x/uapi/domestic-stock/v1/quotations/inquire-price?a=1",
            tr_id="FHKST01010100")

    msg = "\n".join(r.message for r in caplog.records)
    assert "EGW00201" in msg
    assert "첫 거부" in msg, f"첫 거부는 즉시 남아야 한다: {msg}"
    assert "TR FHKST01010100" in msg, f"거부된 요청을 특정할 수 없다: {msg}"
    assert "스레드" in msg and "실효 한도 18.00" in msg
    assert "미경유 재전송" in msg, "게이트 아래 누출 여부가 빠졌다"
    assert "중복 프로세스" in msg, "다른 프로세스 여부가 빠졌다"


def test_rate_limit_log_does_not_spam(sess, caplog):
    """주기 안에서는 한 줄만 남기고 나머지는 묶는다(백오프는 매번 적용된다)."""
    import logging

    sess.adaptive_limit_real = 18.0
    with caplog.at_level(logging.WARNING, logger="api"):
        for _ in range(30):
            sess._last_tps_drop = 0.0     # 서로 다른 혼잡 창을 가정
            sess._tps_on_rate_limit_real(tr_id="FHKST01010100")

    lines = [r for r in caplog.records if "EGW00201" in r.message]
    assert len(lines) == 1, f"거부 30건에 로그가 {len(lines)}줄 나왔다 — 집계가 안 된다"
    assert sess.adaptive_limit_real < 18.0 * 0.9, "로그를 묶느라 백오프까지 건너뛰었다"


def test_band_is_speed_first_but_capped_at_nominal(sess):
    """[정책 2026-08-09] 밴드는 명목 한도(20) 아래에 붙되, 실측 무릎(6)까지 내려가지 않는다.

    [실측 · tools/probe_kis_tps.py 고정 속도 스윕]
        6 TPS → 5.92/s 통과(거부 0%)      10 TPS →  8.33/s (15.8%)
        8 TPS → 6.83/s (14.6%)            20 TPS → 15.58/s (22.1%)
    거부는 초과분만 쳐낼 뿐 처벌이 아니고, 성공 처리량은 목표 TPS에 단조 증가한다.
    그래서 '무릎(6)에서 수렴'은 처리량을 2.6배 버리는 선택이다 — 운용 판단은 속도 우선이고,
    문서 한도 20을 넘지 않는 선에서 최대한 붙여 운행한다.

    (이 자리에는 직전까지 '밴드가 무릎을 품어야 한다'는 테스트가 있었다. 거부를 없애는
     것이 목표라면 맞지만, 목표가 속도라면 틀린 기준이다. 정책이 바뀌었으므로 함께 바꾼다.)
    """
    lo, hi, start = sess._real_tps_bounds()
    nominal = config.REAL_TX_PER_SECOND
    assert hi <= nominal and start <= nominal, f"명목 한도({nominal})를 넘는다: start={start}, hi={hi}"
    assert lo >= nominal * 0.7, (
        f"하한 {lo:.1f} — 속도 우선 정책에서 이만큼 물러나면 처리량 손실이 크다")


def test_backoff_descends_then_holds_at_floor(sess):
    """거부가 반복되면 하한까지 내려가고 거기서 멈춘다(그 아래로는 가지 않는다)."""
    lo, _hi, _start = sess._real_tps_bounds()
    sess.adaptive_limit_real = _hi          # 천장에서 시작
    seen = []
    for _ in range(40):   # 명목 20 → 하한 1까지 ×0.9 로 약 29회 필요
        sess._last_tps_drop = 0.0     # 매번 다른 혼잡 창으로 본다(같은 창은 1회만 내린다)
        sess._tps_on_rate_limit_real()
        seen.append(sess.adaptive_limit_real)

    assert seen[0] > seen[1], f"연속 백오프가 감소하지 않는다: {seen[:3]}"
    assert seen[-1] == pytest.approx(lo), f"하한({lo})에 수렴하지 않았다: {seen[-1]}"
    assert all(v >= lo - 1e-9 for v in seen), "하한 아래로 내려갔다"


def test_single_reject_backs_off_once(sess):
    """[회귀 방지] 한 응답에 백오프가 두 번 걸리지 않는다.

    HTTP 500 본문 검사와 msg_cd 검사가 같은 응답을 각각 처리하면 실효 한도가 실제보다
    두 배 빠르게 내려간다(2026-08-05 로그: 같은 스레드가 1ms 간격으로 두 줄).
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    #  구 api.py 는 2026-08-23 패키지로 분해됐다 — TPS 게이트는 api/http.py 로 옮겼다.
    src = open(os.path.join(root, "api", "http.py"), encoding="utf-8").read()
    assert "rate_limited_handled = False" in src, "중복 백오프 방지 플래그가 없다"
    assert "not rate_limited_handled" in src, "msg_cd 분기가 플래그를 확인하지 않는다"


# ==========================================================
# [추가 2026-08-09] 거부 원인을 가르는 계측
# ==========================================================
def test_adapter_retries_are_sealed():
    """[회귀 방지] 어댑터 레벨 재시도는 0이어야 한다.

    어댑터 재시도는 TPS 게이트 아래에서 일어나 히스토리에 잡히지 않는다. 하나라도
    열려 있으면 한 논리 요청이 소켓에는 여러 번 나가고, 게이트가 세는 전송률과 서버가
    보는 전송률이 갈린다 — '한도보다 낮은데 거부당한다'의 유력한 후보였다.
    """
    assert isinstance(api.retry_strategy, api.GatedRetry)
    assert api.retry_strategy.total == 0, "어댑터 재시도가 다시 열렸다(게이트 우회 경로)"
    assert 500 not in (api._token_session.get_adapter("https://x").max_retries.status_forcelist or []), \
        "토큰 세션이 HTTP 500을 재시도한다 — EGW00201이 500으로 오므로 게이트 밖에서 연사된다"


def test_gated_retry_counts_only_actual_resends():
    """계수기는 '실제로 다시 보낸' 횟수만 센다(예산 소진 후의 시도는 재전송이 아니다)."""
    before = api.GatedRetry.ungated_resends
    r = api.GatedRetry(total=1, backoff_factor=0)
    r2 = r.increment(method="GET", url="/x", error=Exception("boom"))
    assert api.GatedRetry.ungated_resends == before + 1

    from urllib3.exceptions import MaxRetryError
    with pytest.raises(MaxRetryError):
        r2.increment(method="GET", url="/x", error=Exception("boom"))
    assert api.GatedRetry.ungated_resends == before + 1, "재전송하지 않은 시도까지 셌다"


def test_connection_drop_uses_short_wait():
    """끊긴 keep-alive 는 장애용 지수 백오프가 아니라 짧은 대기로 재시도한다.

    어댑터 재시도를 봉인하면서 이 흔한 경우가 앱 레벨로 올라왔다. 지수 백오프를 그대로
    태우면 서버가 유휴 소켓을 닫을 때마다 초 단위로 멈춘다.
    """
    drop = api._retry_wait_seconds(2, "('Connection aborted.', RemoteDisconnected('...'))")
    fault = api._retry_wait_seconds(2, "KIS Server Intermittent Error (MCI): 게이트웨이")
    assert drop <= api.RATE_LIMIT_RETRY_WAIT_MAX + 0.5 + 1e-9
    assert fault > drop, "연결 끊김이 진짜 장애와 같은 대기를 쓴다"


# ==========================================================
# [추가 2026-08-09] 곱셈 감소의 주기와 기준
# ==========================================================
def test_backoff_applies_once_per_congestion_window(sess):
    """[핵심] 한 번의 혼잡에 한 번만 물러난다.

    초과가 나면 여러 스레드가 동시에 거부당한다. 그걸 건건이 곱하면 한 혼잡에 ×0.9가
    수십 번 걸려 한도가 바닥까지 무너진다 — 실측에서 거부 20건이 실효 한도를 20 → 2.43
    까지 끌어내렸고, 조회 캡이 1 TPS(요청 간격 1초)가 되어 메뉴 2-5가 눈에 띄게 느려졌다.
    종전에는 하한 17이 이 붕괴를 가려 주고 있었을 뿐이다.
    TCP의 곱셈 감소가 RTT당 1회인 것과 같은 이유다 — 같은 창의 추가 손실은 이미 반영됐다.
    """
    now = time.time()
    sess.adaptive_limit_real = 20.0
    sess.request_history_real.extend([now - i * 0.05 for i in range(18)])   # 실제 18건/초

    for _ in range(20):
        sess._tps_on_rate_limit_real(tr_id="FHKST01010100")

    assert sess.adaptive_limit_real == pytest.approx(18 * config.TPS_ADAPT_BACKOFF), (
        f"한 혼잡에 백오프가 여러 번 걸렸다 — 실효 한도가 {sess.adaptive_limit_real:.2f}까지 무너졌다")


def test_backoff_anchors_to_measured_send_rate(sess):
    """[핵심] 물러나는 기준은 설정 한도가 아니라 직전 1초의 실제 전송 건수다.

    한도가 20인데 실제로는 8/s를 보내는 중이면 20×0.9=18은 아무것도 바꾸지 못하는
    헛걸음이다. 그 헛걸음이 쌓여야 비로소 실효가 되는데, 그때는 이미 지나치게 내려간 뒤다.
    실측 전송률에서 물러나면 한 번에 맞는 자리로 간다.
    """
    now = time.time()
    sess.adaptive_limit_real = 20.0
    # 하한(15) 위에서 봐야 기준이 드러난다 — 그 아래는 클램프가 가린다.
    sess.request_history_real.extend([now - i * 0.05 for i in range(18)])   # 실제 18건/초

    sess._tps_on_rate_limit_real(tr_id="FHKST01010100")

    assert sess.adaptive_limit_real == pytest.approx(18 * config.TPS_ADAPT_BACKOFF), (
        f"실측 18건/초에서 물러났어야 하는데 {sess.adaptive_limit_real:.2f}가 됐다")
