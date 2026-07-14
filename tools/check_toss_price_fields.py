#!/usr/bin/env python3
"""
국내 종목 현재가/기준가(전일 종가) 원본 응답 확인 도구.

목적: mode 3(TOSS)의 등락률 기준가가 mode 2(KIS)와 왜 다른지 규명한다.
  - TOSS /prices·/stocks·/price-limits 원본 응답에 '전일 정규장 종가' 또는
    '등락률' 필드가 있는지 확인한다. (있으면 역산 로직을 폐기하고 그대로 쓰면 됨)
  - 우리 코드가 만들어내는 가공 결과(get_current_price_data, _toss_base_price)도
    나란히 찍어 어디서 값이 어긋나는지 비교한다.

사용법:
  python tools/check_toss_price_fields.py 3                  # TOSS(mode 3), 기본 샘플
  python tools/check_toss_price_fields.py 2                  # KIS 실전(mode 2), 비교용
  python tools/check_toss_price_fields.py 3 105560 000660    # 종목 직접 지정

결과 전체를 그대로 복사해 붙여주시면 원인 분석에 사용합니다.
"""
import sys
import os
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console

console = Console()

# 기본 샘플: 문제 케이스 모음
#  105560 KB금융(mode3 기준가), 000660 SK하이닉스(mode3 하락률 과장),
#  000720 현대건설(mode2 현재가 stale·강도만 갱신 의심), 003490 대한항공(NXT 미지원→장전 0%),
#  005930 삼성전자(역산 실패→yfinance 폴백 대조군)
DEFAULT_CODES = ["105560", "000660", "000720", "003490", "005930"]


def _dump(label, obj):
    console.print(f"[bold cyan]{label}[/bold cyan]")
    try:
        console.print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    except Exception as e:
        console.print(f"(직렬화 실패: {e}) -> {obj!r}")
    console.print("")


def dump_toss(code):
    import toss_api
    console.rule(f"[bold]TOSS {code}")

    # 1) 원본 API 응답 (전일종가/등락률 필드 존재 여부 확인이 핵심)
    for name, fn in (
        ("toss_api.get_price (/api/v1/prices)", lambda: toss_api.get_price(code)),
        ("toss_api.get_stock (/api/v1/stocks)", lambda: toss_api.get_stock(code)),
        ("toss_api.get_price_limit (/api/v1/price-limits)", lambda: toss_api.get_price_limit(code)),
    ):
        try:
            _dump(name, fn())
        except Exception as e:
            console.print(f"[red]{name} 오류: {e}[/red]\n")

    # 2) 우리 코드의 가공 결과
    try:
        _dump("api.get_current_price_data(code, False) → output",
              (api.get_current_price_data(code, False) or {}).get('output'))
    except Exception as e:
        console.print(f"[red]get_current_price_data 오류: {e}[/red]\n")

    try:
        console.print(f"[bold cyan]api._toss_base_price(code) (역산 기준가)[/bold cyan] = "
                      f"{api._toss_base_price(code)}\n")
    except Exception as e:
        console.print(f"[red]_toss_base_price 오류: {e}[/red]\n")


def _kis_price_raw(code, market_div):
    """KIS inquire-price 원본 주요필드 (market_div='J'=KRX, 'NX'=NXT)."""
    import constants
    raw = api.call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"],
                       "domestic", "quotations", "price",
                       params={"fid_cond_mrkt_div_code": market_div, "fid_input_iscd": code},
                       timeout=3, retries=0)
    o = (raw or {}).get('output', {}) or {}
    return {
        "rt_cd": (raw or {}).get('rt_cd'), "msg": (raw or {}).get('msg1', ''),
        "stck_prpr": o.get('stck_prpr'), "stck_sdpr": o.get('stck_sdpr'),
        "prdy_vrss": o.get('prdy_vrss'), "prdy_ctrt": o.get('prdy_ctrt'),
        "acml_vol": o.get('acml_vol'), "stck_oprc": o.get('stck_oprc'),
    }


def _kis_vol_raw(code, market_div):
    """KIS inquire-ccnl(체결강도) 원본 tday_rltv (market_div='J'/'NX')."""
    import constants
    raw = api.call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"],
                       "domestic", "quotations", "vol_strength",
                       params={"FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": code},
                       timeout=3, retries=0)
    items = (raw or {}).get('output', []) or []
    return {"rt_cd": (raw or {}).get('rt_cd'), "msg": (raw or {}).get('msg1', ''),
            "tday_rltv": (items[0].get('tday_rltv') if items else None)}


def dump_kis(code):
    from datetime import datetime as _dt
    console.rule(f"[bold]KIS {code}")
    try:
        phase = api._nxt_quote_phase()
    except Exception as e:
        phase = f"(err {e})"
    console.print(f"시각={_dt.now().strftime('%H:%M:%S')}  _nxt_quote_phase={phase} "
                  f"(active=NXT시간 08:00~09:00·15:30~20:00 / skip=정규장 / offhours=야간)\n")

    # 1) 오버뷰가 실제로 쓰는 가공 결과 (현재가·기준가·등락률)
    try:
        _dump("api.get_current_price_data(include_nxt=True) → output",
              (api.get_current_price_data(code, False, include_nxt=True) or {}).get('output'))
    except Exception as e:
        console.print(f"[red]get_current_price_data 오류: {e}[/red]\n")

    # 2) KRX(J) vs NXT(NX) 현재가 원본 — 어느 쪽이 신선한지 대조
    try:
        _dump("KRX inquire-price(J) 원본", _kis_price_raw(code, "J"))
    except Exception as e:
        console.print(f"[red]KRX(J) 원본 오류: {e}[/red]\n")
    try:
        _dump("NXT inquire-price(NX) 원본", _kis_price_raw(code, "NX"))
    except Exception as e:
        console.print(f"[red]NXT(NX) 원본 오류: {e}[/red]\n")

    # 3) 현재가 NXT 병합에 실제로 쓰는 헬퍼 (0이면 KRX 전일종가로 폴백 → 0% 원인)
    try:
        console.print(f"[bold cyan]api.fetch_nxt_price(code)[/bold cyan] = {api.fetch_nxt_price(code)} "
                      f"[dim](0이면 현재가가 KRX 전일종가로 폴백)[/dim]\n")
    except Exception as e:
        console.print(f"[red]fetch_nxt_price 오류: {e}[/red]\n")

    # 4) 멀티시세(정규장 경로) — 종목별 stck_prpr/stck_sdpr 확인
    try:
        api._MULTI_PRICE_DISABLED = False
        m = api.get_multi_current_prices([code]) or {}
        _dump("get_multi_current_prices → output", m.get(code))
    except Exception as e:
        console.print(f"[red]멀티시세 오류: {e}[/red]\n")

    # 5) 체결강도 KRX(J) vs NXT(NX) 원본 — 강도는 신선한데 가격만 stale인지 대조
    try:
        _dump("체결강도 KRX(J) 원본", _kis_vol_raw(code, "J"))
        _dump("체결강도 NXT(NX) 원본", _kis_vol_raw(code, "NX"))
        console.print(f"[bold cyan]api.get_realtime_vol_strength(code)[/bold cyan] = "
                      f"{api.get_realtime_vol_strength(code)}\n")
    except Exception as e:
        console.print(f"[red]체결강도 오류: {e}[/red]\n")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "3"
    codes = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_CODES

    config.session.initialize(mode=mode)
    server = {"1": "모의투자(KIS)", "2": "실전투자(KIS)", "3": "토스증권(TOSS)"}.get(mode, mode)
    console.print(f"\n[bold]현재가/기준가 원본 필드 확인 — {server} / 종목: {', '.join(codes)}[/bold]\n")

    for code in codes:
        if config.session.is_toss:
            dump_toss(code)
        else:
            dump_kis(code)

    if config.session.is_toss:
        console.print("[dim]※ TOSS 응답에 전일 정규장 종가/등락률 필드가 없으므로 역산/저장/yfinance로 기준가를 확보한다.[/dim]")
    else:
        console.print("[dim]※ KIS: '현재가 stale + 강도 갱신' 종목은 KRX(J)/NXT(NX) 현재가 원본과 fetch_nxt_price를 "
                      "대조하라. NXT(NX) 원본 stck_prpr는 살아있는데 fetch_nxt_price=0 이거나, 멀티시세 stck_prpr가 "
                      "전일종가면 그 지점이 stale 원인이다.[/dim]")


if __name__ == "__main__":
    main()
