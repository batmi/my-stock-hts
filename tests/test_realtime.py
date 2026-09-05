import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers import realtime as rt


def _mk_fields(n, overrides):
    f = ["0"] * n
    for i, v in overrides.items():
        f[i] = v
    return f


def test_parse_h0stcnt0_extracts_price_and_vol_strength():
    fields = _mk_fields(46, {0: "005930", 2: "70000", 5: "1.50", 13: "1000000", 18: "120.5"})
    body = "^".join(fields)
    rows = rt.parse_h0stcnt0(body, count="1")
    assert len(rows) == 1
    r = rows[0]
    assert r['code'] == "005930"
    assert r['price'] == 70000.0
    assert r['change_rate'] == 1.5
    assert r['volume'] == 1000000.0
    assert r['vol_strength'] == 120.5


def test_parse_h0stcnt0_multiple_records():
    rec1 = _mk_fields(46, {0: "005930", 2: "70000", 18: "110"})
    rec2 = _mk_fields(46, {0: "000660", 2: "120000", 18: "95"})
    body = "^".join(rec1 + rec2)
    rows = rt.parse_h0stcnt0(body, count="2")
    assert [r['code'] for r in rows] == ["005930", "000660"]
    assert rows[1]['price'] == 120000.0


def test_parse_h0stasp0_total_ask_bid():
    fields = _mk_fields(60, {0: "005930", 43: "5000", 44: "8000"})
    rows = rt.parse_h0stasp0("^".join(fields), count="1")
    assert len(rows) == 1
    assert rows[0]['code'] == "005930"
    assert rows[0]['total_ask'] == 5000.0
    assert rows[0]['total_bid'] == 8000.0


def test_parser_ignores_short_records():
    assert rt.parse_h0stcnt0("a^b^c", count="1") == []
    assert rt.parse_h0stasp0("a^b^c", count="1") == []


def test_subscription_priority_always_on_plus_rotation():
    # 현재가 우선: 예산 4 전부 현재가에 소모 → 호가 여유 없음. 시스템 고정 + 그 외 로테이션.
    m = rt.SubscriptionManager(max_regs=4, subscribe_orderbook=True)
    m.set_symbols(priority=["A", "B"], other=["X", "Y", "Z"])

    def syms(regs):
        return [c for (t, c) in regs if t == rt.TR_PRICE]

    plan = m.plan()
    assert all(reg[0] in (rt.TR_PRICE, rt.TR_ASK) for reg in plan)
    assert syms(plan) == ["A", "B", "X", "Y"]   # 시스템 항상 + 그 외 2개 로테이션
    m.advance()
    assert syms(m.plan()) == ["A", "B", "Y", "Z"]  # 시스템 고정, 그 외만 회전


def test_subscription_priority_overflow_rotates_within_priority():
    # 예산 2인데 시스템이 4개 → 시스템끼리만 로테이션(현재가), 그 외 제외
    m = rt.SubscriptionManager(max_regs=2, subscribe_orderbook=True)
    m.set_symbols(priority=["A", "B", "C", "D"], other=["X"])

    def syms(regs):
        return [c for (t, c) in regs if t == rt.TR_PRICE]

    assert syms(m.plan()) == ["A", "B"]
    m.advance()
    assert syms(m.plan()) == ["B", "C"]
    assert "X" not in syms(m.plan())


def test_subscription_price_only_full_capacity():
    m = rt.SubscriptionManager(max_regs=4, subscribe_orderbook=False)
    m.set_symbols(priority=["A", "B", "C", "D"], other=[])
    regs = m.plan()
    # 호가 미구독 → 종목당 1건 → 4종목 모두 수용
    assert sorted(c for (t, c) in regs) == ["A", "B", "C", "D"]
    assert all(t == rt.TR_PRICE for (t, c) in regs)


def test_subscription_orderbook_uses_leftover_slots():
    # 현재가 우선 배정 후 남는 등록 슬롯에만 호가를 best-effort로 얹는다(현재가 커버 유지).
    m = rt.SubscriptionManager(max_regs=6, subscribe_orderbook=True)
    m.set_symbols(priority=["A", "B"], other=[])
    regs = m.plan()
    price = [c for (t, c) in regs if t == rt.TR_PRICE]
    ask = [c for (t, c) in regs if t == rt.TR_ASK]
    assert price == ["A", "B"]     # 현재가 커버리지 유지(절반으로 안 줄어듦)
    assert ask == ["A", "B"]       # 남는 4슬롯 중 2개를 호가에 사용
    assert len(regs) == 4


def test_subscription_orderbook_partial_when_price_dominates():
    # 예산이 빠듯하면 현재가를 최대한 덮고 호가는 상위 우선순위에만 얹힌다.
    m = rt.SubscriptionManager(max_regs=5, subscribe_orderbook=True)
    m.set_symbols(priority=["A", "B", "C"], other=[])
    regs = m.plan()
    price = [c for (t, c) in regs if t == rt.TR_PRICE]
    ask = [c for (t, c) in regs if t == rt.TR_ASK]
    assert price == ["A", "B", "C"]   # 현재가 3종목 전부 커버
    assert ask == ["A", "B"]          # 남는 2슬롯만 호가(우선순위순)
    cov = m.coverage()
    assert cov['priority'] == 3 and cov['price_covered'] == 3
    assert cov['ob_covered'] == 2 and cov['rest_fallback'] == 0


def test_dedup_and_other_excludes_priority():
    m = rt.SubscriptionManager(max_regs=20, subscribe_orderbook=False)
    m.set_symbols(priority=["A", "A", "B"], other=["B", "C", "C"])
    regs = m.plan()
    codes = [c for (t, c) in regs]
    assert codes.count("A") == 1 and codes.count("B") == 1  # B는 priority에만
    assert "C" in codes


def test_start_feed_toss_returns_none(monkeypatch):
    # 토스(mode 3)는 공식 WS 미지원 → 피드 시작 안 함(항상 REST 폴백)
    import config
    monkeypatch.setattr(rt, "_feed", None)
    monkeypatch.setattr(config.session, "is_toss", True)
    assert rt.start_feed() is None


def test_get_price_returns_none_when_no_data():
    # 미구독/끊김/캐시 없음 → None → 읽기 경로가 REST로 폴백
    feed = rt.KisRealtimeFeed()
    assert feed.get_price("005930", max_age=3.0) is None
    assert feed.get_vol_strength("005930", max_age=3.0) is None


# ---- 체결통보(H0STCNI0/9) ----
def _aes_encrypt(plain, key, iv):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import base64
    data = plain.encode()
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def test_aes_decrypt_roundtrip_and_parse_exec():
    key = "0123456789abcdef0123456789abcdef"  # 32바이트(AES256)
    iv = "abcdef0123456789"                   # 16바이트
    fields = ["CUST", "5012", "0001234567", "0000000000", "02", "0", "00", "0",
              "005930", "10", "70000", "093015", "0", "2"]
    plain = "^".join(fields)
    cipher = _aes_encrypt(plain, key, iv)
    assert rt.aes_cbc_decrypt(cipher, key, iv) == plain
    n = rt.parse_h0stcni(plain)
    assert n['code'] == "005930" and n['buy_sell'] == "02"
    assert n['qty'] == 10.0 and n['price'] == 70000.0
    assert n['is_fill'] is True and n['rejected'] is False


def test_parse_h0stcni_normalizes_long_code_and_rejects_short():
    long_code = ["C", "A", "O", "OO", "01", "0", "0", "0",
                 "KR7005930003", "5", "100", "0900", "0", "1"]
    n = rt.parse_h0stcni("^".join(long_code))
    assert n['code'] == "005930"          # 표준코드 → 단축코드 6자리
    assert n['is_fill'] is False          # '1' = 접수
    assert rt.parse_h0stcni("a^b^c") is None  # 필드 부족 → None


def test_reserved_slot_leaves_room_price_only():
    m = rt.SubscriptionManager(max_regs=5, subscribe_orderbook=False)
    assert m.capacity_symbols() == 5
    m.set_reserved(1)
    assert m.capacity_symbols() == 4   # 체결통보 슬롯 1개 예약


def test_handle_exec_frame_invokes_callback():
    feed = rt.KisRealtimeFeed()
    got = []
    feed.register_exec_callback(lambda n: got.append(n))
    key, iv = "0123456789abcdef0123456789abcdef", "abcdef0123456789"
    feed._aes_key, feed._aes_iv = key, iv
    fields = ["C", "A", "O0001", "OO", "02", "0", "0", "0",
              "000660", "3", "120000", "1010", "0", "2"]
    feed._handle_exec_frame(_aes_encrypt("^".join(fields), key, iv))
    assert len(got) == 1 and got[0]['code'] == "000660" and got[0]['is_fill'] is True


def test_handle_exec_frame_no_key_is_silent():
    feed = rt.KisRealtimeFeed()
    got = []
    feed.register_exec_callback(lambda n: got.append(n))
    feed._aes_key = feed._aes_iv = None
    feed._handle_exec_frame("anything")  # 키 미보유 → 조용히 무시(REST 폴백)
    assert got == []


# ===========================================================================
# 호가 구독은 실제로 몇 건이 붙는가 (2026-09-05)
#
# config 주석은 WS_SUBSCRIBE_ORDERBOOK 를 켜면 "매수후보/매도조건 분석에서 종목당 호가
# REST 1콜을 절감한다"고 약속했다. 그런데 plan() 은 등록 예산을 현재가에 **전부** 배정한
# 뒤 "남는 슬롯"에 호가를 얹는다 — 남는 슬롯은 `구독 대상 종목 수 < 용량`일 때만 생긴다.
#
# 관심종목이 국내 64개(주식 44 + ETF 20)인 실제 운용에서는 호가가 **한 건도** 붙지 않는다.
# 켜 뒀으니 되고 있다고 읽히는 자리라, 산술을 테스트로 못박고 코드가 그 사실을 알리게 한다.
#
# 배분 정책 자체(현재가 커버리지 ↔ 호가)는 제로섬이라 여기서 바꾸지 않는다 — 운영자 축이다.
from brokers.realtime import TR_ASK, TR_PRICE, SubscriptionManager


def _mgr(n_priority, n_other, reserved=1, max_regs=41):
    m = SubscriptionManager(max_regs=max_regs, subscribe_orderbook=True)
    m.set_reserved(reserved)
    m.set_symbols(priority=[f"P{i:04d}" for i in range(n_priority)],
                  other=[f"O{i:04d}" for i in range(n_other)])
    return m


def _counts(m):
    regs = m.plan()
    return (sum(1 for t, _ in regs if t == TR_PRICE),
            sum(1 for t, _ in regs if t == TR_ASK))


def test_관심종목이_용량을_넘으면_호가는_한_건도_안_붙는다():
    """실제 관심종목 규모(64종목)에서의 동작을 못박는다."""
    price, ob = _counts(_mgr(4, 60))
    assert price == 40, f"현재가가 용량을 다 쓰지 않았다: {price}"
    assert ob == 0, (
        f"호가 {ob}건 — 이 산술에서는 0이어야 한다. 0이 아니게 바꿨다면 "
        "현재가 커버리지를 그만큼 내준 것이므로 config 주석과 함께 고칠 것")


def test_종목이_용량보다_적을_때만_호가가_붙는다():
    """대조군 — 호가 경로 자체는 살아 있다(죽은 코드가 아니다)."""
    price, ob = _counts(_mgr(4, 10))
    assert price == 14 and ob == 14


def test_경계에서_한_칸씩_붙는다():
    """용량 40에 39종목이면 남는 1슬롯이 호가로 간다."""
    price, ob = _counts(_mgr(4, 35))          # 39종목
    assert (price, ob) == (39, 1)
    price, ob = _counts(_mgr(4, 36))          # 40종목 = 용량
    assert (price, ob) == (40, 0)


def test_커버리지_보고가_그_사실을_그대로_말한다():
    """coverage()가 호가 0건을 숨기지 않아야 운영자가 알 수 있다."""
    cov = _mgr(4, 60).coverage()
    assert cov['ob_covered'] == 0
    assert cov['price_covered'] == cov['capacity'] == 40
    assert cov['rest_fallback'] == 0, "시스템 종목은 전부 현재가가 붙어야 한다"


def test_무동작을_알리는_문이_있다(caplog):
    """켜 뒀으니 되고 있다고 읽히는 자리 — 실제 0건이면 남겨야 한다."""
    import brokers.realtime as rt

    feed = rt.KisRealtimeFeed.__new__(rt.KisRealtimeFeed)
    feed.manager = _mgr(4, 60)
    with caplog.at_level("INFO", logger="hts"):
        feed._warn_if_orderbook_inert()
    assert "호가 구독 0건" in caplog.text, caplog.text

    caplog.clear()
    feed.manager = _mgr(4, 10)
    with caplog.at_level("INFO", logger="hts"):
        feed._warn_if_orderbook_inert()
    assert "호가 구독 0건" not in caplog.text, "붙는 상황에서 경고가 났다"
