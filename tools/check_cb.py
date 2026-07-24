import sys
import os
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.dart_api import get_dart_disclosures, get_dart_document_text

def parse_conversion_exercise(doc):
    text = get_dart_document_text(doc['rcept_no'])
    text_clean = re.sub('<[^<]+>', '\n', text)
    lines = [line.strip() for line in text_clean.split('\n') if line.strip()]
    
    start_idx = -1
    for i, line in enumerate(lines):
        if '미전환사채 잔액' in line or '전환사채 잔액' in line:
            if i + 3 < len(lines) and (str(lines[i+1]).isdigit() or str(lines[i+2]).isdigit() or str(lines[i+3]).isdigit()):
                start_idx = i
                break
                
    if start_idx == -1:
        for i, line in enumerate(lines):
            if line in ['2', '3', '4', '5']:
                if i + 1 < len(lines) and '000,000,000' in lines[i+1]:
                    start_idx = i - 5
                    break
                    
    if start_idx != -1:
        print("\n--- 현재 잔존 미상환 전환사채 현황 ---")
        idx = start_idx
        while idx < len(lines):
            line = lines[idx]
            if line in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10']:
                round_num = line
                issued = lines[idx+1]
                offset = 1
                while idx+offset < len(lines) and 'KRW' not in lines[idx+offset]:
                    offset += 1
                issued = lines[idx+offset-1]
                
                offset2 = offset + 1
                while idx+offset2 < len(lines) and 'KRW' not in lines[idx+offset2]:
                    offset2 += 1
                remaining = lines[idx+offset2-1]
                
                conv_price = lines[idx+offset2+1]
                shares = lines[idx+offset2+2]
                
                print(f"제{round_num}회차 CB")
                print(f" - 발행총액: {issued}원")
                print(f" - 미상환잔액: {remaining}원")
                print(f" - 전환가액: {conv_price}원")
                print(f" - 전환가능주식수: {shares}주")
                idx += offset2 + 2
            else:
                idx += 1
            if idx > start_idx + 100:
                break
    else:
         print("공시 본문에서 '전환사채 잔액' 표를 찾을 수 없습니다.")

def parse_issue_decision(doc):
    text = get_dart_document_text(doc['rcept_no'])
    text_clean = re.sub('<[^<]+>', '\n', text)
    lines = [line.strip() for line in text_clean.split('\n') if line.strip()]
    
    round_num = "알수없음"
    issued = "알수없음"
    conv_price = "알수없음"
    
    for i, line in enumerate(lines):
        if '1. 사채의 종류' in line:
            for j in range(1, 5):
                if lines[i+j].isdigit():
                    round_num = lines[i+j]
                    break
        elif '사채의 권면' in line and '총액' in line:
            if lines[i+1].replace(',', '').isdigit():
                issued = lines[i+1]
        elif '전환가액 (원/주)' in line:
            if lines[i+1].replace(',', '').isdigit():
                conv_price = lines[i+1]

    print("\n--- 가장 최근 전환사채 발행결정 현황 (전환청구 내역 없음) ---")
    print("아직 해당 회차의 전환청구권이 행사된 이력이 없어 전액 잔존 상태로 추정됩니다.")
    print(f"제{round_num}회차 CB")
    print(f" - 발행총액: {issued}원")
    print(f" - 미상환잔액: {issued}원 (추정)")
    print(f" - 전환가액: {conv_price}원")
    
    try:
        issue_val = int(issued.replace(',', ''))
        price_val = int(conv_price.replace(',', ''))
        shares = issue_val // price_val
        print(f" - 전환가능주식수: {shares:,}주 (추정)")
    except:
        pass


def check_cb(stock_code):
    print(f"\n[{stock_code}] 종목의 전환사채(CB) 관련 공시를 조회합니다...")
    docs = get_dart_disclosures(stock_code, days=730)
    
    exercise_doc = None
    issue_doc = None
    
    if docs:
        for doc in docs:
            title = doc.get('report_nm', '')
            if '전환청구권행사' in title and not exercise_doc:
                exercise_doc = doc
            if '주요사항보고서(전환사채권발행결정)' in title and not issue_doc:
                issue_doc = doc
            
    if exercise_doc:
        print(f"[{exercise_doc['rcept_dt']}] {exercise_doc['report_nm']} 공시 기준")
        parse_conversion_exercise(exercise_doc)
    elif issue_doc:
        print(f"[{issue_doc['rcept_dt']}] {issue_doc['report_nm']} 공시 기준")
        parse_issue_decision(issue_doc)
    else:
        print(f"최근 2년 내 '{stock_code}' 종목의 CB 발행 또는 전환청구 공시가 없습니다.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DART 공시 기반 전환사채(CB) 잔존 물량 확인 스크립트")
    parser.add_argument("stock_code", nargs="?", help="종목코드 (예: 950160)")
    
    args = parser.parse_args()
    code = args.stock_code
    if not code:
        code = input("조회할 종목코드를 입력하세요 (예: 950160): ").strip()
        
    if not code:
        print("종목코드가 입력되지 않아 종료합니다.")
    else:
        check_cb(code)
