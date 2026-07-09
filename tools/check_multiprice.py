#!/usr/bin/env python3
"""
관심종목 멀티시세(FHKST11300006, intstock-multprice) 지원 여부 및 응답 필드 확인 도구.

메뉴2(종목 시세 분석)의 국내 현재가 일괄 조회(get_multi_current_prices)가
현재 접속 서버(모의/실전)에서 동작하는지, 원본 응답 필드가 코드의 정규화 매핑과
일치하는지 검증한다. 개별 현재가 API와 값도 나란히 비교한다.

사용법: python tools/check_multiprice.py [mode]
  mode: 1=모의투자(기본), 2=실전투자
"""
import sys
import os
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console
from rich.table import Table

console = Console()

# 비교 대상 샘플 (삼성전자, 카카오)
SAMPLE_CODES = ["005930", "035720"]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "1"
    config.session.initialize(mode=mode)
    server = "모의투자(VTS)" if config.session.is_simulation else "실전투자"
    console.print(f"\n[bold]관심종목 멀티시세 확인 — {server}[/bold]\n")

    # 1. 원본(raw) 응답 확인
    params = {}
    for j, c in enumerate(SAMPLE_CODES, start=1):
        params[f"FID_COND_MRKT_DIV_CODE_{j}"] = "J"
        params[f"FID_INPUT_ISCD_{j}"] = c
    raw = api.call_api("uapi/domestic-stock/v1/quotations/intstock-multprice",
                       "domestic", "quotations", "multi_price", params=params,
                       tr_id="FHKST11300006", timeout=5, retries=1)
    console.print(f"rt_cd = {raw.get('rt_cd')} | msg = {raw.get('msg1', '')}")
    outputs = raw.get('output') or raw.get('output1') or []
    if not outputs:
        console.print("[red]output이 비어 있습니다. 이 서버에서는 멀티시세 TR이 미지원일 수 있습니다.[/red]")
        console.print("[dim]→ 프로그램은 자동으로 종목별 현재가 조회로 폴백하므로 동작에는 문제 없습니다.[/dim]")
        console.print(json.dumps(raw, indent=2, ensure_ascii=False)[:2000])
        return
    console.print(f"\n[bold]원본 응답 필드 (첫 종목):[/bold]")
    console.print(json.dumps(outputs[0], indent=2, ensure_ascii=False))

    # 2. 정규화 결과 vs 개별 현재가 API 비교
    api._MULTI_PRICE_DISABLED = False
    multi = api.get_multi_current_prices(SAMPLE_CODES)
    if not multi:
        console.print("[red]get_multi_current_prices가 None을 반환했습니다 (정규화 실패).[/red]")
        return

    table = Table(title="멀티시세 vs 개별 현재가 비교", show_header=True)
    table.add_column("종목")
    table.add_column("필드")
    table.add_column("멀티시세", justify="right")
    table.add_column("개별 현재가", justify="right")
    fields = ["stck_prpr", "prdy_vrss", "prdy_ctrt", "stck_sdpr", "acml_vol", "rprs_mrkt_kor_name"]
    for c in SAMPLE_CODES:
        single = api.get_current_price_data(c, is_overseas=False, include_nxt=False)
        s_out = single.get('output', {}) if single and single.get('rt_cd') == '0' else {}
        m_out = multi.get(c, {})
        for f in fields:
            table.add_row(c, f, str(m_out.get(f, '-')), str(s_out.get(f, '-')))
    console.print(table)
    console.print("\n[green]멀티시세 사용 가능. 메뉴2 국내 그룹은 현재가를 30종목/1콜로 수집합니다.[/green]")
    console.print("[dim]※ 값 차이는 두 호출 사이의 실시간 체결 변동일 수 있습니다. 필드 자체가 '-'면 매핑 문제입니다.[/dim]")


if __name__ == "__main__":
    main()
