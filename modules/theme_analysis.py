import logging
from rich.panel import Panel
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn
from google import genai
from google.genai import types
import config

logger = logging.getLogger(__name__)

def analyze_market_trends_with_gemini():
    """
    Gemini의 Google Search Grounding을 사용하여 실시간 시장 테마 분석
    """
    if not config.GEMINI_API_KEY:
        config.console.print("\n[red]※ Gemini API 키가 설정되지 않았습니다.[/red]")
        config.console.print("[dim]  Google AI Studio에서 키를 발급받아 설정해주세요.[/dim]")
        return None

    # 프롬프트 설계
    prompt = """
    현재 시간 기준으로 한국 주식 시장(KOSPI, KOSDAQ)에서 가장 강세를 보이고 있는 '주도 테마' 5가지를 검색해서 찾아줘.
    
    각 테마별로 다음 내용을 정리해줘:
    1. 테마명
    2. 상승 이유 (관련 뉴스나 이슈 요약)
    3. 대표적인 대장주(종목명) 2~3개
    
    보고서 형식으로 깔끔하게 요약해서 출력해줘.
    """

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=config.console
    ) as progress:
        task_desc = f"[bold green]Google Gemini가 실시간 시장 정보를 검색 중입니다...[/bold green]\n[dim]  (모델: {config.GEMINI_MODEL}, 도구: Google Search)[/dim]"
        task = progress.add_task(task_desc, total=None)

        try:
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

def run_theme_analysis():
    """테마 트랜드 분석 메인 함수"""
    config.console.print("\n[bold magenta]=== 테마 트랜드 분석 (Theme Trend Analysis) ===[/]")
    config.console.print("[dim]Google Gemini의 실시간 검색(Grounding)을 통해 현재 시장 주도 테마를 분석합니다.[/dim]\n")

    result = analyze_market_trends_with_gemini()

    if result:
        config.console.print(Panel(Markdown(result), title="실시간 테마 분석 리포트", border_style="cyan"))
        
        config.console.print("\n[dim]※ 위 내용은 AI가 실시간 웹 검색을 통해 생성한 정보입니다.[/dim]")
        config.console.print("[dim]   실제 투자 시에는 반드시 HTS/MTS에서 시세를 다시 확인하시기 바랍니다.[/dim]")