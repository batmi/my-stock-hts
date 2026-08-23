# test_unfilled_orders.py
import time
import sys
import os
from datetime import datetime, timedelta

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console

console = Console()

# [추가] 상세 디버깅을 위해 로그 레벨 설정
config.SCREEN_DEBUG_LEVEL = "DEBUG"

def test_simulation_unfilled_orders():
    console.print("[bold cyan]=== 모의투자 미체결 내역 조회 테스트 ===[/bold cyan]")

    # 1. 모의투자 모드로 초기화
    console.print("[dim]시스템 초기화 중...[/dim]")
    # 모의투자 모드('1')로 강제 초기화
    config.session.initialize(mode="1") 
    
    if not config.session.is_simulation:
        console.print("[bold red]오류: 모의투자 모드(1)로 초기화되지 않았습니다.[/bold red]")
        return

    # API 토큰 발급
    console.print("[dim]API 토큰 발급 중...[/dim]")
    if not api.get_access_token():
        console.print("[bold red]토큰 발급 실패. 앱 키/시크릿 설정을 확인하세요.[/bold red]")
        return

    # 2. 테스트 종목 및 가격 설정
    # 삼성전자(005930)를 현재가보다 20% 낮게 주문하여 미체결 유도
    code = "005930" 
    console.print(f"[dim]{code} 현재가 조회 중...[/dim]")
    current_price = api.get_current_price(code, is_overseas=False)
    
    if current_price <= 0:
        console.print(f"[bold red]{code} 현재가 조회 실패. 장 운영 시간이 아니거나 API 오류입니다.[/bold red]")
        return
    
    # 호가 단위 맞추기 (100원 단위로 가정)
    test_price = int(current_price * 0.8) 
    test_price = (test_price // 100) * 100 
    
    qty = 1
    
    console.print(f"\n[yellow]테스트 주문 정보:[/yellow]")
    console.print(f"  - 종목: 삼성전자({code})")
    console.print(f"  - 현재가: {current_price:,}원")
    console.print(f"  - 주문가: {test_price:,}원 (현재가 대비 -20%)")
    console.print(f"  - 수량: {qty}주")
    
    # 3. 주문 전송
    console.print("\n[green]1. 매수 주문 전송 중...[/green]")
    # ord_dvsn="00" (지정가)
    res = api.place_order("domestic", "buy", code, qty, test_price, "00")
    
    # [추가] 주문 응답 전체 출력
    console.print(f"[dim]주문 응답 데이터: {res}[/dim]")
    
    if res['rt_cd'] != '0':
        console.print(f"[bold red]주문 실패: {res['msg1']} (Code: {res.get('msg_cd')})[/bold red]")
        return
        
    odno = res['output']['ODNO']
    console.print(f"[bold green]주문 접수 완료. 주문번호: {odno}[/bold green]")
    
    # 4. 대기 및 반복 조회 (서버 반영 지연 대응)
    console.print("\n[dim]서버 반영 대기 및 조회 (최대 5회 시도)...[/dim]")
    
    unfilled_list = []
    for i in range(5):
        time.sleep(2)
        console.print(f"[dim]  Attempt {i+1}: 조회 중...[/dim]")
        unfilled_list = api.get_unfilled_orders()
        # 주문번호가 리스트에 있는지 확인
        if any(str(o.get('odno')) == str(odno) for o in unfilled_list):
            console.print(f"[green]  => 주문 발견![/green]")
            break
    
    # 5. 미체결 내역 조회 (변경된 로직 검증)
    console.print("\n[green]2. 미체결 내역 조회 (api.get_unfilled_orders)...[/green]")
    # 내부적으로 모의투자일 때 inquire-daily-ccld (VTTC8001R) 호출
    unfilled_list = api.get_unfilled_orders()
    
    found_order = None
    console.print(f"조회된 미체결 건수: {len(unfilled_list)}")
    
    if unfilled_list:
        console.print(f"[dim]조회된 리스트: {unfilled_list}[/dim]")
    else:
        console.print("[dim]반환된 미체결 리스트가 비어있습니다.[/dim]")
    
    for order in unfilled_list:
        r_odno = order.get('odno')
        r_code = order.get('pdno')
        r_qty = int(order.get('rmn_qty', 0))
        
        if str(r_odno) == str(odno):
            found_order = order
            break
            
    if found_order:
        console.print(f"\n[bold cyan]✅ 테스트 성공! 미체결 내역에서 주문을 확인했습니다.[/bold cyan]")
        console.print(f"  - 주문번호: {found_order.get('odno')}")
        console.print(f"  - 종목명: {found_order.get('prdt_name')}")
        console.print(f"  - 주문단가: {int(found_order.get('ord_unpr', 0)):,}원")
        console.print(f"  - 미체결잔량: {found_order.get('rmn_qty')}주")
    else:
        console.print(f"\n[bold red]❌ 테스트 실패: 미체결 내역에서 주문번호({odno})를 찾을 수 없습니다.[/bold red]")
        console.print("[dim]참고: 모의투자 서버 지연이나 API 로직 문제일 수 있습니다.[/dim]")
        
        # [추가] 상세 분석 로직
        console.print("\n[yellow]🔍 상세 분석: 전체 주문 내역(체결+미체결+취소) 조회 시도...[/yellow]")
        
        # 직접 API 호출 (CCLD_DVSN="00" : 전체)
        url = "uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "VTTC8001R"
        today = datetime.now().strftime("%Y%m%d")
        
        params = {
            "CANO": config.session.cano,
            "ACNT_PRDT_CD": config.session.acnt_prdt_cd,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "00", # 00: 전체
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        console.print(f"[dim]요청 파라미터: {params}[/dim]")
        
        try:
            res = api.call_api(url, "domestic", "inquiry", "history", params=params, tr_id=tr_id)
            
            console.print(f"[dim]API 응답 코드: {res.get('rt_cd')}, 메시지: {res.get('msg1')} ({res.get('msg_cd')})[/dim]")
            
            if res.get('rt_cd') == '0':
                all_orders = res.get('output1', [])
                console.print(f"[dim]조회된 전체 주문 건수: {len(all_orders)}[/dim]")
                if all_orders:
                    console.print(f"[dim]첫 번째 주문 샘플: {all_orders[0]}[/dim]")
                
                target = next((o for o in all_orders if str(o.get('odno')) == str(odno)), None)
                
                if target:
                    console.print(f"[cyan]👉 전체 내역에서 주문 발견![/cyan]")
                    console.print(f"  - 주문번호: {target.get('odno')}")
                    console.print(f"  - 종목명: {target.get('prdt_name')}")
                    console.print(f"  - 주문구분: {target.get('sll_buy_dvsn_cd_name')}")
                    console.print(f"  - 주문수량: {target.get('ord_qty')}")
                    console.print(f"  - 체결수량: {target.get('tot_ccld_qty')}")
                    console.print(f"  - 취소수량: {target.get('cncl_cfrm_qty')}")
                    console.print(f"  - 잔량: {target.get('rmn_qty')}")
                    
                    if int(target.get('rmn_qty', 0)) > 0:
                        console.print(f"  => [bold yellow]상태: 미체결 잔량 존재함 (API 필터링 조건 확인 필요)[/bold yellow]")
                    else:
                        console.print(f"  => [bold magenta]상태: 이미 전량 체결되거나 취소됨[/bold magenta]")
                else:
                    console.print(f"[red]전체 내역에서도 주문번호({odno})를 찾을 수 없습니다.[/red]")
            else:
                console.print(f"[red]전체 내역 조회 실패: {res.get('msg1')} ({res.get('msg_cd')})[/red]")
        except Exception as e:
            console.print(f"[red]상세 분석 1 오류: {e}[/red]")

        # [추가] 상세 분석 2: 정정/취소 가능 주문 조회 (TTTC8036R) 시도
        console.print("\n[yellow]🔍 상세 분석 2: 정정/취소 가능 주문 조회 (TTTC8036R) 시도...[/yellow]")
        url_rvse = "uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        # 모의투자용 TR ID가 별도로 없으므로 실전용 ID 사용 시도 (혹시 동작할지 확인)
        tr_id_rvse = "TTTC8036R" 
        
        params_rvse = {
            "CANO": config.session.cano,
            "ACNT_PRDT_CD": config.session.acnt_prdt_cd,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0"
        }
        
        try:
            res_rvse = api.call_api(url_rvse, "domestic", "inquiry", "open_orders", params=params_rvse, tr_id=tr_id_rvse)
            console.print(f"[dim]API 응답 코드: {res_rvse.get('rt_cd')}, 메시지: {res_rvse.get('msg1')}[/dim]")
            
            if res_rvse.get('rt_cd') == '0':
                rvse_list = res_rvse.get('output', [])
                console.print(f"[dim]조회된 정정/취소 가능 건수: {len(rvse_list)}[/dim]")
                target = next((o for o in rvse_list if str(o.get('odno')) == str(odno)), None)
                if target:
                    console.print(f"[cyan]👉 정정/취소 가능 목록에서 주문 발견![/cyan]")
                    console.print(f"  - 주문번호: {target.get('odno')}")
                    console.print(f"  - 잔량: {target.get('psbl_qty')}")
        except Exception as e:
            console.print(f"[red]상세 분석 2 오류: {e}[/red]")

        # [추가] 상세 분석 3: 주문번호(ODNO)로 직접 조회 시도
        console.print(f"\n[yellow]🔍 상세 분석 3: 주문번호({odno})로 직접 조회 시도...[/yellow]")
        
        params_odno = params.copy()
        params_odno["ODNO"] = odno
        
        try:
            res_odno = api.call_api(url, "domestic", "inquiry", "history", params=params_odno, tr_id=tr_id)
            console.print(f"[dim]API 응답 코드: {res_odno.get('rt_cd')}, 메시지: {res_odno.get('msg1')}[/dim]")
            
            if res_odno.get('rt_cd') == '0':
                odno_list = res_odno.get('output1', [])
                console.print(f"[dim]조회된 건수: {len(odno_list)}[/dim]")
                if odno_list:
                    console.print(f"[cyan]👉 ODNO 직접 조회 성공: {odno_list[0]}[/cyan]")
                else:
                    console.print(f"[red]ODNO로 조회했으나 데이터가 없습니다.[/red]")
        except Exception as e:
            console.print(f"[red]상세 분석 3 오류: {e}[/red]")

        # [추가] 상세 분석 4: 조회 기간 확장 및 정렬 변경 시도
        console.print("\n[yellow]🔍 상세 분석 4: 조회 기간 확장 (3일 전) 및 정렬 변경(정순)...[/yellow]")
        
        params_ext = params.copy()
        params_ext["INQR_STRT_DT"] = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
        params_ext["INQR_DVSN"] = "01" # 01: 정순
        
        try:
            res_ext = api.call_api(url, "domestic", "inquiry", "history", params=params_ext, tr_id=tr_id)
            console.print(f"[dim]API 응답 코드: {res_ext.get('rt_cd')}, 메시지: {res_ext.get('msg1')}[/dim]")
            if res_ext.get('rt_cd') == '0':
                ext_list = res_ext.get('output1', [])
                console.print(f"[dim]확장 조회 건수: {len(ext_list)}[/dim]")
        except Exception as e:
            console.print(f"[red]상세 분석 4 오류: {e}[/red]")

        # [추가] 상세 분석 5: 체결구분(CCLD_DVSN)을 '02'(미체결)로 명시하여 조회
        console.print("\n[yellow]🔍 상세 분석 5: 체결구분(CCLD_DVSN)을 '02'(미체결)로 설정하여 조회...[/yellow]")
        
        params_ccld = params.copy()
        params_ccld["CCLD_DVSN"] = "02" # 02: 미체결
        
        try:
            res_ccld = api.call_api(url, "domestic", "inquiry", "history", params=params_ccld, tr_id=tr_id)
            console.print(f"[dim]API 응답 코드: {res_ccld.get('rt_cd')}, 메시지: {res_ccld.get('msg1')}[/dim]")
            
            if res_ccld.get('rt_cd') == '0':
                ccld_list = res_ccld.get('output1', [])
                console.print(f"[dim]조회된 미체결 건수: {len(ccld_list)}[/dim]")
                
                target = next((o for o in ccld_list if str(o.get('odno')) == str(odno)), None)
                if target:
                    console.print(f"[bold green]👉 '02'(미체결) 옵션으로 주문 발견![/bold green]")
                    console.print(f"  - 주문번호: {target.get('odno')}")
                    console.print(f"  - 잔량: {target.get('rmn_qty')}")
                    console.print(f"[bold cyan]💡 해결책: api/account.py의 get_unfilled_orders() 함수에서 CCLD_DVSN 파라미터를 '02'로 수정해야 합니다.[/bold cyan]")
                else:
                    console.print(f"[red]'02' 옵션으로도 주문을 찾을 수 없습니다.[/red]")
        except Exception as e:
            console.print(f"[red]상세 분석 5 오류: {e}[/red]")

        # [추가] 상세 분석 6: 강제 취소 시도 (Blind Cancel)
        console.print("\n[yellow]🔍 상세 분석 6: 주문번호로 강제 취소 시도 (API 누락 확인용)...[/yellow]")
        
        # 취소 주문: org_no, code, qty, price="0", type_cd="02"(취소), ord_dvsn="00"
        cancel_res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
        
        console.print(f"[dim]취소 응답: {cancel_res}[/dim]")
        
        if cancel_res['rt_cd'] == '0':
            console.print(f"[bold green]✅ 강제 취소 성공! 주문이 실제로는 미체결 상태였습니다.[/bold green]")
            console.print(f"[dim]결론: API 조회(`VTTC8001R`)는 실패했지만 주문은 살아있었습니다. 로컬 관리 로직이 필요합니다.[/dim]")
            found_order = True # 청소 단계 스킵용 (이미 취소됨)
        else:
            console.print(f"[red]강제 취소 실패: {cancel_res['msg1']} ({cancel_res.get('msg_cd')})[/red]")
            console.print(f"[dim]결론: 주문이 이미 체결되었거나 유효하지 않습니다.[/dim]")

    # 6. 테스트 주문 취소 (청소)
    if found_order:
        console.print("\n[green]3. 테스트 주문 취소 (정리)...[/green]")
        # 취소 주문: org_no, code, qty, price="0", type_cd="02"(취소), ord_dvsn="00"
        cancel_res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
        
        if cancel_res['rt_cd'] == '0':
            console.print("[bold green]주문 취소 완료.[/bold green]")
        else:
            console.print(f"[bold red]주문 취소 실패: {cancel_res['msg1']}[/bold red]")
            console.print(f"[dim]HTS나 MTS에서 수동으로 취소해주세요. (주문번호: {odno})[/dim]")

if __name__ == "__main__":
    try:
        test_simulation_unfilled_orders()
    except KeyboardInterrupt:
        console.print("\n[yellow]테스트 중단됨[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]오류 발생: {e}[/bold red]")
