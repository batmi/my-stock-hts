"""분봉(장중 봉) 수집·캐시 — 청산/진입의 '체결 시점' 검증 전용.

[왜 필요한가] 일봉 백테스트는 하루에 한 번만 판정·체결할 수 있다. 그런데 실매매는
 감시 주기마다 실시간가로 판정한다(손절·트레일링 트리거는 항상 실시간가). 두 세계의
 차이를 tools/audit_exit_timing.py 가 일봉의 고가·저가로 근사해 쟀지만, 거기엔 두 가지
 한계가 남는다.
   · 고가·저가의 **선후를 모른다** → low_first/high_first 두 경로를 다 재는 수밖에 없었다
   · 종가 판정 팔은 '그날 종가'를 미리 안다 → 판정 시점의 이점을 지울 수 없었다
 실제 분봉이 있으면 둘 다 사라진다. 하루를 봉 순서대로 되감으면 선후가 확정되고,
 '15:00 봉 시점 판정'은 그 시점 정보만 쓴다.

[데이터] TradingView(tvDatafeed). 실측 2026-08-16 기준 KRX 종목 60분봉이 5,000봉
 (2023-09~, 약 715 거래일) 나오고, 일봉으로 합치면 KRX 확정 일봉(pykrx/FDR)과
 OHLC 99.3~99.7% 일치한다. 30분봉은 약 1.5년, 5분봉은 약 3개월이다.
 tvDatafeed는 간헐적으로 타임아웃하므로(기존 지수 조회에서도 같은 증상) 재시도하고,
 받은 것은 디스크에 캐시해 재실행에서 네트워크를 타지 않는다.

[캐시] data/intraday_tv/{code}_{interval}.pkl (data/ 는 .gitignore 대상)
"""
import os
import time

import pandas as pd

import config

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "intraday_tv")
INTERVALS = {"60m": "in_1_hour", "30m": "in_30_minute", "15m": "in_15_minute",
             "5m": "in_5_minute"}


def cache_path(code, interval="60m"):
    return os.path.join(CACHE_DIR, f"{code}_{interval}.pkl")


def load(code, interval="60m"):
    """캐시된 분봉. 없으면 None. 컬럼: datetime(index), open/high/low/close/volume."""
    p = cache_path(code, interval)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_pickle(p)
        return df if isinstance(df, pd.DataFrame) and not df.empty else None
    except Exception:
        return None


def _tv():
    """tvDatafeed 인스턴스. 지수 조회와 같은 싱글턴을 재사용한다(로그인·토큰 캐시 공유)."""
    from modules import analysis
    return analysis._get_tvdatafeed()


def fetch(code, interval="60m", n_bars=5000, retries=3, pause=3.0):
    """TradingView에서 분봉을 받아온다. 실패하면 None."""
    from tvDatafeed import Interval
    iv = getattr(Interval, INTERVALS[interval])
    tv = _tv()
    if tv is None:
        return None
    for attempt in range(retries):
        try:
            df = tv.get_hist(code, "KRX", interval=iv, n_bars=n_bars)
            if df is not None and not df.empty:
                df = df.copy()
                df.index = pd.to_datetime(df.index)
                keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
                return df[keep].sort_index()
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(pause * (attempt + 1))
    return None


def ensure(codes, interval="60m", n_bars=5000, force=False, log=print):
    """캐시에 없는 종목만 받아 저장한다. (성공 코드 집합, 실패 코드 목록)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    have, failed = set(), []
    for i, code in enumerate(codes, 1):
        if not force and load(code, interval) is not None:
            have.add(code)
            continue
        df = fetch(code, interval, n_bars)
        if df is None:
            failed.append(code)
            log(f"  [{i}/{len(codes)}] {code} 실패")
            continue
        df.to_pickle(cache_path(code, interval))
        have.add(code)
        log(f"  [{i}/{len(codes)}] {code} {len(df)}봉 {df.index[0].date()}~{df.index[-1].date()}")
    return have, failed


def by_day(df, session_end="15:30"):
    """{'YYYYMMDD': [(HHMM, open, high, low, close, volume), ...]} 로 접는다(시간 오름차순).

    session_end 이후 봉은 버린다 — 지표·일봉이 전부 KRX 정규장 기준이므로
    시간외 체결이 섞이면 백테스트와 입력이 갈린다(config.USE_KRX_CLOSE_AFTER_HOURS 주석).
    """
    cut = session_end.replace(":", "")
    out = {}
    for ts, r in df.iterrows():
        hhmm = f"{ts.hour:02d}{ts.minute:02d}"
        if hhmm > cut:
            continue
        out.setdefault(ts.strftime("%Y%m%d"), []).append(
            (hhmm, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
             float(r.get("volume", 0) or 0)))
    for k in out:
        out[k].sort(key=lambda x: x[0])
    return out


def validate(code, interval="60m", days=1200):
    """분봉을 일봉으로 합쳐 KRX 확정 일봉과 대조한다. (대조일수, OHLC일치%, 종가일치%)."""
    from modules import backtest
    df = load(code, interval)
    if df is None:
        return 0, 0.0, 0.0
    g = df.groupby(df.index.date).agg(open=("open", "first"), high=("high", "max"),
                                      low=("low", "min"), close=("close", "last"))
    dd = backtest.get_backtest_data(code, False, days)
    if dd is None or dd.empty:
        return 0, 0.0, 0.0
    dd = dd.copy()
    dd["d"] = pd.to_datetime(dd["date"].astype(str)).dt.date
    j = g.join(dd.set_index("d")[["open", "high", "low", "close"]], how="inner", rsuffix="_krx")
    if j.empty:
        return 0, 0.0, 0.0
    eq = lambda a, b: (j[a].round(0) == j[b].round(0))  # noqa: E731
    ok_all = (eq("open", "open_krx") & eq("high", "high_krx")
              & eq("low", "low_krx") & eq("close", "close_krx")).mean() * 100
    return len(j), float(ok_all), float(eq("close", "close_krx").mean() * 100)


def status_cache_path(code, interval="60m"):
    return os.path.join(CACHE_DIR, f"status_{code}_{interval}.pkl")


# 시점판정 캐시에 함께 저장하는 '무엇으로 만들었나' 표식. 날짜 키(YYYYMMDD)와 섞이지
#  않도록 밑줄로 시작한다.
STATUS_META_KEY = "_meta"


def current_thresholds():
    """시점판정을 만들 때 쓰는 임계값 한 벌. 빌더와 게이트가 같은 것을 봐야 한다.

    종전에는 tools/build_intraday_status.py 안에만 있어, 게이트는 '어떤 임계값으로 만든
    판정인가'를 물어볼 방법이 없었다.
    """
    return {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": dict(config.SCORING_WEIGHTS),
    }


def status_meta(thresholds, lookback, bars_last):
    """시점판정 캐시의 표식. 이 셋 중 하나라도 달라지면 그 판정은 지금 것이 아니다."""
    return {"thresholds": thresholds, "lookback": lookback, "bars_last": str(bars_last)}


def bars_last_date(code, interval="60m"):
    """캐시된 분봉의 마지막 날짜(YYYY-MM-DD 문자열). 없으면 None."""
    df = load(code, interval)
    if df is None or df.empty:
        return None
    return str(df.index[-1].date())


def save_status(code, interval, data, thresholds, lookback):
    """시점판정을 표식과 함께 저장한다."""
    payload = dict(data)
    payload[STATUS_META_KEY] = status_meta(thresholds, lookback,
                                           bars_last_date(code, interval))
    pd.to_pickle(payload, status_cache_path(code, interval))


def load_status(code, interval="60m", expect=None):
    """시점판정 캐시. 없거나 **지금 설정으로 만든 것이 아니면** None.

    [왜 검증하는가 · 2026-09-04] 이 캐시에는 만들 당시의 임계값·가중치·룩백과 그때의
     분봉이 통째로 구워져 있다(진입 가부 판정까지 들어 있다). 그런데 무엇으로 만들었는지
     적어 두지 않아서, 임계값을 바꾸거나 분봉을 새로 받아도 빌더는 '이미 있다'며 건너뛰고
     감사 도구는 **옛 설정의 판정**으로 결과를 냈다. 계측기가 조용히 다른 것을 재는
     상황이라, 표식이 없거나 어긋나면 없는 것으로 친다(호출부는 시끄럽게 실패한다).
    """
    p = status_cache_path(code, interval)
    if not os.path.exists(p):
        return None
    try:
        d = pd.read_pickle(p)
    except Exception:
        return None
    if not isinstance(d, dict) or not d:
        return None
    meta = d.pop(STATUS_META_KEY, None)
    if expect is not None and meta != expect:
        return None
    return d or None


# 장중 한 시점의 판정 결과. run_portfolio 의 진입 게이트·사이징이 쓰는 값만 담는다.
STATUS_FIELDS = ("raw", "chk", "can_buy", "state", "reason", "rsi", "w52", "atr", "close", "high")


def precompute_intraday_status(code, bars_by_day, thresholds, days=1200, lookback=260):
    """실매매식 '진행 중 봉'을 시간대별로 재현해 그 시점의 판정을 미리 계산한다.

    실매매는 과거 일봉에 당일 진행 중 봉을 얹어 지표를 계산한다(api._get_cached_chart 의
    현재가 오버레이: 시가 = 당일 시가, 고/저 = 현재까지의 고저, 종가 = 현재가,
    거래량 = 누적). 같은 모양을 분봉 누적으로 만든다.

      일봉(전일까지) + [시가, 누적고가, 누적저가, 봉종가, 누적거래량] → 같은 채점 함수

    lookback: 실매매가 실제로 들고 있는 봉 수(기본 260). 전 구간을 넣으면 EMA120·OBV가
      실매매보다 잘 워밍업돼 백테스트 쪽에 유리해진다(tools/audit_live_backtest_parity.py
      의 'B-live' 대조와 같은 이유).

    반환: {날짜: {HHMM: (raw, chk, can_buy, state, reason, rsi, w52, atr, close, high)}}
    """
    from modules import backtest

    raw_df = backtest.get_backtest_data(code, False, days)
    if raw_df is None or raw_df.empty:
        return {}
    raw_df = backtest._append_smart_money_signal(raw_df, code, False)
    recs = raw_df.to_dict("records")
    idx_of = {str(r["date"]): i for i, r in enumerate(recs)}

    out = {}
    for day, bars in bars_by_day.items():
        i = idx_of.get(day)
        if i is None or i < 2:
            continue
        hist = recs[max(0, i - lookback):i]      # 전일까지 (당일 제외)
        if len(hist) < 60:
            continue
        base = dict(recs[i])                      # 당일 확정 봉 — 비가격 컬럼(수급 등) 재사용
        day_open = bars[0][1]
        run_hi, run_lo, run_vol = -1e18, 1e18, 0.0
        per_time = {}
        for hhmm, _o, bh, bl, bc, bv in bars:
            run_hi = max(run_hi, bh)
            run_lo = min(run_lo, bl)
            run_vol += bv
            syn = dict(base)
            syn.update({"open": day_open, "high": run_hi, "low": run_lo,
                        "close": bc, "volume": run_vol})
            frame = pd.DataFrame(hist + [syn])
            try:
                f = backtest.compute_price_indicators(frame)
                fr = f.to_dict("records")
                st = backtest.calculate_daily_status(fr[-1], fr[-2], thresholds=thresholds)
                last = fr[-1]
                per_time[hhmm] = (float(st[0]), float(st[1]), bool(st[2]), st[3], st[4],
                                  float(last.get("RSI", 0) or 0),
                                  float(last.get("w52_pos", 0) or 0),
                                  float(last.get("ATR", 0) or 0),
                                  float(bc), float(run_hi))
            except Exception:
                continue
        if per_time:
            out[day] = per_time
    return out


def gate_universe(dfs, interval="60m", min_match=98.0, min_coverage=0.9, need_status=True,
                  lookback=260):
    """분봉으로 돌릴 수 있는 종목만 남긴다. (bars, status, keep, drop)

    두 관문을 통과해야 한다.
      ① 일봉 정합 — 분봉을 일봉으로 합쳐 KRX 확정 일봉과 OHLC가 min_match% 이상 일치.
         TradingView 심볼이 다른 계열을 가리키거나 수정주가 소급 시점이 다르면 25~60%까지
         떨어진다(실측: 삼성SDI 45.9 · 삼성바이오 24.9). 배율 보정으로도 맞지 않으므로 뺀다.
      ② 커버리지 — 분봉 일수가 중앙값의 min_coverage 배 이상. 늦게 상장한 한 종목이
         교집합을 잡을 때 창 전체를 잘라먹는 것을 막는다(LG CNS 2025-02 상장 → 715일 창이
         361일로 붕괴한 실측이 있다).

    drop: [(code, 사유)] — 호출부가 무엇이 왜 빠졌는지 반드시 표시할 것.
    """
    import numpy as np

    #  시점판정은 '지금 설정으로 만든 것'만 받는다(load_status 주석 참조). 표식이 없거나
    #   임계값·룩백·분봉 끝날짜가 어긋나면 없는 것으로 치고 사유를 남긴다 — 옛 판정으로
    #   조용히 재는 것보다 감사가 멈추는 편이 낫다.
    th = current_thresholds() if need_status else None

    bars, status, keep, drop = {}, {}, [], []
    for code, df in dfs.items():
        raw = load(code, interval)
        if raw is None:
            drop.append((code, "분봉 없음"))
            continue
        st = {}
        if need_status:
            last = str(raw.index[-1].date()) if not raw.empty else None
            st = load_status(code, interval, expect=status_meta(th, lookback, last))
            if not st:
                drop.append((code, "시점판정 없음/낡음 → tools/build_intraday_status.py 재실행"))
                continue
        g = raw.groupby(raw.index.date).agg(open=("open", "first"), high=("high", "max"),
                                            low=("low", "min"), close=("close", "last"))
        d = df.copy()
        d["d"] = pd.to_datetime(d["date"].astype(str)).dt.date
        j = g.join(d.set_index("d")[["open", "high", "low", "close"]], how="inner", rsuffix="_k")
        if j.empty:
            drop.append((code, "겹치는 날 없음"))
            continue
        eq = lambda a: (j[a].round(0) == j[a + "_k"].round(0))  # noqa: E731
        m = (eq("open") & eq("high") & eq("low") & eq("close")).mean() * 100
        if m < min_match:
            drop.append((code, f"일봉 불일치 {m:.1f}%"))
            continue
        bars[code] = by_day(raw)
        status[code] = st or {}
        keep.append(code)

    if keep:
        cov = {c: len(bars[c]) for c in keep}
        med = float(np.median(list(cov.values())))
        short = [c for c in keep if cov[c] < med * min_coverage]
        for c in short:
            drop.append((c, f"기간 짧음 {cov[c]}일/{med:.0f}일"))
        keep = [c for c in keep if c not in short]
        bars = {c: bars[c] for c in keep}
        status = {c: status[c] for c in keep}
    return bars, status, keep, drop


def covered_dates(bars, dates):
    """모든 종목에 분봉이 있는 거래일만 남긴다(없는 날은 종가 모델로 새어 팔이 섞인다)."""
    if not bars:
        return []
    common = set.intersection(*(set(v) for v in bars.values()))
    return [d for d in dates if d in common]
