"""대조군 — 같은 종목 동일가중 단순 보유 · 지수(코스피·코스닥) 구간 수익과 낙폭.

[2026-10-17] 감사는 팔끼리(설정 A vs B) 상대 비교만 했고, 지수나 '그냥 들고 있기'와 견준 도구가
 하나도 없었다. 첫 측정(재선별 워크포워드 12개월·10년): 전략 적합도 상위 +74%/MDD -30 vs 같은 종목
 보유 -2%/-60 vs 코스피 +169%/-39 — 낙폭 방어는 견고하지만 지수 대비 우위는 창에 따라 뒤집혔다.
[2026-10-23] 메뉴 4 백테스트 화면도 절대 수익만 보여 줬다 — 감사 도구(tools/audit_common)에만 있던
 이 함수들을 여기로 옮기고 화면과 감사가 같은 산식을 쓴다([[no-subprocess-for-features]]).
"""
import sqlite3
from datetime import datetime

INDEX_EDGE_TOLERANCE_DAYS = 14      # 지수 저장분이 구간 양 끝에서 이만큼(달력일)까지 비어도 같은 구간으로 본다


def buy_hold(frames, d0, d1, dates=None, is_overseas=False):
    """동일가중 단순 보유 — d0 이후 첫 종가에 사서 d1 이전 마지막 종가에 판다. (수익%, MDD%) | (None, None).

    frames: [DataFrame(date 'YYYYMMDD', close)] — 백테스트와 같은 수정 일봉을 넘긴다.
    중도에 봉이 끊긴 종목(폐지)은 마지막 봉 값으로 남는다(전략 팔과 같은 규약). 왕복 비용은
    core.trading_cost 로 뺀다. MDD 는 '산 뒤 방치' 일별 자산곡선(종목별 비중이 흘러간다)으로 잰다.
    dates: 자산곡선의 날짜 축. 없으면 종목들 날짜의 합집합.
    is_overseas: 해외 종목이면 해외 수수료 체계로 비용을 뺀다(단일 종목 백테스트 화면).
    """
    from core import trading_cost
    paths = []
    for df in frames:
        if df is None or getattr(df, "empty", True):
            continue
        s = df[(df["date"].astype(str) >= d0) & (df["date"].astype(str) <= d1)]
        if len(s) < 2:
            continue
        cl = s["close"].astype(float).to_numpy()
        b, e = cl[0], cl[-1]
        if b <= 0:
            continue
        cost = trading_cost.round_trip_cost(b, e, 1, is_overseas) / b
        paths.append((dict(zip(s["date"].astype(str), cl / b)), e / b - cost))
    if not paths:
        return None, None
    ret = (sum(p[1] for p in paths) / len(paths) - 1) * 100
    axis = sorted(x for x in (dates or set().union(*(p[0] for p in paths))) if d0 <= x <= d1)
    last = [1.0] * len(paths)
    peak, mdd = 0.0, 0.0
    for x in axis:
        for i, (m, _r) in enumerate(paths):
            if x in m:
                last[i] = m[x]
        v = sum(last) / len(last)
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1) * 100)
    return ret, mdd


def _gap_days(a, b):
    return abs((datetime.strptime(a, "%Y%m%d") - datetime.strptime(b, "%Y%m%d")).days)


def index_series(name, d0, d1):
    """KRX Open API 로컬 저장소의 지수('코스피'·'코스닥') 일별 종가 [(YYYYMMDD, 종가)]. 못 덮으면 None. 읽기 전용.

    [2026-10-23] 저장분이 구간을 덮지 못하면(파이처럼 저장소가 짧은 기기) None — 종전엔 있는 날만으로
     계산해 3년 백테스트 옆에 1년짜리 지수 수익이 같은 줄로 놓일 수 있었다. 양 끝 허용 폭은
     INDEX_EDGE_TOLERANCE_DAYS(연휴·전일 확정분만 실리는 게시 지연을 흡수).
    """
    from modules import krx_openapi
    try:
        c = sqlite3.connect(f"file:{krx_openapi._db_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = [(str(r[0]), float(r[1])) for r in c.execute(
            "SELECT bas_dd, close FROM index_daily WHERE name=? AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd",
            (name, d0, d1))]
    except sqlite3.Error:
        return None
    finally:
        c.close()
    if len(rows) < 2:
        return None
    if (_gap_days(rows[0][0], d0) > INDEX_EDGE_TOLERANCE_DAYS
            or _gap_days(rows[-1][0], d1) > INDEX_EDGE_TOLERANCE_DAYS):
        return None
    return rows


def index_return(name, d0, d1):
    """지수 구간 수익%·MDD%. 없으면 (None, None). 구간·끝단 규약은 index_series 와 같다."""
    rows = index_series(name, d0, d1)
    if rows is None:
        return None, None
    peak, mdd = 0.0, 0.0
    for _d, v in rows:
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1) * 100)
    return (rows[-1][1] / rows[0][1] - 1) * 100, mdd


def exposure_matched(series, dates, cash_curve):
    """전략이 그날 들고 있던 주식 비중만큼만 지수를 들었다면 — (수익%, MDD%, 평균 노출%) | (None, None, None).

    [왜 · 2026-11-15] 지수 줄은 자산 100%를 지수에 넣은 것이고, 전략은 평균 현금이 절반을 넘는다
     (시장 필터 보류·빈 슬롯·변동성 타겟팅). 둘을 그대로 견주면 '종목을 못 골라서 뒤졌나'와 '덜 들고 있어서
     뒤졌나'가 한 숫자에 섞인다. 같은 날 같은 비중만 지수를 든 줄을 사이에 두면 둘이 갈린다:
       지수 − 이 줄  = 덜 들고 있어서 놓친(또는 피한) 몫       (노출)
       전략 − 이 줄  = 같은 노출에서 종목 선별·매매가 더한 몫  (선별·매매)

    series: index_series 결과. dates·cash_curve: run_portfolio 에 넘긴 날짜 축과 결과의 'cash_curve'
     (그날 종가 시점·그날 매매 전의 현금 비중 %). 전략은 종가에 매매하므로 dates[i-1]→dates[i] 구간에
     들고 있던 비중은 dates[i] 에 잰 값이다(그날 등락만큼 흘러간 뒤의 비중이라 오른 날 조금 크게 잡힌다).
     지수 쪽은 매매 비용을 빼지 않는다(index_return 과 같은 규약). 현금에는 이자를 붙이지 않는다(전략과 같다).
    """
    if not series or not dates or cash_curve is None or len(cash_curve) != len(dates):
        return None, None, None
    px = dict(series)
    v, peak, mdd, prev, exps = 1.0, 1.0, 0.0, None, []
    for d, cash_pct in zip(dates, cash_curve):
        p = px.get(d)
        if p is None or p <= 0:
            continue                    # 지수 봉이 없는 날 — 다음 봉에서 그 사이 등락을 한 번에 받는다
        if prev is not None:
            e = min(1.0, max(0.0, 1.0 - float(cash_pct) / 100.0))
            v *= 1 + e * (p / prev - 1)
            exps.append(e)
            peak = max(peak, v)
            mdd = min(mdd, (v / peak - 1) * 100)
        prev = p
    if not exps:
        return None, None, None
    return (v - 1) * 100, mdd, sum(exps) / len(exps) * 100
