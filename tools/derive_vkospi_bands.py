"""VIX 와 V코스피200 의 색상 밴드를 같이 써도 되는지 실측한다.

[왜 이 도구인가] 화면상 두 지수는 하나의 밴드(15/20/30/40)를 공유한다. 같은 '변동성
지수'라는 이름을 공유할 뿐, 기초자산도 시장도 다르므로 같은 숫자가 같은 의미일 근거는
없다. 근거 없이 숫자를 옮겨 적는 대신, 두 분포를 **같은 기간**에서 비교해 확인한다.

[방법] 디젤 ULSD 밴드를 뽑을 때와 같다. VIX 의 기존 임계값이 그 분포에서 몇 퍼센타일에
해당하는지 구하고, 그 퍼센타일을 V코스피200 분포에 그대로 얹는다. 절대 수준이 아니라
'얼마나 드문 상태인가'를 맞추는 방식이다.

[한계] V코스피200 은 무료 소스에 없다 — yfinance·FinanceDataReader 는 티커 자체가 없고,
pykrx 와 KRX 데이터 API 는 로그인을 요구하며, TradingView 에는 상장폐지된 선물(KRX:VKI)만
있고 현물은 없다. 그래서 KIS 업종코드 0503 을 쓴다(실전 시세 계좌 필요, mode 2 또는 4).
조회 구간은 KIS 업종 일봉이 주는 만큼이며, 그 기간에 패닉 국면이 없으면 상단 밴드
(30/40 대응)는 외삽이다 — 출력에 그 사실을 함께 찍는다.

읽기 전용이다. 주문도 설정 변경도 하지 않는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import api  # noqa: E402

VKOSPI_INDEX_CODE = "0503"          # KIS 업종코드 — V코스피200
VIX_THRESHOLDS = (15.0, 20.0, 30.0, 40.0)
BAND_LABELS = ("안정", "경계 진입", "위험 구간", "공포/패닉", "시스템 위기")


def _vkospi_series():
    df = api.get_domestic_index_chart(VKOSPI_INDEX_CODE)
    if df is None or df.empty:
        return None
    col = "close" if "close" in df.columns else df.columns[-1]
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    s.index = pd.to_datetime(df.loc[s.index, "date"], errors="coerce")
    return s[s > 0].sort_index()


def _vix_series(start, end):
    import yfinance as yf
    df = yf.download("^VIX", start=start, end=end, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return pd.to_numeric(s, errors="coerce").dropna()


def main():
    print(f"\n[대상] V코스피200(업종 {VKOSPI_INDEX_CODE}) vs VIX(^VIX) — 같은 기간 분포 비교")
    if config.session.is_toss:
        print("❌ 토스 모드는 V코스피200 대체 소스가 없다. 실전 시세 계좌(mode 2/4)로 실행할 것.")
        return 1

    vk = _vkospi_series()
    if vk is None or len(vk) < 100:
        print(f"❌ V코스피200 조회 실패 또는 표본 부족 ({0 if vk is None else len(vk)}봉).")
        return 1

    start, end = vk.index.min(), vk.index.max()
    vix = _vix_series(start, end + pd.Timedelta(days=1))
    if vix is None or len(vix) < 100:
        print("❌ VIX 조회 실패.")
        return 1
    # 같은 기간에서만 비교한다 — 서로 다른 창을 쓰면 국면 차이가 분포 차이로 둔갑한다.
    vix = vix[(vix.index >= start) & (vix.index <= end)]

    print(f"  기간 {start.date()} ~ {end.date()}  ·  V코스피200 {len(vk)}봉 · VIX {len(vix)}봉\n")
    print(f"{'':14}{'현재':>8}{'중앙':>8}{'평균':>8}{'p90':>8}{'p99':>8}{'최대':>8}")
    for nm, s in (("V코스피200", vk), ("VIX", vix)):
        print(f"{nm:14}{s.iloc[-1]:8.2f}{s.median():8.2f}{s.mean():8.2f}"
              f"{s.quantile(.90):8.2f}{s.quantile(.99):8.2f}{s.max():8.2f}")

    print("\n[퍼센타일 매칭] VIX 임계값이 몇 퍼센타일인지 → 같은 퍼센타일의 V코스피200 값")
    print(f"{'VIX 기준':>10}{'VIX 백분위':>12}{'→ V코스피200':>14}   판정")
    derived = []
    for thr, label in zip(VIX_THRESHOLDS, BAND_LABELS[1:]):
        pct = float((vix < thr).mean())
        mapped = float(np.quantile(vk.values, pct)) if pct < 1.0 else float(vk.max())
        derived.append((thr, pct, mapped))
        note = "" if pct < 0.999 else "  ← 기간 내 미도달(외삽)"
        print(f"{thr:10.0f}{pct * 100:11.1f}%{mapped:14.2f}   {label}{note}")

    print("\n[제안 밴드]")
    print("  VIX        : " + " / ".join(f"{t:.0f}" for t in VIX_THRESHOLDS) + "  (현행 유지)")
    print("  V코스피200 : " + " / ".join(f"{m:.0f}" for _t, _p, m in derived))

    spread = max(abs(m - t) for t, _p, m in derived)
    if spread < 1.5:
        print(f"\n두 분포의 임계값 차이가 최대 {spread:.1f}p 다 — 공통 밴드를 유지해도 무방하다.")
    else:
        print(f"\n임계값이 최대 {spread:.1f}p 어긋난다 — 밴드를 분리하는 것이 맞다.")
    print("\n[주의] 위 결과는 조회된 기간의 분포에만 근거한다. 그 기간에 패닉이 없었다면")
    print("       상단 밴드는 외삽이므로, '미도달(외삽)' 표시가 붙은 값은 그대로 쓰지 말 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
