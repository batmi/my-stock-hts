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


def test_recovery_to_ceiling_takes_realistic_time(sess):
    """천장 복귀에 걸리는 시간이 '초 단위'가 아니라 '분 단위'여야 한다.

    이 여유가 있어야 컨트롤러가 천장이 아니라 실제 한도 아래에서 수렴한다.
    """
    lo, hi, _start = sess._real_tps_bounds()
    step = getattr(config, 'TPS_ADAPT_STEP', 0.05)
    windows_needed = (hi - lo) / step
    assert windows_needed >= 30, (
        f"바닥에서 천장까지 {windows_needed:.0f}초 — 너무 빨라 천장에 상시 붙는다")


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
            sess._tps_on_rate_limit_real(tr_id="FHKST01010100")

    lines = [r for r in caplog.records if "EGW00201" in r.message]
    assert len(lines) == 1, f"거부 30건에 로그가 {len(lines)}줄 나왔다 — 집계가 안 된다"
    assert sess.adaptive_limit_real < 18.0 * 0.9, "로그를 묶느라 백오프까지 건너뛰었다"


def test_band_can_reach_the_measured_knee(sess):
    """[핵심] AIMD 밴드는 실측 무릎을 품어야 한다 — 그러지 못하면 컨트롤러가 정지한다.

    [실측 2026-08-09 · tools/probe_kis_tps.py 고정 속도 스윕]
      앱키 2개(REAL·VIRT) × 기기 2대에서 같은 무릎이 나왔다.
        ~5 TPS 거부 0% · 6 TPS 1.7% · 7 TPS 12.9% · 8 TPS 14.4%

    종전 밴드는 [17.0, 19.6]으로, 무릎(6)보다 **통째로 위**에 있었다. 그래서 어떤 입력을
    줘도 하한에 눌린 채 움직일 수 없었고, 운영 로그 495건(08-06~08-08)의 100%가
    '하한 도달'이었다 — AIMD가 4일 내내 정지 상태였다는 뜻이다.

    (종전 이 자리에는 '하한을 내려도 거부는 그대로고 처리량만 깎인다'는 2026-08-05
     관측이 근거로 박혀 있었다. 그 관측은 지금 고친 잘못된 지표(수신 시각 기준 전송률)로
     읽은 것이라 기각한다. 통제된 속도로 다시 재니 무릎은 분명히 존재했다.)
    """
    lo, hi, _start = sess._real_tps_bounds()
    knee = 6.0
    assert lo < knee < hi, (
        f"밴드 [{lo:.2f}, {hi:.2f}]가 실측 무릎 {knee} TPS를 품지 못한다 — "
        f"컨트롤러가 한쪽 끝에 눌려 적응이 멈춘다")


def test_backoff_descends_then_holds_at_floor(sess):
    """거부가 반복되면 하한까지 내려가고 거기서 멈춘다(그 아래로는 가지 않는다)."""
    lo, _hi, _start = sess._real_tps_bounds()
    sess.adaptive_limit_real = _hi          # 천장에서 시작
    seen = []
    for _ in range(40):   # 명목 20 → 하한 1까지 ×0.9 로 약 29회 필요
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
    src = open(os.path.join(root, "api.py"), encoding="utf-8").read()
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
