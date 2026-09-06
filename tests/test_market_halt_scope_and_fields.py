"""VI·CB 감시: **모르는 것은 '정상'이 아니고, 상한은 돈이 걸린 쪽부터 지킨다**.

[왜 이 파일이 있나 · 2026-09-07]
2026-09-05 에 '조회 실패를 해제로 읽지 않는다'를 세 축에서 막았다
([[unknown-vs-empty]], tests/test_halt_unknown_vs_released.py). 그 규칙이 닿지
않은 자리가 셋 더 남아 있었다 — 셋 다 같은 모양이다: **응답이 오긴 왔는데 그 안에
답이 없다.** 실패가 예외로 올라오는 길만 막아 두면 이쪽은 그대로 열린다.

 ① 감시 대상 상한 — 관심종목을 먼저 채우고 뒤에서 잘랐다. 관심종목만으로 상한을
    넘으면 보유분이 한 종목도 남지 않는다. 실제 설정(국내 관심 64개 > 상한 40)에서
    이미 그랬고, 조용했다. 2026-09-04 에 계좌 컨텍스트 쪽에서 '보유가 통째로 빠지는'
    같은 결함을 고쳐 놓고 열 줄 아래에서 다시 벌어지고 있었다.
 ② CB temp_stop_yn — `out.get("temp_stop_yn", "N")` 이 **필드 누락을 '정상'으로**
    접고 조회 성공으로 셌다. 필드가 빠지거나 이름이 바뀌면 바스켓 전체가 '정지 아님'이
    되어 정지 중인 시장에 해제 오보가 나간다. 같은 가드가 VI 경로(vi_cls_code)에는
    이미 있었고 CB 경로에만 없었다.
 ③ 토스 get_warnings — 브로커 계층의 `or []` 가 조회 실패(2xx·비JSON, result 키
    부재)를 '주의사항 없음'으로 접었다. 토스는 비공식 API라 스키마가 실제로 바뀐다
    ([[toss-balance-sell-gate]]). 그 빈 목록이 checked 로 들어가 VI 해제 오보가 된다.
"""
import sys
import types
from unittest.mock import patch

import pytest

import config
from modules import market_halt


@pytest.fixture
def monitor():
    m = object.__new__(market_halt.MarketHaltMonitor)
    m._init()
    return m


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(market_halt, "alert_delivered",
                        lambda msg, urgent=False: (box.append(msg), True)[1])
    return box


class _NoopAccountContext:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _watchlist(n_stocks, n_etfs=0):
    return {
        "stocks_kr": [{"code": f"1000{i:02d}", "name": f"관심{i}"} for i in range(n_stocks)],
        "etfs_kr": [{"code": f"2000{i:02d}", "name": f"ETF{i}"} for i in range(n_etfs)],
    }


def _with_holdings(monkeypatch, holdings):
    monkeypatch.setattr(market_halt.utils, "system_trading_account",
                        lambda: ("12345678", "01"))
    monkeypatch.setattr(market_halt.utils, "AccountContext", _NoopAccountContext)
    monkeypatch.setattr(market_halt.api, "get_domestic_balance",
                        lambda cano, acnt: (holdings, {}))


# --------------------------------------------------------------------------
# ① 상한은 보유부터 지킨다
# --------------------------------------------------------------------------
HOLDINGS = [
    {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10"},
    {"pdno": "000660", "prdt_name": "SK하이닉스", "hldg_qty": "5"},
]


def test_관심종목이_상한을_넘어도_보유는_감시에서_빠지지_않는다(monitor, monkeypatch):
    """실제 설정 모양(관심 64 > 상한 40). 종전에는 보유가 0건 남았다."""
    monkeypatch.setattr(config.session, "stock_data", _watchlist(44, 20), raising=False)
    monkeypatch.setattr(config, "MARKET_HALT_VI_MAX_CODES", 40, raising=False)
    _with_holdings(monkeypatch, HOLDINGS)

    targets = monitor._domestic_targets()

    assert len(targets) == 40
    assert "005930" in targets and targets["005930"] == "삼성전자"
    assert "000660" in targets


def test_상한에_잘리면_조용히_지나가지_않는다(monitor, monkeypatch, caplog):
    monkeypatch.setattr(config.session, "stock_data", _watchlist(44, 20), raising=False)
    monkeypatch.setattr(config, "MARKET_HALT_VI_MAX_CODES", 40, raising=False)
    _with_holdings(monkeypatch, HOLDINGS)

    with caplog.at_level("WARNING", logger="modules.market_halt"):
        monitor._domestic_targets()

    assert any("상한" in r.message for r in caplog.records)


def test_상한_안이면_보유와_관심이_모두_남는다(monitor, monkeypatch):
    """상한을 지키는 것이 목적이지 관심종목을 버리는 것이 목적이 아니다."""
    monkeypatch.setattr(config.session, "stock_data", _watchlist(5), raising=False)
    monkeypatch.setattr(config, "MARKET_HALT_VI_MAX_CODES", 40, raising=False)
    _with_holdings(monkeypatch, HOLDINGS)

    targets = monitor._domestic_targets()

    assert len(targets) == 7
    assert "005930" in targets and "000660" in targets
    assert "100000" in targets


# --------------------------------------------------------------------------
# ② CB: temp_stop_yn 필드가 없으면 '모른다'
# --------------------------------------------------------------------------
def _cb_price(field_map):
    """field_map: code -> output dict (그대로 내려준다)."""
    def _f(code, is_overseas=False):
        return {"rt_cd": "0", "output": field_map.get(code, {})}
    return _f


@pytest.fixture
def kospi_basket(monkeypatch):
    monkeypatch.setattr(market_halt, "_CB_BASKET",
                        {"KOSPI": [("005930", "a"), ("000660", "b"), ("005380", "c")]})


def test_CB_필드가_없으면_해제로_읽지_않는다(monitor, sent, monkeypatch, kospi_basket):
    """응답은 rt_cd=0 인데 temp_stop_yn 만 빠졌다 — 정지 여부를 모르는 것이다."""
    monitor.cb_active["KOSPI"] = True
    monkeypatch.setattr(monitor, "_index_rate", lambda mk: None)
    monkeypatch.setattr(market_halt.api, "get_current_price_data",
                        _cb_price({c: {"stck_prpr": "70000"}
                                   for c in ("005930", "000660", "005380")}))

    monitor._check_cb_kis()

    assert sent == []
    assert monitor.cb_active["KOSPI"] is True


def test_CB_필드가_있으면_정상적으로_해제를_알린다(monitor, sent, monkeypatch, kospi_basket):
    """모름과 '아니다'를 가르는 것이지 해제 알림을 막는 것이 아니다."""
    monitor.cb_active["KOSPI"] = True
    monkeypatch.setattr(monitor, "_index_rate", lambda mk: None)
    monkeypatch.setattr(market_halt.api, "get_current_price_data",
                        _cb_price({c: {"temp_stop_yn": "N"}
                                   for c in ("005930", "000660", "005380")}))

    monitor._check_cb_kis()

    assert len(sent) == 1 and "해제" in sent[0]
    assert monitor.cb_active["KOSPI"] is False


def test_CB_필드_있는_종목이_둘_미만이면_판정하지_않는다(monitor, sent, monkeypatch, kospi_basket):
    """필드 누락도 checked 를 못 채운다 — 기존 'checked < 2' 규칙과 이어져야 한다."""
    monitor.cb_active["KOSPI"] = True
    monkeypatch.setattr(monitor, "_index_rate", lambda mk: None)
    monkeypatch.setattr(market_halt.api, "get_current_price_data",
                        _cb_price({"005930": {"temp_stop_yn": "N"},
                                   "000660": {"stck_prpr": "1"},
                                   "005380": {}}))

    monitor._check_cb_kis()

    assert sent == []
    assert monitor.cb_active["KOSPI"] is True


# --------------------------------------------------------------------------
# ③ 토스 VI: 빈 목록과 '모름'은 다르다
# --------------------------------------------------------------------------
@pytest.fixture
def toss_stub(monkeypatch):
    fake = types.ModuleType("brokers.toss_api")
    monkeypatch.setitem(sys.modules, "brokers.toss_api", fake)
    import brokers
    monkeypatch.setattr(brokers, "toss_api", fake, raising=False)
    return fake


def test_토스_경고조회_실패는_checked에_들어가지_않는다(monitor, sent, monkeypatch, toss_stub):
    monitor.vi_active = {"005930"}
    monitor.vi_names = {"005930": "삼성전자"}
    monkeypatch.setattr(monitor, "_domestic_targets", lambda: {"005930": "삼성전자"})
    toss_stub.get_warnings = lambda code: None

    current, checked = monitor._check_vi_toss()
    monitor._diff_vi_alerts(current, checked)

    assert checked == set()
    assert sent == []
    assert monitor.vi_active == {"005930"}


def test_토스_빈_목록은_주의사항_없음으로_읽는다(monitor, sent, monkeypatch, toss_stub):
    """[] 는 답이다 — 실제로 VI 가 풀린 것이므로 해제를 알려야 한다."""
    monitor.vi_active = {"005930"}
    monitor.vi_names = {"005930": "삼성전자"}
    monkeypatch.setattr(monitor, "_domestic_targets", lambda: {"005930": "삼성전자"})
    toss_stub.get_warnings = lambda code: []

    current, checked = monitor._check_vi_toss()
    monitor._diff_vi_alerts(current, checked)

    assert checked == {"005930"}
    assert len(sent) == 1 and "VI 해제" in sent[0]
    assert monitor.vi_active == set()


def test_브로커_get_warnings는_실패를_빈목록으로_접지_않는다(monkeypatch):
    """계약 자체를 못 박는다 — 호출부가 하나뿐이라 여기서 접히면 아무도 못 본다."""
    from brokers import toss_api

    monkeypatch.setattr(toss_api, "_request", lambda *a, **k: None)
    assert toss_api.get_warnings("005930") is None

    monkeypatch.setattr(toss_api, "_request", lambda *a, **k: [])
    assert toss_api.get_warnings("005930") == []
