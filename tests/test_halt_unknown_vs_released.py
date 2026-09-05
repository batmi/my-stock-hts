"""서킷브레이커·VI 감시: **조회 실패는 '해제'가 아니다**.

[왜 이 파일이 있나 · 2026-09-05]
market_halt 는 REST 폴링 결과를 직전 상태와 비교해 발동/해제를 알린다. 그런데 비교의
한쪽(현재 집합)에는 **조회에 성공한 종목만** 담기고, 실패한 종목은 그냥 빠졌다. 빠진 것과
'해제된 것'이 같은 모양이라 다음 두 가지가 실제로 일어났다(둘 다 실측 재현):

 · VI — 발동 중인 종목의 REST 호출이 한 번 타임아웃하면 '🔄 VI 해제'가 나가고 래치까지
   풀린다. 다음 주기에 다시 보이면 '⚡ VI 발동'이 또 나간다. VI 지속은 약 2분, 폴링은
   30초라 이 왕복은 드문 일이 아니다. 알림이 스스로 뒤집히면 사람은 알림을 믿지 않게 된다.
 · CB — 바스켓 3종목 중 1종목만 응답하면 판정에 필요한 2를 채울 수 없어 **무조건**
   '정지 아님'이 되고, 정지 중인 시장에 '✅ 서킷브레이커 해제'가 나간다. 하필 CB 중은
   모두가 시세를 두드려 조회가 가장 잘 실패하는 순간이다.

세 번째 축은 전달이다. 상태를 먼저 뒤집고 알림을 보내면, 텔레그램이 끊긴 동안 난 사건은
영영 알려지지 않는다(다음 주기엔 상태가 같아 재알림이 없다). 전달을 확인한 뒤에 뒤집는다.
([[unknown-vs-empty]], [[infra-layer-audit-2026-09]] 의 '알림은 전달 확인 뒤 표시'와 같은 규칙)
"""
from unittest.mock import patch

import pytest

from modules import market_halt


@pytest.fixture
def monitor():
    m = market_halt.MarketHaltMonitor.__new__(market_halt.MarketHaltMonitor)
    m._init()
    return m


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(market_halt, "alert_delivered",
                        lambda msg, urgent=False: (box.append(msg), True)[1])
    return box


def _price(vi_map, fail=()):
    def _f(code, is_overseas=False):
        if code in fail:
            raise RuntimeError("timeout")
        return {"rt_cd": "0", "output": {"vi_cls_code": vi_map.get(code, "0")}}
    return _f


# --------------------------------------------------------------------------
# VI
# --------------------------------------------------------------------------
TARGETS = {"005930": "삼성전자", "000660": "SK하이닉스"}


def test_vi_조회_실패는_해제로_읽지_않는다(monitor, sent):
    """못 본 종목은 직전 상태를 유지한다 — 알림이 발동→해제→발동으로 흔들리지 않는다."""
    with patch.object(monitor, "_domestic_targets", lambda: TARGETS):
        with patch.object(market_halt.api, "get_current_price_data",
                          side_effect=_price({"005930": "1"})):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())
        assert monitor.vi_active == {"005930"}

        with patch.object(market_halt.api, "get_current_price_data",
                          side_effect=_price({"005930": "1"}, fail={"005930"})):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())

    assert monitor.vi_active == {"005930"}, "조회 실패로 VI 래치가 풀렸다"
    assert not any("VI 해제" in s for s in sent), f"거짓 해제 알림: {sent}"


def test_vi_실제_해제는_알린다(monitor, sent):
    """조회에 성공했는데 빠졌으면 그것은 진짜 해제다."""
    with patch.object(monitor, "_domestic_targets", lambda: TARGETS):
        with patch.object(market_halt.api, "get_current_price_data",
                          side_effect=_price({"005930": "1"})):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())
        with patch.object(market_halt.api, "get_current_price_data",
                          side_effect=_price({})):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())

    assert monitor.vi_active == set()
    assert any("VI 해제" in s and "삼성전자" in s for s in sent)


def test_vi_필드가_없으면_조회_성공으로_세지_않는다(monitor, sent):
    """vi_cls_code 가 없는 응답은 'VI 아님'이 아니라 '모름'이다."""
    with patch.object(monitor, "_domestic_targets", lambda: TARGETS):
        with patch.object(market_halt.api, "get_current_price_data",
                          side_effect=_price({"005930": "1"})):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())

        def _no_field(code, is_overseas=False):
            return {"rt_cd": "0", "output": {"stck_prpr": "70000"}}
        with patch.object(market_halt.api, "get_current_price_data", side_effect=_no_field):
            monitor._diff_vi_alerts(*monitor._check_vi_kis())

    assert monitor.vi_active == {"005930"}
    assert not any("VI 해제" in s for s in sent)


def test_감시대상이_통째로_비어도_전부_해제되지_않는다(monitor, sent):
    """잔고 조회가 실패하면 보유 종목이 대상에서 빠진다 — 그때 전부 '해제'가 나가면 안 된다."""
    with patch.object(monitor, "_domestic_targets", lambda: TARGETS), \
         patch.object(market_halt.api, "get_current_price_data",
                      side_effect=_price({"005930": "1", "000660": "2"})):
        monitor._diff_vi_alerts(*monitor._check_vi_kis())
    assert monitor.vi_active == {"005930", "000660"}

    sent.clear()
    with patch.object(monitor, "_domestic_targets", lambda: {}):
        monitor._diff_vi_alerts(*monitor._check_vi_kis())
    assert monitor.vi_active == {"005930", "000660"}
    assert sent == []


def test_vi_전달_실패면_상태를_바꾸지_않는다(monitor, monkeypatch):
    """다음 주기에 다시 보내야 한다 — 못 보낸 발동을 '보낸 것'으로 굳히지 않는다."""
    monkeypatch.setattr(market_halt, "alert_delivered", lambda msg, urgent=False: False)
    with patch.object(monitor, "_domestic_targets", lambda: TARGETS), \
         patch.object(market_halt.api, "get_current_price_data",
                      side_effect=_price({"005930": "1"})):
        monitor._diff_vi_alerts(*monitor._check_vi_kis())
    assert monitor.vi_active == set(), "전달에 실패했는데 발동 상태가 굳었다"


def test_diff_는_조회_성공_집합을_반드시_받는다():
    """기본값을 두면 '전부 봤다'로 굳어 같은 결함이 조용히 돌아온다."""
    import inspect
    sig = inspect.signature(market_halt.MarketHaltMonitor._diff_vi_alerts)
    assert sig.parameters["checked"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# 서킷브레이커
# --------------------------------------------------------------------------
def _cb(halted, fail=()):
    def _f(code, is_overseas=False):
        if code in fail:
            raise RuntimeError("timeout")
        return {"rt_cd": "0", "output": {"temp_stop_yn": "Y" if halted else "N"}}
    return _f


def test_cb_바스켓_응답이_한_종목뿐이면_판정하지_않는다(monitor, sent, monkeypatch):
    """판정에 최소 2종목이 필요하다 — 못 세면 직전 상태를 유지한다."""
    monkeypatch.setattr(market_halt.MarketHaltMonitor, "_index_rate", lambda self, mk: None)
    with patch.object(market_halt.api, "get_current_price_data", side_effect=_cb(True)):
        monitor._check_cb_kis()
    assert monitor.cb_active["KOSPI"] is True

    sent.clear()
    with patch.object(market_halt.api, "get_current_price_data",
                      side_effect=_cb(True, fail={"000660", "005380", "196170"})):
        monitor._check_cb_kis()
    assert monitor.cb_active["KOSPI"] is True, "조회 실패로 CB 해제 오보가 났다"
    assert not any("해제" in s for s in sent), f"거짓 해제 알림: {sent}"


def test_cb_전달_실패면_상태를_바꾸지_않는다(monitor, monkeypatch):
    monkeypatch.setattr(market_halt.MarketHaltMonitor, "_index_rate", lambda self, mk: None)
    monkeypatch.setattr(market_halt, "alert_delivered", lambda msg, urgent=False: False)
    with patch.object(market_halt.api, "get_current_price_data", side_effect=_cb(True)):
        monitor._check_cb_kis()
    assert monitor.cb_active["KOSPI"] is False, "전달에 실패했는데 발동 상태가 굳었다"
