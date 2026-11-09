"""토스 Open API 1.2.28 신규 엔드포인트(거래내역·투자자 순매수 랭킹·섹터)의 계약.

[왜 거래내역 해석을 따로 고정하는가 · 2026-11-08]
 거래내역은 정지 중 입출금 보정(trader._reconcile_offline_transfer)의 **정답지**로 쓴다. 여기서
 틀리면 대조가 틀린 쪽으로 통과하거나 막는다. 그리고 스펙이 조용히 아픈 자리를 여럿 갖고 있다:

   · 금액에 부호가 없다 — 방향은 거래 유형이, 취소 건(isCancellation)은 그 반대가 정한다
   · 건수가 limit 보다 적어도 다음 페이지가 있을 수 있다 — 끝은 hasNext 로만 안다
   · 입금(DEPOSIT)이라도 assetClass=BOND 면 채권 원리금이다
   · 새 거래 유형이 생길 수 있다(unknown enum 을 허용하라)

 모두 '숫자는 나오는데 틀린' 형태라 사람이 화면에서 알아채기 어렵다.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from brokers import toss_api  # noqa: E402


def _tx(kind, amount, currency="KRW", asset=None, cancel=False, market=None, net=None):
    return {"transactionType": kind, "currency": currency, "assetClass": asset,
            "marketCountry": market or ("KR" if currency == "KRW" else "US"),
            "amount": None if amount is None else str(amount),
            "netAmount": None if (net is None and amount is None) else str(net if net is not None else amount),
            "isCancellation": cancel, "settled": True}


def _pages(*pages):
    """get_transactions 대역 — 페이지를 차례로 돌려준다. 마지막 쪽만 hasNext=False."""
    calls = []
    seq = list(pages)

    def fake(**kw):
        calls.append(kw)
        i = len(calls) - 1
        rows = seq[i] if i < len(seq) else []
        last = i >= len(seq) - 1
        return {"transactions": rows, "hasNext": not last,
                "nextCursor": None if last else f"C{i + 1}"}
    return fake, calls


def _flows(*pages):
    import api
    fake, calls = _pages(*pages)
    with patch.object(toss_api, "get_transactions", side_effect=fake):
        return api._toss_period_cash_flows("2026-11-01", "2026-11-08"), calls


# ------------------------------------------------------------------ 분류·방향
def test_입출금과_환전은_입출금이다():
    out, _ = _flows([_tx("DEPOSIT", 1_000_000), _tx("WITHDRAWAL", 300_000),
                     _tx("EXCHANGE_OUT", 138_520), _tx("EXCHANGE_IN", 50_000)])
    assert out["transfer"] == 1_000_000 - 300_000 - 138_520 + 50_000
    assert out["income"] == 0 and out["unknown"] == 0


def test_취소_건은_돈이_반대로_움직인다():
    """스펙: 취소 내역은 원래 유형을 유지한다 — 입금 취소는 계좌에서 금액이 빠져나간다."""
    out, _ = _flows([_tx("DEPOSIT", 500_000, cancel=True), _tx("WITHDRAWAL", 200_000, cancel=True)])
    assert out["transfer"] == -500_000 + 200_000


def test_배당_예탁금이자_수수료는_수익분이다():
    out, _ = _flows([_tx("DIVIDEND", 3610, asset="STOCK", net=3054),
                     _tx("INTEREST", 1230, net=1050), _tx("FEE", 500)])
    assert out["income"] == 3054 + 1050 - 500, "세후 정산액(netAmount)으로 더해야 한다"
    assert out["transfer"] == 0


def test_채권_원리금과_세금은_입출금_쪽이다():
    """채권은 주식 운용 바깥의 자산이다 — 그 돈이 원화 계좌로 오가는 것은 원금 기준으로 입출금이다."""
    out, _ = _flows([_tx("DEPOSIT", 1_000_000, asset="BOND"), _tx("INTEREST", 21_250, asset="BOND"),
                     _tx("WITHDRAWAL", 180, asset="BOND"), _tx("BUY", 985_000, asset="BOND")])
    assert out["transfer"] == 1_000_000 + 21_250 - 180 - 985_000
    assert out["income"] == 0


def test_주식_매매는_원금_불변식이_설명하므로_뺀다():
    out, _ = _flows([_tx("BUY", 700_000, asset="STOCK"), _tx("SELL", 710_000, asset="STOCK")])
    assert out["transfer"] == 0 and out["income"] == 0 and out["rows"] == 2


def test_상품군_없는_매매는_주식인지_모른다():
    out, _ = _flows([_tx("SELL", 710_000, asset=None)])
    assert out["unknown"] == 1 and out["transfer"] == 0


def test_달러_거래는_원화_원금을_움직이지_않는다():
    """원화→달러 환전은 원화 쪽 EXCHANGE_OUT 으로 잡힌다 — 달러 쪽 EXCHANGE_IN 까지 더하면 이중이다."""
    out, _ = _flows([_tx("EXCHANGE_IN", 1000, currency="USD"), _tx("DIVIDEND", 12.5, currency="USD"),
                     _tx("EXCHANGE_OUT", 1_385_200)])
    assert out["transfer"] == -1_385_200 and out["income"] == 0


def test_국내_증권_입출고는_건수로만_센다():
    out, _ = _flows([_tx("SECURITIES_TRANSFER_IN", None, asset="STOCK"),
                     _tx("SECURITIES_TRANSFER_OUT", None, currency="USD", asset="OPTION")])
    assert out["securities_moves"] == 1, "미국 옵션 만기 출고는 원화 원금과 무관하다"
    assert out["unknown"] == 0


def test_모르는_거래_유형과_금액_누락은_unknown_이다():
    """0 으로 읽으면 '그 돈이 없었다'가 되어 대조가 틀린 쪽으로 통과한다."""
    out, _ = _flows([_tx("SOMETHING_NEW_2027", 1000), _tx("DEPOSIT", None),
                     _tx("DEPOSIT", "N/A")])
    assert out["unknown"] == 3 and out["transfer"] == 0


# ------------------------------------------------------------------ 페이지
def test_끝은_hasNext로만_안다():
    """반환 건수가 limit 보다 적어도 다음 페이지가 있을 수 있다(스펙 명시)."""
    out, calls = _flows([_tx("DEPOSIT", 100)], [], [_tx("DEPOSIT", 200)])
    assert out["transfer"] == 300 and len(calls) == 3
    assert calls[1]["cursor"] == "C1" and calls[2]["cursor"] == "C2"
    assert all(c["from_date"] == "2026-11-01" and c["to_date"] == "2026-11-08" for c in calls)


def test_조회_실패는_흐름_없음이_아니다():
    import api
    with patch.object(toss_api, "get_transactions", return_value=None):
        with pytest.raises(RuntimeError):
            api._toss_period_cash_flows("2026-11-01", "2026-11-08")


def test_페이지_상한에서_끊기면_반쪽_합계를_내지_않는다():
    import api
    endless = {"transactions": [_tx("DEPOSIT", 1)], "hasNext": True, "nextCursor": "X"}
    with patch.object(toss_api, "get_transactions", return_value=endless):
        with pytest.raises(RuntimeError):
            api._toss_period_cash_flows("2026-01-01", "2026-11-08")


# ------------------------------------------------------------------ 공개 진입점
def test_토스가_아니면_장부가_없다():
    import api
    assert config.session.is_toss is False
    assert api.get_period_cash_flows("2026-11-01", "2026-11-08") is None


def test_토스_조회_실패는_None(monkeypatch):
    import api
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    with patch.object(toss_api, "get_transactions", side_effect=toss_api.TossApiError("x", "y", 500)):
        assert api.get_period_cash_flows("2026-11-01", "2026-11-08") is None


def test_계좌_모듈은_날짜를_ISO로_맞춰_넘긴다():
    from modules import account
    with patch("api.get_period_cash_flows", return_value={"transfer": 0}) as m:
        assert account.fetch_period_cash_flows("20261101", "2026-11-08") == {"transfer": 0}
    assert m.call_args[0] == ("2026-11-01", "2026-11-08")
    with patch("api.get_period_cash_flows", return_value=None):
        assert account.fetch_period_cash_flows("2026-11-01") is None


# ------------------------------------------------------------------ 래퍼·한도 그룹
def test_새_엔드포인트는_스펙의_한도_그룹으로_부른다():
    """그룹을 틀리면 다른 그룹의 버킷을 갉아먹거나, 기본값(10)으로 돌아 429 쿨다운을 부른다."""
    with patch.object(toss_api, "_request", return_value={}) as m:
        toss_api.get_transactions(transaction_types=["DEPOSIT", "WITHDRAWAL"],
                                  from_date="2026-11-01", to_date="2026-11-08", limit=100)
        toss_api.get_investor_trading_rankings(count=5)
        toss_api.get_sectors()
        toss_api.get_sector_rankings("TOP_GAINERS", "KR", "1w")
        toss_api.get_sector_stocks("100", "KR")
    calls = m.call_args_list
    tx = calls[0]
    assert tx[0][1] == "/api/v1/transactions" and tx[1]["group"] == "TRANSACTION"
    assert tx[1]["params"] == {"transactionTypes": "DEPOSIT,WITHDRAWAL", "from": "2026-11-01",
                               "to": "2026-11-08", "limit": 100}
    assert tx[1].get("account", True) is True, "거래내역은 계좌 헤더가 필요하다"
    assert calls[1][0][1] == "/api/v1/rankings/investor-trading" and calls[1][1]["group"] == "RANKING"
    assert calls[2][1]["group"] == "SECTOR"
    assert calls[3][0][1] == "/api/v1/sectors/rankings" and calls[3][1]["group"] == "SECTOR_RANKING"
    assert calls[4][0][1] == "/api/v1/sectors/100/stocks" and calls[4][1]["group"] == "SECTOR"
    assert all(c[1].get("account") is False for c in calls[1:]), "시세성 조회에 계좌 헤더를 붙이지 않는다"


def test_쓰는_그룹은_모두_한도_표에_있다():
    """표에 없으면 config.TOSS_TX_PER_SECOND(10)로 돈다 — 실측한 적 없는 값이다."""
    for g in ("STOCK_TRADING_TREND", "TRANSACTION", "SECTOR", "SECTOR_RANKING"):
        assert g in toss_api._GROUP_RPS, g
    assert toss_api._GROUP_RPS["STOCK_TRADING_TREND"] <= toss_api._GROUP_RPS["STOCK"]


# ------------------------------------------------------------------ 증권 입출고(보유분 복원 진단)
def test_증권_입출고_순수량을_종목별로_센다():
    import api
    rows = [
        {"transactionType": "SECURITIES_TRANSFER_IN", "marketCountry": "KR", "symbol": "005930",
         "quantity": "6", "isCancellation": False},
        {"transactionType": "SECURITIES_TRANSFER_OUT", "marketCountry": "KR", "symbol": "005930",
         "quantity": "1", "isCancellation": False},
        {"transactionType": "SECURITIES_TRANSFER_IN", "marketCountry": "KR", "symbol": "005930",
         "quantity": "2", "isCancellation": True},         # 입고 취소 = 출고
        {"transactionType": "SECURITIES_TRANSFER_IN", "marketCountry": "KR", "symbol": "000660",
         "quantity": "9", "isCancellation": False},          # 묻지 않은 종목
        {"transactionType": "SECURITIES_TRANSFER_OUT", "marketCountry": "US", "symbol": "AAPL",
         "quantity": "1", "isCancellation": False},
    ]
    fake, calls = _pages(rows)
    with patch.object(toss_api, "get_transactions", side_effect=fake):
        out = api._toss_period_securities_moves(["005930"], months=12)
    assert out == {"005930": 6 - 1 - 2}
    assert calls[0]["transaction_types"] == ["SECURITIES_TRANSFER_IN", "SECURITIES_TRANSFER_OUT"]


def test_증권_입출고_수량을_못_읽으면_모른다():
    import api
    rows = [{"transactionType": "SECURITIES_TRANSFER_IN", "marketCountry": "KR", "symbol": "005930",
             "quantity": None}]
    fake, _ = _pages(rows)
    with patch.object(toss_api, "get_transactions", side_effect=fake):
        with pytest.raises(RuntimeError):
            api._toss_period_securities_moves(["005930"])
    assert api.get_period_securities_moves(["005930"]) is None, "토스가 아니면 장부가 없다"


# ------------------------------------------------------------------ 한도 확인 도구(--limits)
def test_한도_도구는_그룹마다_한_번만_부르고_초과를_찾는다(monkeypatch):
    """파이에서 그룹당 1회 호출로 서버 한도 헤더를 읽는다 — 램프 없이 설정과 대조한다."""
    from tools import toss_tps_probe as probe

    class _Res:
        def __init__(self, limit):
            self.status_code = 200
            self.headers = {"X-RateLimit-Limit": str(limit)}

        def json(self):
            return {"result": {}}

    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append((url, headers.get("X-Tossinvest-Account")))
        return _Res(2 if url.endswith("/api/v1/sectors") else 10)   # SECTOR 서버 한도 2 < 설정 3

    monkeypatch.setattr(probe.requests, "get", fake_get)
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)
    monkeypatch.setattr(toss_api, "resolve_account_seq", lambda *a, **k: 7)
    worst = probe.check_limits("TOKEN", "005930", toss_api)
    assert worst == 1, "SECTOR 설정(3)이 서버 한도(2)를 넘는데 찾지 못했다"
    assert len(seen) == len(probe.ENDPOINTS) + len(probe.LIMIT_ONLY), "그룹마다 정확히 1회여야 한다"
    tx = [s for s in seen if s[0].endswith("/api/v1/transactions")]
    assert tx and tx[0][1] == "7", "거래내역은 계좌 헤더를 붙여야 한다"
    assert all(acc is None for url, acc in seen if not url.endswith("/api/v1/transactions"))
    assert any(url.endswith("/api/v1/stocks/005930/investor-trading") for url, _ in seen)


def test_한도_도구는_인증_오류에서_멈춘다(monkeypatch):
    from tools import toss_tps_probe as probe

    class _Res:
        status_code = 401
        headers = {}

    calls = []
    monkeypatch.setattr(probe.requests, "get", lambda *a, **k: calls.append(1) or _Res())
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)
    assert probe.check_limits("TOKEN", "005930", toss_api) == 2
    assert len(calls) == 1


def test_한도_도구는_새_응답이_코드가_기대는_모양인지_알려_준다():
    """실응답을 못 본 채 스펙으로 구현했다 — 파이에서 한 번 돌리면 키 누락이 바로 보여야 한다."""
    from tools import toss_tps_probe as probe
    ok = {"result": {"transactions": [{k: None for k in probe.SHAPE["transaction"][1]}],
                     "hasNext": False}}
    assert probe.shape_note("transaction", ok) == "transaction: 키 정상"
    bad = {"result": {"transactions": [{"transactionType": "DEPOSIT"}]}}
    assert "누락" in probe.shape_note("transaction", bad) and "netAmount" in probe.shape_note("transaction", bad)
    assert "항목 없음" in probe.shape_note("sector", {"result": {"sectors": []}})
    assert "객체가 아닙니다" in probe.shape_note("investor_rank", {"result": []})
    assert probe.shape_note("price", {"result": []}) is None
