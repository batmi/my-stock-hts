#!/usr/bin/env python3
"""
mode 3(TOSS) 등락률 기준가가 4단 우선순위 중 어디서 끊기는지 특정하는 진단 도구.

목적: 기준가가 mode 2(KIS)와 어긋날 때 원인을 눈으로 확인한다.
  _toss_base_price는 1)랭킹 basePrice → 2)저장된 KRX 마감가 → 3)yfinance 일봉 종가
  → 4)전일 NXT 종가 순으로 단락 평가한다. 3)의 실패는 debug 로그로만 남고 화면에는
  아무 표시가 없어(30분 쿨다운까지 걸린다) 원인을 알 수 없으므로, 단계별 반환값을
  나란히 찍어 어디서 폴백이 일어났는지 드러낸다.

사용법:
  python tools/check_toss_base_price.py 3                    # TOSS(mode 3), 기본 샘플
  python tools/check_toss_base_price.py 3 128940 030200      # 종목 직접 지정
  python tools/check_toss_base_price.py 3 128940=372500      # 기대값(mode 2 기준가) 함께 지정

결과 전체를 그대로 복사해 붙여주시면 원인 분석에 사용합니다.
"""
import sys
import os
import time
import traceback
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api

# 기본 샘플: 2026-07-22 17:21 mode2/mode3 기준가가 어긋났던 종목과 그때의 mode 2 기준가
DEFAULT_CODES = [("128940", 372500), ("373220", 317000), ("064350", 153100),
                 ("030200", 52700), ("055550", 104700), ("052690", 87400)]


def _f(v):
    if isinstance(v, (int, float)) and v:
        return f"{v:,.0f}"
    return str(v) if v else "-"


def _parse_args(argv):
    """[mode] [code | code=기대값 ...] 파싱."""
    args = list(argv[1:])
    if args and args[0] in ("1", "2", "3"):
        args.pop(0)          # 모드는 실행 스크립트(run.sh)가 이미 정한다. 위치만 흡수.
    if not args:
        return DEFAULT_CODES
    out = []
    for a in args:
        code, _, exp = a.partition("=")
        try:
            out.append((code.strip(), float(exp) if exp else None))
        except ValueError:
            out.append((code.strip(), None))
    return out


def main():
    targets = _parse_args(sys.argv)

    print("=" * 78)
    print(f"실행 시각  : {datetime.now()}")
    print(f"파이썬     : {sys.version.split()[0]}  ({sys.platform})")
    for mod in ("yfinance", "pandas"):
        try:
            m = __import__(mod)
            print(f"{mod:10s} : {getattr(m, '__version__', '?')}")
        except Exception as e:
            print(f"{mod:10s} : ★ import 실패 → {e}")
    print(f"기준가 폴백: _toss_yf_krx_close 존재 = {hasattr(api, '_toss_yf_krx_close')}")
    print(f"TOSS 모드  : {config.session.is_toss}")
    if not config.session.is_toss:
        print("  ※ TOSS 모드가 아니면 일봉·랭킹 조회가 비어 결과가 달라집니다.")
    print("=" * 78)

    # 기준일(전 거래일) — _toss_base_price 와 동일한 규칙으로 산출
    ref = None
    try:
        df = api._toss_cached_daily_chart(targets[0][0])
        if df is not None and len(df) >= 2:
            today = datetime.now().strftime("%Y%m%d")
            last = str(df.iloc[-1]["date"]).replace("-", "")[:8]
            prev = str(df.iloc[-2]["date"]).replace("-", "")[:8]
            ref = last if last < today else prev
    except Exception as e:
        print(f"[일봉 조회 실패] {e}")
    if not ref:
        ref = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        print(f"  ※ 일봉으로 기준일을 못 구해 어제({ref})로 가정합니다.")
    print(f"기준일(ref_date) = {ref}\n")

    # [1] yfinance 원본 응답 — 3순위가 통째로 죽었는지 먼저 가른다
    print("-" * 78)
    print(f"[1] yfinance 원본 응답 ({targets[0][0]}.KS)")
    try:
        s = (datetime.strptime(ref, "%Y%m%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        e = (datetime.strptime(ref, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        t0 = time.time()
        raw = api.fetch_yfinance_data(f"{targets[0][0]}.KS", start=s, end=e)
        el = time.time() - t0
        if raw is None:
            print(f"    ★ None 반환 ({el:.1f}s)")
        elif getattr(raw, "empty", True):
            print(f"    ★ 빈 DataFrame ({el:.1f}s) — 레이트리밋/네트워크 차단 의심")
        else:
            print(f"    OK ({el:.1f}s)  rows={len(raw)}  columns={list(raw.columns)[:6]}")
            print(raw.tail(3))
    except Exception:
        print("    ★ 예외 발생:")
        traceback.print_exc()

    # [2] 종목별 4단 우선순위 추적
    print("-" * 78)
    print("[2] 우선순위별 추적  (① 랭킹 → ② 저장 → ③ yfinance → 최종)")
    print(f"    {'종목':>8s} {'①랭킹':>11s} {'②저장':>11s} {'③yfinance':>11s} "
          f"{'최종':>11s} {'기대(mode2)':>12s}  판정")
    for code, expect in targets:
        try:
            p1 = api._toss_ranking_base(code)
        except Exception as ex:
            p1 = f"ERR {ex}"
        p2 = api._toss_krx_close_get(code, ref)
        cooling = ""
        try:
            with api._toss_yf_base_lock:
                last = api._toss_yf_base_miss.get((code, ref))
            if last and (time.time() - last) < api._TOSS_YF_BASE_RETRY_SEC:
                cooling = f"  (쿨다운 {api._TOSS_YF_BASE_RETRY_SEC - int(time.time() - last)}s 남음)"
        except Exception:
            pass
        try:
            p3 = api._toss_yf_krx_close(code, ref)
        except Exception as ex:
            p3 = f"ERR {ex}"
        try:
            fin = api._toss_base_price(code)
        except Exception as ex:
            fin = f"ERR {ex}"
        if expect is None:
            verdict = ""
        else:
            verdict = "일치" if fin == expect else "★불일치"
        print(f"    {code:>8s} {_f(p1):>11s} {_f(p2):>11s} {_f(p3):>11s} "
              f"{_f(fin):>11s} {_f(expect):>12s}  {verdict}{cooling}")

    print("-" * 78)
    print("판정 방법:")
    print("  ③이 값을 주는데 최종이 다르면      → 우선순위 로직 문제")
    print("  ③이 '-'이고 [1]이 빈 DataFrame     → yfinance 레이트리밋/네트워크 차단")
    print("  ③이 '-'이고 [1]은 OK               → _toss_yf_krx_close 파싱(컬럼·날짜 매칭) 문제")
    print("  ①이 값을 주는데 그 값이 기대와 다르면 → 랭킹 basePrice 자체가 NXT 기준")


if __name__ == "__main__":
    main()
