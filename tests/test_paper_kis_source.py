"""가상투자(mode 1)가 KIS 시세를 쓰되 실계좌를 건드리지 않는가.

[배경] mode 1는 토스 시세를 쓰다가 KIS 실전 시세로 바뀌었다. 토스를 쓸 때는 계좌성
API가 `if config.session.is_toss:` 분기에서 먼저 막혔는데, is_toss가 False가 되면서
그 방어가 통째로 사라졌다. 즉 **가로채기를 빠져나간 계좌성 호출은 이제 실제 KIS로
나간다**. VIRT 앱키에도 실제 계좌가 매여 있으므로, 계좌번호만 맞으면 '가상투자'인데
실 잔고를 읽거나 실주문을 낼 수 있다.

방어는 두 겹이다.
  1) cano = "PAPER" — 계좌번호 자체가 실계좌가 아니다(fail-safe). 가로채기를 놓친
     호출이 있어도 조용히 성공하지 않고 실패한다.
  2) _paper_active() 가드 — 계좌를 요구하는 13개 함수 전부에 있어야 한다.

[앱키 분리] KIS의 TPS(20)·웹소켓 동시 연결(1)·토큰 발급(1분 1회) 제약은 모두 앱키
단위다. 실전 인스턴스와 키를 공유하면 두 프로세스가 서로를 모른 채 각자 18TPS로 밀어
양쪽 다 EGW00201에 갇히고 웹소켓은 서로 끊는다. VIRT_APP_KEY로 분리하는 이유다.
"""
import inspect
import os

import pytest
from unittest.mock import patch

import api
import config
from core.session import SessionManager


@pytest.fixture
def paper_session(monkeypatch, tmp_path):
    """가상투자(mode 1) 세션을 실제 initialize()로 만든다(설정 분기 자체가 검증 대상)."""
    monkeypatch.setenv("VIRT_APP_KEY", "VIRTKEY123")
    monkeypatch.setenv("VIRT_APP_SECRET", "VIRTSECRET123")
    monkeypatch.setenv("REAL_APP_KEY", "REALKEY999")
    monkeypatch.setenv("REAL_APP_SECRET", "REALSECRET999")
    monkeypatch.setenv("REAL_ACC_NUM", "12345678-01")
    s = SessionManager()
    with patch.object(s, '_activate_paper_mode'):      # DB 전환은 이 테스트 대상이 아니다
        s.initialize(mode='1')
    return s


# ───────────────── 세션 구성 ─────────────────

def test_mode1_uses_kis_not_toss(paper_session):
    """[핵심] mode 1는 더 이상 토스가 아니다 — mode 2와 같은 데이터 경로여야 한다."""
    assert paper_session.is_toss is False, "토스 경로면 체결강도·지수·일봉이 mode 2와 달라진다"
    assert paper_session.is_paper is True
    assert paper_session.url_base == config.REAL_URL, "가상투자는 KIS 실전 서버 시세를 쓴다"
    assert paper_session.url_base == config.REAL_URL


def test_mode1_uses_the_virt_app_key(paper_session):
    """[핵심] 실전 앱키를 쓰면 TPS·웹소켓·토큰을 실전 인스턴스와 공유해 양쪽이 깨진다."""
    assert paper_session.app_key == "VIRTKEY123"
    assert paper_session.app_secret == "VIRTSECRET123"
    # 토큰 발급 경로(_fetch_and_set_token "REAL")가 읽는 슬롯도 VIRT여야 한다
    assert paper_session.real_app_key == "VIRTKEY123", \
        "토큰 발급이 실전 앱키로 나가 EGW00133을 실전 인스턴스와 다툰다"
    assert paper_session.auto_app_key == "VIRTKEY123"


def test_mode1_never_carries_a_real_account_number(paper_session):
    """[핵심 fail-safe] 계좌번호가 실계좌면 가로채기를 놓친 호출이 실 잔고를 읽는다."""
    assert paper_session.cano == "PAPER"
    assert paper_session.auto_cano == "PAPER"
    assert "12345678" not in f"{paper_session.cano}{paper_session.auto_cano}", \
        "REAL_ACC_NUM이 새어 들어왔다"


def test_mode3_is_still_toss(monkeypatch):
    """대조군 — mode 3(토스 실전)은 그대로여야 한다(분기 분리 시 회귀 방지)."""
    monkeypatch.setenv("TOSS_APP_KEY", "T"); monkeypatch.setenv("TOSS_APP_SECRET", "S")
    monkeypatch.setenv("TOSS_ACC_NUM", "9999")
    s = SessionManager()
    s.initialize(mode='3')
    assert s.is_toss is True and s.is_paper is False
    assert s.cano == "9999", "토스 실전이 PAPER 계좌를 쓰고 있다"


def test_shared_key_is_warned(monkeypatch, capsys):
    """VIRT와 REAL이 같으면 분리의 의미가 없다 — 조용히 넘기면 안 된다."""
    monkeypatch.setenv("VIRT_APP_KEY", "SAME"); monkeypatch.setenv("VIRT_APP_SECRET", "X")
    monkeypatch.setenv("REAL_APP_KEY", "SAME"); monkeypatch.setenv("REAL_APP_SECRET", "X")
    s = SessionManager()
    with patch.object(s, '_activate_paper_mode'):
        s.initialize(mode='1')
    out = capsys.readouterr().out
    assert "VIRT_APP_KEY" in out and ("경고" in out or "⚠" in out), \
        f"키 공유를 경고하지 않는다: {out}"


def test_missing_virt_key_is_warned(monkeypatch, capsys):
    """키가 없으면 시세 조회가 통째로 실패한다 — 기동 시점에 알려야 한다."""
    monkeypatch.delenv("VIRT_APP_KEY", raising=False)
    monkeypatch.delenv("VIRT_APP_SECRET", raising=False)
    s = SessionManager()
    with patch.object(s, '_activate_paper_mode'):
        s.initialize(mode='1')
    out = capsys.readouterr().out
    assert "VIRT_APP_KEY" in out and ("경고" in out or "⚠" in out)


# ───────────────── 실계좌 차단 ─────────────────

ACCOUNT_FUNCS = [
    # get_today_profit_summary 는 get_period_profit_summary 로 위임하는 얇은 껍데기라
    #  계좌를 직접 요구하지 않는다(가드는 위임받는 쪽에 있다).
    "get_domestic_balance", "get_overseas_balance", "get_period_profit_summary",
    "get_today_history", "get_period_entry_dates", "get_overseas_today_history",
    "get_domestic_open_orders", "get_overseas_open_orders", "place_order",
    "revise_cancel_order", "get_deposit", "get_foreign_deposit", "get_deposit_balance",
    "_fetch_period_executions",
]


@pytest.mark.parametrize("fname", ACCOUNT_FUNCS)
def test_every_account_call_is_intercepted(fname):
    """[핵심 불변식] 계좌를 요구하는 함수는 전부 관찰 모드 가드를 가져야 한다.

    토스 시절에는 is_toss 분기가 대신 막아줬다. 그 방어가 사라졌으므로 여기서 못박는다.
    새 계좌성 API를 추가하고 가드를 빠뜨리면 이 목록과 함께 걸린다.
    """
    src = inspect.getsource(getattr(api, fname))
    assert "_paper_active()" in src, \
        f"{fname}에 관찰 모드 가드가 없다 — 가상투자가 실계좌를 건드린다"


def test_the_guard_list_covers_everything_that_needs_an_account():
    """위 목록이 실제 코드와 어긋나지 않는지 — 목록만 낡으면 검사가 헐거워진다."""
    src = api.__file__ and open(api.__file__, encoding='utf-8').read().splitlines()
    starts = [(i, l) for i, l in enumerate(src) if l.startswith('def ')]
    found = set()
    for idx, line in enumerate(src):
        if '_prepare_account_params(' in line and not line.startswith('def '):
            owner = None
            for i, l in starts:
                if i <= idx:
                    owner = l
                else:
                    break
            if owner:
                found.add(owner.split('(')[0][4:])
    missing = found - set(ACCOUNT_FUNCS)
    assert not missing, f"계좌를 요구하는 새 함수가 검사 목록에 없다: {sorted(missing)}"


def test_paper_deposit_comes_from_the_virtual_account():
    """대조군 — 가드가 '차단'이 아니라 '가상 데이터 대체'로 동작해야 한다."""
    with patch.object(api, '_paper_active', return_value=True), \
         patch('modules.paper_broker.get_cash', return_value=4_200_000):
        res = api.get_deposit()
    assert res["d2_deposit"] == 4_200_000, f"가상 예수금이 오지 않았다: {res}"


def test_paper_holding_days_do_not_query_the_broker():
    """보유일수는 가상 DB가 안다 — 증권사 체결 이력을 뒤지면 실계좌 조회다."""
    with patch.object(api, '_paper_active', return_value=True), \
         patch.object(api, 'call_api') as called:
        assert api.get_period_entry_dates(["005930"]) == {}
    assert not called.called, "관찰 모드인데 증권사 체결 이력을 조회했다"
