"""market_halt 모듈 핵심 로직 테스트 (CB 판정/VI diff/헬퍼)."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import market_halt
from modules.market_halt import MarketHaltMonitor


def _fresh_monitor():
    MarketHaltMonitor._instance = None
    return MarketHaltMonitor()


def test_kis_vi_active_helper():
    assert market_halt._kis_vi_active(None) is False
    assert market_halt._kis_vi_active("") is False
    assert market_halt._kis_vi_active("0") is False
    assert market_halt._kis_vi_active("N") is False
    # 정상값 외에는 발동으로 간주 (보수적)
    assert market_halt._kis_vi_active("1") is True
    assert market_halt._kis_vi_active("2") is True
    assert market_halt._kis_vi_active("Y") is True


def test_toss_warning_is_vi_helper():
    assert market_halt._toss_warning_is_vi({"warningType": "VI"}) is True
    assert market_halt._toss_warning_is_vi({"warningType": "STATIC_VI"}) is True
    assert market_halt._toss_warning_is_vi({"warningType": "CAUTION"}) is False
    assert market_halt._toss_warning_is_vi({}) is False


def test_domestic_code_filter():
    assert market_halt._is_kr_domestic_code("005930") is True
    assert market_halt._is_kr_domestic_code("0193T0") is True   # ETN/ETF 영문 포함
    assert market_halt._is_kr_domestic_code("AAPL") is False    # 해외
    assert market_halt._is_kr_domestic_code("") is False


def test_cb_detection_and_alert(monkeypatch):
    m = _fresh_monitor()
    sent = []
    monkeypatch.setattr(market_halt.api, "send_telegram_message", lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(market_halt.api, "get_domestic_index_price", lambda code: {"rt_cd": "9999"})

    # 1) 코스피 바스켓 전부 정지 → CB 발동 알림 1회
    def all_halted(code, is_overseas=False):
        return {"rt_cd": "0", "output": {"temp_stop_yn": "Y"}}
    monkeypatch.setattr(market_halt.api, "get_current_price_data", all_halted)
    m._check_cb_kis()
    assert m.cb_active["KOSPI"] is True
    assert m.cb_active["KOSDAQ"] is True
    cb_msgs = [s for s in sent if "서킷브레이커 발동" in s]
    assert len(cb_msgs) == 2  # 코스피 + 코스닥

    # 2) 같은 상태 재점검 → 중복 알림 없음
    sent.clear()
    m._check_cb_kis()
    assert sent == []

    # 3) 정상 복귀 → 해제 알림
    def none_halted(code, is_overseas=False):
        return {"rt_cd": "0", "output": {"temp_stop_yn": "N"}}
    monkeypatch.setattr(market_halt.api, "get_current_price_data", none_halted)
    m._check_cb_kis()
    assert m.cb_active["KOSPI"] is False
    assert any("서킷브레이커 해제" in s for s in sent)


def test_cb_single_halt_no_false_positive(monkeypatch):
    """개별 종목 1개만 정지(예: 단일 공시 정지)면 시장 CB로 오판하지 않는다."""
    m = _fresh_monitor()
    sent = []
    monkeypatch.setattr(market_halt.api, "send_telegram_message", lambda msg, *a, **k: sent.append(msg))

    def only_first_halted(code, is_overseas=False):
        stop = "Y" if code == "005930" else "N"
        return {"rt_cd": "0", "output": {"temp_stop_yn": stop}}
    monkeypatch.setattr(market_halt.api, "get_current_price_data", only_first_halted)
    m._check_cb_kis()
    assert m.cb_active["KOSPI"] is False
    assert sent == []


def test_vi_diff_alerts():
    m = _fresh_monitor()
    sent = []
    market_halt.api_send_backup = market_halt.api.send_telegram_message
    market_halt.api.send_telegram_message = lambda msg, *a, **k: sent.append(msg)
    try:
        # 신규 발동 2건
        m._diff_vi_alerts({"005930": "삼성전자", "000660": "SK하이닉스"})
        assert sum("VI 발동" in s for s in sent) == 2
        assert m.vi_active == {"005930", "000660"}

        # 한 종목 해제 + 한 종목 신규
        sent.clear()
        m._diff_vi_alerts({"000660": "SK하이닉스", "247540": "에코프로비엠"})
        assert any("VI 발동" in s and "에코프로비엠" in s for s in sent)
        assert any("VI 해제" in s and "삼성전자" in s for s in sent)
        assert m.vi_active == {"000660", "247540"}
    finally:
        market_halt.api.send_telegram_message = market_halt.api_send_backup


# ==========================================================
# [감사 2026-09-04] VI 감시 대상은 '시스템 트레이딩 계좌'의 보유 종목이다
# ==========================================================

def test_vi_targets_use_the_system_trading_account(monkeypatch):
    """스케줄러 스레드에서도 자동 계좌 잔고를 물어야 한다.

    context.trade_context 는 threading.local 이라 상속되지 않는다. 종전에는 계좌
    컨텍스트 없이 api.get_domestic_balance() 를 불러 **수동 계좌** 잔고가 돌아왔고,
    실전(mode 2)에서 자동매매 보유 종목이 통째로 VI 감시에서 빠졌다.
    """
    from core import context

    s = config.session
    for k, v in (('cano', '11111111'), ('acnt_prdt_cd', '01'),
                 ('auto_cano', '22222222'), ('auto_acnt_prdt_cd', '01')):
        monkeypatch.setattr(s, k, v, raising=False)
    monkeypatch.setattr(context.trade_context, 'use_auto_account', False, raising=False)
    monkeypatch.setattr(config.session, 'stock_data', {}, raising=False)

    seen = {}

    def _balance(cano=None, acnt=None, *a, **k):
        seen['cano'] = cano
        seen['use_auto'] = getattr(context.trade_context, 'use_auto_account', False)
        return ([{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10"}], {})

    monkeypatch.setattr(market_halt.api, "get_domestic_balance", _balance)

    targets = _fresh_monitor()._domestic_targets()

    assert seen['cano'] == '22222222', "자동매매 계좌로 물어야 한다"
    assert seen['use_auto'] is True, "계좌 컨텍스트가 조회 시점에 걸려 있어야 한다"
    assert targets == {"005930": "삼성전자"}
    # 컨텍스트는 되돌아온다(스케줄러의 다른 작업으로 새면 안 된다)
    assert getattr(context.trade_context, 'use_auto_account', False) is False


def test_cb_alert_does_not_claim_trading_is_paused(monkeypatch):
    """알림은 하지 않는 일을 알리지 않는다 — CB 중에도 자동매매는 돈다."""
    m = _fresh_monitor()
    sent = []
    monkeypatch.setattr(market_halt.api, "send_telegram_message", lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(market_halt.MarketHaltMonitor, "_index_rate", lambda self, mk: None)

    m._alert_cb("KOSPI", True)

    assert "매매가 보류됩니다" not in sent[0]
    assert "자동매매는 계속 가동됩니다" in sent[0]
