import logging
import requests
import sqlite3
import concurrent.futures
import warnings
import time
import math
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from rich.panel import Panel
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.padding import Padding
import utils
import context # [추가]

# [수정] google.generativeai 패키지 Deprecation 경고(FutureWarning) 숨김 처리
# (최신 SDK인 google.genai로의 전환 권고 메시지를 숨기고 기존 로직 유지)
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        import google.generativeai as genai
except ImportError:
    genai = None
import config
from modules import db_manager

logger = logging.getLogger(__name__)

def _init_theme_db():
    try:
        # [수정] 스레드 안전성을 위해 매번 새로운 연결 생성
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                    CREATE TABLE IF NOT EXISTS theme_analysis_cache (
                        key TEXT PRIMARY KEY,
                        updated_at TEXT,
                        data TEXT
                    )
                """)
            conn.commit()
    except Exception: pass

def _save_theme_analysis(result):
    try:
        _init_theme_db()
        # [수정] 스레드 안전성을 위해 매번 새로운 연결 생성
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                    INSERT OR REPLACE INTO theme_analysis_cache (key, updated_at, data)
                    VALUES (?, ?, ?)
                """, ("GEMINI_MARKET_TREND", now_str, result))
            conn.commit()
    except Exception as e:
        logger.error(f"테마 분석 저장 실패: {e}")

def _load_theme_analysis():
    try:
        _init_theme_db()
        # [수정] 스레드 안전성을 위해 매번 새로운 연결 생성
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT updated_at, data FROM theme_analysis_cache WHERE key = ?", ("GEMINI_MARKET_TREND",))
            row = cursor.fetchone()
            if row:
                return {'updated_at': row[0], 'data': row[1]}
    except Exception as e:
        logger.error(f"테마 분석 로드 실패: {e}")
    return None

def fetch_naver_themes():
    """네이버 금융 테마 정보 크롤링"""
    url = "https://finance.naver.com/sise/theme.naver"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    try:
        res = requests.get(url, headers=headers, timeout=5)
        # 네이버 금융은 EUC-KR(CP949) 사용
        soup = BeautifulSoup(res.content.decode('cp949', 'ignore'), 'html.parser')
        
        themes = []
        # 테마 테이블 파싱 (table.type_1)
        rows = soup.select('table.type_1 tr')
        
        for row in rows:
            cols = row.select('td')
            if len(cols) < 4: continue
            
            try:
                # 테마명
                name_tag = cols[0].select_one('a')
                if not name_tag: continue
                name = name_tag.text.strip()
                link = name_tag['href']
                
                # 등락률 (col 1)
                rate_txt = cols[1].text.strip().replace('%', '')
                rate = float(rate_txt) if rate_txt else 0.0
                
                # 최근 3일 등락률 (col 2)
                rate3_txt = cols[2].text.strip().replace('%', '')
                rate3 = float(rate3_txt) if rate3_txt else 0.0
                
                themes.append({'name': name, 'rate': rate, 'rate3': rate3, 'link': link})
            except: continue
            
        return themes
    except Exception as e:
        logger.error(f"Naver theme crawling error: {e}")
        return []

def _fetch_theme_detail(theme):
    """(내부함수) 테마 상세 페이지에서 구성 종목 정보를 가져와 주도주(등락률 상위)를 추출"""
    try:
        url = f"https://finance.naver.com{theme['link']}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        res = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(res.content.decode('cp949', 'ignore'), 'html.parser')
        
        stocks = []
        # table.type_5 contains stock list
        rows = soup.select('table.type_5 tr')
        
        for row in rows:
            cols = row.select('td')
            if len(cols) < 5: continue # Name, Desc, Price, Diff, Rate
            
            try:
                name_tag = cols[0].select_one('a')
                if not name_tag: continue
                name = name_tag.text.strip()
                
                # 종목코드 추출
                href = name_tag['href']
                code = href.split('code=')[-1]
                
                # Rate is usually in 4th index (0-based)
                rate_txt = cols[4].text.strip().replace('%', '').strip()
                rate = float(rate_txt) if rate_txt else 0.0
                
                stocks.append({'name': name, 'code': code, 'rate': rate})
            except: continue
            
        # 등락률 순 정렬 (내림차순)
        stocks.sort(key=lambda x: x['rate'], reverse=True)
        
        # 상위 2개 종목 선정
        leading = [f"{s['name']}({s['code']})" for s in stocks[:2]]
        theme['leading'] = ", ".join(leading)
        
    except Exception:
        theme['leading'] = "-"

def evaluate_market_indicator(name, price, yh_rate=None):
    """지표의 현재가를 바탕으로 사용자 정의 룰에 따른 상태를 반환합니다."""
    status_desc = ""
    if name == "미국채 10년물 금리":
        if price >= 5.20: status_desc = "시스템 위기/Valuation 붕괴"
        elif 4.70 <= price < 5.20: status_desc = "임계점/고금리 쇼크"
        elif 4.20 <= price < 4.70: status_desc = "고금리 지속/Sticky 인플레 경계"
        elif 3.50 <= price < 4.20: status_desc = "골디락스/적정 성장"
        elif 2.80 <= price < 3.50: status_desc = "수요 둔화/금리인하 선반영"
        elif price < 2.80: status_desc = "침체 확정/안전자산 선호"
    elif name == "미국채 5년물 금리":
        if price >= 4.80: status_desc = "긴축 강화/금리 재인상 공포"
        elif 3.80 <= price < 4.80: status_desc = "중립 상단/통화정책 불확실성"
        elif 3.20 <= price < 3.80: status_desc = "안정/적정 유동성"
        elif price < 3.20: status_desc = "금리 급락/유동성 경색"
    elif name == "미국채 30년물 금리":
        if price >= 5.50: status_desc = "하이퍼 인플레"
        elif 4.80 <= price < 5.50: status_desc = "재정 적자 우려/기간 프리미엄 급증"
        elif 4.20 <= price < 4.80: status_desc = "장기 안정"
        elif price < 4.20: status_desc = "장기 저성장/디플레이션 우려"
    elif name == "브랜트유":
        if price >= 125: status_desc = "에너지 쇼크"
        elif 105 <= price < 125: status_desc = "임계점/고금리 긴축 강요"
        elif 85 <= price < 105: status_desc = "인플레 압력 상존"
        elif 65 <= price < 85: status_desc = "골디락스"
        elif 45 <= price < 65: status_desc = "수요 둔화"
        elif price < 45: status_desc = "시스템 위기"
    elif name == "WTI 원유":
        if price >= 120: status_desc = "에너지 쇼크"
        elif 100 <= price < 120: status_desc = "임계점/고금리 긴축 강요"
        elif 80 <= price < 100: status_desc = "인플레 압력 상존"
        elif 60 <= price < 80: status_desc = "골디락스"
        elif 40 <= price < 60: status_desc = "수요 둔화"
        elif price < 40: status_desc = "시스템 위기"
    elif name == "가솔린 RBOB":
        if price >= 4.0: status_desc = "에너지 쇼크"
        elif 3.2 <= price < 4.0: status_desc = "임계점"
        elif 2.6 <= price < 3.2: status_desc = "고유가 지속"
        elif 2.1 <= price < 2.6: status_desc = "골디락스"
        elif 1.6 <= price < 2.1: status_desc = "수요 둔화"
        elif price < 1.6: status_desc = "시스템 위기"
    elif name == "천연가스":
        if price >= 10: status_desc = "에너지 쇼크"
        elif 6 <= price < 10: status_desc = "물가 비상"
        elif 4 <= price < 6: status_desc = "수급 타이트"
        elif 2.5 <= price < 4: status_desc = "골디락스"
        elif 1.5 <= price < 2.5: status_desc = "수익성 악화"
        elif price < 1.5: status_desc = "시스템 하강"
    elif name == "밀":
        if price >= 900: status_desc = "식량 안보 위기"
        elif 750 <= price < 900: status_desc = "식량 인플레 심각"
        elif 650 <= price < 750: status_desc = "물가 부담"
        elif 500 <= price < 650: status_desc = "적절한 균형점"
        elif 400 <= price < 500: status_desc = "수요 둔화/공급 과잉"
        elif price < 400: status_desc = "디플레/침체"
    elif name == "달러인덱스":
        if price >= 120: status_desc = "통화 시스템 붕괴 위기"
        elif 110 <= price < 120: status_desc = "매우 강함/신흥국 위기"
        elif 103 <= price < 110: status_desc = "강세/신흥국 경고등"
        elif 90 <= price < 103: status_desc = "가장 안정적인 중립"
        elif 80 <= price < 90: status_desc = "약세"
        elif price < 80: status_desc = "매우 약함"
    elif name == "달러환율":
        if price >= 1600: status_desc = "시스템 위기"
        elif 1500 <= price < 1600: status_desc = "패닉 구간"
        elif 1400 <= price < 1500: status_desc = "구조적 고환율"
        elif 1300 <= price < 1400: status_desc = "강달러 뉴노멀 상단"
        elif 1200 <= price < 1300: status_desc = "뉴노멀 중립"
        elif 1100 <= price < 1200: status_desc = "비정상적 안정"
        elif price < 1100: status_desc = "초강세 원화"
    elif name == "VIX (변동성)":
        if price <= 20: status_desc = "안정"
        elif 20 < price < 30: status_desc = "시장 활발"
        elif 30 <= price < 40: status_desc = "주의"
        elif 40 <= price < 50: status_desc = "경계"
        elif price >= 50: status_desc = "위험"
    
    if yh_rate is not None:
        if name == "SOX (반도체)":
            if yh_rate >= -5.0: status_desc = "신고가 랠리/초강세"
            elif yh_rate >= -12.0: status_desc = "건전한 조정"
            elif yh_rate >= -20.0: status_desc = "기술적 조정기"
            elif yh_rate < -25.0: status_desc = "반도체 하락 사이클/침체"
        elif not status_desc:
            if yh_rate >= -3.0: status_desc = "신고가 근접/초강세"
            elif yh_rate <= -20.0: status_desc = "침체/약세장 진입"
            else: status_desc = "일반 조정/중립"
            
    return status_desc

def _get_macro_context_str():
    """시스템이 직접 실시간 핵심 매크로 지표를 수집하여 AI에게 주입할 텍스트를 생성"""
    import api
    from modules import analysis
    import concurrent.futures
    
    # 핵심 매크로 지표만 제한적으로 수집 (속도 및 프롬프트 최적화)
    core_tickers = [
        ("코스피", "^KS11"), ("코스닥", "^KQ11"),
        ("나스닥", "^IXIC"), ("S&P500", "^GSPC"),
        ("미국채 5년물 금리", "^FVX"), ("미국채 10년물 금리", "^TNX"), ("미국채 30년물 금리", "^TYX"),
        ("WTI 원유", "CL=F"), ("달러환율", "KRW=X"), ("달러인덱스", "DX-Y.NYB"),
        ("VIX (변동성)", "^VIX"), ("비트코인", "BTC-USD")
    ]

    context_lines = ["[시스템 제공 실시간 핵심 매크로 지표 (이 수치들과 현재 상태를 절대적인 팩트로 반영할 것)]"]
    results = {}

    def fetch_ticker(name, ticker):
        try:
            # 1. 국내 지수는 KIS API 우선 활용
            if name in ["코스피", "코스피200", "코스닥", "코스닥150"]:
                m_type = "KOSPI" if "코스피" in name else "KOSDAQ"
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = float(df.iloc[-1]['close'])
                    prev = float(df.iloc[-2]['close']) if len(df) > 1 else curr
                    rate = ((curr - prev) / prev * 100) if prev > 0 else 0.0
                    high_52 = float(df['close'].tail(250).max())
                    return name, curr, rate, high_52

            # 2. 해외 지수, 원자재, 환율 등은 yfinance 단건 조회(마이크로 캐시) 활용
            fi = api.get_yf_fast_info(ticker)
            if fi:
                price = fi.get('last_price')
                prev = fi.get('regular_market_previous_close')
                yh = fi.get('year_high')
                
                # [수정] 미국채 금리 아시아장 실시간 추정 (선물 연동)
                fut_mapping = {
                    "미국채 5년물 금리": {"ticker": "ZF=F", "duration": 4.5},
                    "미국채 10년물 금리": {"ticker": "ZN=F", "duration": 7.5},
                    "미국채 30년물 금리": {"ticker": "ZB=F", "duration": 16.0}
                }
                if name in fut_mapping and price is not None and prev is not None:
                    try:
                        fut_info = fut_mapping[name]
                        fut_fi = api.get_yf_fast_info(fut_info["ticker"])
                        if fut_fi and fut_fi.get('last_price') and fut_fi.get('regular_market_previous_close'):
                            f_curr = float(fut_fi['last_price'])
                            f_prev = float(fut_fi['regular_market_previous_close'])
                            if f_prev > 0:
                                utc_hour = datetime.now(timezone.utc).hour
                                if utc_hour < 13 or utc_hour >= 21:
                                    f_rate = (f_curr - f_prev) / f_prev * 100
                                    est_yield = price - (f_rate / fut_info["duration"])
                                    prev = price
                                    price = est_yield
                                    name = f"{name}(선물적용)"
                    except: pass
                
                if price is not None and not math.isnan(price):
                    rate = ((price - prev) / prev * 100) if (prev and prev > 0) else 0.0
                    return name, price, rate, yh
        except Exception:
            pass
        return name, None, None, None

    # 병렬 처리로 속도 최적화 (API Rate Limit을 고려하여 max_workers=5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_ticker, name, ticker) for name, ticker in core_tickers]
        for future in concurrent.futures.as_completed(futures):
            name, price, rate, yh = future.result()
            if price is not None:
                results[name] = (price, rate, yh)

    # 원래 순서대로 출력
    for name, _ in core_tickers:
        if name in results:
            price, rate, yh = results[name]
            if "환율" in name: val_str = f"{price:,.2f}원"
            elif "국채" in name or "금리" in name: val_str = f"{price:,.3f}%"
            elif name in ["비트코인", "이더리움"]: val_str = f"${price:,.2f}"
            else: val_str = f"{price:,.2f}"
            
            yh_str = ""
            yh_rate = None
            if yh is not None and yh > 0 and price > 0:
                yh_rate = ((price - yh) / yh) * 100
                yh_str = f" / 52주 고점대비 {yh_rate:+.1f}%"
                
            status_desc = evaluate_market_indicator(name, price, yh_rate)
            status_str = f" -> [현재 상태: {status_desc}]" if status_desc else ""
                
            context_lines.append(f" - {name}: {val_str} (전일대비 {rate:+.2f}%{yh_str}){status_str}")

    return "\n".join(context_lines) + "\n"

def analyze_market_trends_with_gemini(custom_prompt=None):
    """
    Gemini의 Google Search Grounding을 사용하여 실시간 시장 테마 분석
    """
    if genai is None:
        config.console.print("\n[red]※ google-generativeai 라이브러리가 설치되지 않았습니다.[/red]")
        return None

    if not config.GEMINI_API_KEY:
        config.console.print("\n[red]※ Gemini API 키가 설정되지 않았습니다.[/red]")
        config.console.print("[dim]  Google AI Studio에서 키를 발급받아 설정해주세요.[/dim]")
        return None

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            
            macro_context = ""
            if not custom_prompt:
                task_macro = progress.add_task("[cyan]핵심 매크로 지표 실시간 수집 중...[/cyan]", total=None)
                macro_context = _get_macro_context_str()
                progress.remove_task(task_macro)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if custom_prompt:
                prompt = f"""
                [현재 시각: {now} (KST)]
                {custom_prompt}
                """
            else:
                prompt = f"""
                [현재 시각: {now} (KST)]
                {macro_context}
                당신은 여의도 최고의 퀀트 전략가이자 수석 주식 분석가입니다. 
                위 제공된 [시스템 제공 실시간 핵심 매크로 지표]의 수치와 [현재 상태]를 절대적인 팩트로 삼고, 오늘 시장을 지배하는 '핵심 주도 테마 TOP 5' 리포트를 작성해 주세요.
                (※ 제공된 지표의 현재 상태 평가를 바탕으로 시장의 거시적 추세를 분석하세요.)

                반드시 다음의 상세 가이드라인을 엄격히 준수하여 분량을 충분히 확보하고 깊이 있게 작성해야 합니다:

                1. **글로벌 정세 및 매크로 브리핑**: 글로벌 지정학적 이슈, 핵심 매크로 지표가 투심에 미치는 영향을 분석할 것.

                2. **핵심 주도 테마 요약 표 (Markdown Table 필수)**:
                  - 표의 컬럼: [순위 | 테마명 | 상승 강도 | 핵심 트리거(한 줄 요약) | 대장주 및 관련주]

                3. **테마별 딥다이브 심층 분석 및 대응 전략**:
                  각 테마별로 다음 내용을 서술: 상승 배경, 밸류체인 연동성, 주도주 기술적 위치, 리스크 대응.

                4. **향후 체크포인트 (Upcoming Events)**:
                  - 이번 주 또는 단기적으로 방향성을 바꿀 수 있는 주요 일정 2~3가지 제시.

                5. **보고서 출력 형식**:
                  - 도입부: [🌟 마켓 브리핑, 매크로 및 글로벌 정세 요약]
                  - 요약부: [📊 오늘의 핵심 테마 TOP 5 요약 표] (반드시 표 형태로 출력)
                  - 본문: [🔍 테마별 심층 분석 (Deep Dive)] (각 테마별로 소제목을 달아 상세히 서술)
                  - 일정: [📅 단기 핵심 체크포인트]
                  - 결론: [💡 수석 전략가의 최종 투자 총평]
                """

            task_ai = progress.add_task(f"[cyan]Google Gemini가 시장 데이터를 분석 중입니다...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            
            # 1. Gemini API 설정
            genai.configure(api_key=config.GEMINI_API_KEY)

            try:
                model = genai.GenerativeModel(
                    model_name=config.GEMINI_MODEL,
                    generation_config={
                        "temperature": 0.2,
                        "top_p": 0.95,
                        "max_output_tokens": 8192,
                    }
                )
                
                logger.debug("[GEMINI_AI_DEBUG] 테마 분석 요청 - API 호출 대기 시작")
                
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                future = executor.submit(model.generate_content, prompt)
                try:
                    response = future.result(timeout=90.0)
                    executor.shutdown(wait=False)
                except concurrent.futures.TimeoutError:
                    executor.shutdown(wait=False)
                    raise Exception("TimeoutError: API 응답 대기 시간 초과 (90초)")
                        
                logger.debug("[GEMINI_AI_DEBUG] 테마 분석 요청 - API 응답 수신 성공")
                if response and response.text:
                    return response.text
            except Exception as e:
                raise e
                
            return "검색 결과가 없거나 응답을 생성하지 못했습니다."

    except KeyboardInterrupt:
        config.console.print("\n[yellow]사용자에 의해 분석이 중단되었습니다.[/yellow]")
        return None
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg:
            config.console.print(f"\n[yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]")
            config.console.print("[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]")
            logger.warning(f"Gemini API Rate Limit (Model: {config.GEMINI_MODEL}): {e}")
        elif "400" in error_msg and "tools" in error_msg:
            config.console.print(f"\n[red]Gemini API 오류: Google Search 도구 사용 불가 - 모델: {config.GEMINI_MODEL}[/red]")
            config.console.print(f"[dim]  API 설정 오류 또는 '{config.GEMINI_MODEL}' 모델이 도구를 지원하지 않을 수 있습니다.[/dim]")
            logger.error(f"Gemini API Error (Tools, Model: {config.GEMINI_MODEL}): {e}")
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            config.console.print(f"\n[red]Gemini 모델을 찾을 수 없습니다 (404 Not Found) - 모델: {config.GEMINI_MODEL}[/red]")
            config.console.print("[dim]  설정된 모델명이 유효하지 않거나, 해당 API 버전에서 지원되지 않습니다.[/dim]")
            config.console.print("[dim]  config.py의 GEMINI_MODEL 설정을 확인하세요. (예: gemini-2.0-flash)[/dim]")
            logger.error(f"Gemini Model Not Found (Model: {config.GEMINI_MODEL}): {e}")
        else:
            config.console.print(f"\n[red]오류 발생: {e}[/red]")
            logger.error(f"Gemini Search Error (Model: {config.GEMINI_MODEL}): {e}")
        return None

def analyze_stock_with_gemini(code, name, tech_info_str):
    """특정 종목의 기술적 지표와 뉴스를 결합하여 심층 진단"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""
    [현재 시각: {now} (KST)]
    당신은 여의도 최고의 퀀트 전략가이자 주식 분석가입니다.
    다음은 '{name}({code})' 종목의 현재 기술적 분석 상태입니다.
    
    [기술적 분석 요약]
    {tech_info_str}
    
    이 기술적 데이터를 바탕으로, 구글 검색을 통해 '{name}'의 최근 핵심 뉴스(실적, 수주, 주요 공시 등)와 펀더멘털 이슈를 찾아주세요.
    그리고 이 두 가지(차트 상태 + 뉴스/모멘텀)를 결합하여 향후 주가 방향성에 대한 '심층 진단 리포트'를 작성해 주세요.
    
    텔레그램 메신저에서 읽기 편하도록 간결하고 가독성 좋게, 텍스트 스타일링(굵게 등)과 이모지를 적절히 활용하여 작성해 주세요.
    
    출력 형식:
    🔍 [기술적 분석 해석] (시스템이 제공한 퀀트 점수와 지표 상태에 대한 전문가의 해석)
    📰 [최신 핵심 모멘텀] (최근 뉴스 및 재료 요약)
    📊 [차트와 재료의 조화] (기술적 위치와 재료의 시너지 분석)
    💡 [최종 투자 전략] (매수/보유/관망/매도 의견 및 리스크, 주요 지지/저항 라인이나 목표가 등 러프한 가이드 제시)
    """
    logger.debug(f"[GEMINI_AI_DEBUG] [{name}({code})] AI 종목 심층 진단 요청 (모델: {config.GEMINI_MODEL})")
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 종목 진단 요청 - API 호출 대기 시작")
        
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
                
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 종목 진단 요청 - API 응답 수신 성공")
        return res.text if res and res.text else "분석 결과를 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"[GEMINI_AI_DEBUG] Gemini Stock Analyze Error: {e}", exc_info=True)
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg:
            return f"⚠️ [yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]"
        elif "400" in error_msg and "tools" in error_msg:
            return f"⚠️ [red]Gemini API 오류: Google Search 도구 사용 불가 - 모델: {config.GEMINI_MODEL}[/red]\n[dim]  API 설정 오류 또는 '{config.GEMINI_MODEL}' 모델이 도구를 지원하지 않을 수 있습니다.[/dim]"
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            return f"⚠️ [red]Gemini 모델을 찾을 수 없습니다 (404 Not Found) - 모델: {config.GEMINI_MODEL}[/red]\n[dim]  설정된 모델명이 유효하지 않거나, 해당 API 버전에서 지원되지 않습니다.[/dim]\n[dim]  config.py의 GEMINI_MODEL 설정을 확인하세요. (예: gemini-2.0-flash)[/dim]"
        elif any(c in error_msg.lower() for c in ["timeouterror", "deadline", "timeout"]):
            return f"⚠️ [yellow]API 서버 응답 대기 시간 초과 (Timeout) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  구글 서버가 현재 불안정합니다. 잠시 후 다시 시도해주세요.[/dim]"
        else:
            return f"⚠️ [red]분석 중 오류 발생: {error_msg}[/red]"

def evaluate_backtest_with_gemini(code, name, backtest_info, mode='single'):
    """백테스팅 결과를 바탕으로 Gemini에게 평가 및 조언을 요청"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if mode == 'monte_carlo':
        prompt = f"""
        [현재 시각: {now} (KST)]
        당신은 여의도 최고의 퀀트 전략가이자 시스템 트레이딩 전문가입니다.
        다음은 '{name}({code})' 종목에 대해 가격 노이즈와 체결 오차를 반영하여 1,000회 반복 수행한 몬테카를로 백테스팅 시뮬레이션 결과입니다.
        
        {backtest_info}
        
        이 백테스팅 결과를 바탕으로 다음 항목들을 심층 분석해 주세요:
        
        1. 🎲 [전략 견고성 평가]: 노이즈가 주입된 1,000번의 시뮬레이션에서 수익 발생 확률, 평균 수익률, 그리고 하위 5% 최악의 경우(VaR, CVaR)를 종합하여 이 전략이 시장의 불확실성을 얼마나 잘 견뎌내는지(Robustness) 평가해 주세요. 전략이 운에 좌우되는지 통계적 우위가 있는지 진단해 주세요.
        2. ⚠️ [테일 리스크 분석]: 평균 최대 낙폭(MDD)과 최악의 낙폭을 고려했을 때, 이 전략을 실제 운용할 경우 발생할 수 있는 극단적 위험(Tail Risk)에 대해 경고하고 자금 관리 측면에서 어떻게 대비해야 하는지 조언해 주세요.
        3. 💡 [실전 운용 조언]: 현재 적용된 파라미터로 실전에 투입해도 될지 최종 의견을 제시하고, 만약 개선이 필요하다면 어떤 방향(예: 보수적 비중 조절, 손절폭 수정 등)으로 수정해야 할지 제안해 주세요.
        
        터미널 화면에서 읽기 편하도록 간결하고 명확하게, 불릿 포인트(•)와 이모지를 적극적으로 활용하여 작성해 주세요.
        """
    else:
        prompt = f"""
        [현재 시각: {now} (KST)]
        당신은 여의도 최고의 퀀트 전략가이자 시스템 트레이딩 전문가입니다.
        다음은 '{name}({code})' 종목에 대해 단일 과거 데이터를 바탕으로 현재 트레이딩 전략을 적용한 백테스팅 결과입니다.
        
        {backtest_info}
        
        이 백테스팅 결과를 바탕으로 다음 항목들을 심층 분석해 주세요:
        
        1. 📊 [전략 성과 평가]: 수익률, 승률, 평균 수익/손실률, 손익비(Profit Factor), 샤프지수 등을 종합하여 현재 전략이 이 종목의 특성(변동성, 추세성 등)과 얼마나 잘 맞는지 평가해 주세요.
        2. ⚠️ [리스크 분석]: 최대 낙폭(MDD) 및 손익 구조를 분석하여 전략의 단점이나 취약한 시장 상황(예: 횡보장, 급락장 등)을 진단하고, 심리적/자금 관리 측면에서 주의할 점을 짚어 주세요.
        3. 💡 [최적 파라미터 설정 조언]: 현재 적용된 파라미터와 시스템이 산출한 최적화 추천값(매수 점수, RSI, 익절/손절률, 가중치 등)을 비교 분석하여, 이 종목에 가장 적합한 매수/매도 조건을 구체적으로 제안해 주세요.
        
        터미널 화면에서 읽기 편하도록 간결하고 명확하게, 불릿 포인트(•)와 이모지를 적극적으로 활용하여 작성해 주세요.
        """

    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 백테스팅 진단 요청 - API 호출 대기 시작")
        
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
                
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 백테스팅 진단 요청 - API 응답 수신 성공")
        return res.text if res and res.text else "분석 결과를 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"Gemini Backtest Evaluate Error: {e}")
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg:
            return f"⚠️ [yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]"
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            return f"⚠️ [red]Gemini 모델을 찾을 수 없습니다 (404 Not Found) - 모델: {config.GEMINI_MODEL}[/red]\n[dim]  설정된 모델명이 유효하지 않거나, 해당 API 버전에서 지원되지 않습니다.[/dim]\n[dim]  config.py의 GEMINI_MODEL 설정을 확인하세요. (예: gemini-2.0-flash)[/dim]"
        elif any(c in error_msg.lower() for c in ["timeouterror", "deadline", "timeout"]):
            return f"⚠️ [yellow]API 서버 응답 대기 시간 초과 (Timeout) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  구글 서버가 현재 불안정합니다. 잠시 후 다시 시도해주세요.[/dim]"
        else:
            return f"⚠️ [red]진단 중 오류 발생: {error_msg}[/red]"

def generate_trading_autopsy(code, name, buy_time, buy_score, sell_reason, profit_rate, holding_days):
    """건별 매도 체결 시 AI 매매 복기 리포트 작성"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    prompt = f"""
    당신은 여의도 최고의 퀀트 전략가입니다.
    방금 시스템 트레이딩에 의해 다음 종목의 매도가 완료되었습니다.
    
    [매매 정보]
    • 종목명: {name}({code})
    • 진입 일시: {buy_time}
    • 진입 당시 퀀트 점수: {buy_score}점 (10점 만점)
    • 보유 기간: {holding_days}일
    • 최종 수익률: {profit_rate:+.2f}%
    • 청산 사유: {sell_reason}
    
    이 거래가 통계적으로 옳은 결정이었는지, 아니면 시장 이슈 때문이었는지 구글 검색을 통해 해당 기간의 뉴스를 파악하여 객관적으로 리뷰해주세요.
    수익이 났다면 성공 요인을, 손실이 났다면 실패 요인(함정, 휩쏘, 돌발 악재 등)을 분석하고, 향후 파라미터(손절폭, 익절 등) 조정을 위한 조언을 1줄로 남겨주세요.
    
    출력 형식:
    🤖 **수석 전략가 분석**:
    (분석 내용)
    
    💡 **조언**:
    (1~2줄의 핵심 조언)
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.2})
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 매매 복기 요청 - API 호출 대기 시작")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 매매 복기 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Trading autopsy AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 매매 복기 분석 중 오류 발생: {err_str}"

def generate_portfolio_diagnosis(portfolio_str):
    """현재 포트폴리오 비중과 매크로 지표를 종합하여 리스크 진단"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    macro_context = _get_macro_context_str()
    prompt = f"""
    당신은 포트폴리오 리스크 관리 및 자산 배분 전문가입니다.
    현재 운용 중인 시스템 트레이딩 계좌의 포트폴리오 현황과 핵심 매크로 지표 상황입니다.
    
    [현재 포트폴리오]
    {portfolio_str}
    
    {macro_context}
    
    위 데이터를 바탕으로 단순 증권사 업종 분류를 넘어서, 실제 이 기업들의 비즈니스 모델이 특정 테마(AI, 금리, 수출 등)에 얼마나 편중되어 있는지 분석해 주세요.
    그리고 현재 매크로 상황을 고려할 때 이 포트폴리오의 가장 큰 취약점(Risk)은 무엇인지 파악하고, 포트폴리오 안정성을 높이기 위한 리밸런싱 및 방어주(헷지) 편입 조언을 제공해 주세요.
    
    출력 형식:
    📊 **섹터/테마 편중도 요약**
    (요약 내용)
    
    🔍 **숨겨진 리스크 분석 (Correlation Risk)**
    (분석 내용)
    
    💡 **리밸런싱 및 대응 제안 (Action Plan)**
    (대체/추가할 섹터 등 구체적 대응 전략)
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.2})
        logger.debug(f"[GEMINI_AI_DEBUG] 포트폴리오 진단 요청 - API 호출 대기 시작")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] 포트폴리오 진단 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Portfolio diagnosis AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 포트폴리오 진단 중 오류 발생: {err_str}"

def generate_morning_briefing(market_data_str):
    """밤사이 글로벌 지수를 바탕으로 장전 시황 브리핑 생성"""
    if genai is None or not config.GEMINI_API_KEY:
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""
    [현재 시각: {now} (KST)]
    당신은 글로벌 매크로 경제 전문가이자 한국 증시 투자 전략가입니다.
    아래는 지난밤 마감된 글로벌 주요 지수 및 지표 데이터입니다.
    
    [글로벌 마감 데이터]
    {market_data_str}
    
    [시장 지표 해석 가이드]
    * 각 지표 옆에 표시된 "[현재 상태: ...]"는 시스템이 사용자 설정 룰에 따라 절대적 수치를 기준으로 미리 평가한 결과입니다.
    * 단순 1일 등락률에 매몰되어 "급락/하락세"로 오판하지 말고, 이 [현재 상태]를 최우선 기준으로 삼아 거시 경제와 추세를 해석하세요.
    
    위 데이터를 분석하고 구글 검색을 통해 간밤의 미국 증시 주요 이슈(주도주 실적, 연준(Fed) 발언, 매크로 지표 발표 등)를 파악한 뒤,
    오늘 아침 개장할 한국 증시(코스피/코스닥)에 미칠 영향과 오늘 가장 주목해야 할 섹터 3가지를 정리해 주세요.
    
    출력 형식:
    🌅 [굿모닝 글로벌 마켓 브리핑]
    
    📌 간밤의 뉴욕 증시 요약 (핵심 이슈 3줄)
    - 
    - 
    - 
    
    🇰🇷 오늘 한국 증시 관전 포인트 & 시황 예측
    (내용 서술)
    
    🎯 오늘의 주목 섹터 TOP 3 및 추천 주도주 (관심 종목 편입 후보)
    (각 섹터별 상승 명분 1줄 및 해당 테마의 수혜가 예상되는 대장주 1~2개를 반드시 '종목명(종목코드)' 형태로 추천해 주세요.)
    1. 
    2. 
    3. 
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] 장전 브리핑 요청 - API 호출 대기 시작")
        
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
                
        logger.debug(f"[GEMINI_AI_DEBUG] 장전 브리핑 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Gemini Morning Briefing Error: {e}")
        return None

def generate_stock_curation():
    """현재 시점 매크로 지표 및 뉴스를 기반으로 관심 종목 큐레이션 (수동 추가용)"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    macro_context = _get_macro_context_str()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""
    [현재 시각: {now} (KST)]
    당신은 여의도 최고의 퀀트 전략가입니다.
    아래 실시간 핵심 매크로 지표와 현재 시장 상황을 종합하여, 지금 당장 시스템 트레이딩 관심 종목(Watchlist)에 편입할 만한 주도주를 큐레이션해 주세요.
    
    {macro_context}
    
    [가이드라인]
    1. 현재 장세(또는 간밤의 미국장)를 분석하여 오늘 자금이 몰릴 확률이 가장 높은 핵심 테마 2~3가지를 선정하세요.
    2. 각 테마별로 실질적인 수혜를 받는 대장주(시총 1천억 이상 우량주 위주)를 1~2개씩 선별하세요.
    3. 추천 종목은 반드시 '종목명(종목코드 6자리)' 형태로 정확히 표기하세요. (예: 삼성전자(005930))
    
    출력 형식:
    🎯 [AI 관심 종목 큐레이션]
    
    📊 1. [테마명] (간략한 추천 사유)
    • 종목명(종목코드) - 선정 이유 한 줄
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.3})
        logger.debug(f"[GEMINI_AI_DEBUG] 큐레이션 요청 - API 호출 대기 시작")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] 큐레이션 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Stock curation AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 종목 큐레이션 중 오류 발생: {err_str}"

def ask_gemini(question):
    """사용자의 자유 질문에 대해 Gemini API로 답변 생성"""
    if genai is None:
        return "⚠️ google-generativeai 라이브러리가 설치되지 않았습니다."

    if not config.GEMINI_API_KEY:
        return "⚠️ Gemini API 키가 설정되지 않았습니다."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""
    [현재 시각: {now} (KST)]
    당신은 친절하고 전문적인 여의도 수석 주식/경제 AI 비서입니다.
    사용자의 다음 질문에 대해 최신 정보를 바탕으로 핵심만 명확하고 이해하기 쉽게 답변해 주세요.
    가독성을 위해 적절한 줄바꿈과 불릿 포인트(•), 이모지를 적극적으로 사용해 주세요.

    질문: {question}
    """

    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] Q&A 요청 - API 호출 대기 시작")
        
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.generate_content, prompt)
        try:
            response = future.result(timeout=60.0)
            executor.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
                
        logger.debug(f"[GEMINI_AI_DEBUG] Q&A 요청 - API 응답 수신 성공")
        if response and response.text:
            return response.text
        return "검색 결과가 없거나 답변을 생성하지 못했습니다."

    except Exception as e:
        logger.error(f"Gemini Ask Error: {e}")
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg:
            return f"⚠️ [yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]"
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            return f"⚠️ [red]Gemini 모델을 찾을 수 없습니다 (404 Not Found) - 모델: {config.GEMINI_MODEL}[/red]\n[dim]  설정된 모델명이 유효하지 않거나, 해당 API 버전에서 지원되지 않습니다.[/dim]\n[dim]  config.py의 GEMINI_MODEL 설정을 확인하세요. (예: gemini-2.0-flash)[/dim]"
        elif any(c in error_msg.lower() for c in ["timeouterror", "deadline", "timeout"]):
            return f"⚠️ [yellow]API 서버 응답 대기 시간 초과 (Timeout) - 모델: {config.GEMINI_MODEL}[/yellow]\n[dim]  구글 서버가 현재 불안정합니다. 잠시 후 다시 시도해주세요.[/dim]"
        else:
            return f"⚠️ [red]AI 답변 생성 중 오류 발생: {error_msg}[/red]"

def _show_naver_themes():
    """네이버 금융 테마 순위 출력"""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]네이버 금융 테마 데이터 수집 중...[/cyan]", total=None)
        themes = fetch_naver_themes()
        
        if not themes:
            config.console.print("[red]테마 데이터를 가져올 수 없습니다.[/red]")
            return

        # 등락률 순 정렬
        themes.sort(key=lambda x: x['rate'], reverse=True)
        
        # 상위 30개 표시
        top_n = 30
        display_themes = themes[:top_n]
        
        # 상위 테마에 대해 상세 페이지 병렬 크롤링으로 주도주 정보 수집
        progress.update(task, description="[cyan]상위 테마의 주도주 정보를 수집 중... (상세 페이지 분석)[/cyan]")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(_fetch_theme_detail, display_themes)

    table = Table(title=f"실시간 테마 등락률 순위 (TOP {top_n})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("순위", justify="center", width=4)
    table.add_column("테마명", justify="left", overflow="fold")
    table.add_column("등락률", justify="right")
    table.add_column("3일 등락", justify="right")
    table.add_column("주도주", justify="left", style="dim")
    
    for i, t in enumerate(display_themes):
        rate_color = "[red]" if t['rate'] > 0 else ("[blue]" if t['rate'] < 0 else "[white]")
        rate3_color = "[red]" if t['rate3'] > 0 else ("[blue]" if t['rate3'] < 0 else "[white]")
        
        table.add_row(
            str(i+1),
            t['name'],
            f"{rate_color}{t['rate']:+.2f}%[/]",
            f"{rate3_color}{t['rate3']:+.2f}%[/]",
            t.get('leading', '')
        )
        
        if (i + 1) % 5 == 0 and (i + 1) < len(display_themes):
            table.add_section()
    
    # 양쪽 마진 적용 (Padding)
    config.console.print(Padding(table, (1, 2)))

def _analyze_with_gemini_ui():
    """Gemini 분석 실행 및 UI 출력 (마진 적용)"""
    cached = _load_theme_analysis()
    result = None
    
    if cached:
        updated_at = cached['updated_at']
        config.console.print(f"\n[bold cyan]기존 분석 결과가 존재합니다. (분석 일시: {updated_at})[/bold cyan]")
        
        menu_items = [("1", "기존 결과 보기", "View Cached"), ("2", "새로 분석 시작", "Analyze New")]
        choice = utils.show_menu("실시간 테마 분석", menu_items, default_choice="2")
        if choice.lower() == 'q': return
        
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict[choice]}")
        
        if choice == '1':
            result = cached['data']
    
    if not result:
        config.console.print("[dim]시스템이 최신 매크로 지표를 수집하고 Google Gemini가 실시간 검색을 융합하여 테마를 분석합니다.[/dim]\n")
        result = analyze_market_trends_with_gemini()
        if result:
            _save_theme_analysis(result)

    if result:
        # Markdown 렌더링
        md = Markdown(result)
        
        # Panel 생성 (내부 패딩 적용)
        panel = Panel(md, title="실시간 테마 분석 리포트", border_style="cyan", padding=(1, 2), width=120)
        
        # 화면 출력 시 양쪽 마진 적용 (Padding: top, right, bottom, left)
        config.console.print()
        config.console.print(Padding(panel, (0, 4)))

def _analyze_with_custom_prompt_ui():
    """사용자 정의 프롬프트로 Gemini 분석 실행"""
    while True:
        config.console.print("[bold]Gemini에게 요청할 내용을 입력하세요:[/bold]")
        config.console.print()
        user_prompt = Prompt.ask("입력 [dim](종료: q 또는 Enter)[/dim]")
        config.console.print()
        if user_prompt.lower() == 'q' or not user_prompt.strip():
            return

        config.console.print("[dim]Google Gemini가 실시간 검색(Grounding)을 통해 분석합니다.[/dim]")
        
        # 사용자 프롬프트 실행 (캐시 저장 안함)
        result = analyze_market_trends_with_gemini(custom_prompt=user_prompt)

        if result:
            md = Markdown(result)
            panel = Panel(md, title="AI 분석 리포트 (Custom)", border_style="cyan", padding=(1, 2), width=120)
            config.console.print()
            config.console.print(Padding(panel, (0, 4)))

def _analyze_stock_ui():
    """개별 종목 AI 심층 진단 UI"""
    import api
    import indicators
    from modules import analysis
    
    menu_items = [
        ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
        ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
    ]
    choice = utils.show_menu("AI 종목 심층 진단 (AI Stock Analysis)", menu_items, default_choice="5")
    if choice.lower() == 'q': return
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    code = None
    name = None
    is_overseas = False
    
    if choice == '5':
        utils.print_breadcrumb()
        keyword = Prompt.ask("종목코드(6자리/티커) 또는 종목명 입력 [dim](이전: q)[/dim]")
        config.console.print()
        if not keyword or keyword.lower() == 'q': return
        context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {keyword}")
        
        # 1. 등록된 관심 종목에서 검색
        all_stocks = config.session.stock_data.get("stocks_kr", []) + config.session.stock_data.get("etfs_kr", [])
        for item in all_stocks:
            if keyword == item['code'] or keyword == item['name']:
                code, name, is_overseas = item['code'], item['name'], False
                break
                
        if not code:
            all_us = config.session.stock_data.get("stocks_us", []) + config.session.stock_data.get("etfs_us", [])
            for item in all_us:
                if keyword.upper() == item['code'] or keyword.lower() == item['name'].lower():
                    code, name, is_overseas = item['code'], item['name'], True
                    break
                    
        # 2. 미등록 종목인 경우 입력값 분석
        if not code:
            if len(keyword) == 6 and keyword[0].isdigit() and keyword.isalnum():
                code = keyword
                name = api.get_stock_name_by_code(code, False) or keyword
                is_overseas = False
            elif all(ord(c) < 128 for c in keyword):
                code = keyword.upper()
                name = api.get_stock_name_by_code(code, True) or keyword
                is_overseas = True
                
        if not code:
            config.console.print(f"[red]'{keyword}' 종목을 찾을 수 없습니다.[/red]")
            return
            
        if not utils.validate_and_confirm_stock(code, name, is_overseas, "이 종목으로 AI 심층 진단을 진행하시겠습니까?"):
            return
    else:
        # 리스트 선택
        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
        target_key = key_map.get(choice)
        stock_list = config.session.stock_data.get(target_key, [])
        
        if not stock_list:
            config.console.print("[yellow]등록된 종목이 없습니다.[/yellow]")
            return
            
        idx, item = utils.search_stock_in_list(stock_list, title=f"{menu_map_dict[choice]} 목록")
        if not item: return
        code, name = item['code'], item['name']
        is_overseas = (choice in ["3", "4"])
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {name}")
        
    config.console.print(f"[dim]'{name}({code})' 심층 진단 중... (차트 분석 + AI 뉴스 검색)[/dim]")
    
    table_title = ""
    if choice == '1': table_title = "국내 주식 분석 정보"
    elif choice == '2': table_title = "국내 ETF 분석 정보"
    elif choice == '3': table_title = "미국 주식 분석 정보"
    elif choice == '4': table_title = "미국 ETF 분석 정보"
    else: table_title = "미국 주식 분석 정보" if is_overseas else "국내 주식 분석 정보"
    
    analysis.print_table(table_title, [(name, code)], is_overseas=is_overseas)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]차트 데이터 및 기술적 지표 분석 중...[/cyan]", total=None)
            
            df = api.get_chart_data(code, is_overseas)
            if df is None or df.empty:
                config.console.print("[red]차트 데이터를 불러올 수 없어 분석할 수 없습니다.[/red]")
                return
                
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            prev_rsi = None
            if len(df) >= 16:
                delta = df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                except: pass

            state, _, state_reason = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )

            score, _ = analysis.calculate_score(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )

            rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            
            price_str = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
            tech_info = (
                f"• 현재가: {price_str}\n"
                f"• 시스템 상태: {state} (사유: {state_reason})\n"
                f"• 퀀트 점수: {score}점 / 10점 만점\n"
                f"• 핵심 지표: RSI {rsi_val} | ADX {adx_val} | CCI {cci_val}"
            )
            
            progress.add_task(f"[cyan]Google Gemini가 실시간 뉴스를 결합하여 심층 진단 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            answer = analyze_stock_with_gemini(code, name, tech_info)
            
        if answer:
            md = Markdown(answer)
            panel = Panel(md, title=f"🤖 AI 종목 심층 진단: {name}({code})", border_style="cyan", padding=(1, 2), width=120)
            config.console.print()
            config.console.print(Padding(panel, (0, 4)))
        else:
            config.console.print("[red]분석 결과를 생성하지 못했습니다.[/red]")
            
    except Exception as e:
        config.console.print(f"[red]진단 중 오류 발생: {e}[/red]")

def _run_tradingview_screener():
    """트레이딩뷰 스크리너 기반 조건 검색 및 종목 발굴"""
    try:
        from tradingview_screener import Query, Column
        import api
        import pandas as pd
    except ImportError:
        config.console.print("\n[red]※ tradingview-screener 라이브러리가 설치되지 않았습니다.[/red]")
        config.console.print("[dim]명령어: pip install tradingview-screener[/dim]")
        return

    menu_items = [
        ("1", "국내 주식", "Domestic Stock"),
        ("2", "미국 주식", "US Stock")
    ]
    market_choice = utils.show_menu("검색할 시장을 선택하세요", menu_items, default_choice="1")
    if market_choice.lower() == 'q': return False
    
    market_map = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{market_choice}] {market_map.get(market_choice, '')}")
    
    market = "korea" if market_choice == "1" else "america"
    vol_cond_str = "거래량 10만, 1달러 이상" if market == "america" else "거래량 10만 이상"
    
    preset_items = [
        ("1", "상승 추세 눌림목 (현재가 > 20일선 & RSI < 40)", "Pullback"),
        ("2", "강한 모멘텀 (현재가 > 20일선 & RSI > 70 & 거래량 상위)", "Momentum"),
        ("3", "바닥 반등 (RSI < 30 & 상승 반전)", "Rebound"),
        ("4", "거래량 급증 (현재가 > 20일선 & 거래량 > 100만)", "Volume"),
        ("5", f"당일 급상승 상위 15종목 ({vol_cond_str})", "Top Gainers"),
        ("6", f"당일 급하락 상위 15종목 ({vol_cond_str})", "Top Losers")
    ]
    preset_choice = utils.show_menu("검색 조건을 선택하세요", preset_items, default_choice="1")
    if preset_choice.lower() == 'q': return False
    
    preset_map = dict((k, v) for k, v, _ in preset_items)
    preset_name = preset_map.get(preset_choice, '').split(' (')[0] # 괄호 안의 긴 설명은 제외하고 이름만 추출
    context.USER_ACTION_BREADCRUMB.append(f"[{preset_choice}] {preset_name}")
    
    market_display = "Domestic Stock" if market == "korea" else "US Stock"
    
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[cyan]TradingView 스크리너로 종목을 검색 중입니다... ({market_display})[/cyan]", total=None)
            
            # [수정] 조회할 컬럼 추가 (52주 고점 제거)
            select_cols = ['name', 'description', 'close', 'change', 'volume', 'RSI', 'SMA20', 'MACD.macd', 'MACD.signal', 'ADX', 'average_volume']
            query = Query().set_markets(market).select(*select_cols)

            if preset_choice == "1":
                query = query.where(Column('close') > Column('SMA20'), Column('RSI') < 40).order_by('volume', ascending=False)
            elif preset_choice == "2":
                query = query.where(Column('close') > Column('SMA20'), Column('RSI') > 70).order_by('volume', ascending=False)
            elif preset_choice == "3":
                query = query.where(Column('RSI') < 30, Column('change') > 0).order_by('volume', ascending=False)
            elif preset_choice == "4":
                query = query.where(Column('close') > Column('SMA20'), Column('volume') > 1000000).order_by('change', ascending=False)
            elif preset_choice == "5":
                if market == "america":
                    query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=False)
                else:
                    query = query.where(Column('volume') > 100000).order_by('change', ascending=False)
            elif preset_choice == "6":
                if market == "america":
                    query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=True)
                else:
                    query = query.where(Column('volume') > 100000).order_by('change', ascending=True)
                
            if preset_choice in ["5", "6"]:
                query = query.limit(15)
            else:
                query = query.limit(20)
            
            count, df = query.get_scanner_data()
            
            if df is None or df.empty:
                config.console.print("[yellow]조건에 맞는 종목이 없습니다.[/yellow]")
                return
                
            # [수정] 컬럼 순서 및 내용 재구성 (52주 고점 대비 제거, 이름 변경)
            table = Table(title="TradingView 스크리너 검색 결과", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            table.add_column("티커/코드", justify="left", style="cyan")
            table.add_column("종목명", justify="left")
            table.add_column("현재가", justify="right")
            table.add_column("등락률", justify="right")
            table.add_column("SMA20", justify="right")
            table.add_column("MACD (Sig)", justify="right")
            table.add_column("RSI", justify="right")
            table.add_column("ADX", justify="right")
            table.add_column("거래량", justify="right")
            table.add_column("평균거래량", justify="right")
            
            for idx, row in df.iterrows():
                ticker = str(row.get('name', '')).strip()
                name = str(row.get('description', ticker)).strip()
                
                # 국내 주식인 경우 한글 종목명 변환 (알파벳으로만 구성된 경우 API 직접 호출)
                if market == "korea":
                    kor_name = api.get_stock_name_by_code(ticker, is_overseas=False)
                    if ticker is None: continue
                    if not kor_name or kor_name == ticker or all(ord(c) < 128 for c in kor_name.replace(' ', '')):
                        try:
                            res = api.get_current_price_data(ticker, is_overseas=False)
                            if res and res.get('rt_cd') == '0':
                                out = res.get('output', {})
                                fetched_name = out.get('prdt_abrv_name') or out.get('prdt_name')
                                if fetched_name: kor_name = fetched_name
                        except Exception:
                            pass

                    if kor_name: name = kor_name

                # [수정] 모든 데이터 가져오기 (NaN 값 안전하게 처리)
                close = row.get('close', 0)
                close = close if pd.notna(close) else 0
                change = row.get('change', 0)
                change = change if pd.notna(change) else 0
                volume = row.get('volume', 0)
                volume = volume if pd.notna(volume) else 0
                rsi = row.get('RSI', None)
                sma20 = row.get('SMA20', 0)
                sma20 = sma20 if pd.notna(sma20) else 0
                macd = row.get('MACD.macd', None)
                macd_signal = row.get('MACD.signal', None)
                adx = row.get('ADX', None)
                average_volume = row.get('average_volume', 0)
                average_volume = average_volume if pd.notna(average_volume) else 0
                
                # --- Formatting and Color Rules ---
                
                # 현재가
                close_str_raw = f"{close:,.2f}" if market == "america" else f"{int(close):,}"
                c_color = "[red]" if close > sma20 else "[blue]"
                close_str = f"{c_color}{close_str_raw}[/]"

                # 등락률
                change_color = "[red]" if change > 0 else ("[blue]" if change < 0 else "[white]")
                change_str = f"{change_color}{change:+.2f}%[/]"

                # SMA20
                sma20_str = f"{sma20:,.2f}" if market == "america" else f"{int(sma20):,}"

                # MACD
                macd_str = f"{macd:+.2f}" if pd.notna(macd) else "-"
                if pd.notna(macd) and pd.notna(macd_signal):
                    m_color = "red" if macd > macd_signal else "blue"
                    macd_str = f"[{m_color}]{macd:+.2f}[/] [dim]({macd_signal:+.2f})[/dim]"

                # RSI
                rsi_str = f"{rsi:.1f}" if pd.notna(rsi) else "-"
                if pd.notna(rsi):
                    if rsi > 70: rsi_str = f"[magenta]{rsi_str}[/]"
                    elif 50 <= rsi <= 70: rsi_str = f"[red]{rsi_str}[/]"
                    elif 30 <= rsi < 50: rsi_str = f"[orange3]{rsi_str}[/]"
                    elif rsi < 30: rsi_str = f"[blue]{rsi_str}[/]"

                # ADX
                adx_str = f"{adx:.1f}" if pd.notna(adx) else "-"
                if pd.notna(adx):
                    if adx > 40: adx_str = f"[magenta]{adx_str}[/]"
                    elif adx >= 30: adx_str = f"[red]{adx_str}[/]"
                    elif adx >= 20: adx_str = f"[orange3]{adx_str}[/]"

                # 거래량
                vol_k = volume / 1000
                vol_str = f"{vol_k:,.0f}K"
                if volume > average_volume:
                    vol_str = f"[red]{vol_str}[/]"
                else:
                    vol_str = f"[blue]{vol_str}[/]"
                
                # 평균거래량
                avg_vol_k = average_volume / 1000
                avg_vol_str = f"{avg_vol_k:,.0f}K"

                # [수정] 단일 행으로 모든 데이터 추가
                table.add_row(
                    ticker, name, close_str, change_str, sma20_str,
                    macd_str, rsi_str, adx_str, vol_str, avg_vol_str
                )
            
        config.console.print()
        config.console.print(table)
        
        # 관심 종목 추가 연동
        config.console.print()
        ans = Prompt.ask("검색된 종목 중 하나를 관심 종목에 추가하시겠습니까?", choices=["y", "n"], default="n")
        if ans == 'y':
            from modules import manage
            manage.get_current_price(mode='add')
                    
    except Exception as e:
        config.console.print(f"\n[red]TradingView 스크리너 실행 중 오류 발생: {e}[/red]")
        logger.error(f"TradingView Screener Error: {e}", exc_info=True)

def run_theme_analysis():
    """종목 트랜드 분석 메인 함수 (서브 메뉴)"""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "1"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        menu_items = [
            ("1", "네이버 금융 테마 순위", "Naver Theme Ranking"),
            ("2", "트레이딩뷰 종목 검색", "TradingView Screener"),
            ("3", "AI 시장 테마 분석", "AI Market Theme Analysis"),
            ("4", "AI 종목 심층 진단", "AI Stock Analysis")
        ]
        choice = utils.show_menu("종목 트랜드 분석 (Stock Trend Analysis)", menu_items, default_choice=last_choice)
        if choice.lower() == 'q': return False
        
        last_choice = choice
        menu_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")
        
        if choice == '1':
            _show_naver_themes()
            utils.pause()
        elif choice == '2':
            if _run_tradingview_screener() is not False: utils.pause()
        elif choice == '3':
            if _analyze_with_gemini_ui() is not False: utils.pause()
        elif choice == '4':
            if _analyze_stock_ui() is not False: utils.pause()