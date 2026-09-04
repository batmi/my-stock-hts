import logging
import re
import requests
import sqlite3
import concurrent.futures
import math
from contextlib import closing
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
from core import utils
import json
from core import context # [추가]
from modules import prompts # [추가] 외부 프롬프트 템플릿 로드
from core.executors import ai_executor, io_executor
from modules import db_manager

# [최적화] google.genai(httpx·pydantic 등 포함)는 AI 기능 최초 사용 시점에 지연 임포트한다
# → 프로그램 시작 시간 단축 (라즈베리파이에서 특히 유효).
# 테스트가 modules.theme_analysis.genai 를 직접 patch하는 관행을 유지하기 위해
# 모듈 전역 genai 심볼은 그대로 두고 _ensure_genai()가 최초 1회만 채운다.
#
# [2026-08-24 SDK 이전] google-generativeai → google-genai.
#  구 패키지는 지원 종료(EOL)를 선언했다("All support ... has ended").
#  달라진 것은 셋이다:
#    1) 전역 configure() 가 없다 → Client 인스턴스를 만들어 들고 다닌다(_gemini_client).
#    2) GenerativeModel 클래스가 없다 → client.models.generate_content_stream(model=...).
#    3) 스트리밍이 **누적 객체가 아니라 청크 제너레이터**다 → 여기서 직접 모은다
#       (_StreamedResponse). 구 SDK 는 반환 객체가 순회 후 .text 로 전체를 줬다.
genai = None
_GENAI_IMPORT_TRIED = False
_GENAI_CLIENT = None
_GENAI_CLIENT_KEY = None

def _ensure_genai():
    """google.genai를 최초 사용 시 임포트해 전역 genai에 바인딩한다.

    미설치 시 None 유지. 이미 로드됐거나 테스트가 genai를 patch한 경우 그대로 반환한다.
    """
    global genai, _GENAI_IMPORT_TRIED
    if genai is None and not _GENAI_IMPORT_TRIED:
        _GENAI_IMPORT_TRIED = True
        try:
            import google.genai as _genai
            genai = _genai
        except ImportError:
            genai = None
    return genai


def _gemini_client():
    """API 키로 만든 genai.Client (키가 바뀌면 새로 만든다).

    구 SDK 의 genai.configure() 는 프로세스 전역이라 한 번 부르면 끝이었지만, 신 SDK 는
    클라이언트가 키를 들고 있다. 매 호출마다 새로 만들면 커넥션 풀이 매번 버려지므로
    키를 캐시 열쇠로 삼아 재사용한다 — 테스트가 config.GEMINI_API_KEY 를 바꿔치기해도
    다음 호출에서 알아서 갈아탄다.
    """
    global _GENAI_CLIENT, _GENAI_CLIENT_KEY
    sdk = _ensure_genai()
    if sdk is None:
        return None
    key = config.GEMINI_API_KEY
    if _GENAI_CLIENT is None or _GENAI_CLIENT_KEY != key:
        _GENAI_CLIENT = sdk.Client(api_key=key)
        _GENAI_CLIENT_KEY = key
    return _GENAI_CLIENT
import config

logger = logging.getLogger(__name__)

def _init_theme_db():
    try:
        # [수정] 스레드 안전성을 위해 매번 새로운 연결 생성
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
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
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
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
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
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
            except Exception: continue
            
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
    if name in config.US_TREASURY_YIELD_BANDS:
        # [수정] 밴드 정의는 config.US_TREASURY_YIELD_BANDS 단일 소스 사용
        #  (지수명 색상은 market, 도움말은 main.show_help가 같은 소스를 공유)
        for band in config.US_TREASURY_YIELD_BANDS[name]["bands"]:
            thr, status = band[0], band[2]
            if thr is None or price >= thr:
                status_desc = status
                break
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
    elif name == "디젤 ULSD":
        # 가솔린이 소비(운전)를 비추는 반면 디젤은 산업·물류·화물을 비춘다.
        #  같은 '고가'라도 읽는 뜻이 다르므로 문구를 따로 둔다.
        if price >= 4.40: status_desc = "산업·물류 비용 쇼크"
        elif 3.70 <= price < 4.40: status_desc = "임계점/원가 전가"
        elif 2.85 <= price < 3.70: status_desc = "물류비 인플레"
        elif 2.25 <= price < 2.85: status_desc = "골디락스"
        elif 1.70 <= price < 2.25: status_desc = "산업 수요 둔화"
        elif price < 1.70: status_desc = "실물 경기 급랭"
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
        # [통일] 섹터 지수·유럽 지수·암호화폐·금/은/구리의 자산별 낙폭 문구는 제거했다.
        #  지수명 색상이 국면 룰로 일원화되면서 이 문구만 낙폭 기준으로 남으면 색과 설명이
        #  어긋나고, 동일 임계값이 market/main에 3중 복제되던 문제도 있었다(아래 공통 문구 사용).
        if not status_desc:
            if yh_rate >= -3.0: status_desc = "신고가 근접/초강세"
            elif yh_rate <= -20.0: status_desc = "침체/약세장 진입"
            else: status_desc = "일반 조정/중립"
            
    return status_desc

def _yh_52w(df):
    """일봉에서 52주 고점. 산출 불가 시 None.

    [왜 직접 세지 않나] 여기서 뽑은 값이 '52주 고점대비 -x%' 문구와
    evaluate_market_indicator 의 국면 판정('신고가 근접/초강세' ↔ '침체/약세장 진입')을
    정하고, 그것이 "절대적인 팩트로 반영할 것"이라는 지시와 함께 AI에 들어간다.
    종전에는 close.tail(250).max() 였다 — 창(250거래일=실측 373일)도 52주보다 넓고,
    종가만 봐서 장중 고가를 놓쳤다. 같은 표의 나머지 지표는 벤더의 52주 고가(year_high)를
    쓰므로 코스피·코스닥·미국채만 다른 잣대로 읽히던 셈이다. _w52_band 가 그 어긋남을
    없애려고 만든 단일 진입점이다(365일 창·고가 기준).
    """
    from modules import analysis
    try:
        h, _ = analysis._w52_band(df)
        return float(h) if h and h > 0 else None
    except Exception:
        return None


def _get_macro_context_str():
    """시스템이 직접 실시간 핵심 매크로 지표를 수집하여 AI에게 주입할 텍스트를 생성"""
    import api
    from modules import analysis
    import concurrent.futures
    
    # 핵심 매크로 지표만 제한적으로 수집 (속도 및 프롬프트 최적화)
    core_tickers = [
        ("코스피", "^KS11"), ("코스닥", "^KQ11"),
        ("나스닥", "^IXIC"), ("S&P500", "^GSPC"),
        ("미국채 2년물 금리", "US02Y"), ("미국채 5년물 금리", "^FVX"), ("미국채 10년물 금리", "^TNX"), ("미국채 30년물 금리", "^TYX"),
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
                    return name, name, curr, rate, _yh_52w(df)

            # [추가] 미국채 금리는 현물(TVC:USxxY, tvDatafeed) 우선 — 현물은 아시아장에도
            #  갱신되어 선물 프록시 추정 불필요. 실패 시 5/10/30년만 yfinance(^FVX류) 폴백,
            #  2년물은 대체 소스가 없어(2YY=F는 유동성 고갈로 죽은 시세) 지표에서 제외한다.
            if name in config.US_TREASURY_SPOT_SYMBOLS:
                df2 = analysis.get_us_treasury_spot_data(config.US_TREASURY_SPOT_SYMBOLS[name])
                if df2 is not None and not df2.empty and len(df2) >= 2:
                    curr = float(df2['close'].iloc[-1])
                    prev2 = float(df2['close'].iloc[-2])
                    rate = ((curr - prev2) / prev2 * 100) if prev2 > 0 else 0.0
                    return name, name, curr, rate, _yh_52w(df2)
                if name == "미국채 2년물 금리":
                    return name, name, None, None, None

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

def _gemini_error_code(e):
    """예외에서 HTTP 상태코드를 꺼낸다. 없으면 None.

    신 SDK(google.genai)는 APIError.code 로 상태코드를 준다 — 문자열에 '429'가 우연히
    섞이길 기다리는 것보다 정확하다. 문자열만 넘어오는 경로(기존 테스트·구 호출부)도
    있으므로 없으면 조용히 None 이다.
    """
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _is_gemini_rate_limit(error):
    """Gemini 무료 티어 한도 초과(429/RESOURCE_EXHAUSTED/Quota) 여부 판별.

    예외 객체와 문자열을 모두 받는다 — 코드가 있으면 코드로, 없으면 메시지로 판정한다.
    """
    if _gemini_error_code(error) == 429:
        return True
    error_msg = error if isinstance(error, str) else str(error)
    return "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg


def _is_gemini_unavailable(error):
    """Gemini 서버 과부하/일시 사용 불가(503/UNAVAILABLE/high demand) 여부 판별."""
    if _gemini_error_code(error) == 503:
        return True
    error_msg = error if isinstance(error, str) else str(error)
    msg = error_msg.lower()
    return "503" in error_msg or "unavailable" in msg or "high demand" in msg or "overloaded" in msg


class GeminiTimeoutError(Exception):
    """Gemini API 응답 대기 시간 초과 (클라이언트 측 타임아웃)"""


# 스트리밍 수신 중 조각(chunk) 간 최대 대기 시간(초).
# 첫 응답(내부 추론 포함)은 호출별 timeout이 담당하고, 일단 생성이 시작되면
# 이 간격 안에 다음 조각이 도착하지 않을 때만 중단한다.
GEMINI_STREAM_CHUNK_TIMEOUT = 30.0

_STREAM_END = object()


class _StreamedResponse:
    """스트리밍 청크를 하나의 응답처럼 보이게 모은 것 (.text / .candidates).

    구 SDK 는 generate_content(stream=True) 가 돌려준 객체를 끝까지 순회하면 그 객체
    자신이 전체 텍스트를 들고 있었다. 신 SDK 의 generate_content_stream 은 **청크
    제너레이터**라 그런 객체가 없다. 하류(_gemini_text)가 .text 와 .candidates 만 보므로
    그 두 가지를 갖춘 얇은 그릇을 만들어 인터페이스를 유지한다.

    candidates 는 **마지막 청크**의 것을 쓴다 — finish_reason(MAX_TOKENS 등)은 마지막에
    확정되기 때문이다.
    """

    __slots__ = ("text", "candidates")

    def __init__(self, text, candidates):
        self.text = text
        self.candidates = candidates


def _chunk_text(chunk):
    """청크에서 텍스트를 안전하게 꺼낸다. 텍스트 파트가 없으면 빈 문자열.

    thinking 모델은 텍스트가 아닌 파트만 담긴 청크를 보내기도 하는데, 그때 .text 접근이
    예외를 던지거나 경고를 남긴다. 한 조각 때문에 전체 수신이 깨지면 안 된다.
    """
    try:
        return chunk.text or ""
    except Exception:      # noqa: BLE001 - 파트 없음/차단 등 SDK 내부 사정
        return ""


def _gemini_stream(content, model_name, generation_config):
    """모델 하나에 대한 스트리밍 청크 제너레이터를 만든다.

    **테스트가 갈아끼우는 이음매**다. SDK 객체 그래프(Client→models→…)를 통째로 흉내 내는
    대신 이 함수만 patch 하면 되도록 좁게 뚫어 둔다. content 를 첫 인자로 두는 것도 그래서다
    — 호출부 검증이 call_args[0][0] 으로 프롬프트를 바로 집을 수 있다.
    """
    client = _gemini_client()
    cfg = genai.types.GenerateContentConfig(
        # 자동 함수 호출(AFC)을 끈다. 우리는 tools 를 넘기지 않으므로 쓸 일이 없는데,
        #  켜져 있으면 SDK 가 호출마다 "직접 쓰지 말고 Chat 을 쓰라"는 경고를 콘솔에 남긴다.
        automatic_function_calling=genai.types.AutomaticFunctionCallingConfig(disable=True),
        **generation_config)
    return client.models.generate_content_stream(
        model=model_name, contents=content, config=cfg)


def _gemini_stream_response(content, model_name, generation_config, first_timeout):
    """스트리밍으로 응답을 조각 단위로 수신해 누적한다.

    first_timeout은 모델의 내부 추론(thinking)을 포함한 **첫 응답 조각까지의** 제한이고,
    그 뒤로는 조각 간 간격이 GEMINI_STREAM_CHUNK_TIMEOUT를 넘을 때만 중단한다. 전체 생성이
    오래 걸려도 진행 중인 응답은 끝까지 받는다.

    [함정 · 신 SDK] generate_content_stream 은 **제너레이터 함수**라 호출만으로는 네트워크를
    타지 않는다(구 SDK 의 generate_content(stream=True) 는 첫 조각까지 블로킹했다). 그래서
    첫 조각을 여기서 **강제로 당긴다** — 당기지 않으면 첫 응답 예산이 first_timeout(최대
    150초)이 아니라 조각 간 제한(30초)으로 쪼그라들어, 추론이 긴 모델이 통째로 타임아웃난다.

    (future.cancel()은 이미 실행 중인 요청을 중단하지 못하므로, 타임아웃된 요청은
    백그라운드에 남지만 ai_executor 워커가 여유 있어 재시도는 가능하다.)

    반환: _StreamedResponse (.text로 전체 텍스트 접근 가능)
    """
    def _open_and_pull_first():
        stream_iter = iter(_gemini_stream(content, model_name, generation_config))
        return stream_iter, next(stream_iter, _STREAM_END)

    future = ai_executor.submit(_open_and_pull_first)
    try:
        stream_iter, chunk = future.result(timeout=first_timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise GeminiTimeoutError(f"TimeoutError: API 응답 대기 시간 초과 (첫 응답 {int(first_timeout)}초)")

    parts = []
    last = None
    while chunk is not _STREAM_END:
        parts.append(_chunk_text(chunk))
        last = chunk
        chunk_future = ai_executor.submit(next, stream_iter, _STREAM_END)
        try:
            chunk = chunk_future.result(timeout=GEMINI_STREAM_CHUNK_TIMEOUT)
        except concurrent.futures.TimeoutError:
            chunk_future.cancel()
            raise GeminiTimeoutError(
                f"TimeoutError: API 응답 대기 시간 초과 "
                f"(스트리밍 중 {int(GEMINI_STREAM_CHUNK_TIMEOUT)}초간 수신 없음)")

    return _StreamedResponse("".join(parts), getattr(last, "candidates", None))


def _gemini_generate(content, generation_config, timeout, timeout_retries=1):
    """기본 모델(config.GEMINI_MODEL)로 콘텐츠 생성을 요청하되,
    무료 티어 한도 초과(429) 시 폴백 모델(config.GEMINI_FALLBACK_MODEL)로
    자동 전환하여 재시도한다. 안내 메시지는 화면에 그대로 출력한다.

    응답은 스트리밍(stream=True)으로 수신하며, timeout은 첫 응답 조각까지의
    제한으로 적용된다. 타임아웃 시에는 같은 모델로 timeout_retries회까지
    재시도한다.

    반환: generate_content 응답 객체. 최종 실패 시 예외를 그대로 전파한다.
    """
    models = [config.GEMINI_MODEL]
    fallback = getattr(config, "GEMINI_FALLBACK_MODEL", "")
    if fallback and fallback != config.GEMINI_MODEL:
        models.append(fallback)

    last_exc = None
    for idx, model_name in enumerate(models):
        try:
            for attempt in range(timeout_retries + 1):
                try:
                    return _gemini_stream_response(content, model_name, generation_config, timeout)
                except GeminiTimeoutError:
                    if attempt < timeout_retries:
                        config.console.print("\n[yellow]API 응답 대기 시간 초과 - 같은 모델로 다시 시도합니다...[/yellow]")
                        logger.warning(f"Gemini timeout on {model_name} (attempt {attempt + 1}/{timeout_retries + 1}); retrying")
                        continue
                    raise
        except Exception as e:
            last_exc = e
            has_fallback = idx < len(models) - 1
            if has_fallback and _is_gemini_rate_limit(e):
                next_model = models[idx + 1]
                config.console.print(f"\n[yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {model_name}[/yellow]")
                config.console.print(f"[dim]  무료 티어 사용량이 초과되어 '{next_model}' 모델로 자동 전환 후 다시 시도합니다.[/dim]")
                logger.warning(f"Gemini rate limit on {model_name}; falling back to {next_model}. {e}")
                continue
            if has_fallback and _is_gemini_unavailable(e):
                next_model = models[idx + 1]
                config.console.print(f"\n[yellow]Gemini 서버 과부하 (503 High Demand) - 모델: {model_name}[/yellow]")
                config.console.print(f"[dim]  일시적인 수요 급증으로 '{next_model}' 모델로 자동 전환 후 다시 시도합니다.[/dim]")
                logger.warning(f"Gemini unavailable (503) on {model_name}; falling back to {next_model}. {e}")
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No generative models available or unknown error occurred.")


def _gemini_text(res, default="분석 결과를 생성하지 못했습니다."):
    """Gemini 응답에서 텍스트를 안전하게 추출한다.

    - 출력 토큰 한도(finish_reason=MAX_TOKENS)로 잘린 경우 경고 문구를 덧붙여
      잘림이 묵살되지 않게 한다.
    - thinking 모델은 내부 추론 토큰도 max_output_tokens에서 차감되므로,
      텍스트 파트가 아예 없어 .text 접근이 예외를 던지는 경우도 방어한다.
    """
    if res is None:
        return default
    try:
        text = res.text
    except Exception as e:
        logger.warning(f"Gemini 응답 텍스트 추출 실패 (파트 없음/차단 등): {e}")
        text = None
    if not text:
        return default
    try:
        finish = res.candidates[0].finish_reason
        finish_name = getattr(finish, "name", "") or str(finish)
        if finish_name == "MAX_TOKENS" or str(finish) == "2":
            logger.warning("Gemini 응답이 출력 토큰 한도(MAX_TOKENS)로 잘렸습니다.")
            text += "\n\n⚠️ 출력 토큰 한도에 도달하여 응답 뒷부분이 잘렸습니다. (max_output_tokens)"
    except Exception:
        pass
    return text


def _gemini_error_text(e, error_prefix="분석", style="rich"):
    """Gemini 호출 예외를 사용자 안내 메시지로 표준 변환한다.

    style:
      - "rich":   터미널용 (rich 마크업 색상 + 상세 힌트)
      - "plain":  텔레그램용 (마크업 없는 짧은 평문)
      - "silent": 조용히 None 반환 (백그라운드 작업용)
    """
    msg = str(e)
    if style == "silent":
        return None
    if style == "plain":
        if _is_gemini_rate_limit(msg):
            return "⚠️ Gemini API 호출 한도 초과 (Rate Limit)"
        if _is_gemini_unavailable(msg):
            return "⚠️ Gemini 서버 과부하 (503) - 잠시 후 다시 시도"
        if any(c in msg.lower() for c in ("timeouterror", "deadline", "timeout")):
            return "⚠️ Gemini API 응답 지연 (Timeout)"
        return f"⚠️ {error_prefix} 중 오류 발생: {msg}"
    # rich (터미널 마크업)
    if _is_gemini_rate_limit(msg):
        return (f"⚠️ [yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]\n"
                f"[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]")
    if _is_gemini_unavailable(msg):
        return ("⚠️ [yellow]Gemini 서버 과부하 (503 High Demand)[/yellow]\n"
                "[dim]  구글 서버의 일시적인 수요 급증입니다. 잠시 후 다시 시도해주세요.[/dim]")
    if "400" in msg and "tools" in msg:
        return (f"⚠️ [red]Gemini API 오류: Google Search 도구 사용 불가 - 모델: {config.GEMINI_MODEL}[/red]\n"
                f"[dim]  API 설정 오류 또는 '{config.GEMINI_MODEL}' 모델이 도구를 지원하지 않을 수 있습니다.[/dim]")
    if "404" in msg and "NOT_FOUND" in msg:
        return (f"⚠️ [red]Gemini 모델을 찾을 수 없습니다 (404 Not Found) - 모델: {config.GEMINI_MODEL}[/red]\n"
                f"[dim]  설정된 모델명이 유효하지 않거나, 해당 API 버전에서 지원되지 않습니다.[/dim]\n"
                f"[dim]  config.py의 GEMINI_MODEL 설정을 확인하세요.[/dim]")
    if any(c in msg.lower() for c in ("timeouterror", "deadline", "timeout")):
        return (f"⚠️ [yellow]API 서버 응답 대기 시간 초과 (Timeout) - 모델: {config.GEMINI_MODEL}[/yellow]\n"
                f"[dim]  구글 서버가 현재 불안정합니다. 잠시 후 다시 시도해주세요.[/dim]")
    return f"⚠️ [red]{error_prefix} 중 오류 발생: {msg}[/red]"


def _run_gemini_report(prompt_content, *, label="분석", timeout=60.0, generation_config=None,
                       default="분석 결과를 생성하지 못했습니다.", error_style="rich", error_prefix=None):
    """리포트형 Gemini 호출 공통 래퍼.

    설정 확인 → 생성(429 시 폴백 모델 재시도) → 텍스트 추출(잘림 경고)
    → 오류 메시지 표준화까지의 보일러플레이트를 한곳으로 모은다.
    """
    if _ensure_genai() is None or not config.GEMINI_API_KEY:
        if error_style == "silent":
            return None
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    gen_cfg = {"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 8192}
    if generation_config:
        gen_cfg.update(generation_config)

    try:
        logger.debug(f"[GEMINI_AI_DEBUG] {label} 요청 - API 호출 대기 시작 (모델: {config.GEMINI_MODEL})")
        res = _gemini_generate(prompt_content, gen_cfg, timeout)
        logger.debug(f"[GEMINI_AI_DEBUG] {label} 요청 - API 응답 수신 성공")
        return _gemini_text(res, default=default)
    except Exception as e:
        logger.error(f"[GEMINI_AI_DEBUG] Gemini {label} Error: {e}", exc_info=True)
        return _gemini_error_text(e, error_prefix or label, error_style)


def analyze_market_trends_with_gemini(custom_prompt=None):
    """
    시스템이 수집한 매크로 지표 + Gemini의 학습된 지식으로 시장 테마를 분석한다.

    [주의 · 2026-08-25] **Google Search Grounding(실시간 웹 검색)은 쓰지 않는다.** 검색
     도구(tools) 전달은 2026년 초 ea3f1ea 에서 제거됐는데(장전 브리핑 오류 대응) 문구만
     남아 "AI가 오늘 뉴스를 봤다"고 읽히고 있었다. 프롬프트(prompts.py)는 이미 모델에게
     '실시간 검색이 불가능하다'고 알리고 있어 서로 어긋나 있었다. 매매 판단에 쓰이는
     화면이라 표기를 사실에 맞춘다. 실시간성이 필요한 재료는 시스템이 직접 모아
     프롬프트에 넣는다(_get_macro_context_str).
    """
    if _ensure_genai() is None:
        config.console.print("\n[red]※ google-genai 라이브러리가 설치되지 않았습니다.[/red]")
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
            
            try:
                logger.debug("[GEMINI_AI_DEBUG] 테마 분석 요청 - API 호출 대기 시작")

                response = _gemini_generate(prompt, {
                    "temperature": 0.2,
                    "top_p": 0.95,
                    "max_output_tokens": 8192,
                }, 150.0)

                logger.debug("[GEMINI_AI_DEBUG] 테마 분석 요청 - API 응답 수신 성공")
                text = _gemini_text(response, default="검색 결과가 없거나 응답을 생성하지 못했습니다.")
                # 테마 리포트도 대장주를 '종목명(코드)'로 적으므로 동일하게 검증한다.
                return verify_stock_codes(text)
            except Exception as e:
                raise e

    except KeyboardInterrupt:
        config.console.print("\n[yellow]사용자에 의해 분석이 중단되었습니다.[/yellow]")
        return None
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg or "Quota" in error_msg:
            config.console.print(f"\n[yellow]Gemini API 호출 한도 초과 (Rate Limit) - 모델: {config.GEMINI_MODEL}[/yellow]")
            config.console.print("[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]")
            logger.warning(f"Gemini API Rate Limit (Model: {config.GEMINI_MODEL}): {e}")
        elif _is_gemini_unavailable(error_msg):
            config.console.print("\n[yellow]Gemini 서버 과부하 (503 High Demand)[/yellow]")
            config.console.print("[dim]  구글 서버의 일시적인 수요 급증입니다. 잠시 후 다시 시도해주세요.[/dim]")
            logger.warning(f"Gemini Unavailable 503 (Model: {config.GEMINI_MODEL}): {e}")
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

# ─────────────────────────────────────────────────────────────────────────────
# AI 출력 종목코드 검증 (할루시네이션 차단)
#  - LLM은 6자리 종목코드를 지어내거나 종목명-코드를 뒤바꾸는 실패 모드가 흔하다.
#    프롬프트 지시(_TICKER_GUARD)만으로는 못 막으므로 출력 후 KRX 상장목록과 대조한다.
#  - 텍스트를 지우지 않고 표시만 덧붙인다(사용자가 판단). 상장목록 조회가 실패하면
#    아무 표시도 하지 않는다 — 없는 종목으로 오판하는 쪽이 더 나쁘다.
# ─────────────────────────────────────────────────────────────────────────────

# '삼성전자(005930)' / '• 삼성전자 (005930)' 형태. 이름은 코드 바로 앞 최대 24자만 본다.
_TICKER_MENTION_RE = re.compile(r'([0-9A-Za-z가-힣&·\.\+\-\s]{0,24}?)\(\s*(\d{6})\s*\)')
# AI가 추천 가능한 최소 시총(1천억). 프롬프트로는 검증할 수 없어 여기서 확인한다.
_CURATION_MIN_MARCAP = 100_000_000_000


def _normalize_stock_name(name):
    """종목명 비교용 정규화 — 공백·기호를 제거하고 소문자로 통일."""
    return re.sub(r'[^0-9a-z가-힣]', '', str(name or '').lower())


def verify_stock_codes(text, min_marcap=_CURATION_MIN_MARCAP):
    """AI 리포트의 '종목명(6자리코드)' 표기를 KRX 상장목록과 대조해 표시를 덧붙인다.

    - 상장목록에 없는 코드   → '(코드 ⚠️미상장 코드)'
    - 종목명이 다른 코드     → '(코드 ⚠️실제: 실제종목명)'
    - 시총이 기준 미만       → '(코드 ⚠️시총 XXX억)'
    조회 실패 시에는 원문을 그대로 돌려준다.
    """
    if not text or not isinstance(text, str):
        return text

    # 종목 표기가 없으면 상장목록을 부를 이유가 없다(네트워크·메모리 절약).
    if not _TICKER_MENTION_RE.search(text):
        return text

    try:
        from modules import krx_daily
        listing = krx_daily.get_listing_map()
    except Exception as e:      # noqa: BLE001 - 검증 실패가 리포트를 막아서는 안 된다
        logger.debug(f"[AI검증] 상장목록 조회 실패: {e}")
        return text

    if not listing:
        logger.debug("[AI검증] 상장목록을 얻지 못해 종목코드 검증을 건너뜁니다.")
        return text

    bad_codes, bad_names, small_caps = [], [], []

    def _mark(m):
        raw_name, code = m.group(1), m.group(2)
        entry = listing.get(code)
        if entry is None:
            label = _normalize_stock_name(raw_name) and raw_name.strip() or code
            bad_codes.append(f"{label}({code})")
            return f"{raw_name}({code} ⚠️미상장 코드)"

        notes = []
        actual = entry.get('name') or ''
        got, want = _normalize_stock_name(raw_name), _normalize_stock_name(actual)
        # 이름 앞에 불릿·번호가 붙으므로 완전일치가 아닌 꼬리일치로 본다.
        if want and got and not (got.endswith(want) or want in got):
            bad_names.append(f"{raw_name.strip()}({code}) → 실제 {actual}")
            notes.append(f"⚠️실제: {actual}")

        marcap = entry.get('marcap') or 0
        if min_marcap and 0 < marcap < min_marcap:
            small_caps.append(f"{actual}({code}) {marcap / 100_000_000:,.0f}억")
            notes.append(f"⚠️시총 {marcap / 100_000_000:,.0f}억")

        if not notes:
            return m.group(0)
        return f"{raw_name}({code} {' '.join(notes)})"

    marked = _TICKER_MENTION_RE.sub(_mark, text)
    if not (bad_codes or bad_names or small_caps):
        return marked

    lines = ["", "─" * 30, "⚠️ 시스템 검증 (KRX 상장목록 대조)"]
    if bad_codes:
        lines.append(f"• 존재하지 않는 종목코드: {', '.join(bad_codes)}")
    if bad_names:
        lines.append(f"• 종목명 불일치: {', '.join(bad_names)}")
    if small_caps:
        lines.append(f"• 시총 {min_marcap / 100_000_000:,.0f}억 미만: {', '.join(small_caps)}")
    lines.append("AI 표기를 그대로 신뢰하지 말고, 관심종목 편입 전에 위 항목을 확인하세요.")
    logger.warning(f"[AI검증] 종목코드 이상 감지 - 미상장 {len(bad_codes)}건 / "
                   f"이름불일치 {len(bad_names)}건 / 소형주 {len(small_caps)}건")
    return marked + "\n" + "\n".join(lines)


def analyze_stock_with_gemini(code, name, tech_info_str):
    """특정 종목의 기술적 지표와 모멘텀을 결합하여 심층 진단"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.STOCK_ANALYSIS_PROMPT.format(now=now, name=name, code=code, tech_info_str=tech_info_str)
    return _run_gemini_report(prompt, label=f"[{name}({code})] 종목 심층 진단", error_prefix="분석")

def analyze_chart_image_with_gemini(image_path, name, code, period_str):
    """생성된 종합 분석 차트(PNG 이미지)를 Gemini 비전 모델로 직접 판독하여 심층 진단.

    차트 이미지를 그대로 전달하므로, 수치 텍스트가 아닌 '차트 전체 그림'을 보고 분석한다.
    """
    sdk = _ensure_genai()
    if sdk is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        logger.error(f"[GEMINI_AI_DEBUG] 차트 이미지 로드 실패: {e}")
        return f"⚠️ [red]차트 이미지를 불러올 수 없습니다: {e}[/red]"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.CHART_IMAGE_ANALYSIS_PROMPT.format(
        now=now, name=name, code=code, period_str=period_str
    )
    # Gemini 멀티모달 입력: [프롬프트 텍스트, 이미지 파트] (PIL 없이 바이트로 직접 전달)
    # 구 SDK 는 {"mime_type": ..., "data": ...} dict 를 받아 줬지만, 신 SDK(google-genai)의
    # contents 는 Part 만 허용한다 — dict 를 넘기면 요청 전에 pydantic 검증에서 터진다.
    image_part = sdk.types.Part.from_bytes(data=image_bytes, mime_type="image/png")

    # 이미지 처리로 텍스트 분석보다 여유 있게 (timeout 120초)
    return _run_gemini_report([prompt, image_part], label=f"[{name}({code})] 차트 이미지 분석",
                              timeout=120.0, error_prefix="차트 분석")

def analyze_index_with_gemini(code, name, tech_info_str):
    """시장 지수의 기술적 지표와 매크로 모멘텀을 결합하여 심층 진단"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.INDEX_ANALYSIS_PROMPT.format(now=now, name=name, code=code, tech_info_str=tech_info_str)
    return _run_gemini_report(prompt, label=f"[{name}({code})] 지수 심층 진단", error_prefix="분석")

def evaluate_backtest_with_gemini(code, name, backtest_info, mode='single'):
    """백테스팅 결과를 바탕으로 Gemini에게 평가 및 조언을 요청"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if mode == 'monte_carlo':
        prompt = prompts.BACKTEST_MONTE_CARLO_PROMPT.format(now=now, name=name, code=code, backtest_info=backtest_info)
    elif mode == 'walk_forward':
        prompt = prompts.BACKTEST_WALK_FORWARD_PROMPT.format(now=now, name=name, code=code, backtest_info=backtest_info)
    else:
        prompt = prompts.BACKTEST_SINGLE_PROMPT.format(now=now, name=name, code=code, backtest_info=backtest_info)

    return _run_gemini_report(prompt, label=f"[{name}] 백테스팅 진단", error_prefix="진단")

def generate_trading_autopsy(code, name, buy_time, buy_score, sell_reason, profit_rate, holding_days):
    """건별 매도 체결 시 AI 매매 복기 리포트 작성"""
    prompt = prompts.TRADING_AUTOPSY_PROMPT.format(name=name, code=code, buy_time=buy_time, buy_score=buy_score, holding_days=holding_days, profit_rate=profit_rate, sell_reason=sell_reason)
    return _run_gemini_report(prompt, label=f"[{name}] 매매 복기", default=None,
                              error_style="plain", error_prefix="매매 복기 분석")

def _get_today_trades_str():
    """DB에서 당일 매매 내역을 조회하여 문자열로 반환"""
    try:
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
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
    if _ensure_genai() is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    macro_context = _get_macro_context_str()
    today_trades_str = _get_today_trades_str()
    prompt = prompts.DAILY_CLOSING_PROMPT.format(portfolio_str=portfolio_str, macro_context=macro_context, today_trades_str=today_trades_str)
    return _run_gemini_report(prompt, label="장 마감 브리핑", default=None,
                              error_style="plain", error_prefix="장 마감 브리핑 생성")

def generate_morning_briefing(market_data_str):
    """밤사이 글로벌 지수를 바탕으로 장전 시황 브리핑 생성"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.MORNING_BRIEFING_PROMPT.format(now=now, market_data_str=market_data_str)
    result = _run_gemini_report(prompt, label="장전 브리핑", default=None, error_style="silent")
    # 주도주를 '종목명(코드)'로 추천하므로 발송 전에 KRX 상장목록과 대조한다.
    return verify_stock_codes(result) if result else result

def generate_stock_curation():
    """현재 시점 매크로 지표 및 뉴스를 기반으로 관심 종목 큐레이션 (수동 추가용)"""
    if _ensure_genai() is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    macro_context = _get_macro_context_str()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.STOCK_CURATION_PROMPT.format(now=now, macro_context=macro_context)
    result = _run_gemini_report(prompt, label="큐레이션", default=None,
                                generation_config={"temperature": 0.3},
                                error_style="plain", error_prefix="종목 큐레이션")
    # 관심종목으로 바로 편입될 후보이므로 코드 존재·이름 일치·시총을 반드시 대조한다.
    if not result or result.startswith("⚠️"):
        return result
    return verify_stock_codes(result)

def ask_gemini(question):
    """사용자의 자유 질문에 대해 Gemini API로 답변 생성"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = prompts.ASK_GEMINI_PROMPT.format(now=now, question=question)
    return _run_gemini_report(prompt, label="Q&A", default="검색 결과가 없거나 답변을 생성하지 못했습니다.",
                              error_prefix="AI 답변 생성")

def summarize_disclosures_with_gemini(items_text):
    """관심종목 공시 목록을 받아 호재/악재로 분류·요약."""
    if _ensure_genai() is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다. (config.GEMINI_API_KEY 확인)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = (
        f"당신은 한국 주식 애널리스트입니다. 현재시각 {now}.\n"
        "아래는 관심종목들의 최근 공시 목록입니다. 투자자 관점에서 분석해 주세요.\n\n"
        f"{items_text}\n\n"
        "요구사항:\n"
        "1) 주가에 영향이 큰 공시를 '호재 🔼 / 악재 🔽 / 중립 ▶' 로 분류해 종목별로 간단히 정리\n"
        "2) 특히 주의가 필요한 공시(유상증자·감자·횡령배임·관리종목·불성실공시 등)를 강조\n"
        "3) 마지막에 한 줄 총평\n"
        "한국어로, 마크다운 불릿으로 간결하게 작성하세요."
    )
    return _run_gemini_report(prompt, label="공시 요약", error_prefix="공시 분석")

def get_latest_news_with_gemini(keyword, code=None):
    """특정 종목의 최신 중요 뉴스 5개 검색 (링크 포함)"""
    if _ensure_genai() is None or not config.GEMINI_API_KEY:
        return "⚠️ Gemini API가 설정되지 않았습니다."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 통합 포털 검색을 사용하여 종목 코드(국내/해외) 의존성 완전 제거 및 수집 성공률 100% 확보
    crawled_news = fetch_realtime_news(keyword, limit=10)

    if not crawled_news:
        return f"⚠️ '{keyword}'에 대한 실시간 뉴스 검색 결과가 없습니다. (구글 뉴스 RSS 수집 실패)"

    # [리팩토링] 함수 내 인라인 중복 프롬프트 제거 → prompts.NEWS_SEARCH_PROMPT 템플릿 사용
    prompt = prompts.NEWS_SEARCH_PROMPT.format(now=now, crawled_news=crawled_news, keyword=keyword)
    return _run_gemini_report(prompt, label=f"[{keyword}] 뉴스 검색",
                              default="검색 결과가 없거나 응답을 생성하지 못했습니다.",
                              generation_config={"temperature": 0.1},
                              error_style="plain", error_prefix="뉴스 검색")

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
        config.console.print("[dim]시스템이 최신 매크로 지표를 수집하고 Google Gemini가 학습된 지식으로 테마를 분석합니다. (실시간 웹 검색 없음)[/dim]\n")
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

        config.console.print("[dim]Google Gemini가 학습된 지식으로 분석합니다. (실시간 웹 검색 없음 — 최신 재료는 시스템이 수집해 전달합니다)[/dim]")
        
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
    from core import indicators
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
            
            # 전일 RSI — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None

            w52_pos = 0.0
            if len(df) > 0:
                w52_pos = indicators.w52_position(df, current_price)
                    
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
                    except Exception: pass
            
            score_adj = 0.0
            if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True) and not is_overseas:
                #  판정 정본은 analysis.get_market_type — 모르면 국면 보정을 건너뛴다.
                market_type = analysis.get_market_type(code)
                if market_type:
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

# ==========================================================
# [공용] TradingView 스크리너 - 단일 관리 지점 (Single Source of Truth)
# ----------------------------------------------------------
# 메뉴6(종목발굴·재무분석)과 텔레그램봇 양쪽에서 동일하게 사용한다.
# 프리셋 조건/유동성 필터/정렬/리밋/후처리를 이 한 곳에서만 정의한다.
# ==========================================================
SCREENER_SELECT_COLS = [
    'name', 'description', 'sector', 'close', 'change', 'volume', 'RSI', 'SMA20', 'SMA50', 'SMA200',
    'MACD.macd', 'MACD.signal', 'ADX', 'ADX+DI', 'ADX-DI', 'average_volume', 'price_earnings_ttm', 'price_book_ratio',
    'return_on_equity', 'debt_to_equity', 'price_52_week_high', 'price_52_week_low',
    'dividend_yield_recent', 'relative_volume_10d_calc', 'market_cap_basic', 'Recommend.All'
]

# 프리셋 메타데이터 (정식 ID -> 표시명/전략설명/리밋)
SCREENER_PRESETS = {
    "gainers":  {"name": "당일 급상승 상위 15종목", "limit": 15,  "desc": None},
    "losers":   {"name": "당일 급하락 상위 15종목", "limit": 15,  "desc": None},
    "gapup":    {"name": "갭상승 출발",             "limit": 20,  "desc": "밤사이 재료(공시·실적·수주)로 전일 종가보다 3% 이상 높게 출발하고 강한 거래량이 붙은 종목을 장 초반에 가장 빨리 포착합니다."},
    "breakout": {"name": "신고가 돌파 주도주",      "limit": 200, "desc": "강세장에서 시장을 주도하며 전고점을 뚫고 날아가는 가장 강한 주식을 잡을 때 사용합니다."},
    "pullback": {"name": "정배열 눌림목",           "limit": 20,  "desc": "완벽한 우상향 추세에 있는 주식이 일시적인 조정(과매도)을 받을 때 안전하게 진입하는 스윙 전략입니다."},
    "volume":   {"name": "폭발적 수급 유입",        "limit": 20,  "desc": "평소 조용하던 주식에 세력이나 기관의 강력한 매수세가 유입되며 시세가 분출하기 시작한 종목을 포착합니다."},
    "oversold": {"name": "낙폭과대 바닥 탈출",      "limit": 20,  "desc": "급락장이나 악재로 과도하게 떨어진 주식이 바닥을 다지고 기술적 반등을 시작하는 정확한 타점을 잡습니다."},
    "value":    {"name": "저평가 우량주 턴어라운드", "limit": 20,  "desc": "실적과 가치는 우수하지만 소외되었던 주식이 20일선을 타며 추세가 호전되기 시작하는 중장기 스윙용입니다."},
    "dividend": {"name": "고배당 상승 추세",        "limit": 20,  "desc": "하락장이나 횡보장에서 하방 경직성이 강하고 안전하게 배당을 받으며 느긋하게 투자할 종목을 찾습니다."},
    "reversal": {"name": "상승 추세 전환",          "limit": 200, "desc": "오랜 하락이나 횡보를 끝내고 본격적인 상승 추세로 진입하는 초기(무릎) 타점을 잡아내는 가장 신뢰도 높은 스윙 전략입니다."},
}

# UI 메뉴 순번("1".."9") -> 정식 프리셋 ID 목록 매핑 (메뉴 1번은 급상승+급하락 통합 실행)
SCREENER_MENU_TO_ID = {
    "1": ["gainers", "losers"], "2": ["gapup"], "3": ["breakout"], "4": ["pullback"], "5": ["volume"],
    "6": ["oversold"], "7": ["value"], "8": ["dividend"], "9": ["reversal"]
}

def screener_liquidity_filters(market):
    """시장별 공통 유동성/규모/종목유형 필터 (나노캡·동전주·우선주·ETF 노이즈 제거).

    market_cap_basic 단위가 한국=KRW, 미국=USD로 다르므로 임계값을 시장별로 분기한다.
    종목유형: 한국=보통주만(우선주/ETF/ETN 제외), 미국=보통주+ADR(TSM 등은 type='dr')·우선주 제외.
    반환: (필터 리스트, 사람이 읽는 라벨 문자열)
    """
    from tradingview_screener import Column
    if market == "korea":
        return [Column('market_cap_basic') > 1e11, Column('volume') > 50000,
                Column('type') == 'stock', Column('typespecs').has('common')], "시총 1,000억↑ · 거래량 5만주↑ · 보통주만"
    return [Column('market_cap_basic') > 3e8, Column('close') >= 1.0, Column('volume') > 100000,
            Column('type').isin(['stock', 'dr']), Column('typespecs').has_none_of('preferred')], "시총 $300M↑ · $1↑ · 거래량 10만주↑ · 보통주/ADR만"

def screener_condition_str(market, preset_id):
    """프리셋별 조건 요약 문자열 (화면/텔레그램 공통 표기)."""
    _, lab = screener_liquidity_filters(market)
    return {
        "gainers":  f"({lab} + 당일 상승 + 거래량 평균 이상)",
        "losers":   f"({lab} + 당일 하락 + 거래량 평균 이상)",
        "gapup":    "(시가 갭 +3%↑ 출발 + 당일 상승 유지 + 거래량 2배↑)",
        "breakout": "(완전정배열 20>50>200 + 52주고점 95%↑ + 종가>20일선 + RSI>60 + ADX>20 + MACD골든·0선위 + 당일 상승 + 거래량 1.2배↑)",
        "pullback": "(완전정배열 20>50>200 + 종가가 50일선 위·20일선 아래 + RSI 35~50 + MACD>0 + ADX>20 + 거래량 1.5배 미만 건전한 조정)",
        "volume":   "(평균 거래량 3배↑ 폭증 + 당일 5%↑ 급등 + 종가>20일선 + MACD골든 + RSI<80 과열제외)",
        "oversold": "(RSI<40 + 주가<20일선 + MACD골든 + 당일 2~15% 반등(추격매수 제외) + 거래량 1.5배↑)",
        "value":    "(PER 1~12 + PBR<1.5 + ROE>15% + 부채비율<150% + 20·50일선 위 + MACD골든)",
        "dividend": "(배당률 5~15% + PER 1~15 + 종가>200·50일선 상승추세)",
        "reversal": "(20일<50일 역배열에서 50일선 강세돌파 + MACD골든 + RSI 50~70 + 거래량 1.5배↑ + 52주 중간값 이하)",
    }.get(preset_id, "")

def _screener_noise_filter(df):
    """이름 기반 노이즈 제거: 스팩/리츠/인프라펀드/ETN 등 TradingView type 필터가 못 거르는 종목.

    (예: KB발해인프라·맥쿼리인프라는 type='stock'/typespecs='common'으로 분류되어 있음)
    """
    if df is None or df.empty or 'description' not in df.columns:
        return df
    noise = df['description'].astype(str).str.contains(
        r'(?i)\b(?:SPAC|REIT|ETN|Fund|Trust)\b|Special Purpose|Acquisition Corp', regex=True, na=False)
    return df[~noise]

def _screener_post_breakout(df):
    return df[df['close'] >= df['price_52_week_high'] * 0.95].head(20)

def _screener_post_reversal(df):
    return df[df['close'] <= (df['price_52_week_high'] + df['price_52_week_low']) / 2].head(20)

def build_screener_query(market, preset_id):
    """정식 프리셋 ID로 TradingView 쿼리를 생성한다 (단일 관리 지점).

    반환: (query, post_filter) — post_filter는 get_scanner_data 결과 df에 적용할 후처리 함수(없으면 None).
    """
    from tradingview_screener import Query, Column
    liq, _ = screener_liquidity_filters(market)
    q = Query().set_markets(market).select(*SCREENER_SELECT_COLS)
    post = None

    if preset_id == "gainers":
        # 급상승: 당일 거래량이 평소(10일 평균) 이상 동반된 실질 상승만 (하락 종목 혼입 방지 change>0)
        q = q.where(*liq, Column('relative_volume_10d_calc') > 1.0, Column('change') > 0).order_by('change', ascending=False)
    elif preset_id == "losers":
        # 급하락: 거래량 동반 투매 (상승 종목 혼입 방지 change<0)
        q = q.where(*liq, Column('relative_volume_10d_calc') > 1.0, Column('change') < 0).order_by('change', ascending=True)
    elif preset_id == "gapup":
        # 갭상승 출발: 시가 갭 +3% 이상 + 갭 유지(당일 상승) + 거래량 2배 폭증 (밤사이 재료 포착)
        q = q.where(*liq, Column('gap') > 3.0, Column('change') > 0,
                    Column('relative_volume_10d_calc') > 2.0).order_by('gap', ascending=False)
    elif preset_id == "breakout":
        # 신고가 돌파 주도주: 완전정배열(20>50>200) + 종가>20일선 + 추세확립(MACD 0선 위)
        # + 당일 상승 중 + 거래량 동반(relvol>1.2) — 하락 중이거나 거래량 없는 고RSI 종목 배제
        q = q.where(*liq, Column('SMA20') > Column('SMA50'), Column('SMA50') > Column('SMA200'),
                    Column('close') > Column('SMA20'), Column('RSI') > 60, Column('ADX') > 20,
                    Column('MACD.macd') > Column('MACD.signal'), Column('MACD.macd') > 0,
                    Column('change') > 0, Column('relative_volume_10d_calc') > 1.2).order_by('Recommend.All', ascending=False)
        post = _screener_post_breakout
    elif preset_id == "pullback":
        # 정배열 눌림목: 완전정배열 + 추세강도(ADX>20) 유지 중 단기 조정
        # 거래량 폭증(relvol≥1.5) 조정은 투매 가능성 → 거래량 잠잠한 건전한 눌림만
        q = q.where(*liq, Column('SMA20') > Column('SMA50'), Column('SMA50') > Column('SMA200'),
                    Column('close') > Column('SMA50'), Column('close') < Column('SMA20'),
                    Column('RSI').between(35, 50), Column('MACD.macd') > 0, Column('ADX') > 20,
                    Column('relative_volume_10d_calc') < 1.5).order_by('Recommend.All', ascending=False)
    elif preset_id == "volume":
        # 폭발적 수급: 거래량 3배 폭증 + 급등 + MACD골든, 과열(RSI≥80) 분출후반 제외
        q = q.where(*liq, Column('relative_volume_10d_calc') > 3.0, Column('change') > 5.0,
                    Column('close') > Column('SMA20'), Column('MACD.macd') > Column('MACD.signal'),
                    Column('RSI') < 80).order_by('relative_volume_10d_calc', ascending=False)
    elif preset_id == "oversold":
        # 낙폭과대 바닥탈출: 과매도 반등에 강한 거래량(1.5배) 동반(데드캣 방어)
        # 반등폭 2~15%로 제한 — 이미 15% 이상 급등한 종목은 '바닥 타점'이 아니라 추격 매수
        q = q.where(*liq, Column('RSI') < 40, Column('close') < Column('SMA20'),
                    Column('MACD.macd') > Column('MACD.signal'), Column('change').between(2, 15),
                    Column('relative_volume_10d_calc') > 1.5).order_by('Recommend.All', ascending=False)
    elif preset_id == "value":
        # 저평가 우량 턴어라운드: 가치+수익성+재무안정(부채비율<150%) + 20·50일선 위 추세 호전
        q = q.where(*liq, Column('price_earnings_ttm').between(1, 12), Column('price_book_ratio') < 1.5,
                    Column('return_on_equity') > 15, Column('debt_to_equity') < 1.5,
                    Column('close') > Column('SMA20'), Column('close') > Column('SMA50'),
                    Column('MACD.macd') > Column('MACD.signal')).order_by('Recommend.All', ascending=False)
    elif preset_id == "dividend":
        # 고배당 상승추세: 배당 5~15%(배당함정 제외) + 저PER + 200일선·50일선 위(장기+중기 상승 추세)
        q = q.where(*liq, Column('dividend_yield_recent').between(5, 15), Column('price_earnings_ttm').between(1, 15),
                    Column('close') > Column('SMA200'), Column('close') > Column('SMA50')).order_by('dividend_yield_recent', ascending=False)
    elif preset_id == "reversal":
        # 상승추세 전환: 역배열에서 50일선 강세돌파 + 모멘텀 전환(RSI 50~70, 과열 추격 제외) + 거래량 1.5배 동반
        q = q.where(*liq, Column('SMA20') < Column('SMA50'), Column('close') > Column('SMA50'),
                    Column('MACD.macd') > Column('MACD.signal'), Column('change') > 0, Column('RSI').between(50, 70),
                    Column('relative_volume_10d_calc') > 1.5).order_by('Recommend.All', ascending=False)
        post = _screener_post_reversal

    q = q.limit(SCREENER_PRESETS[preset_id]["limit"])

    # 모든 프리셋 공통: 스팩/리츠/인프라펀드 등 이름 기반 노이즈 제거 후 프리셋별 후처리 적용
    preset_post = post
    def _combined_post(df, _p=preset_post):
        df = _screener_noise_filter(df)
        if _p is not None and df is not None and not df.empty:
            df = _p(df)
        return df
    return q, _combined_post

def _run_tradingview_screener():
    """트레이딩뷰 스크리너 기반 조건 검색 및 종목 발굴"""
    try:
        from tradingview_screener import Query, Column
        import api
        import pandas as pd
        from modules import analysis
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

    preset_items = [
        ("0", "전체 프리셋 순차 스캔", "All Presets"),
        ("1", "당일 급상승/급하락 상위 15종목", "Top Movers"),
        ("2", "갭상승 출발", "Gap-Up Momentum"),
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
    
    # 프리셋 조건/설명 문자열은 공용 정의(SCREENER_*)에서 파생 (단일 관리 지점)
    preset_conditions = {pid: screener_condition_str(market, pid) for pid in SCREENER_PRESETS}
    preset_desc = {pid: SCREENER_PRESETS[pid]["desc"] for pid in SCREENER_PRESETS if SCREENER_PRESETS[pid]["desc"]}

    try:
        target_choices = [str(i) for i in range(1, 10)] if preset_choice == "0" else [preset_choice]
        # 메뉴 순번 -> 실행할 프리셋 ID들로 전개 (메뉴 1번은 급상승+급하락 2개 프리셋)
        target_ids = [pid for mk in target_choices for pid in SCREENER_MENU_TO_ID[mk]]
        stock_map = {}

        # [진행 표시] 프리셋마다 진행바를 따로 열고 닫는다 — 하나의 전체 진행바로 묶으면
        #  9개 프리셋이 다 끝날 때까지 아무것도 볼 수 없다. 먼저 조회된 프리셋 결과를
        #  먼저 읽고 다음 조회를 기다릴 수 있도록, 진행바 종료 직후 그 자리에서 출력한다.
        total_presets = len(target_ids)
        for seq, pid in enumerate(target_ids, 1):
            p_name = SCREENER_PRESETS[pid]["name"]
            label = p_name if total_presets == 1 else f"[{seq}/{total_presets}] {p_name}"

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                console=config.console,
                transient=True
            ) as progress:
                task = progress.add_task(f"[cyan]{label} 검색 중...[/cyan]", total=None)

                # [단일 관리 지점] 프리셋 쿼리/후처리는 build_screener_query()에서 생성 (telegram봇과 공유)
                query, post_fn = build_screener_query(market, pid)

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
                            # [Fix 2026-09-04] 종전에는 그대로 올려보내 바깥 except 로 나갔다.
                            #  '전체 검색'(프리셋 9개)에서 한 프리셋이 실패하면 — 시장마다
                            #  없는 필드 하나면 충분하다 — 나머지 프리셋이 통째로 취소되고
                            #  검색 결과 연동(개별 종목 분석)까지 건너뛰었다. 실패한 프리셋만
                            #  건너뛰고 계속한다.
                            config.console.print(
                                f"\n[yellow]⚠️ '{p_name}' 검색 실패 — 건너뜁니다: {e}[/yellow]")
                            logger.warning(f"Screener preset '{pid}' failed: {e}", exc_info=True)
                            break

                if df is not None and not df.empty and post_fn is not None:
                    df = post_fn(df)

                table = None
                if df is not None and not df.empty:
                    progress.update(task, description=f"[cyan]{label} 결과 정리 중...[/cyan]",
                                    total=len(df), completed=0)

                    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
                    table.add_column("티커/코드", justify="left", style="cyan")
                    table.add_column("종목명", justify="left")
                    table.add_column("업종", justify="left", style="dim", overflow="fold")
                    table.add_column("현재가", justify="right")
                    table.add_column("등락률", justify="right")
                    table.add_column("52주(%)", justify="right")
                    table.add_column("SMA20", justify="right")
                    table.add_column("MACD (Sig)", justify="right")
                    table.add_column("ADX", justify="right")
                    table.add_column("RSI", justify="right")
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
                        plus_di = row.get('ADX+DI', None)
                        minus_di = row.get('ADX-DI', None)
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

                        # ADX 값 뒤에 DMI 우위 방향(▲/▼/●)을 함께 표기 (표기 규칙은 analysis 단일 소스)
                        adx_str = analysis.format_adx_cell(
                            adx if pd.notna(adx) else None,
                            plus_di if pd.notna(plus_di) else None,
                            minus_di if pd.notna(minus_di) else None,
                        )

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
                            macd_str, adx_str, rsi_str, per_str, roe_str, div_str, vol_str, avg_vol_str
                        )
                        progress.advance(task)

            # 진행바(transient)가 닫힌 뒤 이 프리셋 결과를 즉시 출력한다.
            cond_str = f" {preset_conditions[pid]}" if preset_conditions.get(pid) else ""
            config.console.print(f"\n[bold cyan]▶ {p_name}{cond_str}[/bold cyan]")
            if pid in preset_desc:
                config.console.print(f"   [dim]: {preset_desc[pid]}[/dim]")

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
    """종목발굴·재무분석 메인 함수 (서브 메뉴)"""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "1"
    while True:
        utils.clear_screen()
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        menu_items = [
            ("1", "네이버 금융 테마 순위", "Naver Theme Ranking"),
            ("2", "트레이딩뷰 종목 검색", "TradingView Screener"),
            ("3", "AI 시장 테마 분석", "AI Market Theme Analysis"),
            ("4", "AI 종목 심층 진단", "AI Stock Analysis"),
            ("5", "투자 캘린더", "Investment Calendar"),
            ("6", "공시 모니터링", "Disclosure Monitoring"),
            ("7", "수급 · 물량 신호", "Supply-Demand & Overhang"),
            ("8", "재무 스냅샷", "Financial Snapshot")
        ]
        choice = utils.show_menu("종목발굴·재무분석 (Discovery & Financials)", menu_items, default_choice=last_choice)
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
        elif choice == '5':
            from modules.manage import events
            events.show_calendar()
            is_success = True
        elif choice == '6':
            from modules.manage import disclosure
            disclosure.show_disclosures()
            is_success = True
        elif choice == '7':
            from modules.manage import insider
            insider.show_insider_trades()
            is_success = True
        elif choice == '8':
            from modules.manage import financials
            financials.show_financial_snapshot()
            is_success = True

        if is_success:
            last_choice = choice
            utils.pause()