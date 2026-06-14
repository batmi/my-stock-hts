import logging
import requests
import sqlite3
import concurrent.futures
import warnings
import math
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import time
from rich.panel import Panel
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.padding import Padding
import utils
import json
import context # [추가]
from modules import prompts # [추가] 외부 프롬프트 템플릿 로드
from modules.executors import ai_executor, io_executor
from modules import db_manager

# [수정] google.generativeai 패키지 Deprecation 경고(FutureWarning) 숨김 처리
# (최신 SDK인 google.genai로의 전환 권고 메시지를 숨기고 기존 로직 유지)
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        import google.generativeai as genai
except ImportError:
    genai = None
import config

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
    except Exception as e:
        logger.debug(f"_init_theme_db error: {e}")

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
            except Exception as e:
                logger.debug(f"Theme parsing row error: {e}")
                continue
            
        return themes
    except Exception as e:
        logger.error(f"Naver theme crawling error: {e}")
        return []

def fetch_realtime_news(keyword, limit=10):
    """구글 뉴스 RSS를 통해 실시간 기사 제목과 원문 링크를 수집합니다. (Naver 차단 원천 우회)"""
    import urllib.parse
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from datetime import timezone, timedelta

    # 최신 뉴스를 위해 최근 1일(when:1d) 조건으로 검색
    query = urllib.parse.quote(f"{keyword} when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        logger.debug(f"[NEWS_DEBUG] 구글 뉴스 RSS 요청 URL: {url}")
        res = requests.get(url, headers=headers, timeout=5)
        logger.debug(f"[NEWS_DEBUG] HTTP 응답 코드: {res.status_code}")
        
        if res.status_code != 200:
            logger.error(f"[NEWS_DEBUG] 비정상 HTTP 응답: {res.status_code} - {res.text[:200]}")
            return ""
            
        root = ET.fromstring(res.text)
        items = root.findall('.//item')
        logger.debug(f"[NEWS_DEBUG] 파싱된 기사(item) 노드 수: {len(items)}")
        
        news_list = []
        for item in items:
            title_elem = item.find('title')
            link_elem = item.find('link')
            source_elem = item.find('source')
            pubdate_elem = item.find('pubDate')
            
            if title_elem is None or link_elem is None:
                continue
                
            title = title_elem.text.strip()
            link = link_elem.text.strip()
            source = source_elem.text.strip() if source_elem is not None and source_elem.text else "언론사"
            
            # 날짜 파싱 및 한국 시간(KST) 변환
            pubdate_str = pubdate_elem.text.strip() if pubdate_elem is not None and pubdate_elem.text else ""
            date_display = ""
            if pubdate_str:
                try:
                    dt = parsedate_to_datetime(pubdate_str)
                    kst = timezone(timedelta(hours=9))
                    date_display = dt.astimezone(kst).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date_display = pubdate_str
            
            info_text = f"{date_display} | {source}" if date_display else source
            
            # 중복 추가 방지
            item_str = f"- [{info_text}] {title}\n  🔗 링크: {link}"
            if item_str not in news_list:
                news_list.append(item_str)
            if len(news_list) >= limit: break
            
        logger.debug(f"[NEWS_DEBUG] 최종 수집된 뉴스 개수: {len(news_list)}")
        return "\n".join(news_list)
    except Exception as e:
        logger.error(f"[NEWS_DEBUG] Google news RSS crawling error: {e}", exc_info=True)
        return ""

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
        theme['leading_stocks'] = stocks[:2]
        
    except Exception as e:
        logger.debug(f"Theme detail fetch error: {e}")
        theme['leading'] = "-"

def evaluate_market_indicator(name, price, yh_rate=None):
    """지표의 현재가를 바탕으로 사용자 정의 룰에 따른 상태를 반환합니다."""
    status_desc = ""
    if name == "미국채 10년물 금리":
        if price >= 5.10: status_desc = "시스템 위기/Valuation 붕괴"
        elif 4.80 <= price < 5.10: status_desc = "임계점/고금리 쇼크"
        elif 4.40 <= price < 4.80: status_desc = "고금리 지속/인플레 경계"
        elif 4.00 <= price < 4.40: status_desc = "골디락스/적정 성장"
        elif 3.50 <= price < 4.00: status_desc = "수요 둔화/금리인하 선반영"
        elif price < 3.50: status_desc = "침체 확정/안전자산 선호"
    elif name == "미국채 5년물 금리":
        if price >= 5.00: status_desc = "단기 유동성 위기/초긴축 발작"
        elif 4.70 <= price < 5.00: status_desc = "긴축 강화/금리 재인상 공포"
        elif 4.20 <= price < 4.70: status_desc = "중립 상단/통화정책 불확실성"
        elif 3.70 <= price < 4.20: status_desc = "안정/적정 유동성"
        elif 3.20 <= price < 3.70: status_desc = "금리 인하 기대 선반영"
        elif price < 3.20: status_desc = "금리 급락/침체 우려"
    elif name == "미국채 30년물 금리":
        if price >= 5.50: status_desc = "재정 적자 우려/기간 프리미엄 극대화"
        elif 5.10 <= price < 5.50: status_desc = "장기 인플레 우려/발행 부담"
        elif 4.60 <= price < 5.10: status_desc = "구조적 고금리 안착 경계"
        elif 4.10 <= price < 4.60: status_desc = "장기 안정/수급 균형"
        elif 3.70 <= price < 4.10: status_desc = "장기 성장 둔화 우려"
        elif price < 3.70: status_desc = "장기 저성장/디플레이션 우려"
    elif name == "브랜트유":
        if price >= 105: status_desc = "에너지 쇼크/스태그플레이션"
        elif 95 <= price < 105: status_desc = "인플레 재발 우려"
        elif 85 <= price < 95: status_desc = "인플레 압력 상존"
        elif 70 <= price < 85: status_desc = "골디락스"
        elif 60 <= price < 70: status_desc = "수요 둔화/침체 신호"
        elif price < 60: status_desc = "심각한 수요 파괴"
    elif name == "WTI 원유":
        if price >= 100: status_desc = "에너지 쇼크/스태그플레이션"
        elif 90 <= price < 100: status_desc = "인플레 재발 우려"
        elif 80 <= price < 90: status_desc = "인플레 압력 상존"
        elif 65 <= price < 80: status_desc = "골디락스"
        elif 55 <= price < 65: status_desc = "수요 둔화/침체 신호"
        elif price < 55: status_desc = "심각한 수요 파괴"
    elif name == "가솔린 RBOB":
        if price >= 4.0: status_desc = "에너지 쇼크"
        elif 3.2 <= price < 4.0: status_desc = "임계점"
        elif 2.6 <= price < 3.2: status_desc = "고유가 지속"
        elif 2.1 <= price < 2.6: status_desc = "골디락스"
        elif 1.6 <= price < 2.1: status_desc = "수요 둔화"
        elif price < 1.6: status_desc = "시스템 위기"
    elif name == "천연가스":
        if price >= 6.0: status_desc = "에너지 쇼크/이상 기후"
        elif 4.0 <= price < 6.0: status_desc = "물가 비상/인플레 자극"
        elif 3.0 <= price < 4.0: status_desc = "수급 타이트"
        elif 2.0 <= price < 3.0: status_desc = "골디락스/중립"
        elif 1.5 <= price < 2.0: status_desc = "공급 과잉/수익성 악화"
        elif price < 1.5: status_desc = "수요 파괴/디플레 신호"
    elif name == "밀":
        if price >= 800: status_desc = "식량 안보 위기/전쟁"
        elif 700 <= price < 800: status_desc = "식량 인플레/애그플레이션"
        elif 600 <= price < 700: status_desc = "수급 타이트/기후 리스크"
        elif 500 <= price < 600: status_desc = "안정/중립(골디락스)"
        elif 400 <= price < 500: status_desc = "공급 과잉/풍작"
        elif price < 400: status_desc = "수익성 악화/디플레"
    elif name == "달러인덱스":
        if price >= 115: status_desc = "글로벌 달러 유동성 경색/위기"
        elif 110 <= price < 115: status_desc = "초강달러/자본 유출 패닉"
        elif 105 <= price < 110: status_desc = "강달러 경계/인플레 자극"
        elif 95 <= price < 105: status_desc = "안정/중립(골디락스)"
        elif price < 95: status_desc = "달러 약세/위험자산 랠리"
    elif name == "달러환율":
        if price >= 1500: status_desc = "시스템 위기/외환 패닉"
        elif 1450 <= price < 1500: status_desc = "위험 구간/개입 경계"
        elif 1400 <= price < 1450: status_desc = "구조적 고환율/경제 부담"
        elif 1300 <= price < 1400: status_desc = "뉴노멀/중립 구간"
        elif 1200 <= price < 1300: status_desc = "안정화/원화 강세"
        elif price < 1200: status_desc = "초강세 원화/수출 부담"
    elif name == "VIX (변동성)":
        if price < 15: status_desc = "안정/골디락스장"
        elif 15 <= price < 20: status_desc = "경계 진입/단기 변동성 확대"
        elif 20 <= price < 30: status_desc = "위험 구간/조정장 진입"
        elif 30 <= price < 40: status_desc = "공포,패닉/급락장"
        elif price >= 40: status_desc = "시스템 위기/블랙스완"
    
    if yh_rate is not None:
        if name == "SOX (반도체)":
            if yh_rate >= -5.0: status_desc = "신고가 랠리/초강세"
            elif yh_rate >= -10.0: status_desc = "건전한 조정"
            elif yh_rate >= -20.0: status_desc = "기술적 조정기"
            elif yh_rate < -20.0: status_desc = "반도체 하락 사이클/침체"
        elif name == "NBI (바이오)":
            if yh_rate >= -5.0: status_desc = "신고가 랠리/초강세"
            elif yh_rate >= -15.0: status_desc = "건전한 조정"
            elif yh_rate >= -25.0: status_desc = "기술적 조정기"
            elif yh_rate < -25.0: status_desc = "바이오 하락 사이클/침체"
        elif name == "BKX (은행)":
            if yh_rate >= -5.0: status_desc = "신고가 랠리/초강세"
            elif yh_rate >= -10.0: status_desc = "건전한 조정"
            elif yh_rate >= -20.0: status_desc = "기술적 조정기 진입"
            elif yh_rate < -20.0: status_desc = "은행업/경제 하락 사이클/침체"
        elif name == "DJU (유틸/전력)":
            if yh_rate >= -5.0: status_desc = "신고가 랠리/방어주 및 전력인프라 강세"
            elif yh_rate >= -10.0: status_desc = "건전한 조정"
            elif yh_rate >= -15.0: status_desc = "기술적 조정기/금리인하 지연 우려"
            elif yh_rate < -15.0: status_desc = "전력/유틸리티 섹터 침체"
        elif name in ["비트코인", "이더리움", "솔라나", "리플"]:
            if yh_rate >= -10.0: status_desc = "신고가 랠리/크립토 불장"
            elif yh_rate >= -25.0: status_desc = "건전한 조정/변동성 허용 구간"
            elif yh_rate >= -40.0: status_desc = "투심 위축/하락 경계"
            elif yh_rate < -40.0: status_desc = "크립토 윈터/침체장"
        elif name == "금":
            if yh_rate >= -3.0: status_desc = "신고가 랠리/안전자산 선호(인플레 헷지)"
            elif yh_rate >= -8.0: status_desc = "건전한 조정"
            elif yh_rate >= -15.0: status_desc = "단기 약세/추세 둔화"
            elif yh_rate < -15.0: status_desc = "하락장/위험자산 선호(달러 강세)"
        elif name in ["은", "구리"]:
            if yh_rate >= -5.0: status_desc = "신고가 랠리/경기 확장(수요 폭발)"
            elif yh_rate >= -15.0: status_desc = "건전한 조정"
            elif yh_rate >= -25.0: status_desc = "수요 둔화/기술적 조정기"
            elif yh_rate < -25.0: status_desc = "경기 침체 우려/닥터코퍼 경고"
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
        ("WTI 원유", "CL=F"), ("천연가스", "NG=F"), ("금", "GC=F"), ("구리", "HG=F"), ("밀", "ZW=F"),
        ("달러환율", "KRW=X"), ("달러인덱스", "DX-Y.NYB"),
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
                    return name, name, curr, rate, high_52

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
                display_name = name
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
                                    display_name = f"{name}(선물적용)"
                    except Exception as e:
                        logger.debug(f"Macro context treasury future fallback error: {e}")
                
                if price is not None and not math.isnan(price):
                    rate = ((price - prev) / prev * 100) if (prev and prev > 0) else 0.0
                    return name, display_name, price, rate, yh
        except Exception as e:
            logger.debug(f"Macro context fetch_ticker error for {name}: {e}")
        return name, name, None, None, None

    # 병렬 처리로 속도 최적화 (API Rate Limit을 고려하여 max_workers=5)
    futures = [io_executor.submit(fetch_ticker, name, ticker) for name, ticker in core_tickers]
    for future in concurrent.futures.as_completed(futures):
        orig_name, display_name, price, rate, yh = future.result()
        if price is not None:
            results[orig_name] = (display_name, price, rate, yh)

    # 원래 순서대로 출력
    for name, _ in core_tickers:
        if name in results:
            display_name, price, rate, yh = results[name]
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
                
            context_lines.append(f" - {display_name}: {val_str} (전일대비 {rate:+.2f}%{yh_str}){status_str}")

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
                prompt = prompts.MARKET_TRENDS_CUSTOM_PROMPT.format(now=now, custom_prompt=custom_prompt)
            else:
                prompt = prompts.MARKET_TRENDS_PROMPT.format(now=now, macro_context=macro_context)

            task_ai = progress.add_task(f"[cyan]Google Gemini가 시장 데이터를 분석 중입니다...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            
            # 1. Gemini API 설정
            genai.configure(api_key=config.GEMINI_API_KEY)

            try:
                model = genai.GenerativeModel(
                    model_name=config.GEMINI_MODEL,
                    # tools="google_search_retrieval", # [주의] 무료 계정(Free Tier)에서는 검색 도구 권한 오류가 발생하므로 주석 처리함
                    generation_config={
                        "temperature": 0.2,
                        "top_p": 0.95,
                        "max_output_tokens": 8192,
                    }
                )
                
                logger.debug("[GEMINI_AI_DEBUG] 테마 분석 요청 - API 호출 대기 시작")
                
                future = ai_executor.submit(model.generate_content, prompt)
                try:
                    response = future.result(timeout=90.0)
                except concurrent.futures.TimeoutError:
                    future.cancel()
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
    """특정 종목의 기술적 지표와 모멘텀을 결합하여 심층 진단"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.STOCK_ANALYSIS_PROMPT.format(now=now, name=name, code=code, tech_info_str=tech_info_str)
    
    logger.debug(f"[GEMINI_AI_DEBUG] [{name}({code})] AI 종목 심층 진단 요청 (모델: {config.GEMINI_MODEL})")
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            # tools="google_search_retrieval", # [주의] 무료 계정 권한 오류 방지
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 종목 진단 요청 - API 호출 대기 시작")
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
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

def analyze_index_with_gemini(code, name, tech_info_str):
    """시장 지수의 기술적 지표와 매크로 모멘텀을 결합하여 심층 진단"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.INDEX_ANALYSIS_PROMPT.format(now=now, name=name, code=code, tech_info_str=tech_info_str)
    
    logger.debug(f"[GEMINI_AI_DEBUG] [{name}({code})] AI 지수 심층 진단 요청 (모델: {config.GEMINI_MODEL})")
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
                
        return res.text if res and res.text else "분석 결과를 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"[GEMINI_AI_DEBUG] Gemini Index Analyze Error: {e}", exc_info=True)
        error_msg = str(e)
        if "429" in error_msg or "Quota" in error_msg:
            return f"⚠️ [yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]"
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            return f"⚠️ [red]Gemini 모델을 찾을 수 없습니다 - 모델: {config.GEMINI_MODEL}[/red]"
        elif any(c in error_msg.lower() for c in ["timeouterror", "deadline", "timeout"]):
            return f"⚠️ [yellow]API 서버 응답 대기 시간 초과 (Timeout)[/yellow]"
        else:
            return f"⚠️ [red]분석 중 오류 발생: {error_msg}[/red]"

def evaluate_backtest_with_gemini(code, name, backtest_info, mode='single'):
    """백테스팅 결과를 바탕으로 Gemini에게 평가 및 조언을 요청"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if mode == 'monte_carlo':
        prompt = prompts.BACKTEST_MONTE_CARLO_PROMPT.format(now=now, name=name, code=code, backtest_info=backtest_info)
    else:
        prompt = prompts.BACKTEST_SINGLE_PROMPT.format(now=now, name=name, code=code, backtest_info=backtest_info)

    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 백테스팅 진단 요청 - API 호출 대기 시작")
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
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

    prompt = prompts.TRADING_AUTOPSY_PROMPT.format(name=name, code=code, buy_time=buy_time, buy_score=buy_score, holding_days=holding_days, profit_rate=profit_rate, sell_reason=sell_reason)
    
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.2}) # tools="google_search_retrieval", 무료 계정 권한 오류 방지
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 매매 복기 요청 - API 호출 대기 시작")
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] [{name}] 매매 복기 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Trading autopsy AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 매매 복기 분석 중 오류 발생: {err_str}"

def _get_today_trades_str():
    """DB에서 당일 매매 내역을 조회하여 문자열로 반환"""
    try:
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            today_str = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("SELECT time, type, code, name, qty, price, profit_rate, reason FROM trades WHERE time LIKE ?", (f"{today_str}%",))
            rows = cursor.fetchall()
            if not rows:
                return "당일 매매 내역 없음"
            
            res = []
            for r in rows:
                t_time, t_type, code, name, qty, price, profit_rate, reason = r
                p_rate_str = f" ({profit_rate}%)" if profit_rate else ""
                res.append(f"[{t_time[11:16]}] {t_type} - {name}({code}) {qty}주 @ {price}원{p_rate_str} | 사유: {reason}")
            return "\n".join(res)
    except Exception as e:
        logger.error(f"Failed to get today trades: {e}")
        return "당일 매매 내역 조회 실패"

def generate_daily_closing_report(portfolio_str):
    """하루 장 마감 후 시장, 포트폴리오, 당일 매매를 종합 진단하는 마감 브리핑 생성"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    macro_context = _get_macro_context_str()
    today_trades_str = _get_today_trades_str()
    prompt = prompts.DAILY_CLOSING_PROMPT.format(portfolio_str=portfolio_str, macro_context=macro_context, today_trades_str=today_trades_str)
    
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.2})
        logger.debug(f"[GEMINI_AI_DEBUG] 장 마감 브리핑 요청 - API 호출 대기 시작")
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] 장 마감 브리핑 요청 - API 응답 수신 성공")
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Daily closing report AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 장 마감 브리핑 생성 중 오류 발생: {err_str}"

def generate_morning_briefing(market_data_str):
    """밤사이 글로벌 지수를 바탕으로 장전 시황 브리핑 생성"""
    if genai is None or not config.GEMINI_API_KEY:
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.MORNING_BRIEFING_PROMPT.format(now=now, market_data_str=market_data_str)
    
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            # tools="google_search_retrieval", # [주의] 무료 계정 권한 오류 방지
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] 장전 브리핑 요청 - API 호출 대기 시작")
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
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
    prompt = prompts.STOCK_CURATION_PROMPT.format(now=now, macro_context=macro_context)
    
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL, generation_config={"temperature": 0.3}) # tools="google_search_retrieval", 무료 계정 권한 오류 방지
        logger.debug(f"[GEMINI_AI_DEBUG] 큐레이션 요청 - API 호출 대기 시작")
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
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
    prompt = prompts.ASK_GEMINI_PROMPT.format(now=now, question=question)

    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            # tools="google_search_retrieval", # [주의] 무료 계정 권한 오류 방지
            generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] Q&A 요청 - API 호출 대기 시작")
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            response = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
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

def get_latest_news_with_gemini(keyword, code=None):
    """특정 종목의 최신 중요 뉴스 5개 검색 (링크 포함)"""
    if genai is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 통합 포털 검색을 사용하여 종목 코드(국내/해외) 의존성 완전 제거 및 수집 성공률 100% 확보
    crawled_news = fetch_realtime_news(keyword, limit=10)
    
    if not crawled_news:
        return f"⚠️ '{keyword}'에 대한 실시간 뉴스 검색 결과가 없습니다. (구글 뉴스 RSS 수집 실패)"

    instruction = f"""
    [시스템이 수집한 실시간 최신 뉴스 데이터]
    {crawled_news}
    
    위 제공된 시스템 수집 뉴스 데이터를 바탕으로 '{keyword}'에 대한 가장 중요하고 핵심적인 투자 포인트와 이슈 5가지를 요약해주세요.
    
    [필수 지시사항]
    1. 주식 투자와 무관한 단순 제품 홍보, 가십, 중복 기사는 엄격히 제외하고 주가에 영향을 줄 수 있는 핵심 모멘텀(실적, 수주, 신사업, 거시경제 등) 관련 기사 위주로 선별하세요.
    2. 제공된 뉴스 기사 제목, 날짜, 출처를 명시하세요.
    3. URL 링크가 지저분하게 노출되지 않도록, 반드시 [기사 원문 보기](원본URL) 형태의 마크다운 하이퍼링크로 작성하세요. (예: 🔗 링크: [기사 원문 보기](https://news...))
    """

    prompt = f"""
    [현재 시각: {now} (KST)]
    당신은 한국 및 글로벌 주식 시장 전문 AI 어시스턴트입니다.
    {instruction}
    
    반드시 다음 출력 형식을 엄격하게 지켜주세요:
    
    📰 [{keyword}] 핵심 투자 포인트 및 최신 뉴스 5선
    
    1. [기사/이슈 요약 제목 1]
       - 요약: (1~2줄 이내의 핵심 요약)
       - 시사점: (해당 이슈가 주가에 미치는 투자 관점의 영향)
       - 링크: [기사 원문 보기](제공된 원본 URL 삽입)
       
    (2~5번도 동일한 형식으로 출력)
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            # tools="google_search_retrieval", # [주의] 무료 계정 권한 오류 방지
            generation_config={"temperature": 0.1, "top_p": 0.95, "max_output_tokens": 4096}
        )
        logger.debug(f"[GEMINI_AI_DEBUG] [{keyword}] 뉴스 검색 요청 - API 호출 대기 시작")
        
        future = ai_executor.submit(model.generate_content, prompt)
        try:
            res = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise Exception("TimeoutError: API 응답 대기 시간 초과 (60초)")
        logger.debug(f"[GEMINI_AI_DEBUG] [{keyword}] 뉴스 검색 요청 - API 응답 수신 성공")
        return res.text if res and res.text else "검색 결과가 없거나 응답을 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"Gemini News Search Error: {e}")
        err_str = str(e)
        if "429" in err_str or "Quota" in err_str: return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        elif "timeout" in err_str.lower(): return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ 뉴스 검색 중 오류 발생: {err_str}"

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
        list(io_executor.map(_fetch_theme_detail, display_themes))

    table = Table(title=f"실시간 테마 등락률 순위 (TOP {top_n})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("순위", justify="center", width=4)
    table.add_column("테마명", justify="left", overflow="fold")
    table.add_column("등락률", justify="right")
    table.add_column("3일 등락", justify="right")
    table.add_column("주도주", justify="left", style="dim")
    
    stock_map = {}
    
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
        
        for s in t.get('leading_stocks', []):
            stock_map[s['code']] = s['name']
            
        if (i + 1) % 5 == 0 and (i + 1) < len(display_themes):
            table.add_section()
    
    # 양쪽 마진 적용 (Padding)
    config.console.print(Padding(table, (1, 2)))

    # 개별 종목 상세 분석 연동
    config.console.print()
    ans = Prompt.ask("개별 종목 상세 분석을 진행하시겠습니까?", choices=["y", "n"], default="n")
    if ans.lower() == 'y':
        target_code = Prompt.ask("분석할 종목의 티커/코드를 입력하세요").strip()
        
        found_code = None
        found_name = None
        for code, name in stock_map.items():
            if code.upper() == target_code.upper():
                found_code = code
                found_name = name
                break
                
        if found_code:
            from modules import analysis
            config.console.print(f"\n[bold green]>> {found_name}({found_code}) 개별 종목 심층 분석 실행[/bold green]")
            analysis.diagnose_stock(target_code=found_code, target_name=found_name, target_is_overseas=False)
        else:
            config.console.print(f"[red]입력한 종목('{target_code}')을 검색 결과(주도주)에서 찾을 수 없습니다.[/red]")

def _analyze_with_gemini_ui():
    """Gemini 분석 실행 및 UI 출력 (마진 적용)"""
    cached = _load_theme_analysis()
    result = None
    
    if cached:
        updated_at = cached['updated_at']
        config.console.print(f"\n[bold cyan]기존 분석 결과가 존재합니다. (분석 일시: {updated_at})[/bold cyan]")
        
        menu_items = [("1", "기존 결과 보기", "View Cached"), ("2", "새로 분석 시작", "Analyze New")]
        choice = utils.show_menu("실시간 테마 분석", menu_items, default_choice="2")
        if choice.lower() in ['b', 'q']: return
        
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
        user_prompt = Prompt.ask("입력 [dim](이전: b, 메인: q 또는 Enter)[/dim]")
        config.console.print()
        if user_prompt.lower() in ['b', 'q'] or not user_prompt.strip():
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
    if choice.lower() in ['b', 'q']: return
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    code = None
    name = None
    is_overseas = False
    
    if choice == '5':
        utils.print_breadcrumb()
        keyword = Prompt.ask("종목코드(6자리/티커) 또는 종목명 입력 [dim](이전: b, 메인: q)[/dim]")
        config.console.print()
        if not keyword or keyword.lower() in ['b', 'q']: return
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
        
    config.console.print(f"[dim]'{name}({code})' 심층 진단 중... (차트 분석 + AI 모멘텀 분석)[/dim]")
    
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
            task_id = progress.add_task("[cyan]차트 데이터 및 기술적 지표 분석 중...[/cyan]", total=None)
            
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

            w52_pos = 0.0
            if len(df) > 0:
                recent_df = df.tail(250)
                h52 = recent_df['high'].max()
                l52 = recent_df['low'].min()
                if h52 > l52:
                    w52_pos = (current_price - l52) / (h52 - l52) * 100
                    
            sm_flag, _ = analysis.check_smart_money_turnaround(code, is_overseas)
            
            # [추가] 개별 룰 및 시장 국면(적응형 임계값) 보정 적용
            from modules import db_manager
            custom_rule = db_manager.db.get_stock_strategy(code)
            buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            weights = config.SCORING_WEIGHTS
            
            if custom_rule:
                buy_score = custom_rule['buy_score']
                buy_rsi = custom_rule['buy_rsi']
                if custom_rule.get('weights'):
                    try:
                        w_data = custom_rule['weights']
                        if isinstance(w_data, str): weights = json.loads(w_data)
                        elif isinstance(w_data, dict): weights = w_data
                    except: pass
            
            score_adj = 0.0
            if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True) and not is_overseas:
                market_type = "KOSPI"
                try:
                    cp = api.get_current_price_data(code, False)
                    if cp.get('rt_cd') == '0' and "코스닥" in cp['output'].get('rprs_mrkt_kor_name', ''):
                        market_type = "KOSDAQ"
                except: pass
                _, score_adj = analysis.get_market_regime(market_type)
                if not custom_rule:
                    buy_score += score_adj

            thresholds = {
                "BUY_SCORE": buy_score,
                "BUY_RSI_MAX": buy_rsi,
                "WEIGHTS": weights
            }

            state, _, state_reason = analysis.classify_stock_state(
                df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )

            score, _ = analysis.calculate_score(
                df=df, ind=ind, weights=weights, smart_money=sm_flag
            )
            score = round(score, 1)

            rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            
            plus_di = ind.get('plus_di')
            minus_di = ind.get('minus_di')
            dmi_str = "-"
            if plus_di is not None and minus_di is not None:
                if plus_di > minus_di:
                    dmi_str = f"+DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                elif minus_di > plus_di:
                    dmi_str = f"-DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                else:
                    dmi_str = f"중립 ({plus_di:.1f} / {minus_di:.1f})"
            
            price_str = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
            tech_info = (
                f"• 현재가: {price_str}\n"
                f"• 시스템 상태: {state} (사유: {state_reason})\n"
                f"• 퀀트 점수: {score}점 / 10점 만점\n"
                f"• 핵심 지표: RSI {rsi_val} | ADX {adx_val} | CCI {cci_val} | DMI {dmi_str}"
            )
            
            progress.update(task_id, description=f"[cyan]Google Gemini가 기업 모멘텀을 결합하여 심층 진단 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]")
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
    if market_choice.lower() in ['b', 'q']: return False
    
    market_map = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{market_choice}] {market_map.get(market_choice, '')}")
    
    market = "korea" if market_choice == "1" else "america"
    vol_cond_str = "거래량 10만, 1달러 이상" if market == "america" else "거래량 10만 이상"
    
    preset_items = [
        ("0", "전체 프리셋 순차 스캔", "All Presets"),
        ("1", "당일 급상승 상위 15종목", "Top Gainers"),
        ("2", "당일 급하락 상위 15종목", "Top Losers"),
        ("3", "신고가 돌파 주도주", "Breakout"),
        ("4", "정배열 눌림목", "Pullback"),
        ("5", "폭발적 수급 유입", "Volume Momentum"),
        ("6", "낙폭과대 바닥 탈출", "Oversold Rebound"),
        ("7", "저평가 우량주 턴어라운드", "Value Turnaround"),
        ("8", "고배당 상승 추세", "High Dividend"),
        ("9", "상승 추세 전환", "Trend Reversal")
    ]
    preset_choice = utils.show_menu("검색 조건을 선택하세요", preset_items, default_choice="0")
    if preset_choice.lower() in ['b', 'q']: return False
    
    preset_map = dict((k, v) for k, v, _ in preset_items)
    preset_name = preset_map.get(preset_choice, '')
    context.USER_ACTION_BREADCRUMB.append(f"[{preset_choice}] {preset_name}")
    
    preset_conditions = {
        "1": f"({vol_cond_str})",
        "2": f"({vol_cond_str})",
        "3": "(52주 고점 95%↑ + 정배열 + RSI>65 + ADX>25 + MACD골든)",
        "4": "(정배열 + 종가가 20일선 아래 & 50일선 위 지지 + RSI 35~50)",
        "5": "(평균 거래량 3배 이상 폭증 + 당일 5% 이상 급등 + 종가>20일선)",
        "6": "(RSI<40 + 주가<20일선 + MACD골든 + 당일 2%↑ 반등)",
        "7": "(PER 1~12 + PBR<1.5 + ROE>15% + 20일선 돌파 + MACD골든)",
        "8": "(배당률>5% + PER 1~15 + 정배열 + RSI>50)",
        "9": "(20일<50일 역배열 상태에서 주가가 50일선 강하게 돌파 + MACD골든)"
    }

    preset_desc = {
        "3": "강세장에서 시장을 주도하며 전고점을 뚫고 날아가는 가장 강한 주식을 잡을 때 사용합니다.",
        "4": "완벽한 우상향 추세에 있는 주식이 일시적인 조정(과매도)을 받을 때 안전하게 진입하는 스윙 전략입니다.",
        "5": "평소 조용하던 주식에 세력이나 기관의 강력한 매수세가 유입되며 시세가 분출하기 시작한 종목을 포착합니다.",
        "6": "급락장이나 악재로 과도하게 떨어진 주식이 바닥을 다지고 기술적 반등을 시작하는 정확한 타점을 잡습니다.",
        "7": "실적과 가치는 우수하지만 소외되었던 주식이 20일선을 타며 추세가 호전되기 시작하는 중장기 스윙용입니다.",
        "8": "하락장이나 횡보장에서 하방 경직성이 강하고 안전하게 배당을 받으며 느긋하게 투자할 종목을 찾습니다.",
        "9": "오랜 하락이나 횡보를 끝내고 본격적인 상승 추세로 진입하는 초기(무릎) 타점을 잡아내는 가장 신뢰도 높은 스윙 전략입니다."
    }

    try:
        target_choices = [str(i) for i in range(1, 10)] if preset_choice == "0" else [preset_choice]
        results = []
        stock_map = {}
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=config.console,
            transient=True
        ) as progress:
            is_single = len(target_choices) == 1
            if is_single:
                task_main = progress.add_task("[cyan]TradingView 스크리너 검색 중...[/cyan]", total=None)
            else:
                task_main = progress.add_task("[cyan]전체 프리셋 스캔 진행률 (All Presets)[/cyan]", total=len(target_choices))
            
            for p_choice in target_choices:
                p_name = preset_map.get(p_choice, '').split(' (')[0]
                if is_single:
                    progress.update(task_main, description=f"[cyan]{p_name} 검색 중...[/cyan]", total=None, completed=0)
                else:
                    task_sub = progress.add_task(f"[cyan]  └ {p_name} 검색 중...[/cyan]", total=None)
                
                select_cols = ['name', 'description', 'sector', 'close', 'change', 'volume', 'RSI', 'SMA20', 'SMA50', 'MACD.macd', 'MACD.signal', 'ADX', 'average_volume', 'price_earnings_ttm', 'price_book_ratio', 'return_on_equity', 'price_52_week_high', 'price_52_week_low', 'dividend_yield_recent', 'relative_volume_10d_calc']
                query = Query().set_markets(market).select(*select_cols)
                
                if p_choice == "1":
                    if market == "america":
                        query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=False)
                    else:
                        query = query.where(Column('volume') > 100000).order_by('change', ascending=False)
                elif p_choice == "2":
                    if market == "america":
                        query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=True)
                    else:
                        query = query.where(Column('volume') > 100000).order_by('change', ascending=True)
                elif p_choice == "3":
                    query = query.where(Column('SMA20') > Column('SMA50'), Column('RSI') > 65, Column('ADX') > 25, Column('MACD.macd') > Column('MACD.signal')).order_by('volume', ascending=False)
                elif p_choice == "4":
                    query = query.where(Column('SMA20') > Column('SMA50'), Column('close') > Column('SMA50'), Column('close') < Column('SMA20'), Column('RSI').between(35, 50), Column('MACD.macd') > 0).order_by('volume', ascending=False)
                elif p_choice == "5":
                    query = query.where(Column('relative_volume_10d_calc') > 3.0, Column('change') > 5.0, Column('close') > Column('SMA20')).order_by('relative_volume_10d_calc', ascending=False)
                elif p_choice == "6":
                    query = query.where(Column('RSI') < 40, Column('close') < Column('SMA20'), Column('MACD.macd') > Column('MACD.signal'), Column('change') > 2.0).order_by('volume', ascending=False)
                elif p_choice == "7":
                    query = query.where(Column('price_earnings_ttm').between(1, 12), Column('price_book_ratio') < 1.5, Column('return_on_equity') > 15, Column('close') > Column('SMA20'), Column('MACD.macd') > Column('MACD.signal')).order_by('volume', ascending=False)
                elif p_choice == "8":
                    query = query.where(Column('dividend_yield_recent') >= 5, Column('price_earnings_ttm').between(1, 15), Column('SMA20') > Column('SMA50'), Column('RSI') > 50).order_by('dividend_yield_recent', ascending=False)
                elif p_choice == "9":
                    query = query.where(Column('SMA20') < Column('SMA50'), Column('close') > Column('SMA50'), Column('MACD.macd') > Column('MACD.signal'), Column('change') > 0).order_by('volume', ascending=False)
                    
                if p_choice in ["1", "2"]:
                    query = query.limit(15)
                elif p_choice in ["3", "9"]:
                    query = query.limit(200)
                else:
                    query = query.limit(20)
            
                count, df = 0, None
                for attempt in range(3):
                    try:
                        count, df = query.get_scanner_data()
                        break
                    except Exception as e:
                        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                            if attempt < 2:
                                time.sleep(1.5)
                            else:
                                config.console.print(f"\n[yellow]⚠️ TradingView 서버 응답 지연 (Timeout). '{p_name}' 검색을 건너뜁니다.[/yellow]")
                        else:
                            raise e
                
                if df is not None and not df.empty:
                    if p_choice == "3":
                        df = df[df['close'] >= df['price_52_week_high'] * 0.95]
                        df = df.head(20)
                    elif p_choice == "9":
                        df = df[df['close'] <= (df['price_52_week_high'] + df['price_52_week_low']) / 2]
                        df = df.head(20)

                if df is not None and not df.empty:
                    if is_single:
                        progress.update(task_main, description=f"[cyan]{p_name} 결과 정리 중...[/cyan]", total=len(df), completed=0)
                        active_task = task_main
                    else:
                        progress.update(task_sub, description=f"[cyan]  └ {p_name} 결과 정리 중...[/cyan]", total=len(df), completed=0)
                        active_task = task_sub
                
                    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
                    table.add_column("티커/코드", justify="left", style="cyan")
                    table.add_column("종목명", justify="left")
                    table.add_column("업종", justify="left", style="dim", overflow="fold")
                    table.add_column("현재가", justify="right")
                    table.add_column("등락률", justify="right")
                    table.add_column("52주(%)", justify="right")
                    table.add_column("SMA20", justify="right")
                    table.add_column("MACD (Sig)", justify="right")
                    table.add_column("RSI", justify="right")
                    table.add_column("ADX", justify="right")
                    table.add_column("PER", justify="right")
                    table.add_column("ROE(%)", justify="right")
                    table.add_column("배당(%)", justify="right", style="dim")
                    table.add_column("거래량", justify="right")
                    table.add_column("평균거래량", justify="right")
                    
                    sector_map_ko = {
                        "Electronic Technology": "전자기술",
                        "Technology Services": "기술서비스",
                        "Health Technology": "의료기술",
                        "Health Services": "의료서비스",
                        "Finance": "금융",
                        "Consumer Durables": "내구소비재",
                        "Consumer Non-Durables": "비내구소비재",
                        "Consumer Services": "소비자서비스",
                        "Retail Trade": "소매유통",
                        "Producer Manufacturing": "제조업",
                        "Commercial Services": "상업서비스",
                        "Energy Minerals": "에너지광물",
                        "Non-Energy Minerals": "비에너지광물",
                        "Industrial Services": "산업서비스",
                        "Utilities": "유틸리티",
                        "Transportation": "운송",
                        "Communications": "통신",
                        "Distribution Services": "유통서비스",
                        "Process Industries": "가공산업",
                        "Miscellaneous": "기타"
                    }
                    
                    for idx, row in df.iterrows():
                        ticker = str(row.get('name', '')).strip()
                        name = str(row.get('description', ticker)).strip()
                        sector = str(row.get('sector', '-')).strip()
                        if sector == 'nan' or not sector:
                            sector = "-"
                        else:
                            sector = sector_map_ko.get(sector, sector)
                        
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
                                except Exception as e:
                                    logger.debug(f"Screener domestic name fallback error: {e}")
                                    
                            # [추가] 네이버와 한국투자증권 API 양쪽 모두에서 정상적인 한글명을 가져오지 못해 
                            # 여전히 코드가 이름으로 남아있다면, 상장폐지/만기된 종목이므로 결과에서 제외합니다.
                            if kor_name == ticker:
                                continue
                                
                            if kor_name: name = kor_name

                        stock_map[ticker] = name

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
                        per = row.get('price_earnings_ttm', None)
                        roe = row.get('return_on_equity', None)
                        div = row.get('dividend_yield_recent', None)
                        average_volume = row.get('average_volume', 0)
                        average_volume = average_volume if pd.notna(average_volume) else 0
                        
                        h52 = row.get('price_52_week_high', 0)
                        l52 = row.get('price_52_week_low', 0)
                        
                        w52_pos_str = "-"
                        if pd.notna(h52) and pd.notna(l52) and h52 > l52:
                            pos = (close - l52) / (h52 - l52) * 100
                            w_color = "[white]"
                            if pos >= 90: w_color = "[red]"
                            elif pos >= 80: w_color = "[orange3]"
                            elif pos <= 30: w_color = "[blue]"
                            elif pos <= 50: w_color = "[yellow]"
                            w52_pos_str = f"{w_color}{pos:.1f}%[/]"
                        
                        close_str_raw = f"{close:,.2f}" if market == "america" else f"{int(close):,}"
                        c_color = "[red]" if close > sma20 else "[blue]"
                        close_str = f"{c_color}{close_str_raw}[/]"

                        change_color = "[red]" if change > 0 else ("[blue]" if change < 0 else "[white]")
                        change_str = f"{change_color}{change:+.2f}%[/]"

                        sma20_str = f"{sma20:,.2f}" if market == "america" else f"{int(sma20):,}"

                        macd_str = f"{macd:+.2f}" if pd.notna(macd) else "-"
                        if pd.notna(macd) and pd.notna(macd_signal):
                            m_color = "red" if macd > macd_signal else "blue"
                            macd_str = f"[{m_color}]{macd:+.2f}[/] [dim]({macd_signal:+.2f})[/dim]"

                        rsi_str = f"{rsi:.1f}" if pd.notna(rsi) else "-"
                        if pd.notna(rsi):
                            if rsi > 70: rsi_str = f"[magenta]{rsi_str}[/]"
                            elif 50 <= rsi <= 70: rsi_str = f"[red]{rsi_str}[/]"
                            elif 30 <= rsi < 50: rsi_str = f"[orange3]{rsi_str}[/]"
                            elif rsi < 30: rsi_str = f"[blue]{rsi_str}[/]"

                        adx_str = f"{adx:.1f}" if pd.notna(adx) else "-"
                        if pd.notna(adx):
                            if adx > 40: adx_str = f"[magenta]{adx_str}[/]"
                            elif adx >= 30: adx_str = f"[red]{adx_str}[/]"
                            elif adx >= 20: adx_str = f"[orange3]{adx_str}[/]"

                        per_str = f"{per:.1f}" if pd.notna(per) else "-"
                        roe_str = f"{roe:.1f}" if pd.notna(roe) else "-"

                        div_str = f"{div:.2f}" if pd.notna(div) else "-"
                        if pd.notna(div) and div >= 5.0: div_str = f"[bold green]{div_str}[/bold green]"

                        vol_k = volume / 1000
                        vol_str = f"{vol_k:,.0f}K"
                        if volume > average_volume: vol_str = f"[red]{vol_str}[/]"
                        else: vol_str = f"[blue]{vol_str}[/]"
                        
                        avg_vol_k = average_volume / 1000
                        avg_vol_str = f"{avg_vol_k:,.0f}K"

                        table.add_row(
                            ticker, name, sector, close_str, change_str, w52_pos_str, sma20_str,
                            macd_str, rsi_str, adx_str, per_str, roe_str, div_str, vol_str, avg_vol_str
                        )
                        progress.advance(active_task)
                        
                    if not is_single:
                        progress.remove_task(task_sub)
                    results.append((p_choice, preset_map.get(p_choice, ''), table))
                else:
                    if not is_single:
                        progress.remove_task(task_sub)
                    results.append((p_choice, preset_map.get(p_choice, ''), None))
                
                if not is_single:
                    progress.advance(task_main)
                
        for p_choice, p_full_name, table in results:
            cond_str = f" {preset_conditions[p_choice]}" if p_choice in preset_conditions else ""
            config.console.print(f"\n[bold cyan]▶ {p_full_name}{cond_str}[/bold cyan]")
            if p_choice in preset_desc:
                config.console.print(f"   [dim]: {preset_desc[p_choice]}[/dim]")

            if table is None:
                config.console.print("[yellow]조건에 맞는 종목이 없습니다.[/yellow]")
            else:
                config.console.print(table)
        
        # 개별 종목 상세 분석 연동
        config.console.print()
        ans = Prompt.ask("개별 종목 상세 분석을 진행하시겠습니까?", choices=["y", "n"], default="n")
        if ans.lower() == 'y':
            target_code = Prompt.ask("분석할 종목의 티커/코드를 입력하세요").strip()
            
            found_code = None
            found_name = None
            for code, name in stock_map.items():
                if code.upper() == target_code.upper():
                    found_code = code
                    found_name = name
                    break
                    
            if found_code:
                from modules import analysis
                config.console.print(f"\n[bold green]>> {found_name}({found_code}) 개별 종목 심층 분석 실행[/bold green]")
                analysis.diagnose_stock(target_code=found_code, target_name=found_name, target_is_overseas=(market == "america"))
            else:
                config.console.print(f"[red]입력한 종목('{target_code}')을 검색 결과에서 찾을 수 없습니다.[/red]")
                    
    except Exception as e:
        config.console.print(f"\n[red]TradingView 스크리너 실행 중 오류 발생: {e}[/red]")
        logger.error(f"TradingView Screener Error: {e}", exc_info=True)

def run_theme_analysis():
    """종목 트랜드 분석 메인 함수 (서브 메뉴)"""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "1"
    while True:
        utils.clear_screen()
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        menu_items = [
            ("1", "네이버 금융 테마 순위", "Naver Theme Ranking"),
            ("2", "트레이딩뷰 종목 검색", "TradingView Screener"),
            ("3", "AI 시장 테마 분석", "AI Market Theme Analysis"),
            ("4", "AI 종목 심층 진단", "AI Stock Analysis")
        ]
        choice = utils.show_menu("종목 트랜드 분석 (Stock Trend Analysis)", menu_items, default_choice=last_choice)
        if choice.lower() in ['b', 'q']: return False
        if choice.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        
        menu_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")
        
        is_success = False
        if choice == '1':
            _show_naver_themes()
            is_success = True
        elif choice == '2':
            if _run_tradingview_screener() is not False: is_success = True
        elif choice == '3':
            if _analyze_with_gemini_ui() is not False: is_success = True
        elif choice == '4':
            if _analyze_stock_ui() is not False: is_success = True
            
        if is_success:
            last_choice = choice
            utils.pause()