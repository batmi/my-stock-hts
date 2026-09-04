"""tvDatafeed 초기화 실패에도 회로차단이 걸려야 한다.

[배경] 종전에는 생성이 실패하면 인스턴스가 None 으로 남고, 다음 호출이 초기화를 통째로
다시 밟았다. 그 안에는 캐시 토큰이 없을 때의 실제 로그인(_tv_signin, HTTP POST)이 있다.
토스 모드 지수 화면 한 번이 tvDatafeed 를 7회 부르므로(코스피200·코스닥150·국채 4테너·
HY OAS), TV 가 흔들리는 동안 로그인 시도가 화면 한 번에 7회씩 나갔다 — TradingView 는
반복 로그인에 캡차를 물린다(토큰을 7일 캐시하는 이유와 같다).
"""
import pytest

from modules import analysis


@pytest.fixture
def failing_init(monkeypatch):
    """초기화가 실패하는 상태 — 시도 횟수를 센다."""
    attempts = []

    def _init():
        attempts.append(1)
        analysis._tv_note_failure()
        return None

    monkeypatch.setattr(analysis, "_init_tvdatafeed", _init)
    monkeypatch.setattr(analysis, "_TVDATAFEED_INSTANCE", None, raising=False)
    analysis.reset_tvdatafeed_circuit()
    yield attempts
    analysis.reset_tvdatafeed_circuit()


def test_repeated_calls_try_init_once(failing_init):
    """지수 화면 한 번(7 호출)에 초기화 시도는 1회뿐이다."""
    for _ in range(7):
        assert analysis._get_tvdatafeed() is None
    assert len(failing_init) == 1


def test_user_retry_reopens_immediately(failing_init):
    """사용자가 화면에서 재시도(y)하면 즉시 다시 시도한다 — 안 그러면 재시도가 거짓말이다."""
    analysis._get_tvdatafeed()
    analysis.reset_tvdatafeed_circuit()
    analysis._get_tvdatafeed()
    assert len(failing_init) == 2


def test_force_bypasses_the_breaker(failing_init):
    """의도적 재생성 경로는 차단을 넘는다 — 방금 스스로 버린 인스턴스를 되살려야 한다."""
    analysis._get_tvdatafeed()          # 차단 개시
    assert len(failing_init) == 1
    analysis._get_tvdatafeed()          # 막힌다
    assert len(failing_init) == 1
    analysis._get_tvdatafeed(force=True)
    assert len(failing_init) == 2, "복구 경로가 차단에 막히면 복구가 아니라 고장이 된다"


def test_existing_instance_is_returned_even_while_open(monkeypatch):
    """이미 만들어진 인스턴스는 차단 중에도 그대로 쓴다(차단은 '재생성'만 막는다)."""
    sentinel = object()
    monkeypatch.setattr(analysis, "_TVDATAFEED_INSTANCE", sentinel, raising=False)
    analysis._tv_note_failure()
    try:
        assert analysis._get_tvdatafeed() is sentinel
    finally:
        analysis.reset_tvdatafeed_circuit()


def test_recovery_path_uses_force():
    """국채 조회의 인스턴스 재생성이 force 로 불리는지(계약 고정)."""
    import inspect

    src = inspect.getsource(analysis.get_us_treasury_spot_data)
    assert "_get_tvdatafeed(force=True)" in src
