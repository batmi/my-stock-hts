# main.py
#!/usr/bin/env python3
import sys
import time
import os
from datetime import datetime
import threading
import logging
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import argparse
import config
import context # [추가]
import api
import utils  
from modules import market, analysis, chart, account, manage, trading, backtest, settings, db_manager
from modules import auto_trade, telegram_bot, theme_analysis, db_queue # [추가]

def show_help():
    config.console.print("\n[bold cyan]=== [Help] 색상 및 기능 설명 ===[/bold cyan]")
    table = Table(title="지수 및 종목 상태별 색상 조건", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("항목", style="bold"); table.add_column("조건", justify="left")
    table.add_column("색상", justify="center"); table.add_column("비고", justify="left")

    # [수정] 설정값 로드하여 동적 표시
    ma_period = config.MARKET_REGIME_PARAMS.get('REGIME_MA_PERIOD', 20)
    obv_period = config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5)

    table.add_row("시장 지수", f"지수 > EMA {ma_period}일선 & 이평선우상향 & ADX조건", "[red]빨간색[/]", "강세장 (Bull)")
    table.add_row("(글로벌/원자재/코인)", f"지수 < EMA {ma_period}일선", "[blue]파란색[/]", "약세장 (Bear)")
    table.add_row("", "그 외 구간", "[yellow]노란색[/]", "횡보장 (Sideways)")
    table.add_section()

    table.add_row("브랜트유", "가격 ≥ 125", "[magenta]보라색[/]", "에너지 쇼크: 강제적 수요 파괴 및 스태그플레이션 확정")
    table.add_row("", "105 ≤ 가격 < 125", "[red]빨간색[/]", "임계점: 고금리 긴축 강요, 기업 이익률 급격 둔화")
    table.add_row("", "85 ≤ 가격 < 105", "[orange3]주황색[/]", "고유가 지속: 인플레 압력 상존, 실물 경제 마지노선")
    table.add_row("", "65 ≤ 가격 < 85", "[green]초록색[/]", "골디락스: 산유국 수익성과 물가 안정의 최적 균형점")
    table.add_row("", "45 ≤ 가격 < 65", "[yellow]노란색[/]", "수요 둔화: 경기 하강 신호 및 에너지 투자 위축 시작")
    table.add_row("", "가격 < 45", "[blue]파란색[/]", "시스템 위기: 심각한 경기 침체 혹은 금융 위기 동반")
    table.add_section()

    table.add_row("WTI 원유", "가격 ≥ 120", "[magenta]보라색[/]", "에너지 쇼크: 강제적 수요 파괴 및 스태그플레이션 확정")
    table.add_row("", "100 ≤ 가격 < 120", "[red]빨간색[/]", "임계점: 고금리 긴축 강요, 기업 이익률 급격 둔화")
    table.add_row("", "80 ≤ 가격 < 100", "[orange3]주황색[/]", "고유가 지속: 인플레 압력 상존, 실물 경제 마지노선")
    table.add_row("", "60 ≤ 가격 < 80", "[green]초록색[/]", "골디락스: 산유국 수익성과 물가 안정의 최적 균형점")
    table.add_row("", "40 ≤ 가격 < 60", "[yellow]노란색[/]", "수요 둔화: 경기 하강 신호 및 에너지 투자 위축 시작")
    table.add_row("", "가격 < 40", "[blue]파란색[/]", "시스템 위기: 심각한 경기 침체 혹은 금융 위기 동반")
    table.add_section()

    table.add_row("가솔린 RBOB", "가격 ≥ 4.00", "[magenta]보라색[/]", "에너지 쇼크: 강제적 수요 파괴 및 스태그플레이션 확정")
    table.add_row("", "3.20 ≤ 가격 < 4.00", "[red]빨간색[/]", "임계점: 고금리 긴축 강요, 기업 이익률 급격 둔화")
    table.add_row("", "2.60 ≤ 가격 < 3.20", "[orange3]주황색[/]", "고유가 지속: 인플레 압력 상존, 실물 경제 마지노선")
    table.add_row("", "2.10 ≤ 가격 < 2.60", "[green]초록색[/]", "골디락스: 산유국 수익성과 물가 안정의 최적 균형점")
    table.add_row("", "1.60 ≤ 가격 < 2.10", "[yellow]노란색[/]", "수요 둔화: 경기 하강 신호 및 에너지 투자 위축 시작")
    table.add_row("", "가격 < 1.60", "[blue]파란색[/]", "시스템 위기: 심각한 경기 침체 혹은 금융 위기 동반")
    table.add_section()

    table.add_row("천연가스", "가격 ≥ 10", "[magenta]보라색[/]", "에너지 쇼크: 공급망 붕괴 또는 극단적 기후 위기")
    table.add_row("", "6 ≤ 가격 < 10", "[red]빨간색[/]", "물가 비상: 에너지 비용 급증, 전방위적 인플레 유발")
    table.add_row("", "4 ≤ 가격 < 6", "[orange3]주황색[/]", "수급 타이트: 수출 수요 강세 또는 재고 부족 우려")
    table.add_row("", "2.5 ≤ 가격 < 4", "[green]초록색[/]", "골디락스: 생산-소비-수출의 균형이 가장 이상적인 구간")
    table.add_row("", "1.5 ≤ 가격 < 2.5", "[yellow]노란색[/]", "수익성 악화: 생산 업체 감산 및 경기 둔화 우려")
    table.add_row("", "가격 < 1.5", "[blue]파란색[/]", "시스템 하강: 심각한 수요 파괴 또는 디플레이션 신호")
    table.add_section()

    table.add_row("밀", "가격 ≥ 900", "[magenta]보라색[/]", "식량 안보 위기 (뉴스 헤드라인 장악)")
    table.add_row("", "750 ≤ 가격 < 900", "[red]빨간색[/]", "식량 인플레 심각")
    table.add_row("", "650 ≤ 가격 < 750", "[orange3]주황색[/]", "물가 부담 (재고 바닥권 시 위험 격상)")
    table.add_row("", "500 ≤ 가격 < 650", "[green]초록색[/]", "역사적 평균 대비 적절한 균형점")
    table.add_row("", "400 ≤ 가격 < 500", "[yellow]노란색[/]", "수요 둔화/공급 과잉")
    table.add_row("", "가격 < 400", "[blue]파란색[/]", "디플레/농업  침체")
    table.add_section()

    table.add_row("달러 인덱스", "지수 ≥ 120", "[magenta]보라색[/]", "글로벌 통화 시스템 붕괴 위기")
    table.add_row("", "110 ≤ 지수 < 120", "[red]빨간색[/]", "매우 강함 (신흥국 위기)")
    table.add_row("", "103 ≤ 지수 < 110", "[orange3]주황색[/]", "강세 구간 (신흥국 경고등)")
    table.add_row("", "90 ≤ 지수 < 103", "[green]초록색[/]", "가장 안정적인 중립 구간")
    table.add_row("", "80 ≤ 지수 < 90", "[yellow]노란색[/]", "약세")
    table.add_row("", "지수 < 80", "[blue]파란색[/]", "매우 약함")
    table.add_section()

    table.add_row("달러 환율", "환율 ≥ 1600원", "[magenta]보라색[/]", "시스템 위기 (외환·금융 복합 위기)")
    table.add_row("", "1500 ≤ 환율 < 1600", "[red]빨간색[/]", "패닉 구간 (정책 개입 불가피)")
    table.add_row("", "1400 ≤ 환율 < 1500", "[orange3]주황색[/]", "구조적 고환율 (국가 부담 심화)")
    table.add_row("", "1300 ≤ 환율 < 1400", "[yellow]노란색[/]", "강달러 뉴노멀 상단 (바닥권)")
    table.add_row("", "1200 ≤ 환율 < 1300", "[green]초록색[/]", "뉴노멀 중립 구간")
    table.add_row("", "1100 ≤ 환율 < 1200", "[cyan]청록색[/]", "비정상적 안정 (수출 경쟁력 부담)")
    table.add_row("", "환율 < 1100", "[blue]파란색[/]", "초강세 원화 (일시적/정책성)")
    table.add_section()

    table.add_row("VIX 변동성 지수", "지수 ≤ 20", "[green]초록색[/]", "안정 (평균 회귀 성향 주의)")
    table.add_row("", "20 < 지수 < 30", "[cyan]청록색[/]", "시장 활발 (긍정적/위험하지 않음)")
    table.add_row("", "30 ≤ 지수 < 40", "[yellow]노란색[/]", "주의")
    table.add_row("", "40 ≤ 지수 < 50", "[orange3]주황색[/]", "경계")
    table.add_row("", "지수 ≥ 50", "[red]빨간색[/]", "위험")
    table.add_section()

    table.add_row("SOX 반도체 지수", "낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세")
    table.add_row("", "5% < 낙폭 ≤ 12%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "12% < 낙폭 ≤ 20%", "[yellow]노란색[/]", "기술적 조정기 진입")
    table.add_row("", "낙폭 > 25%", "[blue]파란색[/]", "반도체 하락 사이클/침체")
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

    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    table.add_row("종목 분류", f"{buy_score}점 이상 & RSI<{buy_rsi}", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    table.add_row("", f"{rise_score} ~ {buy_score}점 미만 (상승 추세)", "[orange3]상승[/]", "상승 초입/지속 (대기/소량)")
    table.add_row("", "방향성 불명확 단계", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    table.add_row("", "추세 이탈 / 단기 하락", "[yellow]주의[/]", "신규매수 자제/비중축소 고려")
    table.add_row("", "장기추세 붕괴 및 과열", "[blue]매도[/]", "적극 매도/손절 고려 (위험)")
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

    table.add_row("체결강도", "150% 이상", "[magenta]보라색[/]", "강력한 수급: 공격적인 매수세 유입, 주가 급등 가능성 높음")
    table.add_row("", "120% ~ 150%", "[red]빨간색[/]", "매수 우위: 상승 추세 강화, 단기 모멘텀 발생")
    table.add_row("", "100% ~ 120%", "[orange3]주황색[/]", "점진적 유입: 완만한 매수세, 주가 하방 지지력 강함")
    table.add_row("", "100%", "[white]흰색[/]", "균형: 매수와 매도의 힘이 팽팽하게 맞서는 상태")
    table.add_row("", "80% ~ 100%", "[yellow]노란색[/]", "매도 우위: 주가 탄력 둔화, 관망세 확산")
    table.add_row("", "80% 미만", "[blue]파란색[/]", "하락 압력: 공격적인 매도세, 추가 하락 경계 필요")
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

    table.add_row("추세SMO", "S (SAR)", "[red]⬆[/] / [blue]⬇[/]", "상승 / 하락")
    table.add_row("", "M (MACD)", "[red]+G[/] / [blue]-D[/]", "골든/데드 (0선 위/아래)")
    table.add_row("", "O (OBV)", "[red]▲[/] / [blue]▼[/]", f"수급 양호 (OBV > EMA {obv_period}일) / 약세")
    table.add_section()

    rsi_upper = config.INDICATOR_PARAMS["RSI_UPPER"]
    rsi_lower = config.INDICATOR_PARAMS["RSI_LOWER"]

    table.add_row("RSI", f"RSI ≥ {rsi_upper}", "[magenta]보라색[/]", "과열 (추격금지)")
    table.add_row("", f"55 ≤ RSI < {rsi_upper}", "[red]빨간색[/]", "강세 유지 구간")
    table.add_row("", "45 ≤ RSI < 55", "[orange3]주황색[/]", "강세 조정 구간 (진입후보)")
    table.add_row("", f"{rsi_lower} < RSI < 45", "[yellow]노란색[/]", "단기하락 전환가능")
    table.add_row("", f"RSI ≤ {rsi_lower}", "[blue]파란색[/]", "하락")
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

    cci_upper = config.INDICATOR_PARAMS["CCI_UPPER"]
    cci_lower = config.INDICATOR_PARAMS["CCI_LOWER"]

    table.add_row("CCI", f"CCI ≥ {cci_upper}", "[red]빨간색[/]", "과열 (추격 금물)")
    table.add_row("", f"0 < CCI < {cci_upper}", "[orange3]주황색[/]", "상승 방향시 (추세 매매)")
    table.add_row("", f"{cci_lower} < CCI < 0", "[yellow]노란색[/]", "상승 방향시 (반등 시도)")
    table.add_row("", f"CCI ≤ {cci_lower}", "[blue]파란색[/]", "과매도 (저점 탐색)")
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

    table.add_row("OBV (거래량)", f"OBV > EMA {obv_period}일선", "[red]빨간색[/]", "수급 양호 (매집)")
    table.add_row("", f"OBV < EMA {obv_period}일선", "[blue]파란색[/]", "수급 약세 (분산)")
    table.add_row("", "주가 하락 + OBV 상승", "", "강세 다이버전스 (반등 시그널)")
    table.add_row("", "주가 상승 + OBV 하락", "", "약세 다이버전스 (하락 시그널)")
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
    
    # [추가] 스코어링 가중치 계산
    weights = config.SCORING_WEIGHTS
    r_trend = weights.get("TREND", 4.0) / 4.0
    r_mom = weights.get("MOMENTUM", 2.5) / 2.5
    r_str = weights.get("STRENGTH", 1.5) / 1.5
    r_syn = weights.get("SYNERGY", 2.0) / 2.0

    regime = config.MARKET_REGIME_PARAMS
    ma_p = regime.get('REGIME_MA_PERIOD', 60)
    adx_th = regime.get('REGIME_ADX_THRESHOLD', 20)
    
    market_status_info = None
    filter_info = None
    try:
        with config.console.status("[cyan]현재 시장 상태 및 필터링 분석 중...[/cyan]"):
            kospi_regime, kospi_adj = analysis.get_market_regime("KOSPI")
            kosdaq_regime, kosdaq_adj = analysis.get_market_regime("KOSDAQ")
        
            r_map = {"Bull": "[red]강세장[/]", "Bear": "[blue]약세장[/]", "Sideways": "[yellow]횡보장[/]"}
            k_r_str = r_map.get(kospi_regime, kospi_regime) + "*"
            q_r_str = r_map.get(kosdaq_regime, kosdaq_regime) + "*"

            market_status_info = {
                "kospi_str": k_r_str, "kospi_adj": kospi_adj,
                "kosdaq_str": q_r_str, "kosdaq_adj": kosdaq_adj
            }
            
            # [추가] 실시간 필터링 상태 계산
            if getattr(config, 'USE_MARKET_FILTER', True):
                filter_info = {}
                ma_period_filter = getattr(config, 'MARKET_FILTER_MA', 50)
                for m_type in ["KOSPI", "KOSDAQ"]:
                    try:
                        df = analysis.get_domestic_index_data(m_type)
                        if df is not None and not df.empty and len(df) >= ma_period_filter:
                            ma_val = df['close'].rolling(window=ma_period_filter).mean().iloc[-1]
                            current_idx = df['close'].iloc[-1]
                            filter_info[m_type] = current_idx >= ma_val
                    except:
                        pass
    except Exception:
        pass

    # [추가] 매수 점수 산정 기준 테이블 (README 내용 반영)
    config.console.print()
    score_table = Table(title="스코어링 및 매매 전략 가이드", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    score_table.add_column("구분", style="cyan", justify="center")
    score_table.add_column("조건", justify="left")
    score_table.add_column("점수/행동", justify="center", style="white")
    score_table.add_column("의미", justify="left")

    # 1. Trend Factor
    score_table.add_row("Trend Factor", "현재가 > 20일선", f"+{0.5 * r_trend:.1f}", "단기 지지")
    score_table.add_row("(추세 4.0)", "20일선 > 60일선", f"+{0.5 * r_trend:.1f}", "수급선 정배열")
    score_table.add_row("", "60일선 > 120일선", f"+{0.5 * r_trend:.1f}", "경기선 정배열")
    score_table.add_row("", "MACD > Signal", f"+{1.0 * r_trend:.1f}", "골든크로스 (강력)")
    score_table.add_row("", "MACD > 0", f"+{0.5 * r_trend:.1f}", "상승 국면 진입")
    score_table.add_row("", "주가 > SAR", f"+{1.0 * r_trend:.1f}", "파라볼릭 매수")
    score_table.add_section()

    # 2. Momentum Factor
    score_table.add_row("Momentum Factor", "50 ≤ RSI ≤ 75", f"+{1.5 * r_mom:.1f}", "강세 구간 (주도주)")
    score_table.add_row("(모멘텀 2.5)", "30 ≤ RSI < 50", f"+{0.5 * r_mom:.1f}", "반등/회복 시도")
    score_table.add_row("", "CCI > 0", f"+{0.5 * r_mom:.1f}", "상승 추세")
    score_table.add_row("", f"CCI > {cci_upper}", f"+{0.5 * r_mom:.1f}", "강한 상승 탄력")
    score_table.add_section()

    # 3. Strength & Volume
    score_table.add_row("Strength/Volume", "ADX ≥ 20", f"+{0.5 * r_str:.1f}", "추세 형성 확인")
    score_table.add_row("(강도/수급 1.5)", "OBV > OBV 이동평균", f"+{1.0 * r_str:.1f}", "수급 양호")
    score_table.add_section()

    # 4. Synergy Bonus
    score_table.add_row("Synergy Bonus", "정배열 + MACD양수 + ADX", f"+{1.0 * r_syn:.1f}", "추세 확증 (Trend)")
    score_table.add_row("(가산점 2.0)", "MACD골든 + RSI강세 + OBV", f"+{1.0 * r_syn:.1f}", "모멘텀 폭발 (Thrust)")

   # [병합] 점수대별 의미
    score_table.add_section()
    score_table.add_row("점수대별 의미", "8.5 ~ 10.0점", "[red]매수[/]", "강력 매수. 모든 지표 상승 및 상관관계 완벽. 비중 확대 가능.")
    score_table.add_row("", "7.0 ~ 8.0점", "[orange3]상승[/]", "매수. 추세 확실하나 일부 지표 후행. 분할 매수 권장.")
    score_table.add_row("", "5.5 ~ 6.5점", "[white]관망[/]", "관망/준비. 상승 초입 또는 추세 약화. 7점대 진입 대기.")
    score_table.add_row("", "5.0점 미만", "[blue]매도[/]", "매도/진입 금지. 하락 추세 또는 방향성 없는 횡보장.")
    
    # [추가] 현재 설정 및 적응형 임계값, 시장 상태 정보
    score_table.add_section()
    score_table.add_row("스코어링 가중치", "현재 설정", f"{weights['TREND']} / {weights['MOMENTUM']} / {weights['STRENGTH']} / {weights['SYNERGY']}", "추세/모멘텀/강도/시너지")
    score_table.add_row("", "[dim](기본값)[/dim]", "[dim]4.0 / 2.5 / 1.5 / 2.0[/dim]", "[dim]초기 시스템 권장값[/dim]")
    score_table.add_row("", "[dim](추세 중시)[/dim]", "[dim]5.0 / 2.0 / 1.0 / 2.0[/dim]", "[dim]확실한 상승 추세를 타는 종목 집중[/dim]")
    score_table.add_row("", "[dim](모멘텀 중시)[/dim]", "[dim]3.0 / 3.5 / 1.5 / 2.0[/dim]", "[dim]빠른 단기 반등 및 시세 탄력 집중[/dim]")
    score_table.add_row("", "[dim](수급 중시)[/dim]", "[dim]3.0 / 2.0 / 3.0 / 2.0[/dim]", "[dim]거래량 및 외인/기관 매수세 포착[/dim]")
    score_table.add_row("", "[dim](균등 배분)[/dim]", "[dim]2.5 / 2.5 / 2.5 / 2.5[/dim]", "[dim]모든 팩터를 균형있게 고려[/dim]")
    
    score_table.add_section()
    adaptive_status = "[green]ON[/green]" if regime.get('USE_ADAPTIVE_THRESHOLD') else "[red]OFF[/red]"
    score_table.add_row(f"적응형 임계값 ({adaptive_status})", f"강세장: 지수 > EMA {ma_p}일선 & 이평선우상향 & ADX≥{adx_th}", "[red]완화[/]", f"매수 기준 {regime['BULL_SCORE_ADJ']:+.1f}점 적용")
    score_table.add_row("", f"약세장: 지수 < EMA {ma_p}일선", "[blue]강화[/]", f"매수 기준 {regime['BEAR_SCORE_ADJ']:+.1f}점 적용")
    score_table.add_row("", "횡보장: 그 외 구간", "[yellow]유지[/]", f"매수 기준 {regime['SIDEWAYS_SCORE_ADJ']:+.1f}점 적용")
    
    if market_status_info:
        k_adj_str = f"보정: {market_status_info['kospi_adj']:+.1f}점"
        q_adj_str = f"보정: {market_status_info['kosdaq_adj']:+.1f}점"
        score_table.add_row("현재 시장 상태", f"KOSPI: {market_status_info['kospi_str']}", k_adj_str, "실시간 국면 분석")
        score_table.add_row("", f"KOSDAQ: {market_status_info['kosdaq_str']}", q_adj_str, "KIS API 데이터 기반 판단")
    else:
        score_table.add_row("현재 시장 상태", "분석 실패", "-", "-")
    
    # [추가] 시장 필터링 섹션
    score_table.add_section()
    filter_status = "[green]ON[/green]" if getattr(config, 'USE_MARKET_FILTER', True) else "[red]OFF[/red]"
    ma_period = getattr(config, 'MARKET_FILTER_MA', 50)
    score_table.add_row(f"시장 필터링 ({filter_status})", f"KOSPI/KOSDAQ 지수 < SMA {ma_period}일 이평선", "[blue]보류[/]", "하락장 감지 시 신규 매수 중단")
    
    if filter_info is None and getattr(config, 'USE_MARKET_FILTER', True):
        score_table.add_row("현재 필터링 상태", "확인 불가", "-", "-")
    elif filter_info:
        k_stat = "[green]허용[/]" if filter_info.get("KOSPI", True) else "[red]보류[/]"
        q_stat = "[green]허용[/]" if filter_info.get("KOSDAQ", True) else "[red]보류[/]"
        score_table.add_row("현재 필터링 상태", f"KOSPI: {k_stat} / KOSDAQ: {q_stat}", "-", "실시간 필터링 적용 여부")

    # [추가] 매매 필터링 섹션
    score_table.add_section()
    score_table.add_row("매매 필터링 (위험)", "60일선 & 120일선 동시 이탈 or RSI ≤ 20", "[blue]매도[/]", "매수 금지 / 즉시 매도 (점수 무관)")
    score_table.add_row("매매 필터링 (주의)", "MACD 데드크로스, 60/120선 이탈, SAR 매도", "[yellow]주의[/]", "신규 진입 자제 (보유는 가능)")

    # [추가] 매수 타이밍 섹션
    score_table.add_section()
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]

    score_table.add_row("매수 (진입)", f"종합 점수 ≥ {buy_score}점 & RSI < {buy_rsi_max} & 체결강도 > {buy_vol}%", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    
    use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
    mr_status = "[green]ON[/green]" if use_mr else "[red]OFF[/red]"
    score_table.add_row(f"매수 (역추세) ({mr_status})", f"이격도 ≤ 90% & RSI ≤ 40 반등 & 체결 > 120%", "[magenta]역매수[/]", "낙폭과대 기술적 반등 노리기")
    
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
    use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    half_tp_use = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True)
    time_stop_use = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
    time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 5)
    time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)

    score_table.add_row("매도 (익절)", f"수익률 +{take_profit}% 도달", "[red]익절[/]", "목표 수익 달성 (최우선)")
    
    half_tp_status = "[green]ON[/green]" if half_tp_use else "[red]OFF[/red]"
    score_table.add_row(f"매도 (반익절) ({half_tp_status})", f"수익률 +{take_profit/2:.1f}% 도달", "[red]반익절[/]", "절반(50%) 선매도로 수익 확보")
    
    fixed_sl_status = "[red]OFF[/red]" if use_atr else "[green]ON[/green]"
    score_table.add_row(f"매도 (고정손절) ({fixed_sl_status})", f"손실률 {stop_loss}% 도달", "[blue]손절[/]", "손실 제한 (고정 손절)")
    
    atr_status = "[green]ON[/green]" if use_atr else "[red]OFF[/red]"
    score_table.add_row(f"매도 (ATR손절) ({atr_status})", f"매수가 - (ATR x {atr_mult})", "[blue]손절[/]", "변동성 기반 동적 손절")
    
    time_stop_status = "[green]ON[/green]" if time_stop_use else "[red]OFF[/red]"
    score_table.add_row(f"매도 (시간청산) ({time_stop_status})", f"보유 {time_stop_days}일 경과 & 수익 < {time_stop_min_profit}%", "[blue]시간청산[/]", "장기 횡보 종목 기회비용 보전")
    
    score_table.add_row("매도 (트레일링)", f"수익 {ts_activation}% 도달 후 고점 대비 -{ts_callback}%", "[blue]매도[/]", "수익 보전 (Trailing Stop)")
    score_table.add_row("매도 (과열)", f"RSI > {take_profit_rsi}", "[red]익절[/]", "RSI 과열 시 이익 실현")
    score_table.add_row("매도 (추세이탈)", f"종합 점수 < {sell_score}점 or 위험 상태", "[blue]매도[/]", "추세 붕괴 시 청산")

    # [추가] 주문 집행 상세 섹션
    score_table.add_section()
    slippage_rate = getattr(config, 'SLIPPAGE_RATE', 0.003)
    if slippage_rate > 0:
        slippage_val = slippage_rate * 100
        score_table.add_row("주문 집행", "매수 주문 시", f"[red]+{slippage_val:.2f}%[/]", "체결 확률 확보 (현재가 + 슬리피지)")
        score_table.add_row("", "매도 주문 시", f"[blue]-{slippage_val:.2f}%[/]", "즉시 체결 유도 (현재가 - 슬리피지)")
    else:
        score_table.add_row("주문 집행", "매수 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
        score_table.add_row("", "매도 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
    
    use_vol = getattr(config, 'USE_VOLATILITY_TARGETING', True)
    use_risk = getattr(config, 'SYSTEM_RISK_PER_TRADE', 0) > 0
    if use_vol or use_risk:
        score_table.add_row("", "자산 배분 (마지막)", "[yellow]비중 조절[/]", "리스크/변동성 한도 내에서 집행")
    else:
        score_table.add_row("", "자산 배분 (마지막)", "[green]전액[/]", "마지막 종목은 잔여 예수금 100% 사용")

    config.console.print(score_table)

def flush_input():
    """입력 버퍼를 비워 의도치 않은 입력을 방지합니다."""
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        import sys, termios
        try:
            termios.tcflush(sys.stdin, termios.TCIOFLUSH)
        except:
            pass

def main():
    # [수정] 커맨드 라인 인자 파싱 설정 개선 (상세 도움말 추가)
    parser = argparse.ArgumentParser(
        prog='run.sh / run.bat',
        description='[MyStock HTS] 한국투자증권 API 기반 주식 자동매매 시스템',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
[사용 예시]
  1. 기본 실행 (대화형 메뉴 모드):
     ./run.sh        (macOS/Linux)
     run.bat         (Windows)

  2. 모의투자 모드로 자동매매 바로 시작:
     ./run.sh --mode 1 --auto

  3. 실전투자 모드로 바로 시작 (텔레그램 봇 수신 비활성화):
     ./run.sh --mode 2 --no-bot  (또는 run.bat ...)
"""
    )
    parser.add_argument('--mode', choices=['1', '2'], help='투자 모드 선택 (1: 모의투자, 2: 실전투자)\n지정하지 않으면 실행 시 모드 선택 화면이 출력됩니다.')
    parser.add_argument('--auto', action='store_true', help='프로그램 시작 시 시스템 트레이딩 자동 실행 및 로그 뷰어 활성화')
    parser.add_argument('--no-bot', action='store_true', help='텔레그램 봇 명령어 수신(폴링) 비활성화 (알림 전송 기능은 유지)')
    args = parser.parse_args()

    # [추가] 로깅 설정 초기화
    config.setup_logging()

    # [추가] 프로그램 구동 시작 로그 기록 (mystock.log 생성 보장)
    logging.info("=== MyStock HTS 프로그램 구동 시작 ===")

    # [추가] 메인 스레드 ID 등록 (토큰 발급 권한 제어용)
    context.MAIN_THREAD_ID = threading.get_ident()

    # [추가] DB 큐 프록시 설치 (순차 처리 적용)
    if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
        db_queue.install_proxy(db_manager)

    # [수정] 초기화 로직 분기: 모드 지정 시 즉시 status 표시
    if args.mode:
        with config.console.status("[cyan]시스템 초기화 및 환경 설정 로드 중...[/cyan]") as status:
            # 1. 환경 설정 로드
            config.session.initialize(mode=args.mode)
            
            if args.no_bot:
                config.ENABLE_TELEGRAM = False
                config.console.print()
                config.console.print("[yellow][System] 텔레그램 봇 명령어 수신 기능을 비활성화합니다. (알림 전송만 가능)[/yellow]")

            status.update("[cyan]시스템 리소스 로딩 및 백그라운드 서비스 시작 중...[/cyan]")
            time.sleep(1) # 인지용 대기

            # 2. 종목 데이터 로드
            config.session.load_stock_config()
            
            # 3. 토큰 발급
            if config.session.is_simulation:
                api.get_access_token()
            else:
                api.get_real_access_token()
                # [Fix] 실전투자 모드에서 자동매매 계좌가 별도로 설정된 경우 토큰 미리 발급
                if config.session.auto_app_key:
                    api.get_auto_access_token()

            # 백그라운드 서비스 시작
            api.prefetch_watchlists_async() # [추가] 차트 데이터 캐시 백그라운드 예열
            auto_trade.ConclusionMonitor().start()
            telegram_cmd = telegram_bot.TelegramCommander()
            telegram_cmd.start()
    else:
        # 대화형 모드 (사용자 입력 대기 필요하므로 status 나중에 시작)
        config.session.initialize(mode=args.mode)
        
        if args.no_bot:
            config.ENABLE_TELEGRAM = False
            config.console.print("[yellow][System] 텔레그램 봇 명령어 수신 기능을 비활성화합니다. (알림 전송만 가능)[/yellow]")

        with config.console.status("[cyan]시스템 리소스 로딩 및 백그라운드 서비스 시작 중...[/cyan]"):
            time.sleep(1)
            config.session.load_stock_config()
            if config.session.is_simulation:
                api.get_access_token()
            else:
                api.get_real_access_token()
                # [Fix] 실전투자 모드에서 자동매매 계좌가 별도로 설정된 경우 토큰 미리 발급
                if config.session.auto_app_key:
                    api.get_auto_access_token()
            
            api.prefetch_watchlists_async() # [추가] 차트 데이터 캐시 백그라운드 예열
            auto_trade.ConclusionMonitor().start()
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
            # [추가] 화면 출력 안정화를 위한 플러시 및 지연 (저사양 환경 대응)
            sys.stdout.flush()
            time.sleep(0.2)

            # [추가] 입력 버퍼 비우기 (이전 작업 중 눌린 키 무시)
            flush_input()

            # [추가] 루프 시작 시 토큰 만료 여부 확인 및 갱신
            api.check_and_refresh_token_if_expired()

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            env_str = "[모의투자]" if config.session.is_simulation else "[실전투자]"
            env_color = "green" if config.session.is_simulation else "bold red"
            print("\n" + "━"*50)
            config.console.print(f" [cyan]시스템 시간: {now_str}[/cyan] | [{env_color}]{env_str}[/]")
            print("━"*50)
            
            trader_status = ""
            if trader.is_running:
                if trader.is_market_open():
                    trader_status = " [bold green](RUNNING)[/]"
                else:
                    trader_status = " [bold yellow](WAITING)[/]"
                
            grid = Table.grid(padding=(0, 2))
            grid.add_column(justify="left")
            grid.add_column(justify="left", style="dim")
            grid.add_row("[0] 시스템 설정", "(Settings)")
            grid.add_row("[1] 시장 지수 조회", "(Market Indices)")
            grid.add_row("[2] 종목 시세 분석", "(Stock Analysis)")
            grid.add_row("[3] 종목 차트 분석", "(Chart Analysis)")
            grid.add_row("[4] 종목 트랜드 분석", "(Stock Trend Analysis)")
            grid.add_row("[5] 전략 백테스팅", "(Backtesting)")
            grid.add_row("[6] 시스템 트레이딩", f"(System Trading){trader_status}")
            grid.add_row("[7] 관심 종목 관리", "(Watchlist Management)")
            grid.add_row("[8] 종목 주문 관리", "(Order Management)")
            grid.add_row("[9] 자산 관리", "(Asset Management)")
            config.console.print(grid)
            config.console.print("[Q] 종료 (Quit)  |  [H] 도움말 (Help)")
            print("─" * 50); config.console.print()
            try:
                choice = Prompt.ask("선택 ", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "q", "Q", "h", "H"], default=last_choice)
                
                # [추가] 루프 시작 시 입력 경로 초기화
                context.USER_ACTION_BREADCRUMB = []
                
                # [추가] 운영자 메뉴 선택 로깅
                menu_map = {
                    "0": "시스템 설정", "1": "시장 지수 조회", "2": "종목 시세 분석", "3": "종목 차트 분석",
                    "4": "종목 트랜드 분석", "5": "전략 백테스팅", "6": "시스템 트레이딩",
                    "7": "관심 종목 관리", "8": "종목 주문 관리", "9": "자산 관리",
                    "q": "종료", "h": "도움말"
                }
                menu_name = menu_map.get(choice.lower(), '')
                
                if choice.lower() == "q": 
                    logging.info(f"운영자 실행: [{choice}] {menu_name}")
                    break
                
                if choice.lower() == "h": 
                    logging.info(f"운영자 실행: [{choice}] {menu_name}")
                    show_help()
                    continue

                context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_name}")
                    
                last_choice = choice
                if choice == "0": settings.system_config_menu()
                elif choice == "1": market.show_market_indices()
                elif choice == "2": analysis.show_stock_analysis()
                elif choice == "3": 
                    # [수정] 차트 분석 메뉴 순서 변경 (5번: 시장 지수, 6번: 직접 입력)
                    config.console.print("\n[bold]차트 분석할 대상을 선택하세요:[/bold]")
                    grid = Table.grid(padding=(0, 2))
                    grid.add_column(justify="left")
                    grid.add_column(justify="left", style="dim")
                    grid.add_row("[1] 국내 주식", "(Domestic Stock)")
                    grid.add_row("[2] 국내 ETF", "(Domestic ETF)")
                    grid.add_row("[3] 미국 주식", "(US Stock)")
                    grid.add_row("[4] 미국 ETF", "(US ETF)")
                    grid.add_row("[5] 시장 지수", "(Market Indices)")
                    grid.add_row("[6] 직접 입력", "(Direct Input)")
                    config.console.print(grid)
                    config.console.print()
                    
                    sub_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "6", "q"], default="6")
                    
                    target_code, target_name, target_ovs = None, None, False
                    
                    if sub_choice == '6':
                        raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](취소: q)[/dim]")
                        if raw_input and raw_input.lower() != 'q':
                            if raw_input.isdigit() and len(raw_input) == 6:
                                target_code = raw_input
                                target_name = api.get_stock_name_by_code(target_code, False) or target_code
                                target_ovs = False
                            else:
                                target_code = raw_input.upper()
                                target_name = api.get_stock_name_by_code(target_code, True) or target_code
                                target_ovs = True
                    elif sub_choice == '5':
                        # [수정] 통합 지수 리스트 사용
                        indices_list = market.ALL_INDICES
                        
                        config.console.print(f"\n[bold]시장 지수 목록:[/bold]")
                        for i, (name, code) in enumerate(indices_list):
                            config.console.print(f"[{i+1}] {name}")
                        
                        config.console.print()
                        sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
                        if sel.lower() != 'q' and sel.isdigit() and 1 <= int(sel) <= len(indices_list):
                            target_name, target_code = indices_list[int(sel)-1]
                            target_ovs = True
                    elif sub_choice in ["1", "2", "3", "4"]:
                        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
                        s_list = config.session.stock_data.get(key_map[sub_choice], [])
                        if s_list:
                            for i, s in enumerate(s_list):
                                config.console.print(f"[{i+1}] {s['name']} ({s['code']})")
                            
                            config.console.print()
                            sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
                            if sel.lower() != 'q' and sel.isdigit() and 1 <= int(sel) <= len(s_list):
                                item = s_list[int(sel)-1]
                                target_code, target_name = item['code'], item['name']
                                target_ovs = (sub_choice in ["3", "4"])
                        else:
                            config.console.print("[yellow]목록이 비어있습니다.[/yellow]")

                    if target_code: 
                        logging.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
                        
                        # [추가] 차트 유형 선택 (일봉/분봉)
                        config.console.print("\n[bold]차트 유형을 선택하세요:[/bold]")
                        grid = Table.grid(padding=(0, 2))
                        grid.add_column(justify="left")
                        grid.add_column(justify="left", style="dim")
                        grid.add_row("[1] 일봉", "(Daily)")
                        grid.add_row("[2] 시봉", "(Hourly)")
                        grid.add_row("[3] 분봉", "(Intraday)")
                        config.console.print(grid)
                        config.console.print()
                        c_type = Prompt.ask("선택 (취소: q)", choices=["1", "2", "3", "q"], default="2")
                        
                        if c_type != 'q':
                            p_type = 'daily'
                            if c_type == '2': p_type = 'hourly'
                            elif c_type == '3': p_type = 'intraday'
                            chart.generate_visual_chart(target_code, target_name, target_ovs, period_type=p_type)
                elif choice == "4": theme_analysis.run_theme_analysis()
                elif choice == "5": backtest.run_backtest()
                elif choice == "6": auto_trade.system_trading_menu() 
                elif choice == "7": manage.manage_stock_menu()
                elif choice == "8": trading.stock_order_menu()
                elif choice == "9": 
                    try:
                        account.asset_management_menu()
                    except Exception as e:
                        config.console.print(f"[bold red]⚠️ 자산 관리 메뉴 실행 중 오류 발생: {e}[/bold red]")
                        logging.error(f"자산 관리 메뉴 오류: {e}")
            except KeyboardInterrupt:
                config.console.print()
                config.console.print()
                try:
                    if Prompt.ask("프로그램을 종료하시겠습니까?", choices=["y", "n"], default="n") == "y":
                        break
                except KeyboardInterrupt:
                    break
            except Exception as e:
                config.console.print(f"\n[bold red]치명적인 오류 발생: {escape(str(e))}[/bold red]")
    finally:
        config.console.print()
        # [수정] 종료 프로세스를 console.status로 변경하고 완료 메시지 출력
        with config.console.status("[cyan]시스템 종료 프로세스 진행 중...[/cyan]") as status:
            # 1. 자동매매 종료
            status.update("[cyan][1/4] 자동매매 스레드 안전 종료 중...[/cyan]")
            if trader.is_running:
                trader.stop(use_status=False)
            time.sleep(0.5)
            config.console.print("[1/4] 자동매매 스레드 안전 종료 [bold green][완료][/]")
            
            # 2. 백그라운드 서비스 종료
            status.update("[cyan][2/4] 백그라운드 서비스(텔레그램/감시) 종료 중...[/cyan]")
            auto_trade.ConclusionMonitor().stop()
            telegram_cmd.stop()
            time.sleep(0.5)
            config.console.print("[2/4] 백그라운드 서비스(텔레그램/감시) 종료 [bold green][완료][/]")
            
            # 3. DB 큐 종료
            status.update("[cyan][3/4] DB 작업 큐 처리 및 종료 중...[/cyan]")
            db_queue.shutdown()
            time.sleep(0.5)
            config.console.print("[3/4] DB 작업 큐 처리 및 종료 [bold green][완료][/]")
            
            # 4. DB 최적화 (VACUUM)
            status.update("[cyan][4/4] 데이터베이스 최적화(VACUUM) 수행 중...[/cyan]")
            try:
                # [수정] DB Proxy가 종료되었으므로 원본 DB 객체에 직접 접근하여 실행 (타임아웃 방지)
                real_db = db_manager.db
                if hasattr(real_db, '_real_db'):
                    real_db = real_db._real_db
                real_db.run_vacuum()
            except Exception as e:
                config.console.print(f"[red]VACUUM 실패: {e}[/red]")
            config.console.print("[4/4] 데이터베이스 최적화(VACUUM) 수행 [bold green][완료][/]")

        config.console.print("[yellow]프로그램을 종료합니다.[/yellow]")
        os._exit(0) # [추가] 스레드 대기 없이 즉시 종료 (KeyboardInterrupt Traceback 방지)
        
if __name__ == "__main__":
    main()
