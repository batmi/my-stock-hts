"""화면·로그가 계좌 성격을 거짓말하지 않는가.

파이(가상투자)와 맥북(한투 실전)을 함께 돌린다. 어느 인스턴스인지가 화면과 로그로만
구분되므로, 표시가 틀리면 장애 때 **실계좌부터** 뒤지게 된다. 구문 가드는
test_branch_overwrite_shape.py 가 맡고, 여기서는 실제로 무엇이 찍히는지를 못 박는다.
"""
import pytest

import config
import core.utils as cu


@pytest.fixture
def breadcrumb(monkeypatch):
    monkeypatch.setattr(cu.context, 'USER_ACTION_BREADCRUMB', ["메뉴"], raising=False)
    printed = []
    monkeypatch.setattr(config.console, 'print', lambda *a, **k: printed.append(str(a[0]) if a else ""))
    return printed


def _as(monkeypatch, *, paper=False, toss=False):
    monkeypatch.setattr(config.session, 'is_paper', paper, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', toss, raising=False)


@pytest.mark.parametrize("kw, expected, color", [
    (dict(paper=True), "[가상투자]", "cyan"),
    (dict(toss=True), "[토스증권]", "magenta"),
    (dict(), "[한투증권]", "bold red"),
])
def test_메뉴_헤더가_뜬_모드를_말한다(breadcrumb, monkeypatch, kw, expected, color):
    """종전에는 분기 밖의 한 줄이 셋 다 '[한투증권]'(빨강)으로 덮었다.

    빨강은 실전 경고색이라 색까지 함께 거짓말한다.
    """
    _as(monkeypatch, **kw)
    cu.print_breadcrumb()
    header = next((line for line in breadcrumb if "시스템 시간" in line), None)
    assert header, f"헤더가 찍히지 않았다: {breadcrumb}"
    assert expected in header, header
    assert color in header, f"색이 모드를 따라오지 않는다: {header}"


def test_자동매매_주기_로그가_뜬_모드를_말한다(monkeypatch):
    """trader._run_loop 의 '운용 계좌' 줄 — 관제 화면과 하트비트는 고쳤는데 이 자리가
    남아 있었다. 매 주기 남는 줄이라 사고 후 로그를 되짚을 때 가장 많이 보게 된다."""
    import inspect
    from modules.auto_trade import trader as T

    src = inspect.getsource(T.AutoTrader._run_loop)
    head = src[:src.index('self.log(f"운용 계좌')]
    tail = head[head.rindex('acc_type = "가상투자"'):]
    assert 'else:' in tail, \
        "'한투증권(자동)' 대입이 분기 밖에 있다 — 가상투자·토스도 실전으로 찍힌다"
