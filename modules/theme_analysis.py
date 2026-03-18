import logging
import requests
import sqlite3
import concurrent.futures
import warnings
from bs4 import BeautifulSoup
from datetime import datetime
from rich.panel import Panel
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.padding import Padding

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

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 프롬프트 설계
    if custom_prompt:
        prompt = f"""
        [현재 시각: {now} (KST)]
        {custom_prompt}
        """
    else:
        prompt = f"""
        [현재 시각: {now} (KST)]
        당신은 여의도 최고의 퀀트 전략가이자 국제 정세와 매크로 경제에 정통한 수석 주식 분석가입니다. 
        구글 검색을 통해 최신 실시간 글로벌 뉴스, 경제 지표, 한국 증시(KOSPI/KOSDAQ) 데이터를 완벽하게 수집한 후, 
        오늘 시장을 지배하는 '핵심 주도 테마 TOP 5'에 대한 **매우 심층적이고 전문적인** 마켓 인사이트 리포트를 작성해 주세요.

        반드시 다음의 상세 가이드라인을 엄격히 준수하여 분량을 충분히 확보하고 깊이 있게 작성해야 합니다:

        1. **글로벌 정세, 매크로 및 증시 환경 브리핑 (지정학적/거시적 관점)**:
          - 글로벌 지정학적 핵심 이슈(미중 무역 갈등, 전쟁/지경학적 분쟁, 주요국 정책 변화 등)가 현재 증시와 공급망에 미치는 영향을 반드시 포함하여 심층 분석할 것.
          - 전일 미 증시 동향 및 오늘 한국 증시에 미치는 영향 (구체적인 지수 등락률 및 주요 섹터 움직임 포함).
          - 핵심 매크로 지표(달러 환율, 국채 금리, 주요 원자재 가격 등)의 현재 흐름과 이것이 투심에 미치는 영향.
          - 오늘 시장의 전체적인 수급 동향(외인/기관 포지셔닝) 및 전반적인 시장 심리(투심) 평가.

        2. **핵심 주도 테마 요약 표 (Markdown Table 필수)**:
          - 오늘 시장을 주도하는 TOP 5 테마를 한눈에 파악할 수 있도록 반드시 **마크다운 표(Table)** 형태로 먼저 요약할 것.
          - 표의 컬럼: [순위 | 테마명 | 상승 강도 | 핵심 트리거(한 줄 요약) | 대장주 및 관련주]

        3. **테마별 딥다이브 심층 분석 및 대응 전략 (TOP 5 각각에 대해 매우 상세히 작성)**:
          각 테마별로 아래 항목을 분리하여 **최소 3~4문장 이상** 구체적으로 서술할 것:
          - **상승 배경 및 촉매제**: 단순 뉴스 나열이 아닌, 해당 뉴스가 왜 시장에서 강력한 매수세로 이어졌는지 논리적 배경 설명.
          - **글로벌 밸류체인 연동성**: 이 테마가 글로벌 공급망이나 해외 빅테크(엔비디아, 테슬라, 애플 등)와 어떤 역학 관계로 연결되어 있는지 심층 분석.
          - **주도주 수급 및 기술적 위치**: 대장주와 2~3등주의 이름과 현재 기술적 위치(예: 52주 신고가 돌파, 주요 이평선 지지 등). 외인/기관의 구체적인 수급 특징.
          - **리스크 및 실전 대응 전략**: 단기적인 차익 실현 가능성, 밸류에이션 부담, 재료 소멸 타이밍 등을 경고. "지금 추격 매수해도 되는가?"에 대한 명확한 뷰(View)와 타점(눌림목 등) 제시.

        4. **향후 체크포인트 (Upcoming Events)**:
          - 이번 주 또는 단기적으로 해당 테마 및 증시 전체의 방향성을 바꿀 수 있는 주요 일정(주요국 경제 지표 발표, 빅테크 실적 발표, 정책 이벤트 등) 2~3가지를 날짜와 함께 구체적으로 제시.

        5. **보고서 출력 형식**:
          - 도입부: [🌟 마켓 브리핑, 매크로 및 글로벌 정세 요약]
          - 요약부: [📊 오늘의 핵심 테마 TOP 5 요약 표] (반드시 표 형태로 출력)
          - 본문: [🔍 테마별 심층 분석 (Deep Dive)] (각 테마별로 소제목을 달아 상세히 서술)
          - 일정: [📅 단기 핵심 체크포인트]
          - 결론: [💡 수석 전략가의 최종 투자 총평 및 포트폴리오 비중 조언]
          - 가독성을 위해 불릿 포인트(•)와 적절한 이모지를 적극적으로 활용하되, 내용은 최대한 풍부하고 깊이 있게 작성할 것.

        [중요] 반드시 실시간 구글 검색을 통해 수집된 최신 정보만을 기반으로 분석을 작성해야 하며, 과거 데이터나 일반적인 상식에 의존한 분석은 허용되지 않습니다.
        또한 모든 숫자는 [현재 시각: {now} (KST)] 기준으로 최신 데이터를 반영해야 합니다.
        """

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[bold green]Google Gemini가 실시간 시장 정보를 분석 중입니다...[/bold green]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            
            # 1. Gemini API 설정
            genai.configure(api_key=config.GEMINI_API_KEY)

            # 2. 모델 설정
            model = genai.GenerativeModel(
                model_name=config.GEMINI_MODEL,
                generation_config={
                    "temperature": 0.2,
                    "top_p": 0.95,
                    "max_output_tokens": 8192,
                },
                tools="google_search_retrieval" # [추가] 실시간 웹 검색(Grounding) 활성화
            )
            
            # 3. 콘텐츠 생성
            response = model.generate_content(prompt)
            
            if response and response.text:
                return response.text
            else:
                return "검색 결과가 없거나 응답을 생성하지 못했습니다."

    except KeyboardInterrupt:
        config.console.print("\n[yellow]사용자에 의해 분석이 중단되었습니다.[/yellow]")
        return None
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
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
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
                generation_config={"temperature": 0.2, "top_p": 0.95, "max_output_tokens": 4096},
                tools="google_search_retrieval" # [추가] 실시간 웹 검색 활성화
        )
        res = model.generate_content(prompt)
        return res.text if res and res.text else "분석 결과를 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"Gemini Stock Analyze Error: {e}")
        return f"⚠️ 분석 중 오류 발생: {e}"

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
    
    🎯 오늘의 주목 섹터 TOP 3 (각 섹터별 상승 명분 1줄 포함)
    1. 
    2. 
    3. 
    """
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
                generation_config={"temperature": 0.3, "top_p": 0.95, "max_output_tokens": 4096},
                tools="google_search_retrieval" # [추가] 실시간 웹 검색 활성화
        )
        res = model.generate_content(prompt)
        return res.text if res and res.text else None
    except Exception as e:
        logger.error(f"Gemini Morning Briefing Error: {e}")
        return None

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
            generation_config={
                "temperature": 0.3,
                "top_p": 0.95,
                "max_output_tokens": 4096,
            },
            tools="google_search_retrieval" # [추가] 실시간 웹 검색 활성화
        )
        response = model.generate_content(prompt)
        
        if response and response.text:
            return response.text
        else:
            return "검색 결과가 없거나 답변을 생성하지 못했습니다."

    except Exception as e:
        logger.error(f"Gemini Ask Error: {e}")
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            return "⚠️ API 호출 한도 초과 (Rate Limit). 잠시 후 다시 시도해주세요."
        elif "404" in error_msg and "NOT_FOUND" in error_msg:
            return f"⚠️ 설정된 Gemini 모델({config.GEMINI_MODEL})을 찾을 수 없습니다."
        else:
            return f"⚠️ AI 답변 생성 중 오류 발생: {e}"

def _show_naver_themes():
    """네이버 금융 테마 순위 출력"""
    with config.console.status("[green]네이버 금융 테마 데이터 수집 중...[/]"):
        themes = fetch_naver_themes()
        
    if not themes:
        config.console.print("[red]테마 데이터를 가져올 수 없습니다.[/red]")
        return

    # 등락률 순 정렬
    themes.sort(key=lambda x: x['rate'], reverse=True)
    
    # 상위 30개 표시
    top_n = 30
    display_themes = themes[:top_n]
    
    # [추가] 상위 테마에 대해 상세 페이지 병렬 크롤링으로 주도주 정보 수집
    with config.console.status("[green]상위 테마의 주도주 정보를 수집 중... (상세 페이지 분석)[/]"):
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
        
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="left")
        grid.add_column(justify="left", style="dim")
        grid.add_row("[1] 기존 결과 보기", "(View Cached)")
        grid.add_row("[2] 새로 분석 시작", "(Analyze New)")
        config.console.print(grid)
        
        choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="2")
        if choice.lower() == 'q': return
        
        if choice == '1':
            result = cached['data']
    
    if not result:
        config.console.print("[dim]Google Gemini의 실시간 검색(Grounding)을 통해 현재 시장 주도 테마를 분석합니다.[/dim]\n")
        result = analyze_market_trends_with_gemini()
        if result:
            _save_theme_analysis(result)

    if result:
        # Markdown 렌더링
        md = Markdown(result)
        
        # Panel 생성 (내부 패딩 적용)
        panel = Panel(md, title="실시간 테마 분석 리포트", border_style="cyan", padding=(1, 2))
        
        # 화면 출력 시 양쪽 마진 적용 (Padding: top, right, bottom, left)
        config.console.print(Padding(panel, (0, 4)))
        
        config.console.print("\n[dim]※ 위 내용은 AI가 실시간 웹 검색을 통해 생성한 정보입니다.[/dim]", justify="center")
        config.console.print("[dim]   실제 투자 시에는 반드시 HTS/MTS에서 시세를 다시 확인하시기 바랍니다.[/dim]", justify="center")

def _analyze_with_custom_prompt_ui():
    """사용자 정의 프롬프트로 Gemini 분석 실행"""
    while True:
        config.console.print("\n[bold]Gemini에게 요청할 내용을 입력하세요:[/bold]")
        
        user_prompt = Prompt.ask("입력 [dim](종료: q 또는 Enter)[/dim]")
        if user_prompt.lower() == 'q' or not user_prompt.strip():
            return

        config.console.print("[dim]Google Gemini가 실시간 검색(Grounding)을 통해 분석합니다.[/dim]\n")
        
        # 사용자 프롬프트 실행 (캐시 저장 안함)
        result = analyze_market_trends_with_gemini(custom_prompt=user_prompt)

        if result:
            md = Markdown(result)
            panel = Panel(md, title="AI 분석 리포트 (Custom)", border_style="cyan", padding=(1, 2))
            config.console.print(Padding(panel, (0, 4)))
            
            config.console.print("\n[dim]※ 위 내용은 AI가 실시간 웹 검색을 통해 생성한 정보입니다.[/dim]", justify="center")
            config.console.print("[dim]   실제 투자 시에는 반드시 HTS/MTS에서 시세를 다시 확인하시기 바랍니다.[/dim]", justify="center")

def run_theme_analysis():
    """테마 트랜드 분석 메인 함수 (서브 메뉴)"""
    config.console.print("\n[bold magenta]=== 테마 트랜드 분석 (Theme Trend Analysis) ===[/]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 네이버 금융 테마 순위", "(Naver Theme Ranking)")
    grid.add_row("[2] 시장 주도 테마 분석", "(Market Theme Analysis)")
    # grid.add_row("[3] 직접 프롬프트 입력", "(Custom Prompt)")
    config.console.print(grid)
    config.console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="2")
    
    if choice == '1':
        _show_naver_themes()
    elif choice == '2':
        _analyze_with_gemini_ui()
    # elif choice == '3':
    #     _analyze_with_custom_prompt_ui()