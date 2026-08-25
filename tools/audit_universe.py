"""유니버스 — 몇 종목을 보고 있어야 하는가, 그리고 생존 편향은 결론을 바꾸는가.

[축 A · 크기] 슬롯은 4개인데 관심종목은 44개다. 이 숫자가 정해진 근거는 없다.
 메모리에 "시드는 성과가 아니라 커버리지 문제"라고 적혀 있는데 정작 커버리지 자체를
 잰 적이 없다. 표본 크기를 바꿔 재면 '종목을 더 넣으면 슬롯 경쟁이 좋아지는가, 아니면
 잡음만 느는가'가 나온다. 이건 사용자가 곧바로 실행할 수 있는 결론이다(관심종목 추가).
 ※ 유니버스가 커질수록 종목당 데이터가 같으니 총 자본은 고정한다 — 슬롯 4개 경쟁만 바뀐다.

[축 B · 생존 편향] 모든 감사가 **현재 상장된** 44종목에서 표본을 뽑는다. 10년 백테스트인데
 10년 전에는 이 44개를 고를 수 없었다. 절대 수익률이 부풀 뿐 아니라 **다이얼 결론의 방향까지
 바뀔 수 있다** — 손절·시간청산이 '결국 살아남은 종목'에만 맞춰 느슨하게 튜닝됐을 수 있다.
 FDR의 상장폐지 목록(KRX-DELISTING)으로 2016년 이후 폐지된 주권을 섞어 다시 잰다.
 폐지 종목의 일봉은 프로젝트 데이터 경로(pykrx/FDR)로 그대로 조회된다(확인 완료).

 [2026-08-25 · 이 축의 수치를 읽는 법] 폐지 종목은 봉이 창 중간에서 끝난다. 종전 시뮬레이터는
 그 포지션을 영영 평가하지 않아 슬롯을 창 끝까지 묶고 투입 자본을 자산곡선에서 지웠다
 (합성 실측: 자산 -78% 절벽, MDD -6.24% → -78.44%). 즉 **이 축이 종전에 낸 생존 프리미엄은
 편향의 크기가 아니라 그 결함의 크기였다** — 이전 측정치는 폐기하고 다시 재야 한다.
 지금은 마지막 봉의 종가로 '데이터종료' 청산이 나간다. 다만 실제 정리매매 회수액은 그보다
 훨씬 낮은 것이 보통이므로 **이 팔은 여전히 낙관 쪽**이다. 즉 여기서 나오는 프리미엄은
 하한선으로 읽어야 한다.

 [무엇이 공정한 비교인가] 같은 표본 크기에서 '현행 풀'과 '폐지 포함 풀'을 비교한다.
 절대 성과 차이 = 생존 프리미엄. 여기에 손절·시간청산 다이얼을 함께 흔들어, 폐지 종목이
 섞이면 **최적값이 옮겨가는지**를 본다. 옮겨가지 않으면 기존 결론들은 안전하다.

[실행] python3 tools/audit_universe.py --axis A --days 3650 --trials 15 --seeds 3
       python3 tools/audit_universe.py --axis B --days 3650 --trials 15 --seeds 3 --dead 40
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
# 축 B에서 함께 흔들 다이얼 — 생존 편향이 '느슨한 튜닝'을 만들었다면 여기서 드러난다.
DIALS = [
    ("현행", []),
    ("ATR손절 1.5", [("sell", "ATR_STOP_MULTIPLIER", 1.5)]),
    ("시간청산 10일", [("sell", "TIME_STOP_DAYS", 10)]),
    ("콜백 2.5", [("sell", "TRAILING_ATR_MULTIPLIER", 2.5)]),
]


def apply(ov):
    prev = []
    for tgt, key, val in ov:
        d = config.SELL_STRATEGY if tgt == "sell" else config.ANALYSIS_THRESHOLDS
        prev.append((tgt, key, d.get(key)))
        d[key] = val
    return prev


_LISTING_ANNOUNCED = set()


def _listing_paths(kind):
    import os
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "listing_cache")
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, kind.replace("/", "_"))
    return base + ".csv", base + ".meta.json"


def _listing(kind, refresh=None):
    """FDR 종목 목록. **스냅샷으로 고정**하고, 갱신은 명시적으로만 한다.

    [종전 동작과 왜 바꿨나 — 2026-08-24] 이 함수는 매 호출마다 원격을 받아 캐시를
    덮어썼고, 캐시는 원격 실패 시의 폴백일 뿐이었다. 당시 근거는 "목록은 거의 안 변하고
    팔 사이 비교의 배경일 뿐이라 최신성이 결론에 영향을 주지 않는다"였다.
    **그 전제가 틀렸다.** `extend_targets(mode="random")` 은 시총 내림차순으로 정렬한
    뒤 상위 `pool`개를 셔플해 뽑는다. 정렬 순서가 조금만 흔들려도 셔플의 입력 순서가
    통째로 바뀌므로 **뽑히는 종목이 갈린다.**
      실측(캐시된 KRX 목록에 시총 지터를 걸어 8회): ±2%면 60종목 중 **41.5개 교체**,
      ±5%면 48.6개, ±10%면 51.2개. ±2%는 하루치 주가 변동 수준이다.
    즉 같은 씨드로 같은 도구를 **다른 날** 돌리면 다른 유니버스를 재고 있었다. 실제로
    2026-08-24 TQ 국면 축 재검증에서 다이얼 세 개를 전부 되돌려도 기록이 재현되지 않았고,
    남은 설명이 이것이었다([[allocation-equal-weight-lead]]).

    [규약] 스냅샷이 있으면 **무조건 그것을 쓴다.** 없을 때만 받는다. 갱신은
    `AUDIT_LISTING_REFRESH=1` 또는 `python3 tools/audit_universe.py --refresh-listing`.
    갱신하면 그 이전에 찍힌 수치와는 유니버스가 달라진다 — 되돌릴 수 없으니 의도해서 할 것.
    스냅샷도 원격도 없으면 조용히 빈 결과를 주지 않고 그대로 터뜨린다.
    """
    import json
    import os
    import pandas as pd
    f, meta_f = _listing_paths(kind)
    if refresh is None:
        refresh = os.environ.get("AUDIT_LISTING_REFRESH", "") not in ("", "0")

    if os.path.exists(f) and not refresh:
        if kind not in _LISTING_ANNOUNCED:
            _LISTING_ANNOUNCED.add(kind)
            when = "?"
            try:
                with open(meta_f, encoding="utf-8") as fh:
                    when = json.load(fh).get("fetched_at", "?")
            except Exception:
                import datetime as _dt
                when = _dt.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d")
            print(f"[목록] {kind} 스냅샷 {when} 사용 — 유니버스를 고정한다. "
                  f"갱신은 AUDIT_LISTING_REFRESH=1", flush=True)
        return pd.read_csv(f, dtype={"Code": str, "Symbol": str})

    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing(kind)
        if df is None or not len(df):
            raise RuntimeError("빈 목록")
    except Exception as e:
        if os.path.exists(f):
            print(f"[목록] {kind} 원격 실패({type(e).__name__}) → 스냅샷 사용: {f}", flush=True)
            return pd.read_csv(f, dtype={"Code": str, "Symbol": str})
        raise
    df.to_csv(f, index=False, encoding="utf-8")
    import datetime as _dt
    with open(meta_f, "w", encoding="utf-8") as fh:
        json.dump({"kind": kind, "rows": int(len(df)),
                   "fetched_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}, fh,
                  ensure_ascii=False)
    print(f"[목록] {kind} 스냅샷 갱신 — {len(df):,}행. "
          f"이전 수치와 유니버스가 달라진다.", flush=True)
    return df


def _pit_date(days):
    """백테스트 창의 **시작일**(YYYYMMDD) — 유니버스를 그 시점 기준으로 고르기 위한 날짜.

    휴장일이면 직전 영업일로 당긴다. KRX 는 휴장일에도 직전 영업일 값을 주지만, 그러면
    스냅샷 파일명이 날짜마다 갈려 같은 데이터가 여러 벌 쌓인다 — 하나로 모은다.
    """
    import datetime as _dt
    d = (_dt.date.today() - _dt.timedelta(days=int(days))).strftime("%Y%m%d")
    try:
        from pykrx import stock
        return stock.get_nearest_business_day_in_a_week(d)
    except Exception:       # noqa: BLE001 - pykrx 없거나 조회 실패면 원래 날짜로 간다
        return d


def _pit_marcap(date):
    """그 **시점의** 시가총액 {티커: 시총}. KOSPI+KOSDAQ. 조회 불가면 None.

    [왜 필요한가 — 2026-08-24] `_listing("KRX")` 의 Marcap 은 **오늘의** 시총이다. 그것으로
    10년 백테스트의 유니버스를 고르면 "2016년에는 고를 수 없었던 종목"을 2016년에 심는 셈이다
    (실측: 2016-01-04 시총 2위는 **한국전력**이었고, 오늘 목록으로는 재현되지 않는다).
    이 함수는 그날 실제 순위를 준다 — 폐지된 종목도 그 시점엔 살아 있었으므로 자연히 섞인다.

    [자격증명] pykrx 의 시총 조회는 data.krx.co.kr 로그인(KRX_ID/KRX_PW)이 있어야 열린다.
    없으면 빈 프레임이 오므로 None 을 돌려주고, 호출부가 기존 모드로 안내한 뒤 멈춘다.

    [고정] `_listing` 과 같은 규약으로 **디스크 스냅샷**에 박는다. 과거 시총은 불변이라
    갱신할 이유가 없고, 매 실행마다 KRX 를 두드리면 레이트리밋에 걸린다.
    """
    import os
    import pandas as pd
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "listing_cache")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, f"pit_marcap_{date}.csv")
    if os.path.exists(f):
        df = pd.read_csv(f, dtype={"ticker": str})
        return dict(zip(df["ticker"], df["marcap"]))

    try:
        from pykrx import stock
    except Exception as e:      # noqa: BLE001
        print(f"[PIT] pykrx 로드 실패: {e}", flush=True)
        return None

    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            cap = stock.get_market_cap(date, market=market)
        except Exception as e:  # noqa: BLE001
            print(f"[PIT] {market} 시총 조회 실패({date}): {type(e).__name__}: {e}", flush=True)
            return None
        if cap is None or cap.empty:
            return None
        frames.append(cap[["시가총액"]])

    out = pd.concat(frames)
    out = out[out["시가총액"] > 0]
    pd.DataFrame({"ticker": out.index.astype(str), "marcap": out["시가총액"].values}).to_csv(
        f, index=False, encoding="utf-8")
    print(f"[PIT] {date} 시총 스냅샷 생성 — {len(out):,}종목 (KOSPI+KOSDAQ)", flush=True)
    return dict(zip(out.index.astype(str), out["시가총액"]))


def _name_map():
    """{코드: 이름} — 현재 상장 목록과 폐지 목록을 합친다.

    PIT 유니버스에는 **지금은 없는 종목**이 섞이므로(그게 요점이다) 폐지 목록의 이름까지
    있어야 스팩·리츠를 걸러낼 수 있다. 어느 쪽에도 없으면 이름 기반 필터는 통과시킨다 —
    코드 기반 우선주 필터는 그대로 걸린다.
    """
    out = {}
    for kind, code_col in (("KRX", "Code"), ("KRX-DELISTING", "Symbol")):
        try:
            df = _listing(kind)
        except Exception as e:      # noqa: BLE001
            print(f"[PIT] {kind} 목록을 못 읽었다({type(e).__name__}) — 이름 필터가 느슨해진다",
                  flush=True)
            continue
        if code_col not in df.columns or "Name" not in df.columns:
            continue
        for c, n in zip(df[code_col].astype(str), df["Name"].astype(str)):
            out.setdefault(c, n)
    return out


def _is_excluded_ticker(code, name):
    """우선주·스팩·리츠인가. (mode 공용 — 현재 목록과 PIT 목록이 같은 규칙을 쓰게 한다)

    우선주는 두 가지 표기가 있다. 구형은 끝자리 5·7·9(005935 삼성전자우), 신형은
    **여섯째 자리가 알파벳**이다(00680K 미래에셋증권2우B). 끝자리 숫자만 보던 필터는
    신형을 통째로 놓쳤다 — 2026-08-24 스냅샷 기준 상위 500 풀 안에 3종(미래에셋증권2우B·
    한화3우B·CJ4우(전환))이 남아 있었고, pool을 키우면 그만큼 더 샌다.
    ※ `0120G0`(삼양바이오팜)처럼 **가운데**에 문자가 오는 코드는 최근 상장된 보통주의
      신규 채번이다. 그래서 '문자 포함'이 아니라 '끝자리가 문자'로 가른다.
    """
    import re
    code = str(code)
    if code.endswith(("5", "7", "9")):
        return True
    if re.match(r"^\d{5}[A-Z]$", code):
        return True
    return bool(name) and ("스팩" in name or "리츠" in name)


def _hash_draw(cand, pool, limit, seed):
    """상위 `pool` 안에서 **종목별 해시**로 뽑는다.

    [2026-08-24] '리스트를 셔플해서 앞에서 자르기'를 버렸다. 고정 씨드 셔플은
    '인덱스 → 자리'의 고정 치환이라, 리스트에서 **한 종목의 위치만 밀려도** 그 뒤 전부의
    자리가 바뀐다. 그래서 뽑히는 60개가 통째로 갈렸다.
      실측(시총에 지터를 걸어 8회):
        셔플·시총순   ±2% → 60 중 41.5개 교체  |  ±5% 48.6  |  ±10% 51.2
        셔플·코드순   ±2% → 60 중 20.9개 교체  |  ±5% 34.0  |  ±10% 40.1
        해시(현행)    ±2% → 60 중  0.8개 교체  |  ±5%  1.0  |  ±10%  1.4
    정작 데이터는 거의 안 움직인다 — 같은 지터에서 상위 pool의 **구성원**은 500개 중
    3개(0.6%)만 바뀐다. 흔들린 것은 유니버스가 아니라 계측기였다. 해시 방식은 한 종목의
    당락이 **그 종목만의 함수**라, 구성원이 3개 바뀌면 뽑히는 것도 그만큼만 바뀐다.
    """
    import hashlib
    return sorted(cand[:pool],
                  key=lambda c: hashlib.md5(f"{seed}|{c[0]}".encode()).hexdigest())[:limit]


def extend_targets(exclude, limit, mode="marcap", pool=500, seed=20260816, pit_date=None):
    """관심종목에 없는 종목으로 풀을 넓힌다. '44개를 넘기면 나아지는가'를 재려면 필요하다.

    우선주·스팩·리츠는 뺀다 — 추세추종 대상이 아니고 유동성 성격도 다르다.

    [mode='marcap'] **현재** 시총 상위에서 뽑는다. 편하지만 생존 편향이 이 축에서 최대로
      작동한다 — 지금 시총이 큰 종목은 정의상 지난 10년간 크게 오른 종목이다. 이 팔만으로는
      '종목을 늘려서 좋아진 것'과 '지금 큰 종목을 과거에 심어서 좋아진 것'을 못 가른다.
    [mode='random'] **현재** 시총 상위 `pool`개 안에서 무작위로 뽑는다. '거래 가능성'만
      통제하고 승자 선택은 제거한다. 다만 pool 자체가 오늘 기준이라 look-ahead 가 남는다.
    [mode='pit'] **그 시점(pit_date)의** 시총 상위 `pool` 안에서 뽑는다. random 과 뽑기
      방식(해시)이 같고 **시총을 언제 재느냐만 다르다** — 그래서 둘을 나란히 돌리면
      look-ahead 의 크기가 그대로 드러난다. 그 시점에 살아 있던 종목을 쓰므로 나중에
      폐지된 종목도 자연히 섞인다(축 B 의 생존 편향과 겹치는 부분이 있다).
      KRX_ID/KRX_PW 가 없으면 시총 조회가 막혀 있어 쓸 수 없다.
    """
    if mode == "pit":
        if not pit_date:
            raise ValueError("mode='pit' 은 pit_date 가 필요하다")
        caps = _pit_marcap(pit_date)
        if not caps:
            print("[PIT] 그 시점 시총을 받지 못했다 — KRX_ID/KRX_PW 를 확인하라. "
                  "(--extend-mode random 으로는 계속 잴 수 있다)", flush=True)
            return []
        names = _name_map()
        cand = [(c, names.get(c, c)) for c in caps
                if c not in exclude and not _is_excluded_ticker(c, names.get(c, ""))]
        cand.sort(key=lambda t: caps[t[0]], reverse=True)
        return _hash_draw(cand, pool, limit, seed)

    df = _listing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
    bad = [not _is_excluded_ticker(r["Code"], r["Name"]) for _, r in df.iterrows()]
    df = df[bad].sort_values("Marcap", ascending=False)
    cand = [(r["Code"], r["Name"]) for _, r in df.iterrows() if r["Code"] not in exclude]
    if mode == "random":
        return _hash_draw(cand, pool, limit, seed)
    return cand[:limit]


def dead_targets(limit, since="2016-01-01"):
    """상장폐지 주권 목록. 스팩·피흡수합병처럼 '전략과 무관한 소멸'은 뺀다."""
    import pandas as pd
    df = _listing("KRX-DELISTING")
    df["DelistingDate"] = pd.to_datetime(df["DelistingDate"], errors="coerce")
    m = (df["DelistingDate"] >= since) & (df["SecuGroup"] == "주권")
    df = df[m].copy()
    drop = df["Name"].str.contains("스팩", na=False)
    df = df[~drop]
    # 폐지 사유가 합병·완전자회사화면 주가가 급락으로 끝나지 않아 편향 측정 대상이 아니다.
    keep = ~df["Reason"].fillna("").str.contains("합병|완전자회사|해산|지주회사")
    df = df[keep]
    df = df.sort_values("DelistingDate", ascending=False).head(limit * 3)
    return [(r["Symbol"], r["Name"]) for _, r in df.iterrows()]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "worst": profits[-1] if profits else 0.0,
        "loss30": sum(1 for p in profits if p <= -30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "cash": r.get("avg_cash_ratio", 0.0),
    }


def prep(targets, days, label):
    dfs, mf, dates, failed = pb.prepare_universe(targets, days)
    print(f"[준비] {label}: 요청 {len(targets)} → 사용 {len(dfs)}종목 (실패 {len(failed)})")
    return dfs, mf, dates


def refresh_listings():
    """종목 목록 스냅샷을 의도적으로 새로 받고, **유니버스가 얼마나 움직였는지** 보고한다.

    갱신은 되돌릴 수 없다(옛 스냅샷을 덮어쓴다). 그래서 조용히 하지 않는다 — 갱신 뒤에
    찍은 수치는 갱신 전 기록과 유니버스가 다르고, 얼마나 다른지는 여기 출력이 말해 준다.
    """
    import pandas as pd
    before = {}
    for kind in ("KRX", "KRX-DELISTING"):
        f, _ = _listing_paths(kind)
        if os.path.exists(f):
            df = pd.read_csv(f, dtype={"Code": str, "Symbol": str})
            col = "Code" if "Code" in df.columns else "Symbol"
            before[kind] = set(df[col].astype(str))
    old_pick = set()
    try:
        old_pick = {c for c, _ in extend_targets(set(), 60, mode="random")}
    except Exception:
        pass

    for kind in ("KRX", "KRX-DELISTING"):
        _listing(kind, refresh=True)

    print()
    for kind in ("KRX", "KRX-DELISTING"):
        f, _ = _listing_paths(kind)
        df = pd.read_csv(f, dtype={"Code": str, "Symbol": str})
        col = "Code" if "Code" in df.columns else "Symbol"
        now = set(df[col].astype(str))
        if kind in before:
            print(f"[{kind}] {len(before[kind]):,} → {len(now):,}행 "
                  f"(신규 {len(now - before[kind]):,} · 사라짐 {len(before[kind] - now):,})")
        else:
            print(f"[{kind}] 스냅샷 신규 생성 {len(now):,}행")

    new_pick = {c for c, _ in extend_targets(set(), 60, mode="random")}
    if old_pick:
        kept = len(old_pick & new_pick)
        print(f"\n[확장 유니버스] mode=random 60종목 중 {kept}개 유지 · "
              f"{60 - kept}개 교체")
        print("  → 교체된 만큼, 갱신 전 기록과 이 뒤의 수치는 **다른 표본**을 잰 것이다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-listing", action="store_true",
                    help="종목 목록 스냅샷을 새로 받는다(되돌릴 수 없다). 유니버스 변화량을 찍는다")
    ap.add_argument("--axis", default="A", choices=["A", "B"])
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--sizes", default="10,20,30,44")
    ap.add_argument("--extend-mode", default="marcap", choices=["marcap", "random", "pit"],
                    help="확장 종목 선정: marcap=현재 시총 상위(생존 편향 최대) / "
                         "random=현재 상위 pool 내 무작위 / "
                         "pit=**그 시점** 상위 pool 내 무작위(look-ahead 제거, KRX_ID 필요). "
                         "random 과 pit 을 나란히 돌리면 look-ahead 의 크기가 드러난다")
    ap.add_argument("--extend-pool", type=int, default=500)
    ap.add_argument("--extend", type=int, default=0,
                    help="축 A: 시총 상위에서 관심종목에 없는 종목을 이만큼 추가해 44개 너머를 잰다")
    ap.add_argument("--dead", type=int, default=40, help="축 B: 섞을 폐지 종목 수")
    ap.add_argument("--dead-frac", type=float, default=0.0,
                    help="축 A: 표본을 이 비율만큼 폐지 종목으로 채운다. 10년 실제 폐지율은 "
                         "약 20%%(563/2700)다 — 과거에 고른 관심종목이라면 그만큼 사라졌다")
    ap.add_argument("--sample", type=int, default=25, help="축 B: 고정 표본 크기")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    if args.refresh_listing:
        refresh_listings()
        return
    seed_notice(args.seeds, example="--seeds 3")

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 축 {args.axis} · 관심종목 {len(live)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf, dates = prep(live, args.days, "현행 풀")
    ext_dfs = {}
    if args.axis == "A" and args.extend > 0:
        pit_date = _pit_date(args.days)
        et = extend_targets({c for c, _n in live}, args.extend,
                            mode=args.extend_mode, pool=args.extend_pool, seed=args.seed,
                            pit_date=pit_date)
        tag = {"marcap": "시총 상위",
               "random": f"상위{args.extend_pool} 내 무작위",
               "pit": f"{pit_date} 시점 상위{args.extend_pool} 내 무작위"}[args.extend_mode]
        ext_dfs, ext_mf, _ed = prep(et, args.days, f"확장 풀({tag} {len(et)})")
        mf.update({c: ext_mf.get(c, set()) for c in ext_dfs})
    dead_dfs = {}
    if args.axis == "A" and args.dead_frac > 0:
        # [크기 축의 진짜 반례] 확장 풀도 '오늘까지 살아남은' 종목이라, 종목을 늘린 효과에
        #  생존 편향이 그대로 얹힌다. 표본을 실제 폐지율만큼 폐지 종목으로 채워, 과거에
        #  실제로 고를 수 있었던 관심종목에 가깝게 만든 뒤 같은 기울기가 남는지 본다.
        # 필요한 폐지 종목 수는 '가장 큰 표본 × 폐지 비율'이다. 100으로 고정해 두면
        # 100종목 너머를 잴 때 폐지 풀이 모자라 혼합 비율이 조용히 낮아진다.
        _smax = max([int(x) for x in args.sizes.split(",") if x] or [100])
        need = int(max(_smax, 100) * args.dead_frac) + 10
        dt = dead_targets(need)
        dead_dfs, dead_mf, _dd = prep(dt, args.days, f"폐지 풀(요청 {len(dt)})")
        mf.update({c: dead_mf.get(c, set()) for c in dead_dfs})
    if args.axis == "B":
        dt = dead_targets(args.dead)
        dead_dfs, dead_mf, _dd = prep(dt, args.days, f"폐지 풀(요청 {len(dt)})")
        # 폐지 종목은 창 끝까지 데이터가 없다 — 그게 이 감사의 요점이다.
        keep = list(dead_dfs)[:args.dead]
        dead_dfs = {c: dead_dfs[c] for c in keep}
        mf.update({c: dead_mf.get(c, set()) for c in keep})
        print(f"[준비] 폐지 풀 확정 {len(dead_dfs)}종목")

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    allf = dict(dfs); allf.update(dead_dfs); allf.update(ext_dfs)
    status = pb.precompute_status(allf, thresholds)
    print(f"[준비] 거래일 {len(dates)}일 ({dates[0]}~{dates[-1]})")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or d < cut]
    tail = [d for d in dates if cut and cut != "0" and d >= cut]
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    live_codes = list(dfs)
    dead_codes = list(dead_dfs)
    ext_codes = list(ext_dfs)
    pool_a = live_codes + ext_codes
    if args.axis == "A":
        sizes = [int(x) for x in args.sizes.split(",") if x]
        sizes = [s for s in sizes if s <= len(pool_a)]
        arms = []
        for s in sizes:
            tag = " (현행)" if s == len(live_codes) else (" +확장" if s > len(live_codes) else "")
            arms.append((f"{s}종목{tag}", s, []))
        base_label = next((l for l, s, _ in arms if s == len(live_codes)), arms[-1][0])
    else:
        arms = [(f"현행풀·{lbl}", args.sample, ov) for lbl, ov in DIALS]
        arms += [(f"폐지포함·{lbl}", args.sample, ov) for lbl, ov in DIALS]
        base_label = "현행풀·현행"

    all_results = {}
    total = len(windows) * args.seeds * args.trials * len(arms)
    done = 0
    for wname, wdates in windows:
        res = {lbl: [] for lbl, _s, _o in arms}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                # 같은 시행 안에서는 같은 난수열을 쓰되, 팔마다 필요한 만큼 뽑는다.
                seedv = rng.random()
                for lbl, n, ov in arms:
                    r2 = random.Random(int(seedv * 1e9) + n)
                    pool = pool_a if args.axis == "A" else live_codes
                    if args.axis == "B" and lbl.startswith("폐지포함"):
                        pool = live_codes + dead_codes
                    if args.axis == "A" and args.dead_frac > 0 and dead_codes:
                        nd = min(int(round(n * args.dead_frac)), len(dead_codes))
                        pick = (r2.sample(dead_codes, nd)
                                + r2.sample(pool, min(n - nd, len(pool))))
                    else:
                        pick = r2.sample(pool, min(n, len(pool)))
                    sd = {c: allf[c] for c in pick}
                    sc = {c: status[c] for c in pick}
                    sm = {c: mf.get(c, set()) for c in pick}
                    prev = apply(ov)
                    try:
                        r = pb.run_portfolio(sd, sc, wdates,
                                             initial_capital=args.seed_capital, slots=slots,
                                             market_filter_dates=sm,
                                             risk_scale_by_date=new_scale_fn())
                    finally:
                        apply(prev)
                    res[lbl].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_results[wname] = res
    print(" " * 60, end="\r")

    W = 118
    title = ("유니버스 크기 — 몇 종목을 보고 있어야 하는가"
             if args.axis == "A" else
             f"생존 편향 — 폐지 {len(dead_codes)}종목을 섞으면 결론이 바뀌는가")
    print(f"\n{'=' * W}\n{title} — {args.trials}회 × 씨드 {args.seeds}개 (기준선 {base_label})\n{'=' * W}")
    for wname, wdates in windows:
        res = all_results[wname]
        base = res[base_label]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'팔':<18}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'상위10%':>9}"
              f"{'최대':>9}{'>30%':>6}{'최악%':>8}{'≤-30%':>7}{'현금%':>7}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for lbl, _n, _o in arms:
            rs = res[lbl]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = lbl == base_label
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{lbl:<18}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('worst'):>8.1f}{m('loss30'):>7.0f}{m('cash'):>7.1f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    if args.axis == "A":
        print("[읽는 법] 종목을 늘려도 성과가 그대로면 커버리지는 이미 충분하다. 늘수록 좋아지면 관심종목을 늘려라.")
        print("          현금% 가 함께 내려가는지 볼 것 — 슬롯을 못 채우는 것이 진짜 병목이면 여기서 드러난다.")
    else:
        print("[읽는 법] 절대 성과 차이 = 생존 프리미엄. 다이얼 순위가 두 풀에서 같으면 기존 결론은 안전하다.")
        print("[한계] 폐지 종목은 창 중간에 데이터가 끝난다. 그 종목이 뽑힌 시행은 실질 유니버스가 작아진다.")


if __name__ == "__main__":
    main()
