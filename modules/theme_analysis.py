import logging
import requests
import sqlite3
import concurrent.futures
from bs4 import BeautifulSoup
from datetime import datetime
from rich.panel import Panel
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.padding import Padding
from google import genai
from google.genai import types
import config
from modules import db_manager

logger = logging.getLogger(__name__)

def _init_theme_db():
    try:
        # Use the global db instance
        conn = db_manager.db._get_conn()
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
        # Use the global db instance
        conn = db_manager.db._get_conn()
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
        # Use the global db instance
        conn = db_manager.db._get_conn()
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
        당신은 여의도 최고의 전략가이자 국제 정세와 매크로 경제에 정통한 주식 분석 전문가입니다. 
        구글 검색을 통해 실시간 글로벌 뉴스 및 한국 증시 데이터를 수집한 후, 오늘 한국 증시(KOSPI, KOSDAQ)를 관통하는 '글로벌 연동형 핵심 테마 5가지'를 분석해 주세요.

        반드시 다음의 상세 가이드라인을 엄격히 준수하세요:

        1. **국제 정세 및 매크로 환경 분석 (신규 추가)**:
          - 각 테마를 선정하기 전, 현재 한국 증시에 가장 큰 영향을 끼치고 있는 '글로벌 이슈 3가지'(예: 미 연준 금리 향방, 지경학적 갈등, 유가/환율 변동성 등)를 요약하고, 이것이 오늘 국내 테마 형성에 어떤 논리로 작용했는지 서술할 것.

        2. **실시간 테마 및 모멘텀 분석**:
          - 거래대금 상위 및 뉴스 노출 빈도를 바탕으로 주도 테마 선정.
          - 글로벌 공급망 변화, 외신 보도, 해외 기업(엔비디아, 테슬라 등)과의 연동성을 구체적으로 명시할 것.

        3. **종목 및 수급 분석**:
          - 각 테마별 대장주 1개와 핵심 관련주 2개를 선정.
          - 해당 종목들에 대한 외국인/기관의 수급 특징(숏커버링 유입 여부, 바스켓 매수 등)을 실시간 속보 기반으로 분석할 것.

        4. **기술적 전략 및 리스크 관리**:
          - 현재 시점의 매수 적정성(과열 여부) 판정.
          - 국제 정세 급변에 따른 '변동성 리스크'와 '재료 소멸 시점'을 날카롭게 지적할 것.

        5. **보고서 형식**: 
          - [글로벌 매크로 요약] -> [테마별 상세 분석 표] -> [최종 투자 총평] 순서로 작성하여 가독성 높은 '마켓 인사이트 리포트' 형태로 출력할 것.
        """

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[bold green]Google Gemini가 실시간 시장 정보를 검색 중입니다...[/bold green]\n[dim]  (모델: {config.GEMINI_MODEL}, 도구: Google Search)[/dim]", total=None)
            # Google Search 도구는 v1beta 버전에서 지원됩니다.
            client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={'api_version': 'v1beta'})
            
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    response_modalities=["TEXT"]
                )
            )
            
            # 응답 처리
            if response.candidates and response.candidates[0].content.parts:
                return response.text
            else:
                return "검색 결과가 없거나 응답을 생성하지 못했습니다."

    except KeyboardInterrupt:
        config.console.print("\n[yellow]사용자에 의해 분석이 중단되었습니다.[/yellow]")
        return None
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            config.console.print("\n[yellow]Gemini API 호출 한도 초과 (Rate Limit)[/yellow]")
            config.console.print("[dim]  무료 티어 사용량이 초과되었습니다. 잠시 후 다시 시도하세요.[/dim]")
            logger.warning(f"Gemini API Rate Limit: {e}")
        elif "400" in error_msg and "tools" in error_msg:
            config.console.print("\n[red]Gemini API 오류: Google Search 도구 사용 불가[/red]")
            config.console.print("[dim]  API 설정 오류 또는 모델이 도구를 지원하지 않습니다.[/dim]")
            logger.error(f"Gemini API Error (Tools): {e}")
        else:
            config.console.print(f"\n[red]오류 발생: {e}[/red]")
            logger.error(f"Gemini Search Error: {e}")
        return None

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
        config.console.print("[1] 기존 결과 보기")
        config.console.print("[2] 새로 분석 시작")
        
        choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
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
    config.console.print("\n[bold]Gemini에게 요청할 내용을 입력하세요:[/bold]")
    config.console.print("[dim](예: 현재 2차전지 관련 최신 뉴스와 전망을 요약해줘)[/dim]")
    
    user_prompt = Prompt.ask("입력 [dim](취소: q)[/dim]")
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
    config.console.print("[1] 네이버 금융 테마 순위 (크롤링)")
    config.console.print("[2] AI 시장 주도 테마 분석 (Gemini)")
    config.console.print("[3] 직접 프롬프트 입력 (Gemini)")
    config.console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q"], default="1")
    
    if choice == '1':
        _show_naver_themes()
    elif choice == '2':
        _analyze_with_gemini_ui()
    elif choice == '3':
        _analyze_with_custom_prompt_ui()