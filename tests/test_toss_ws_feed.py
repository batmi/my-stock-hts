"""토스 주문 이벤트 웹소켓 피드(brokers.realtime.TossWsFeed)의 프레임 처리 계약.

[왜 프레임 처리만 따로 재는가 · 2026-09-09]
 연결·재연결은 네트워크가 있어야 하지만, **틀리면 조용히 아픈 곳은 전부 프레임 처리 쪽**이다.
 토스 웹소켓은 한 연결로 ack·데이터·에러·pong 이 섞여 오고, 실패가 예외로 드러나지 않는다:

   · 구독이 거부돼도(`rejected`) 연결은 살아 있고 데이터만 영영 안 온다 → 무증상 REST 폴백
   · 모르는 `event` 값을 버리면 그 상태 변화만 다음 폴링 주기까지 방치된다
   · 재연결 후 재동기화를 안 하면 끊긴 구간의 체결이 통째로 늦는다(무손실은 세션 안에서만)

 셋 다 "동작은 하는데 느려진다"로 나타나 사람이 알아채기 어렵다. 그래서 사실을 남기는지,
 깨우는지를 여기서 고정한다.

[관련] AsyncAPI 3.0.0 · info.version 1.2.2 · wss://openapi-ws.tossinvest.com/ws/v1
"""
import json
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers import realtime as rt


def _feed_with_sink():
    feed = rt.TossWsFeed()
    got = []
    feed.register_exec_callback(got.append)
    return feed, got


def _order_frame(event="FILL", side="BUY", symbol="005930", order_id="ORD1", seq="3"):
    return json.dumps({
        "type": "message",
        "topic": f"personal:order:{seq}",
        "data": {
            "event": event,
            "accountSeq": seq,
            "order": {"orderId": order_id, "symbol": symbol, "side": side,
                      "orderType": "LIMIT", "status": "FILLED", "quantity": "10",
                      "currency": "KRW", "orderedAt": "2026-09-09T09:30:00+09:00",
                      "execution": {}},
        },
    })


# ---------------------------------------------------------------- 주문 이벤트
def test_체결_이벤트가_콜백을_깨운다():
    feed, got = _feed_with_sink()
    feed._on_message(_order_frame(event="FILL", side="BUY"))
    assert len(got) == 1
    n = got[0]
    assert n['code'] == "005930" and n['odno'] == "ORD1"
    assert n['is_fill'] is True and n['rejected'] is False
    assert n['buy_sell'] == "02"      # KIS 표기: '02'=매수


def test_매도는_KIS와_같은_표기로_넘어간다():
    """콜백(_on_ws_exec_notice)이 KIS 통보와 같은 키를 읽는다 — 표기가 갈리면 안 된다."""
    feed, got = _feed_with_sink()
    feed._on_message(_order_frame(side="SELL"))
    assert got[0]['buy_sell'] == "01"


def test_거부는_거부로_표시된다():
    """거부 통보는 콜백이 무시하고 폴링의 미체결 정리 경로가 맡는다 — 그 갈림길을 고정한다."""
    feed, got = _feed_with_sink()
    for ev in ("REJECTED", "CANCEL_REJECTED", "REPLACE_REJECTED"):
        got.clear()
        feed._on_message(_order_frame(event=ev))
        assert got[0]['rejected'] is True, ev
        assert got[0]['is_fill'] is False, ev


def test_모르는_이벤트도_버리지_않고_깨운다():
    """스펙이 'unknown code 를 허용하라'고 적는다.

    모르는 상태 변화일수록 REST 로 확인해야 한다 — 버리면 다음 주기까지 방치된다.
    """
    feed, got = _feed_with_sink()
    feed._on_message(_order_frame(event="SOMETHING_NEW_2027"))
    assert len(got) == 1
    assert got[0]['is_fill'] is False and got[0]['rejected'] is False


def test_시세_토픽은_1단계에서_무시한다():
    """구독하지 않으므로 올 리 없지만, 와도 주문 콜백을 깨우지 않는다."""
    feed, got = _feed_with_sink()
    feed._on_message(json.dumps({"type": "message", "topic": "trade:kr:005930",
                                 "data": {"price": "72000", "volume": "10"}}))
    assert got == []


# ---------------------------------------------------------------- ack / 에러
def test_구독_거부는_반드시_로그에_남는다(caplog):
    """삼키면 그 계좌의 주문 이벤트가 영영 안 오는데 흔적이 없다(무증상 폴백)."""
    feed, _ = _feed_with_sink()
    with caplog.at_level(logging.WARNING, logger="hts"):
        feed._on_message(json.dumps({
            "type": "subscriptions", "subscribed": [],
            "rejected": [{"target": "personal:order:9", "code": "account-not-found",
                          "message": "계좌를 찾을 수 없습니다."}],
        }))
    text = caplog.text
    assert "account-not-found" in text and "personal:order:9" in text
    assert feed._subscribed is False


def test_확정된_구독이_없으면_그_사실을_말한다(caplog):
    feed, _ = _feed_with_sink()
    with caplog.at_level(logging.WARNING, logger="hts"):
        feed._on_message(json.dumps({"type": "subscriptions", "subscribed": [], "rejected": []}))
    assert "REST 폴링" in caplog.text


def test_구독_확정이_커버리지에_반영된다():
    feed, _ = _feed_with_sink()
    assert feed.coverage()['order_subscribed'] is False
    feed._on_message(json.dumps({"type": "subscriptions",
                                 "subscribed": ["personal:order:3"], "rejected": []}))
    assert feed.coverage()['order_subscribed'] is True


def test_서버배포_알림은_경고가_아니라_안내다(caplog):
    """server-shutdown 은 예정된 종료다 — 백오프 루프가 재연결한다.

    이것을 에러로 적으면 배포 때마다 관제에 가짜 경보가 뜬다.
    """
    feed, _ = _feed_with_sink()
    with caplog.at_level(logging.INFO, logger="hts"):
        feed._on_message(json.dumps({"type": "error", "error": {
            "code": "server-shutdown", "message": "서버가 재시작됩니다."}}))
    rec = [r for r in caplog.records if "server-shutdown" in r.message]
    assert rec and rec[0].levelno == logging.INFO


def test_선언_실패는_경고로_남기되_기존_구독을_지운다고_말하지_않는다(caplog):
    """스펙: 선언 전체 실패 시에도 **기존 구독은 유지**된다."""
    feed, _ = _feed_with_sink()
    feed._subscribed = True
    with caplog.at_level(logging.WARNING, logger="hts"):
        feed._on_message(json.dumps({"type": "error", "error": {
            "code": "rate-limit-exceeded", "message": "declare rate limit exceeded"}}))
    assert "rate-limit-exceeded" in caplog.text
    assert feed._subscribed is True


def test_pong과_빈_프레임은_조용히_흘린다():
    feed, got = _feed_with_sink()
    for frame in ('{"type":"pong"}', "", "   ", "PONG", None):
        feed._on_message(frame)
    assert got == []


# ---------------------------------------------------------------- 프로토콜 규약
def test_구독_선언은_full_replace_배열이다():
    """subscribe/unsubscribe 액션이 없다 — 배열 하나가 곧 구독 전체다.

    KIS 처럼 증분 등록으로 착각해 '추가할 것만' 보내면, 보내지 않은 구독이 해제된다.
    """
    import asyncio

    sent = []

    class _Ws:
        async def send(self, msg):
            sent.append(msg)

    asyncio.run(rt.TossWsFeed()._declare(_Ws(), "3"))
    payload = json.loads(sent[0])
    assert isinstance(payload, list)
    entries = [e for e in payload if e.get("type")]
    assert entries == [{"type": "personal:order", "codes": ["3"]}]
    assert any("id" in e and "type" not in e for e in payload), "응답을 짝지을 요청 id가 없다"


def test_핑은_JSON이_아니라_대문자_텍스트다():
    """스펙: 따옴표 없는 순수 텍스트 `PING`. JSON 으로 감싸면 서버가 못 알아듣고,
    180초 뒤 idle 로 끊긴다 — 그런데 그 사이 데이터는 잘 오므로 원인을 찾기 어렵다.
    """
    import asyncio
    import config

    sent = []

    class _Ws:
        async def send(self, msg):
            sent.append(msg)
            raise RuntimeError("한 번만 보내고 끝낸다")

    feed = rt.TossWsFeed()
    orig = getattr(config, 'TOSS_WS_PING_INTERVAL_SEC', 60)
    config.TOSS_WS_PING_INTERVAL_SEC = 0
    try:
        asyncio.run(feed._ping_loop(_Ws()))
    finally:
        config.TOSS_WS_PING_INTERVAL_SEC = orig
    assert sent == ["PING"]


def test_핑_주기는_서버_한도_180초보다_짧다():
    """서버가 보내는 데이터는 idle 타이머를 리셋하지 않는다(스펙 명시).

    '데이터를 받고 있으니 살아 있다'는 가정으로 주기를 늘리면 조용히 끊긴다.
    """
    import config
    assert getattr(config, 'TOSS_WS_PING_INTERVAL_SEC', 60) < 180


def test_콜백_예외가_피드를_죽이지_않는다():
    feed = rt.TossWsFeed()
    ok = []
    feed.register_exec_callback(lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    feed.register_exec_callback(ok.append)
    feed._on_message(_order_frame())
    assert len(ok) == 1


def test_콜백은_중복_등록되지_않는다():
    feed, got = _feed_with_sink()
    feed.register_exec_callback(got.append)   # 같은 함수 두 번
    feed._on_message(_order_frame())
    assert len(got) == 1


def test_재연결하면_재동기화를_깨운다():
    """무손실 보장은 연결 세션 안에서만이다 — 끊긴 구간의 이벤트는 다시 오지 않는다.

    rejected=True 로 만들면 콜백(_on_ws_exec_notice)이 통째로 무시하므로, 재동기화가
    조용히 사라진다. 그 실수를 여기서 막는다.
    """
    n = rt.TossWsFeed._resync_notice()
    assert n['resync'] is True and n['event'] == 'RECONNECT'
    assert n['rejected'] is False


def test_재동기화_통보를_체결감시가_무시하지_않는다():
    """소비자 쪽 갈림길(거부면 return)에 걸리지 않는지 실제 조건으로 확인한다."""
    from modules.auto_trade import conclusion  # noqa: F401  (임포트 가능성 확인)
    n = rt.TossWsFeed._resync_notice()
    assert not n.get('rejected'), "이 조건이 True 면 conclusion 이 곧바로 return 한다"


# ------------------------------------------------- 시세 채널 (2·3단계)
def _quote_feed(**cfg):
    """시세 구독을 켠 피드 + 구독 계획을 만들어 둔다."""
    feed = rt.TossWsFeed()
    feed.set_symbols(["005930", "000660"], ["035420"])
    return feed


def test_체결_프레임은_가격만_담고_거래량은_담지_않는다():
    """스펙이 **수신 합산으로 누적 거래량을 재구성하지 말라고 명시**한다.

    프레임에 누적 거래량·매수/매도 구분이 없고 LOSSY 라 중간이 빠진다. KIS 캐시에는
    volume 자리가 있어 옮겨 적기 쉬운데, 그러면 '못 잰 값'이 '0 거래'로 둔갑한다.
    """
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "message", "topic": "trade:kr:005930",
        "data": {"price": "72000", "volume": "120", "currency": "KRW",
                 "timestamp": "2026-09-09T09:30:42.000+09:00"}}))
    assert feed.get_price("005930") == 72000.0
    entry = feed._price["005930"]
    assert set(entry) == {"price", "ts"}, f"거래량이 새어 들어왔다: {entry}"


def test_호가_총잔량은_REST와_같이_상위10호가만_더한다():
    """api/toss.py 의 _toss_order_book 이 `for i in range(10)` 으로 10단만 더한다.

    여기서 전 호가를 더하면 같은 순간에도 WS 경로와 REST 경로가 다른 비율을 낸다 —
    매수 게이트 판정이 '어느 경로가 먼저 답했는가'에 따라 갈린다.
    """
    feed = _quote_feed()
    asks = [{"price": str(72000 + i), "volume": "10"} for i in range(15)]
    bids = [{"price": str(71900 - i), "volume": "20"} for i in range(15)]
    feed._on_message(json.dumps({
        "type": "message", "topic": "orderbook:kr:005930",
        "data": {"currency": "KRW", "asks": asks, "bids": bids}}))
    ob = feed.get_orderbook("005930")
    assert ob == {"total_ask": 100.0, "total_bid": 200.0}, "10단을 넘겨 더했다"


def test_호가가_얕아도_있는_만큼만_더한다():
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "message", "topic": "orderbook:kr:005930",
        "data": {"currency": "KRW",
                 "asks": [{"price": "72000", "volume": "7"}], "bids": []}}))
    assert feed.get_orderbook("005930") == {"total_ask": 7.0, "total_bid": 0.0}


def test_못_읽은_수치는_0이_아니라_건너뛴다():
    """가격이 깨진 프레임을 0 으로 적으면 캐시가 '0원'을 신선한 현재가로 답한다."""
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "message", "topic": "trade:kr:005930", "data": {"price": "N/A"}}))
    assert feed.get_price("005930") is None


def test_신선도가_지나면_REST로_넘긴다():
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "message", "topic": "trade:kr:005930", "data": {"price": "72000"}}))
    assert feed.get_price("005930", max_age=3.0) == 72000.0
    feed._price["005930"]["ts"] -= 10
    assert feed.get_price("005930", max_age=3.0) is None


# ------------------------------------------------- 구독 선언 (full-replace)
def _declared(feed, seq="3", force=True):
    import asyncio
    sent = []

    class _Ws:
        async def send(self, msg):
            sent.append(msg)

    asyncio.run(feed._declare(_Ws(), seq, force=force))
    return json.loads(sent[0]) if sent else None


def test_시세를_켜면_주문과_시세를_한_배열에_함께_선언한다():
    feed = _quote_feed()
    payload = _declared(feed)
    types = {e["type"] for e in payload if e.get("type")}
    assert types == {"personal:order", "trade:kr", "orderbook:kr"}


def test_시세를_꺼도_주문_구독은_남는다(monkeypatch):
    """full-replace 라 '주문만' 담은 배열이 곧 전체 구독이 된다 — 시세만 해제된다."""
    import config
    monkeypatch.setattr(config, "TOSS_WS_SUBSCRIBE_QUOTES", False, raising=False)
    payload = _declared(_quote_feed())
    entries = [e for e in payload if e.get("type")]
    assert entries == [{"type": "personal:order", "codes": ["3"]}]


def test_같은_내용이면_다시_선언하지_않는다():
    """선언 빈도 한도가 5회/초다 — 넘기면 rate-limit-exceeded 로 선언 자체가 실패한다."""
    feed = _quote_feed()
    assert _declared(feed, force=True) is not None
    assert _declared(feed, force=False) is None, "내용이 같은데 또 보냈다"


def test_재연결하면_내용이_같아도_다시_선언한다():
    """새 연결에는 아무 구독도 없다. 장부를 안 비우면 '이미 했다'며 한 건도 안 보낸다."""
    feed = _quote_feed()
    _declared(feed, force=True)
    feed._declared = ()          # _run 이 연결 직후 하는 일
    assert _declared(feed, force=False) is not None


def test_구독_수가_한도를_넘지_않는다():
    """한도 100건(채널×종목). 주문 채널 한 자리를 미리 뺀 뒤 계획을 세운다."""
    import config
    feed = rt.TossWsFeed()
    feed.set_symbols([f"{i:06d}" for i in range(200)])
    payload = _declared(feed)
    total = sum(len(e["codes"]) for e in payload if e.get("type"))
    assert total <= getattr(config, "TOSS_WS_MAX_SUBSCRIPTIONS", 100)


def test_관심종목이_한도_안이면_호가도_붙는다():
    """KIS(41건)에서는 호가가 0건이었다 — 한도가 넉넉해진 것이 이 축의 실이득이다."""
    feed = _quote_feed()
    payload = _declared(feed)
    books = [e for e in payload if e.get("type") == "orderbook:kr"]
    assert books and len(books[0]["codes"]) > 0


# ------------------------------------------------- 끊김
def test_끊기면_시세_캐시를_버린다():
    """TTL 3초 안이면 읽기 경로가 '신선하다'고 믿는다 — 연결이 없으면 REST 로 가야 한다."""
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "message", "topic": "trade:kr:005930", "data": {"price": "72000"}}))
    assert feed.get_price("005930") == 72000.0
    with feed._cache_lock:              # _run 의 finally 가 하는 일
        feed._price.clear()
        feed._ask.clear()
    assert feed.get_price("005930") is None


def test_주문_구독_거부는_시세_확정에_묻히지_않는다():
    """시세가 다 붙었다는 이유로 '구독 정상'이 되면, 체결 인지가 죽은 사실이 묻힌다."""
    feed = _quote_feed()
    feed._on_message(json.dumps({
        "type": "subscriptions",
        "subscribed": ["trade:kr:005930", "orderbook:kr:005930"],
        "rejected": [{"target": "personal:order:3", "code": "account-not-found",
                      "message": "계좌 없음"}]}))
    assert feed.coverage()["order_subscribed"] is False
