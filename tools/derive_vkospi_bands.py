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

[결론 2026-08-10] **밴드를 분리하지 않는다.** 2011~2025 15년(3,688봉)에서 VIX 임계값
15/20/30/40 을 퍼센타일로 옮기면 V코스피200 은 14.9/19.7/28.2/39.1 이다 — 현행 공통 밴드와
2p 안쪽이고, 꼬리(p99)도 40.56 대 41.47 로 사실상 같다. 지수 두 개를 위해 규칙을 둘로 늘릴
근거가 없다.

[그 결론에 이르기까지 틀렸던 것] 처음엔 전 구간(2026 포함)으로 계산해 "15/20/35/74, 최대
34.2p 어긋나므로 분리가 맞다"는 답을 냈다. 완전히 반대였다. 2026 년은 한국 시장에 전례가
없는 국면이라(연중앙 61.58, 40 이상 126 일 — 나머지 15 년 합계가 42 일) 그 한 해가 분포의
꼬리를 통째로 만든다. 50 이상인 날의 90%, 60 이상인 날의 93% 가 거기서 나왔다. 그 창으로
뽑은 밴드는 '지금 국면'을 설명할 뿐 평시를 못 읽는다.

그래서 이 도구는 전 구간과 이상국면 제외 구간을 **함께** 출력하고, 둘이 갈리면 제외 구간을
채택하도록 만들었다. 밴드는 '지금이 얼마나 드문가'가 아니라 '이 수준이 무슨 뜻인가'를
말해야 하고, 그 기준선은 정상 기간에서만 나온다.

읽기 전용이다. 주문도 설정 변경도 하지 않는다.

[실행 모드] 기본은 mode 4(가상투자)다. mode 4 는 KIS **실전 시세**를 VIRT_APP_KEY 로 받으므로
실계좌를 건드리지 않고 0503 을 읽을 수 있고, 무엇보다 라즈베리파이에서 돌고 있는 인스턴스와
같은 앱키·같은 토큰 캐시를 쓴다 — 디스크에 유효한 토큰이 있으면 새로 발급받지 않는다.
다른 앱키(mode 2)로 돌리면 토큰을 새로 받게 되고, 그건 검증 중인 인스턴스를 건드리는 일이다.
지수 일봉 몇 페이지만 읽으므로 TPS 부담은 무시할 수준이다.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import api  # noqa: E402

VKOSPI_INDEX_CODE = "0503"          # KIS 업종코드 — V코스피200
VIX_THRESHOLDS = (15.0, 20.0, 30.0, 40.0)
BAND_LABELS = ("안정", "경계 진입", "위험 구간", "공포/패닉", "시스템 위기")


def _vkospi_series(years):
    """V코스피200 일봉을 years 년치까지 거슬러 받는다.

    [왜 api.get_domestic_index_chart 를 쓰지 않는가] 그 함수는 화면·지표용이라 300봉(약 1.2년)
    에서 끊는다. 런타임에는 맞는 상한이지만 분포의 꼬리를 봐야 하는 밴드 산출에는 못 쓴다.
    같은 TR·같은 파라미터를 쓰되 상한만 걷어낸다.

    [KIS 업종 일봉의 성질] 요청 구간이 아무리 길어도 **한 번에 50봉**만, 그것도 DATE_2 에서
    거슬러 최근 50봉만 준다. 그래서 DATE_2 를 받은 것의 최고참 하루 전으로 밀며 반복한다.

    [처음에 잘못했던 것] TPS 거부(EGW00201)로 빈 응답이 오는 것을 '더 이상 과거가 없다'로
    읽어 200봉에서 멈췄다. 그 200봉이 전부 지금의 극단 국면이라, 그대로 뒀으면 '평시'를
    패닉 수준으로 잡는 밴드가 나왔을 것이다. 거부는 재시도로 넘기고, 호출 사이를 띄운다.
    """
    from datetime import datetime, timedelta
    import constants

    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_CHART"]
    floor_date = (datetime.now() - timedelta(days=int(years * 365.25))).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    items, empty_streak, pages = [], 0, 0
    while pages < 400 and empty_streak < 3:
        pages += 1
        data = api.call_api(url_path, "domestic", "quotations", "index_chart",
                            params={"FID_COND_MRKT_DIV_CODE": "U",
                                    "FID_INPUT_ISCD": VKOSPI_INDEX_CODE,
                                    "FID_INPUT_DATE_1": floor_date,
                                    "FID_INPUT_DATE_2": end_date,
                                    "FID_PERIOD_DIV_CODE": "D"},
                            tr_id="FHKUP03500100", retries=2)
        rows = [it for it in ((data or {}).get("output2") or []) if it.get("stck_bsop_date")]
        if not rows:
            empty_streak += 1
            time.sleep(1.0)
            continue
        empty_streak = 0
        items.extend(rows)
        oldest = min(r["stck_bsop_date"] for r in rows)
        print(f"    … {len(items):,}봉 (최고참 {oldest})", end="\r", flush=True)
        if oldest <= floor_date:
            break
        end_date = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        time.sleep(0.35)   # 업종 TR은 TPS 거부가 잦다 — 재시도보다 간격이 싸다

    print(" " * 70, end="\r")
    if not items:
        return None
    df = pd.DataFrame(items).drop_duplicates(subset=["stck_bsop_date"])
    s = pd.to_numeric(df["bstp_nmix_prpr"], errors="coerce")
    s.index = pd.to_datetime(df["stck_bsop_date"], format="%Y%m%d", errors="coerce")
    return s.dropna()[lambda x: x > 0].sort_index()


def _vix_series(start, end):
    import yfinance as yf
    df = yf.download("^VIX", start=start, end=end, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return pd.to_numeric(s, errors="coerce").dropna()


def _derive(vk, vix, tag, quiet=False):
    """VIX 임계값의 퍼센타일을 V코스피200 분포에 얹는다. 반환은 대응 임계값 리스트."""
    if not quiet:
        print(f"\n===== {tag}")
        print(f"  V코스피200 {len(vk):,}봉 {vk.index.min().date()}~{vk.index.max().date()}"
              f"  ·  VIX {len(vix):,}봉")
        print(f"  {'':12}{'중앙':>8}{'p90':>8}{'p99':>8}{'최대':>8}")
        for nm, x in (("V코스피200", vk), ("VIX", vix)):
            print(f"  {nm:12}{x.median():8.2f}{x.quantile(.90):8.2f}"
                  f"{x.quantile(.99):8.2f}{x.max():8.2f}")
    out = []
    for thr in VIX_THRESHOLDS:
        pct = float((vix < thr).mean())
        mapped = float(np.quantile(vk.values, pct)) if pct < 1.0 else float(vk.max())
        out.append(mapped)
        if not quiet:
            note = "   ← 기간 내 미도달(외삽)" if pct >= 0.999 else ""
            print(f"    VIX {thr:>2.0f} = {pct * 100:5.1f}pct  →  V코스피200 {mapped:6.2f}{note}")
    if not quiet:
        print("  대응 밴드: " + " / ".join(f"{x:.0f}" for x in out))
    return out


def main():
    ap = argparse.ArgumentParser(description="VIX vs V코스피200 색상 밴드 실측")
    ap.add_argument("--mode", default="4", choices=["1", "2", "4"],
                    help="투자 모드 (기본 4=가상투자: KIS 실전 시세 + 실행 중 인스턴스와 토큰 공유)")
    ap.add_argument("--years", type=float, default=15.0, help="조회 기간(년)")
    ap.add_argument("--exclude-from", default="2026-01-01",
                    help="이상국면 시작일. 이 이후를 뺀 구간이 밴드의 기준선이 된다.")
    args = ap.parse_args()

    # 프로그램이 기동 시 하는 자격 증명 로드를 도구도 똑같이 거쳐야 한다
    #  (없으면 EGW00104 'AppSecret은 필수입니다'로 떨어진다).
    config.session.initialize(args.mode)

    print(f"\n[대상] V코스피200(업종 {VKOSPI_INDEX_CODE}) vs VIX(^VIX)")
    if config.session.is_toss:
        print("❌ 토스 모드는 V코스피200 대체 소스가 없다. 실전 시세 계좌(mode 2/4)로 실행할 것.")
        return 1

    print(f"  V코스피200 일봉 수집 중(최대 {args.years:.0f}년, 호출당 50봉)…")
    vk = _vkospi_series(args.years)
    if vk is None or len(vk) < 500:
        print(f"❌ 표본 부족 ({0 if vk is None else len(vk)}봉). 밴드 산출에는 최소 500봉이 필요하다.")
        return 1

    vix_all = _vix_series(vk.index.min(), vk.index.max() + pd.Timedelta(days=1))
    if vix_all is None or len(vix_all) < 500:
        print("❌ VIX 조회 실패.")
        return 1

    def _pair(a):
        # 같은 기간에서만 비교한다 — 서로 다른 창을 쓰면 국면 차이가 분포 차이로 둔갑한다.
        b = vix_all[(vix_all.index >= a.index.min()) & (vix_all.index <= a.index.max())]
        return a, b

    full = _derive(*_pair(vk), tag="A. 전 구간 (이상국면 포함)")

    cut = pd.Timestamp(args.exclude_from)
    normal = vk[vk.index < cut]
    if len(normal) < 500:
        print(f"\n⚠️  {cut.date()} 이전 표본이 부족해 이상국면 제외 구간을 낼 수 없다.")
        return 0
    base = _derive(*_pair(normal), tag=f"B. {cut.date()} 이전 (기준선)")

    # 이상국면이 꼬리를 얼마나 지배하는지 — 두 창이 갈리는 이유를 숫자로 보인다.
    recent = vk[vk.index >= cut]
    if len(recent):
        print(f"\n[이상국면 지배력] {cut.date()} 이후는 전체의 {len(recent) / len(vk) * 100:.1f}%"
              f" 인데, 임계 초과일의 상당수가 거기서 나온다")
        for thr in (30, 40, 50, 60):
            tot = int((vk >= thr).sum())
            if tot:
                print(f"    {thr} 이상 {tot:4d}일 중 {int((recent >= thr).sum()):4d}일"
                      f" ({(recent >= thr).sum() / tot * 100:3.0f}%)")

    print("\n" + "=" * 70)
    print("  VIX (현행)      : " + " / ".join(f"{t:.0f}" for t in VIX_THRESHOLDS))
    print("  V코스피200 (A)  : " + " / ".join(f"{x:.0f}" for x in full) + "   ← 채택하지 말 것")
    print("  V코스피200 (B)  : " + " / ".join(f"{x:.0f}" for x in base) + "   ← 기준선")
    print("=" * 70)

    gap = max(abs(m - t) for m, t in zip(base, VIX_THRESHOLDS))
    if gap < 3.0:
        print(f"\n기준선 기준 최대 {gap:.1f}p 차이 — 공통 밴드를 유지한다."
              f" 지수 두 개를 위해 규칙을 둘로 늘릴 근거가 없다.")
    else:
        print(f"\n기준선 기준 최대 {gap:.1f}p 어긋난다 — 밴드를 분리하는 것이 맞다.")
    print("\n[읽는 법] A 와 B 가 갈리면 B 를 쓴다. 밴드는 '지금이 얼마나 드문가'가 아니라")
    print("          '이 수준이 무슨 뜻인가'를 말해야 하고, 그 기준선은 정상 기간에서만 나온다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
