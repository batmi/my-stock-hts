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


def index_return(name, d0, d1):
    """KRX Open API 로컬 저장소의 지수('코스피'·'코스닥') 구간 수익%·MDD%. 없으면 (None, None). 읽기 전용.

    [2026-10-23] 저장분이 구간을 덮지 못하면(파이처럼 저장소가 짧은 기기) None — 종전엔 있는 날만으로
     계산해 3년 백테스트 옆에 1년짜리 지수 수익이 같은 줄로 놓일 수 있었다. 양 끝 허용 폭은
     INDEX_EDGE_TOLERANCE_DAYS(연휴·전일 확정분만 실리는 게시 지연을 흡수).
    """
    from modules import krx_openapi
    try:
        c = sqlite3.connect(f"file:{krx_openapi._db_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, None
    try:
        rows = [(str(r[0]), float(r[1])) for r in c.execute(
            "SELECT bas_dd, close FROM index_daily WHERE name=? AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd",
            (name, d0, d1))]
    except sqlite3.Error:
        return None, None
    finally:
        c.close()
    if len(rows) < 2:
        return None, None
    if (_gap_days(rows[0][0], d0) > INDEX_EDGE_TOLERANCE_DAYS
            or _gap_days(rows[-1][0], d1) > INDEX_EDGE_TOLERANCE_DAYS):
        return None, None
    peak, mdd = 0.0, 0.0
    for _d, v in rows:
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1) * 100)
    return (rows[-1][1] / rows[0][1] - 1) * 100, mdd
