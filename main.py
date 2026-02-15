# main.py
#!/usr/bin/env python3
import sys
import time
from datetime import datetime
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.markup import escape
import argparse
import config
import api
import utils  
from modules import market, analysis, chart, account, manage, trading, backtest
from modules import auto_trade, telegram_bot

def show_help():
    config.console.print("\n[bold cyan]=== [Help] 색상 및 기능 설명 ===[/bold cyan]")
    table = Table(title="지수 및 종목 상태별 색상 조건", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("항목", style="bold"); table.add_column("조건", justify="left")
    table.add_column("색상", justify="center"); table.add_column("비고", justify="left")

    table.add_row("WTI 원유", "가격 ≥ 120", "[magenta]보라색[/]", "에너지 위기 수준")
    table.add_row("", "100 ≤ 가격 < 120", "[red]빨간색[/]", "인플레 강한 압력")
    table.add_row("", "80 ≤ 가격 < 100", "[orange3]주황색[/]", "부담 있지만 정상")
    table.add_row("", "60 ≤ 가격 < 80", "[green]초록색[/]", "이상적인 안정 구간")
    table.add_row("", "40 ≤ 가격 < 60", "[yellow]노란색[/]", "경기 둔화 우려")
    table.add_row("", "가격 < 40", "[blue]파란색[/]", "경기 침체 신호")
    table.add_section()

    table.add_row("천연가스", "가격 ≥ 10", "[magenta]보라색[/]", "에너지 위기")
    table.add_row("", "6 ≤ 가격 < 10", "[red]빨간색[/]", "강한 비용 압력")
    table.add_row("", "4 ≤ 가격 < 6", "[orange3]주황색[/]", "부담")
    table.add_row("", "2.5 ≤ 가격 < 4", "[green]초록색[/]", "안정")
    table.add_row("", "1.5 ≤ 가격 < 2.5", "[yellow]노란색[/]", "수요 둔화")
    table.add_row("", "가격 < 1.5", "[blue]파란색[/]", "경기 침체")
    table.add_section()

    table.add_row("밀", "가격 ≥ 900", "[magenta]보라색[/]", "글로벌 식량 위기")
    table.add_row("", "750 ≤ 가격 < 900", "[red]빨간색[/]", "식량 인플레 심각")
    table.add_row("", "650 ≤ 가격 < 750", "[orange3]주황색[/]", "물가 부담")
    table.add_row("", "500 ≤ 가격 < 650", "[green]초록색[/]", "균형 구간")
    table.add_row("", "400 ≤ 가격 < 500", "[yellow]노란색[/]", "수요 둔화/공급 과잉")
    table.add_row("", "가격 < 400", "[blue]파란색[/]", "디플레/농업  침체")
    table.add_section()

    table.add_row("달러 인덱스", "지수 ≥ 120", "[magenta]보라색[/]", "극단적 강세 (위기 가능성)")
    table.add_row("", "110 ≤ 지수 < 120", "[red]빨간색[/]", "매우 강함")
    table.add_row("", "100 ≤ 지수 < 110", "[orange3]주황색[/]", "강세 구간")
    table.add_row("", "90 ≤ 지수 < 100", "[green]초록색[/]", "중립")
    table.add_row("", "80 ≤ 지수 < 90", "[yellow]노란색[/]", "약세")
    table.add_row("", "지수 < 80", "[blue]파란색[/]", "매우 약함")
    table.add_section()

    table.add_row("달러 환율", "환율 ≥ 1600원", "[magenta]보라색[/]", "시스템 위기 (외환·금융 복합 위기)")
    table.add_row("", "1500 ≤ 환율 < 1600", "[red]빨간색[/]", "위기 구간 (정책 개입 불가피)")
    table.add_row("", "1400 ≤ 환율 < 1500", "[orange3]주황색[/]", "구조적 고환율 (국가 부담 심화)")
    table.add_row("", "1300 ≤ 환율 < 1400", "[yellow]노란색[/]", "강달러 뉴노멀 상단")
    table.add_row("", "1200 ≤ 환율 < 1300", "[green]초록색[/]", "뉴노멀 중립 구간")
    table.add_row("", "1100 ≤ 환율 < 1200", "[cyan]청록색[/]", "원화 강세 (비정상적 안정)")
    table.add_row("", "환율 < 1100", "[blue]파란색[/]", "초강세 원화 (일시적/정책성)")
    table.add_section()

    table.add_row("VIX 변동성 지수", "지수 ≤ 20", "[green]초록색[/]", "안정")
    table.add_row("", "20 < 지수 < 30", "[white]흰색[/]", "중립")
    table.add_row("", "30 ≤ 지수 < 40", "[yellow]노란색[/]", "주의")
    table.add_row("", "40 ≤ 지수 < 50", "[orange3]주황색[/]", "경계")
    table.add_row("", "지수 ≥ 50", "[red]빨간색[/]", "위험")
    table.add_section()

    table.add_row("SOX 반도체 지수", "낙폭 > -5.0%", "[red]빨간색[/]", "초강세 (신고가 근접)")
    table.add_row("", "-15.0% < 낙폭 ≤ -10.0%", "[orange3]주황색[/]", "주의 (단기 추세 이탈)")
    table.add_row("", "-20.0% ≤ 낙폭 ≤ -15.0%", "[yellow]노란색[/]", "경계 (기술적 조정 진입)")
    table.add_row("", "낙폭 < -25.0%", "[blue]파란색[/]", "침체 (기술적 하락장)")                
    table.add_section()

    table.add_row("등락폭/등락률", "상승 (> 0)", "[red]빨간색[/]", "전일 대비 상승")
    table.add_row("", "하락 (< 0)", "[blue]파란색[/]", "전일 대비 하락")
    table.add_row("", "보합 (== 0)", "[white]흰색[/]", "전일 대비 보합")
    table.add_section()

    table.add_row("52주 고점대비", "등락률 > -3.0%", "[red]빨간색[/]", "신고가 근접 (초강세)")
    table.add_row("", "등락률 < -20.0%", "[blue]파란색[/]", "침체/약세장 진입")
    table.add_row("", "-3.0% ~ -20.0%", "[white]흰색[/]", "일반 조정/중립")
    table.add_section()

    table.add_row("종목명 색상", "이평선 정배열 & ADX ≥ 40 & RSI ≥ 70 & CCI ≥ 100", "[magenta]보라색[/]", "과열/하락 반전 주의")
    table.add_row("", "이평선 정배열 & 현재가 > 5일선 & ADX ≥ 30 & RSI ≥ 55 & CCI ≥ 100", "[red]빨간색[/]", "강력한 상승 추세")
    table.add_row("", "이평선 역배열 & 현재가 > 5일선 & ADX ≥ 20 & RSI ≥ 45 & CCI ≥ 0", "[orange3]주황색[/]", "바닥권 상승 반전 시도")
    table.add_row("", "이평선 20선 > 60선 > 5선 & ADX ≥ 30 & RSI ≤ 30 & CCI ≤ 100", "[blue]파란색[/]", "하락 심화/매도 우위")
    table.add_section()

    table.add_row("종목 분류", "8점 이상 & RSI<60 (보수적)", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    table.add_row("", "6~8점 (상승 추세)", "[orange3]상승[/]", "상승 초입/지속 (대기/소량)")
    table.add_row("", "방향성 불명확 단계", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    table.add_row("", "추세 이탈 / 단기 하락", "[yellow]주의[/]", "신규매수 자제/비중축소 고려")
    table.add_row("", "장기추세 붕괴 및 과열", "[blue]위험[/]", "적극 매도/손절 고려")
    table.add_section()

    table.add_row("현재가 (이평선)", "[정배열] 현재가 > 5일선", "[red]빨간색[/]", "강세 지속")
    table.add_row("", "[정배열] 현재가 < 5일 or 20일선", "[dim]회색[/]", "약세 조정")
    table.add_row("", "[정배열] 현재가 < 60일선", "[blue]파란색[/]", "약세 지속 (붕괴)")
    table.add_row("", "[역배열] 현재가 < 5일선", "[blue]파란색[/]", "약세 지속")
    table.add_row("", "[역배열] 현재가 > 20/60일선", "[orange3]주황색[/]", "강세 전환")
    table.add_row("", "[역배열] 현재가 > 5일선", "[white]흰색[/]", "강세 조정 (반등)")
    table.add_row("", "[혼조세] 5일선이 20~60선 사이 & 현재가>20선", "[orange3]주황색[/]", "강세 전환")
    table.add_row("", "[혼조세] 5일선이 20~60선 사이 & 현재가<20선", "[white]흰색[/]", "강세 조정")
    table.add_row("", "[혼조세] 현재가 < 5일선", "[blue]파란색[/]", "약세 지속")
    table.add_section()

    table.add_row("EMA 5일선", "5일선 > 20, 60, 120일선 (정배열)", "[red]빨간색[/]", "강세 (가장 높음)")
    table.add_row("", "5일선이 20일선과 60일선 사이", "[yellow]노란색[/]", "경계")
    table.add_row("", "5일선이 60일선과 120일선 사이", "[orange3]주황색[/]", "주의")
    table.add_row("", "5일선 < 20, 60, 120일선 (역배열)", "[blue]파란색[/]", "약세 (가장 낮음)")
    table.add_section()

    table.add_row("EMA 20일선", "20일선 > 60, 120일선", "[red]빨간색[/]", "골든크로스")
    table.add_row("", "20일선이 60일선과 120일선 사이", "[yellow]노란색[/]", "크로스 경계")
    table.add_row("", "20일선 < 60, 120일선", "[blue]파란색[/]", "데드크로스")
    table.add_section()

    table.add_row("EMA 60일선", "120선 > 60선 > 5, 20선", "[blue]파란색[/]", "역배열")
    table.add_row("", "120선 < 60선 < 5, 20선", "[red]빨간색[/]", "정배열")
    table.add_row("", "이 외", "[yellow]노란색[/]", "혼조세")
    table.add_section()

    table.add_row("EMA 120일선", "60일선 > 120일선 (정배열)", "[red]빨간색[/]", "중장기 상승 추세 (지지)")
    table.add_row("", "60일선 < 120일선 (역배열)", "[blue]파란색[/]", "중장기 하락 추세 (저항)")
    table.add_section()

    table.add_row("파라볼릭 SAR", "주가 > SAR (SAR이 주가 아래)", "[red]빨간색[/]", "상승 추세 (매수/보유)")
    table.add_row("", "주가 < SAR (SAR이 주가 위)", "[blue]파란색[/]", "하락 추세 (매도/청산)")
    table.add_row("", "", "", "")
    table.add_row("", "SAR 는 추세가 끝났는지 아닌지 빠르게 알려주는 지표", "", "추세 유지/종료 판단용")
    table.add_row("", "SAR 전환 발생 + 종가 기준 EMA60 이탈 + RSI 60 이상", "", "추세 종료 확정")
    table.add_row("", "SAR 상승 중 + EMA60 위 + RSI 50~65", "", "보유")
    table.add_row("", "SAR 하향 전환 +  EMA60 유지 + RSI 55 ", "", "관망")
    table.add_row("", "SAR 하향 전환 +  EMA60 종가 이탈 + RSI 65 이상", "", "정리")
    table.add_row("", "SAR은 추세 없을 때 쓰면 안됨", "", "ADX 필수확인")
    table.add_section()

    table.add_row("RSI", "RSI ≥ 70", "[magenta]보라색[/]", "과열 (추격금지)")
    table.add_row("", "55 ≤ RSI < 70", "[red]빨간색[/]", "강세 유지 구간")
    table.add_row("", "45 ≤ RSI < 55", "[orange3]주황색[/]", "강세 조정 구간 (진입후보)")
    table.add_row("", "30 < RSI < 45", "[yellow]노란색[/]", "단기하락 전환가능")
    table.add_row("", "RSI ≤ 30", "[blue]파란색[/]", "하락")
    table.add_row("", "", "", "")
    table.add_row("", "SAR이 알려주는 전환이 과열/과매도 구간인지 확인", "", "SAR 전환 해석")
    table.add_row("", "RSI 70 이상 / 과매수구간", "", "SAR 전환시 단기고점/매수금지/분할매도 ")
    table.add_row("", "RSI 60 ~ 70 부근 / 강한상승구간", "", "SAR 전환시 조정가능성 높음/단기조정/횡보")
    table.add_row("", "RSI 50 ~ 60 부근 / 약한상승구간", "", "SAR 전환시 가짜신호 확률높음")
    table.add_row("", "RSI 40 ~ 50 부근 / 강세조정구간", "", "SAR 전환시 눌림후 재상승 가능성/분할매수")
    table.add_row("", "RSI 30 ~ 40 부근 / 약세진입구간", "", "SAR 전환시 하락추세 전환가능성 증가/신규매수금지")
    table.add_row("", "RSI 30 이하 / 과매도구간", "", "SAR 전환시 단기반등 시그널/신규매수금지")
    table.add_section()

    table.add_row("ADX", "0 ~ 15 미만", "[white]흰색[/]", "추세 없음 (횡보/박스권)")
    table.add_row("", "15 ~ 20 미만", "[yellow]노란색[/]", "추세 형성 중 (CCI 방향 확인)")
    table.add_row("", "20 ~ 30 미만", "[orange3]주황색[/]", "안정적 추세 (매매 최적)")
    table.add_row("", "30 ~ 40 미만", "[red]빨간색[/]", "강한 추세 (과열 주의)")
    table.add_row("", "40 이상", "[magenta]보라색[/]", "과열 (조정 주의)")
    table.add_row("", "", "", "")
    table.add_row("", "ADX가 지금 SAR를 써도 되는지 알려줌", "", "SAR 신뢰도")
    table.add_row("", "ADX 15 미만", "", "SAR 사용금지")
    table.add_row("", "ADX 15 이상 20 미만 ", "", "주의")
    table.add_row("", "ADX 20 이상", "", "SAR 사용가능")
    table.add_section()

    table.add_row("CCI", "CCI ≥ 100", "[red]빨간색[/]", "과열 (추격 금물)")
    table.add_row("", "0 < CCI < 100", "[orange3]주황색[/]", "상승 방향시 (추세 매매)")
    table.add_row("", "-100 < CCI < 0", "[yellow]노란색[/]", "상승 방향시 (반등 시도)")
    table.add_row("", "CCI ≤ -100", "[blue]파란색[/]", "과매도 (저점 탐색)")
    table.add_row("", "", "", "")
    table.add_row("", "SAR 타이밍 정밀화 용도로 사용", "", "SAR 해석")
    table.add_row("", "+100선 이상", "", "SAR 추세 연장")
    table.add_row("", "0선 하향 + SAR 반전", "", "SAR 추세 종료")
    table.add_row("", "-100선 근처", "", "하락 가속 가능")
    table.add_section()

    table.add_row("52주 위치", "90% 이상", "[red]빨간색[/]", "신고가 근접/초강세")
    table.add_row("", "80% 이상", "[orange3]주황색[/]", "상승세 우위")
    table.add_row("", "50% 이하", "[yellow]노란색[/]", "약세/바닥권 진입")
    table.add_row("", "30% 이하", "[blue]파란색[/]", "신저가 근접/침체")
    table.add_row("", "그 외 (50~80%)", "[white]흰색[/]", "중립")
    table.add_section()

    table.add_row("OBV (거래량)", "OBV > OBV 이동평균", "[red]빨간색[/]", "수급 양호 (상승 추세)")
    table.add_row("", "OBV ≤ OBV 이동평균", "[blue]파란색[/]", "수급 약세 (하락 추세)")
    table.add_section()

    table.add_row("투자자 동향", "순매수 (> 0)", "[red]빨간색[/]", "매수 우위")
    table.add_row("(개인/외인/기관)", "순매도 (< 0)", "[blue]파란색[/]", "매도 우위")
    table.add_section()

    table.add_row("MDD (최대 낙폭)", "0% ~ -10%", "[green]초록색[/]", "매우 안정적 (방어력 우수)")
    table.add_row("", "-10% ~ -20%", "[yellow]노란색[/]", "일반적인 주식 투자 수준")
    table.add_row("", "-20% ~ -30%", "[orange3]주황색[/]", "주의 (높은 변동성)")
    table.add_row("", "-30% 이하", "[red]빨간색[/]", "고위험 (큰 하락 감내 필요)")
    table.add_section()

    table.add_row("손익비 (Profit Factor)", "2.0 이상", "[red]빨간색[/]", "매우 훌륭한 전략")
    table.add_row("", "1.5 ~ 2.0 미만", "[orange3]주황색[/]", "우수한 전략")
    table.add_row("", "1.0 ~ 1.5 미만", "[green]초록색[/]", "수익 전략 (평범)")
    table.add_row("", "1.0 미만", "[blue]파란색[/]", "손실 전략 (개선 필요)")
    table.add_section()

    table.add_row("샤프 지수 (Sharpe)", "1.0 이상", "[red]빨간색[/]", "매우 우수 (위험 대비 수익 높음)")
    table.add_row("", "0.5 ~ 1.0", "[green]초록색[/]", "양호")
    table.add_row("", "0.5 미만", "[blue]파란색[/]", "위험 대비 수익 낮음")
    table.add_section()

    config.console.print(table)
    
    # [추가] 매수 점수 산정 기준 테이블 (README 내용 반영)
    config.console.print()
    score_table = Table(title="매매 전략 가이드 (매수/매도 기준)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    score_table.add_column("구분", style="cyan", justify="center")
    score_table.add_column("조건", justify="left")
    score_table.add_column("점수/행동", justify="center", style="red")
    score_table.add_column("의미", justify="left")

    score_table.add_row("이동평균 점수", "현재가 > 20일선", "+1", "단기 상승세")
    score_table.add_row("", "20일선 > 60일선", "+1", "정배열 초기/지속")
    score_table.add_row("", "60일선 > 120일선", "+1", "중장기 정배열")
    score_table.add_row("","", "", "")
    score_table.add_row("SAR 점수", "SAR < 현재가 (주가 아래)", "+1", "상승 추세 진행 중")
    score_table.add_row("","", "", "")
    score_table.add_row("RSI 점수", "40 ≤ RSI ≤ 55", "+2", "이상적인 매수 구간 (무릎~허리)")
    score_table.add_row("", "55 < RSI ≤ 65", "+1", "상승 지속 (강세)")
    score_table.add_row("", "30 ≤ RSI < 40", "+1", "바닥 탈출 시도 (반등)")
    score_table.add_row("","", "", "")
    score_table.add_row("ADX 점수", "ADX ≥ 25", "+1", "추세 강도 확보")
    score_table.add_row("","", "", "")
    score_table.add_row("CCI 점수", "CCI > 0", "+1", "상승 국면 진입")
    score_table.add_row("", "CCI > 100", "+1", "강한 상승 탄력 (중복 적용)")
    score_table.add_row("","", "", "")
    score_table.add_row("OBV 점수", "OBV > OBV 이동평균", "+1", "수급 양호 (거래량 뒷받침)")
    
    # [추가] 시장 필터링 섹션
    score_table.add_section()
    filter_status = "[green]ON[/green]" if getattr(config, 'USE_MARKET_FILTER', True) else "[dim]OFF[/dim]"
    ma_period = getattr(config, 'MARKET_FILTER_MA', 20)
    score_table.add_row(f"시장 필터링 ({filter_status})", f"KOSPI/KOSDAQ 지수 < {ma_period}일 이평선", "[blue]보류[/]", "하락장 감지 시 신규 매수 중단")
    
    # [추가] 필터링 (위험/주의) 섹션
    score_table.add_section()
    score_table.add_row("필터링 (위험)", "60일선 & 120일선 동시 이탈 or RSI ≤ 20", "[blue]위험[/]", "매수 금지 / 즉시 매도 (점수 무관)")
    score_table.add_row("필터링 (주의)", "60일선 or 120일선 이탈, SAR 매도, RSI ≥ 80 or RSI ≤ 30", "[yellow]주의[/]", "신규 진입 자제 (보유는 가능)")

    # [추가] 매수 타이밍 섹션
    score_table.add_section()
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]

    score_table.add_row("매수 (진입)", f"종합 점수 ≥ {buy_score}점 & RSI < {buy_rsi_max}", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    score_table.add_row("관망 (상승)", f"{rise_score}점 ≤ 종합 점수 < {buy_score}점", "[orange3]상승[/]", "상승 초입/지속 (대기/소량)")
    score_table.add_row("관망 (중립)", f"종합 점수 < {rise_score}점", "[white]관망[/]", "방향성 탐색 (거래 비권장)")

    # [추가] 매도 규칙 섹션
    score_table.add_section()
    sell_score = config.SELL_STRATEGY["SELL_SCORE"]
    stop_loss = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)

    score_table.add_row("매도 (익절)", f"수익률 +{take_profit}% 도달", "[red]익절[/]", "목표 수익 달성 (최우선)")
    score_table.add_row("매도 (손절)", f"손실률 {stop_loss}% 도달", "[blue]손절[/]", "손실 제한 (Stop Loss)")
    score_table.add_row("매도 (트레일링)", f"수익 {ts_activation}% 도달 후 고점 대비 -{ts_callback}%", "[blue]매도[/]", "수익 보전 (Trailing Stop)")
    score_table.add_row("매도 (과열)", f"RSI > {take_profit_rsi}", "[red]익절[/]", "RSI 과열 시 이익 실현")
    score_table.add_row("매도 (추세이탈)", f"종합 점수 < {sell_score}점 or 위험 상태", "[blue]매도[/]", "추세 붕괴 시 청산")
    
    config.console.print(score_table)

def main():
    # [추가] 커맨드 라인 인자 파싱
    parser = argparse.ArgumentParser(description='Stock Trading System')
    parser.add_argument('--mode', choices=['1', '2'], help='투자 모드 (1: 모의투자, 2: 실전투자)')
    parser.add_argument('--auto', action='store_true', help='시스템 트레이딩 자동 시작 및 로그 뷰어 실행')
    parser.add_argument('--no-bot', action='store_true', help='텔레그램 봇 명령어 수신(폴링) 비활성화 (알림 전송은 유지)')
    args = parser.parse_args()

    # [추가] 로깅 설정 초기화
    config.setup_logging()

    # 1. 환경 설정 로드
    config.session.initialize(mode=args.mode)
    
    # [추가] CLI 인자로 봇 비활성화 요청 시 설정 변경
    if args.no_bot:
        config.ENABLE_TELEGRAM = False
        config.console.print("[yellow][System] 텔레그램 봇 명령어 수신 기능을 비활성화합니다. (알림 전송만 가능)[/yellow]")

    # 2. 종목 데이터 로드
    config.session.load_stock_config()
    
    # 3. 토큰 발급 (초기 실행 시 모드에 따라 즉시 발급)
    if config.session.is_simulation:
        api.get_access_token()
    else:
        api.get_real_access_token()

    # [추가] 체결 감시자 시작 (백그라운드)
    auto_trade.ConclusionMonitor().start()

    # [추가] 텔레그램 명령어 수신 시작 (백그라운드)
    telegram_cmd = telegram_bot.TelegramCommander()
    telegram_cmd.start()

    trader = auto_trade.AutoTrader()
    last_choice = "1"
    
    # [추가] 자동 시작 모드 처리
    if args.auto:
        config.console.print("\n[bold magenta]=== 자동 시작 모드 (Auto Start) ===[/]")
        # 비대화형 모드로 트레이딩 시작
        trader.start(interactive=False)
        
        # 로그 뷰어 실행 (메인 스레드 블로킹 유지)
        time.sleep(1)
        trader.view_log_file()
    
    try:
        while True:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            env_str = "[모의투자]" if config.session.is_simulation else "[실전투자]"
            env_color = "green" if config.session.is_simulation else "bold red"
            print("\n" + "━"*50)
            config.console.print(f" [cyan]시스템 시간: {now_str}[/cyan] | [{env_color}]{env_str}[/]")
            print("━"*50)
            config.console.print("[0] 자산 관리"); config.console.print("[1] 시장 지수 조회")
            config.console.print("[2] 종목 시세 분석"); config.console.print("[3] 종목 차트 분석")
            
            trader_status = ""
            if trader.is_running:
                if trader.is_market_open():
                    trader_status = " [bold green](RUNNING)[/]"
                else:
                    trader_status = " [bold yellow](WAITING)[/]"
                
            config.console.print("[4] 관심 종목 관리"); config.console.print(f"[5] 시스템 트레이딩{trader_status}")
            config.console.print(f"[6] 전략 백테스팅"); config.console.print("[7] [red]매수[/red] 주문")
            config.console.print("[8] [blue]매도[/blue] 주문"); config.console.print("[9] [magenta]정정/취소[/magenta] 주문")
            config.console.print("[Q] 종료  |  [H] 도움말 (색상 설명)")
            print("─" * 50); config.console.print()
            try:
                choice = Prompt.ask("선택 ", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "q", "Q", "h", "H"], default=last_choice)
                if choice.lower() == "q": break
                
                if choice.lower() == "h": 
                    show_help()
                    continue
                    
                last_choice = choice
                if choice == "0": 
                    account.asset_management_menu()
                elif choice == "1": market.show_market_indices()
                elif choice == "2": analysis.show_stock_analysis()
                elif choice == "3": 
                    code, name, is_ovs = utils.select_stock_for_chart()
                    if code: chart.generate_visual_chart(code, name, is_ovs)
                elif choice == "4": manage.manage_stock_menu()
                elif choice == "5": auto_trade.system_trading_menu() 
                elif choice == "6": backtest.run_backtest()
                elif choice == "7": trading.send_order("buy")
                elif choice == "8": trading.send_order("sell")
                elif choice == "9": trading.modify_order()
            except KeyboardInterrupt: break
            except Exception as e:
                config.console.print(f"\n[bold red]치명적인 오류 발생: {escape(str(e))}[/bold red]")
    finally:
        config.console.print()
        with config.console.status("[bold red]시스템 종료 프로세스 진행 중... (자동매매/DB/텔레그램 정리)[/]"):
            # [추가] 프로그램 종료 시 실행 중인 자동매매가 있다면 안전하게 종료 (텔레그램 알림 발송)
            if trader.is_running:
                trader.stop()
            
            # [추가] 체결 감시자 및 텔레그램 봇 종료
            auto_trade.ConclusionMonitor().stop()
            telegram_cmd.stop()

        config.console.print("[yellow]프로그램을 종료합니다.[/yellow]")
        
if __name__ == "__main__":
    main()
