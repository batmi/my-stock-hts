"""렌더가 실패해도 캔버스는 돌려줘야 한다.

[배경 · 2026-09-04] 두 렌더 진입점(generate_visual_chart · generate_monte_carlo_histogram)은
_release_render_memory() 를 **성공 경로에서만** 부른다. 그런데 subplots() 와 savefig() 사이에는
지표 계산·박스권 탐지·추세선·저장이 다 들어 있고, 그중 어디서든 예외가 나면 Figure 가
살아남는다 — pyplot 이 레지스트리에 강한 참조를 들고 있어 GC 로도 돌아오지 않는다.

실측(16x22.4in): 실패 한 번마다 피크 +18MB(100DPI) 가 누적되고, 300DPI 저장 중에 실패하면
캔버스 버퍼(약 146MB)째로 남는다. 렌더를 직렬화해 최악 피크를 낮춘 취지(1GB 라즈베리파이
OOM)가 실패 한 번에 무너진다.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pytest

from modules import chart


@pytest.fixture(autouse=True)
def clean_canvas():
    chart._ensure_matplotlib()
    plt.close('all')
    yield
    plt.close('all')


@chart._serialized_render
def _boom():
    plt.subplots(7, 1, figsize=chart.CHART_FIGSIZE)
    raise RuntimeError("렌더 도중 실패")


@chart._serialized_render
def _ok():
    plt.subplots(2, 1)
    chart._release_render_memory()
    return "그렸다"


def test_a_failed_render_does_not_leak_the_figure():
    with pytest.raises(RuntimeError):
        _boom()
    assert plt.get_fignums() == [], "실패한 렌더가 Figure 를 남겼다"


def test_repeated_failures_do_not_accumulate():
    """운영 프로세스는 계속 살아 있다 — 누적이 다음 렌더의 피크에 그대로 얹힌다."""
    for _ in range(5):
        with pytest.raises(RuntimeError):
            _boom()
    assert plt.get_fignums() == []


def test_the_original_exception_is_not_swallowed():
    """회수는 하되 실패를 숨기면 안 된다 — 호출부가 '차트 생성 실패'를 알아야 한다."""
    with pytest.raises(RuntimeError, match="렌더 도중 실패"):
        _boom()


def test_the_lock_is_released_after_a_failure():
    """락을 쥔 채 죽으면 다음 차트 요청이 영원히 멈춘다."""
    with pytest.raises(RuntimeError):
        _boom()
    assert chart._RENDER_LOCK.acquire(blocking=False)
    chart._RENDER_LOCK.release()


def test_the_success_path_is_untouched(caplog):
    """성공 경로는 이미 회수했다 — 그물이 다시 쓸어 담거나 경고를 남기면 안 된다."""
    with caplog.at_level("WARNING"):
        assert _ok() == "그렸다"
    assert plt.get_fignums() == []
    assert not [r for r in caplog.records if "Figure 를 남긴 채" in r.getMessage()]


def test_it_survives_matplotlib_never_being_loaded(monkeypatch):
    """토스 시봉처럼 matplotlib 적재 전에 빠져나가는 경로가 있다 — 그물이 터지면 안 된다."""
    monkeypatch.setattr(chart, "plt", None)

    @chart._serialized_render
    def _early_return():
        return None

    assert _early_return() is None
