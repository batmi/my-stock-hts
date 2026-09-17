"""선물 '전일 종가'는 yfinance 일봉이 아니라 **직전 세션 마지막 분봉**이다 (2026-09-17).

[실측] NQ=F, 9월물 만기 전날(KST 19:54). yfinance 일봉 16일 종가 28,963.5 · fast_info prev_close
 도 28,963.5. 그런데 그 세션(15일 18:00 → 16일 17:00 ET) 15분봉은 29,129~29,524 를 벗어난 적이
 없고 마지막 봉은 29,263.75 다. 15일도 같은 모양(28,955 vs 29,277). 롤오버 주에 일봉·prev_close 가
 다른 월물을 가리키는 것이다. 그 값으로 낸 등락률이 +2.09% — 토스·HTS 가 보여 주는 +1.06% 의
 두 배였다. 현재가(fast_info last)와 같은 시리즈인 분봉이 정본이다.
"""
import numpy as np
import pandas as pd
import pytest

from modules import market

ET = "America/New_York"


def _intraday(start, end, base=29_000.0, step=15):
    """start~end(ET) 15분봉. 값은 시각 순서대로 1씩 올라가 '어느 봉을 골랐나'가 값에 드러난다."""
    idx = pd.date_range(start, end, freq=f"{step}min", tz=ET)
    idx = idx[idx.hour != 17]                     # CME 휴게(17:00~18:00 ET)에는 봉이 없다 — 실데이터와 같게
    close = base + np.arange(len(idx), dtype=float)
    return pd.DataFrame({"close": close}, index=idx.tz_convert("UTC"))


def _at(day_hm):
    return pd.Timestamp(day_hm, tz=ET)


def test_장중에는_지금_세션_시작_직전_봉이_전일_종가다():
    df = _intraday("2026-09-15 18:00", "2026-09-17 06:45")
    prev = market._futures_session_prev_close(df, now_et=_at("2026-09-17 06:45"))
    #  지금 세션은 16일 18:00 시작 → 그 직전 봉은 16일 16:45(17:00 마감) 봉이다.
    want = float(df.loc[df.index.tz_convert(ET) == _at("2026-09-16 16:45"), "close"].iloc[0])
    assert prev == want


def test_저녁_세션_개시_직후에도_같은_기준이다():
    """18:00 ET 에 새 세션이 열리면 기준이 방금 끝난 세션의 마지막 봉으로 넘어간다."""
    df = _intraday("2026-09-14 18:00", "2026-09-16 18:15")
    before_open = market._futures_session_prev_close(df, now_et=_at("2026-09-16 17:30"))
    after_open = market._futures_session_prev_close(df, now_et=_at("2026-09-16 18:15"))
    v = lambda hm: float(df.loc[df.index.tz_convert(ET) == _at(hm), "close"].iloc[0])
    assert before_open == v("2026-09-15 16:45"), "17:30 은 아직 16일 세션(휴게) — 기준은 15일 마감"
    assert after_open == v("2026-09-16 16:45"), "18:15 는 17일 세션 — 기준은 16일 마감"


def test_주말에는_마지막으로_거래된_세션의_기준으로_물러난다():
    """금 17:00 마감 뒤 토·일에는 지금 세션에 봉이 없다 — 0% 로 굳지 않게 하루씩 물러난다."""
    df = _intraday("2026-09-16 18:00", "2026-09-18 16:45")          # 목 저녁 ~ 금 마감
    prev = market._futures_session_prev_close(df, now_et=_at("2026-09-19 12:00"))   # 토요일
    v = lambda hm: float(df.loc[df.index.tz_convert(ET) == _at(hm), "close"].iloc[0])
    assert prev == v("2026-09-17 16:45"), "금요일 세션(목18:00→금17:00)의 전일 종가는 목요일 마감"


def test_지금_세션을_덮지_못한_분봉은_None_이다():
    """캐시가 세션 넘김 전 것이면 '전 세션 마지막 봉'을 확정할 수 없다 — 호출부가 다시 받는다."""
    df = _intraday("2026-09-15 18:00", "2026-09-16 16:45")          # 16일 세션 도중까지만
    assert market._futures_session_prev_close(df, now_et=_at("2026-09-17 06:45")) is None


def test_빈_분봉과_close_없는_분봉은_None_이다():
    assert market._futures_session_prev_close(pd.DataFrame()) is None
    assert market._futures_session_prev_close(None) is None
    df = _intraday("2026-09-15 18:00", "2026-09-16 16:45").rename(columns={"close": "x"})
    assert market._futures_session_prev_close(df, now_et=_at("2026-09-16 12:00")) is None


def test_선물_이름_목록이_한_곳이다():
    """워커의 is_futures 와 다운로드의 분봉 추가 수신이 같은 목록을 봐야 한다."""
    import inspect
    src = inspect.getsource(market._process_index_worker)
    assert "is_futures = name in FUTURES_NAMES" in src
    assert "나스닥 선물" in market.FUTURES_NAMES and "금" in market.FUTURES_NAMES
    dl = inspect.getsource(market.get_market_indices) if hasattr(market, "get_market_indices") else open(market.__file__, encoding="utf-8").read()
    assert "futures_tickers = {t for n, t in indices_map.items() if n in FUTURES_NAMES}" in dl


def test_워커는_분봉_기준을_일봉보다_먼저_쓴다():
    import inspect
    src = inspect.getsource(market._process_index_worker)
    a = src.index("_futures_session_prev_close(df_intraday)")
    b = src.index("_daily_prev_close_idx(df_daily, last_price, is_futures)")
    assert a < b, "일봉이 먼저 오면 롤오버 주의 틀린 전일 종가가 다시 쓰인다"
    assert "elif (is_crypto or is_futures)" in src, "분봉으로 정했으면 일봉이 덮어쓰지 않아야 한다"
