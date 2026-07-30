#!/usr/bin/env python3
import sys
import urllib.request
from bs4 import BeautifulSoup

def get_kospi200_ranked():
    print("\n[데이터 수집 중] 네이버 금융에서 KOSPI 200 종목 순위를 가져옵니다...")
    ranked_list = []
    
    for i in range(1, 22):
        url = f"https://finance.naver.com/sise/entryJongmok.naver?&page={i}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req).read().decode("euc-kr", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
            
            # entryJongmok 테이블 파싱
            table = soup.find("table", class_="type_1")
            if not table:
                continue
                
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    a_tag = cols[0].find("a")
                    if a_tag and "/item/main.naver?code=" in a_tag["href"]:
                        code = a_tag["href"].split("code=")[-1]
                        name = a_tag.text.strip()
                        # 순위 처리는 가져온 순서대로 (네이버 금융은 시총/비중 순으로 제공함)
                        ranked_list.append((code, name))
                        
        except Exception as e:
            pass
            
    return ranked_list

def get_stock_detail(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req).read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        
        name_div = soup.find("div", class_="wrap_company")
        if not name_div:
            return None
            
        name = name_div.find("h2").text.strip() if name_div.find("h2") else "Unknown"
        
        # 현재가
        today_div = soup.find("div", class_="today")
        price = "0"
        if today_div:
            blind = today_div.find("span", class_="blind")
            if blind:
                price = blind.text.strip()
        
        # 시가총액
        market_cap = ""
        mc_em = soup.find("em", id="_market_sum")
        if mc_em:
            market_cap = "".join(mc_em.text.split()) + "억원"
            
        return {"name": name, "price": price, "market_cap": market_cap}
        
    except Exception as e:
        return None

def main():
    while True:
        print("\n" + "="*50)
        print("📊 KOSPI 200 분석 CLI 툴")
        print("="*50)
        print("1. 전체 KOSPI 200 종목 순위별 출력")
        print("2. 개별 종목 상세 정보 및 KOSPI 200 포함 여부 확인")
        print("0. 종료")
        print("="*50)
        
        choice = input("메뉴를 선택하세요: ").strip()
        
        if choice == "1":
            ranked_list = get_kospi200_ranked()
            if not ranked_list:
                print("데이터를 가져오는 데 실패했습니다.")
                continue
                
            print(f"\n✅ KOSPI 200 종목 (총 {len(ranked_list)}개)")
            print("-" * 40)
            for idx, (code, name) in enumerate(ranked_list, 1):
                print(f"{idx:3d}위 | {name} ({code})")
            print("-" * 40)
            
        elif choice == "2":
            code = input("\n종목 코드 6자리를 입력하세요 (예: 005930): ").strip()
            if len(code) != 6 or not code.isdigit():
                print("올바른 6자리 숫자를 입력해주세요.")
                continue
                
            print(f"\n[{code}] 종목 정보를 조회 중입니다...")
            detail = get_stock_detail(code)
            
            if not detail:
                print("종목 정보를 찾을 수 없거나 조회에 실패했습니다.")
                continue
                
            ranked_list = get_kospi200_ranked()
            k200_codes = [r[0] for r in ranked_list]
            
            is_k200 = code in k200_codes
            rank_str = ""
            if is_k200:
                rank = k200_codes.index(code) + 1
                rank_str = f" (KOSPI 200 내 {rank}위)"
                
            print("\n" + "-"*40)
            print(f"📌 종목명: {detail['name']} ({code})")
            print(f"💰 현재가: {detail['price']}원")
            print(f"🏢 시가총액: {detail['market_cap']}")
            print("-" * 40)
            if is_k200:
                print(f"🟢 [결과] 해당 종목은 KOSPI 200 편입 종목입니다!{rank_str}")
            else:
                print("🔴 [결과] 해당 종목은 KOSPI 200 편입 종목이 아닙니다.")
            print("-" * 40)
            
        elif choice == "0":
            print("프로그램을 종료합니다.")
            break
        else:
            print("잘못된 입력입니다. 다시 선택해주세요.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다.")
