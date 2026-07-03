# modules/chart.py
import logging
import platform
import os
import re
import config
import api
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import indicators
from datetime import datetime
from contextlib import nullcontext

# [메모리 최적화] matplotlib/numpy 지연 로딩
# matplotlib+numpy는 import만으로도 RSS를 수십~100MB 이상 점유한다. 차트는 자동매매 중
# 텔레그램 전송 등 '실제로 그릴 때'만 필요하므로, 모듈 import 시점이 아니라 호출 시점에 적재한다.
# (RAM이 작은 라즈베리파이에서 시작 피크 메모리를 크게 낮춰 OOM(Killed)을 방지)
plt = None
np = None
rc = None
MaxNLocator = None
_matplotlib_ready = False

def _ensure_matplotlib():
    """matplotlib/numpy를 최초 사용 시점에 1회만 로드한다 (Agg 백엔드, 스레드 안전)."""
    global plt, np, rc, MaxNLocator, _matplotlib_ready
    if _matplotlib_ready and plt is not None and np is not None and rc is not None:
        return
    import matplotlib
    matplotlib.use('Agg')  # GUI 백엔드 미사용 (스레드 안전성 확보)
    import matplotlib.pyplot as _plt
    import numpy as _np
    from matplotlib import rc as _rc
    from matplotlib.ticker import MaxNLocator as _MaxNLocator
    # 이미 설정된 전역(예: 테스트의 mock 패치)은 덮어쓰지 않는다.
    if plt is None: plt = _plt
    if np is None: np = _np
    if rc is None: rc = _rc
    if MaxNLocator is None: MaxNLocator = _MaxNLocator
    _matplotlib_ready = True

def setup_korean_font():
    _ensure_matplotlib()  # [메모리 최적화] 차트 진입점에서 matplotlib 지연 로드
    current_os = platform.system()
    try:
        if current_os == "Windows": rc('font', family='Malgun Gothic')
        elif current_os == "Darwin": rc('font', family='AppleGothic')
        else: rc('font', family='NanumGothic')
    except Exception: pass
    plt.rcParams['axes.unicode_minus'] = False

def _is_before_krx_open():
    """현재(KST 로컬 시각)가 KRX 정규장 시작(09:00) 이전인지 여부."""
    now = datetime.now()
    return (now.hour, now.minute) < (9, 0)

def generate_visual_chart(code, name, is_overseas, open_file=True, dpi=300, quiet=False, period_type='daily', months=6):
    # [토스] 시봉(시간봉) 미제공 → KIS 데이터 없음. 일반 실패 대신 명확한 안내 후 종료.
    # (matplotlib 적재 전에 차단하여 라즈베리파이 메모리 점유도 방지)
    if period_type == 'hourly' and config.session.is_toss:
        if not quiet:
            config.console.print("[yellow]토스증권은 시봉(시간봉) 차트를 제공하지 않습니다. 일봉 또는 분봉을 이용해주세요.[/yellow]")
        return

    setup_korean_font()
    
    # [로그] 차트 생성 요청 시작
    if not quiet and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 차트 생성 요청: {name} ({code}) | Type: {period_type}[/dim cyan]")

    if quiet:
        status_ctx = nullcontext()
    else:
        status_ctx = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        )

    with status_ctx as progress:
        if not quiet:
            progress.add_task(f"[cyan]{name} 맞춤형 분석 차트 생성 중...[/cyan]", total=None)
            
        df = api.get_chart_data(code, is_overseas, period_type)
        
        if df is None or df.empty:
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] 차트 데이터 수신 실패 (Empty)[/dim red]")
            else:
                logging.error(f"[Chart] {name}({code}) 데이터 수신 실패 (Empty DataFrame). Period: {period_type}")
            # 국내 분봉은 KRX 정규장 데이터만 제공 → 장 시작 전이면 안내 메시지로 구분
            if period_type == 'intraday' and not is_overseas and _is_before_krx_open():
                config.console.print("[yellow]분봉 차트는 KRX 장 시작(09:00) 이후에 확인할 수 있습니다.[/yellow]")
            else:
                config.console.print(f"[red]{name} 데이터를 불러올 수 없습니다.[/]")
            return
        
        # [로그] 데이터 수신 확인
        if config.SCREEN_DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim magenta][DEBUG] 차트 데이터 수신 완료: {len(df)}행[/dim magenta]")
            if not df.empty:
                tail_str = df.tail(2).to_string().replace('\n', '\n[DEBUG]      ')
                config.console.print(f"[dim magenta][DEBUG]      Data Tail:\n[DEBUG]      {tail_str}[/dim magenta]")
        
        df = df.reset_index(drop=True)
        
        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
            config.console.print(f"[dim cyan][TRACE] 보조지표 계산 시작...[/dim cyan]")

        ind = indicators.calculate_indicators(df) 
        df['EMA5'] = df['close'].ewm(span=5, adjust=False).mean()
        df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['EMA60'] = df['close'].ewm(span=60, adjust=False).mean()
        df['EMA120'] = df['close'].ewm(span=120, adjust=False).mean()
        df['BB_mid'] = df['close'].rolling(window=20).mean()
        df['BB_std'] = df['close'].rolling(window=20).std()
        df['BB_up'] = df['BB_mid'] + (df['BB_std'] * 2)
        df['BB_low'] = df['BB_mid'] - (df['BB_std'] * 2)
        df['SAR'] = indicators.get_psar_full_series(df)
        df['ADX'], df['PLUS_DI'], df['MINUS_DI'] = indicators.get_adx_full_series(df)
        df['RSI'] = indicators.get_rsi_full_series(df)
        df['CCI'] = indicators.get_cci_full_series(df)
        df['OBV'] = indicators.get_obv_full_series(df)
        df['MACD'], df['MACD_Signal'], df['MACD_Hist'] = indicators.get_macd_full_series(df)

        # [이격도] 종가 / 단순이동평균 * 100 (5/10/20/60일)
        df['DISP5'] = df['close'] / df['close'].rolling(window=5).mean() * 100
        df['DISP10'] = df['close'] / df['close'].rolling(window=10).mean() * 100
        df['DISP20'] = df['close'] / df['close'].rolling(window=20).mean() * 100
        df['DISP60'] = df['close'] / df['close'].rolling(window=60).mean() * 100

        # [일봉 표시기간] 지표는 전체 데이터로 계산한 뒤 표시 구간만 잘라낸다(EMA120 등 정확도 유지).
        # months가 지정되면 최근 N개월(거래일 ≈ 21일/월)만 표시.
        if period_type == 'daily' and months:
            disp_rows = int(months * 21)
            if len(df) > disp_rows:
                df = df.iloc[-disp_rows:].reset_index(drop=True)

        # [변경] 서브플롯 6개로 조정 (이격도를 가격차트 바로 아래, MACD 위로 배치)
        fig, (ax1, ax6, ax2, ax3, ax4, ax5) = plt.subplots(6, 1, figsize=(20, 25), sharex=True, gridspec_kw={'height_ratios': [4, 1, 1, 1, 1, 1]})

        # [1] Price Chart
        ax1.plot(df.index, df['BB_up'], color='gray', linestyle=':', linewidth=1, alpha=0.3)
        ax1.plot(df.index, df['BB_low'], color='gray', linestyle=':', linewidth=1, alpha=0.3)
        ax1.fill_between(df.index, df['BB_low'], df['BB_up'], color='gray', alpha=0.05)
        # [1] Price Chart (캔들차트)
        up = df[df['close'] >= df['open']]
        down = df[df['close'] < df['open']]
        
        # 양봉 (Red)
        ax1.bar(up.index, up['close'] - up['open'], bottom=up['open'], color='red', edgecolor='red', width=0.6, alpha=0.8)
        ax1.vlines(up.index, up['low'], up['high'], color='red', linewidth=1)
        
        # 음봉 (Blue)
        ax1.bar(down.index, down['open'] - down['close'], bottom=down['close'], color='blue', edgecolor='blue', width=0.6, alpha=0.8)
        ax1.vlines(down.index, down['low'], down['high'], color='blue', linewidth=1)
        
        # 종가선(선택적 희미한 표시)
        ax1.plot(df.index, df['close'], label='종가', color='black', linewidth=1.0, alpha=0.3)
        ax1.plot(df.index, df['EMA5'], label='EMA 5', color='green', linewidth=1.2)
        ax1.plot(df.index, df['EMA20'], label='EMA 20', color='red', linewidth=1.2)
        ax1.plot(df.index, df['EMA60'], label='EMA 60', color='orange', linewidth=1.2)
        ax1.plot(df.index, df['EMA120'], label='EMA 120', color='purple', linewidth=1.2)
        
        sar_color = np.where(df['close'] > df['SAR'], 'red', 'blue')
        ax1.scatter(df.index, df['SAR'], s=5, c=sar_color, alpha=0.4, label='SAR')

        # [박스권] 최근 횡보 구간을 적응적으로 탐지 + 현재가 돌파/이탈 판정
        try:
            x_end = len(df) - 1
            box = indicators.detect_recent_box(df)
            if box:
                box_high, box_low = box['high'], box['low']
                status = box['status']
                # 상태별 색상: 상단돌파=빨강, 하단이탈=파랑, 박스권내=회색
                status_color = {'상단 돌파': 'red', '하단 이탈': 'blue', '박스권 내': 'dimgray'}[status]
                # 박스 경계선은 형성 구간부터 현재까지 연장해 현재가와의 관계를 표시
                ax1.hlines([box_high, box_low], box['start_idx'], x_end,
                           colors=['red', 'blue'], linestyles='--', linewidths=1.2, alpha=0.6)
                # 박스 형성 구간만 음영
                box_x = list(range(box['start_idx'], box['end_idx'] + 1))
                ax1.fill_between(box_x, box_low, box_high, color='orange', alpha=0.10)
                ax1.text(x_end, box_high, f" 박스상단 {box_high:,.0f}", color='red',
                         fontsize=8, fontweight='bold', va='bottom', ha='left', alpha=0.85)
                ax1.text(x_end, box_low, f" 박스하단 {box_low:,.0f}", color='blue',
                         fontsize=8, fontweight='bold', va='top', ha='left', alpha=0.85)
                # 현재 상태 배지 (우측 상단 — 좌측 상단 범례와 겹치지 않도록)
                ax1.text(0.99, 0.97, f"박스권: {status}", transform=ax1.transAxes,
                         fontsize=11, fontweight='bold', color=status_color, va='top', ha='right',
                         bbox=dict(boxstyle='round', facecolor='white', edgecolor=status_color, alpha=0.8))
                # 배지 아래에 현재 박스권 설정값 표기 (BOX_PERIOD / BOX_VALUE_AREA_PCT)
                box_period = config.INDICATOR_PARAMS.get("BOX_PERIOD", 20)
                box_va_pct = config.INDICATOR_PARAMS.get("BOX_VALUE_AREA_PCT", 50.0)
                ax1.text(0.99, 0.94, f"{box_period}일 / {box_va_pct:g}%", transform=ax1.transAxes,
                         fontsize=9.5, fontweight='bold', color='dimgray', va='top', ha='right', alpha=0.95)

            # [추세선] 스윙 피봇 연결 (상승=저점, 하락=고점)
            trend = indicators.get_trend_lines(df)
            for key, base_color in (('support', 'blue'), ('resistance', 'red')):
                if key not in trend:
                    continue
                slope, intercept, x_start = trend[key]
                
                # 기울기에 따라 실제 추세 방향 및 라벨 결정
                trend_dir = "상승" if slope > 0 else "하락"
                line_type = "지지선" if key == 'support' else "저항선"
                label = f"{trend_dir} {line_type}"
                
                tx = np.array([x_start, x_end])
                ty = slope * tx + intercept
                ax1.plot(tx, ty, color=base_color, linestyle='-', linewidth=1.4, alpha=0.6, label=label)
        except Exception as e:
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][DEBUG] 박스권/추세선 산출 실패: {e}[/dim red]")

        is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or 'DX-Y' in code)
        
        if period_type == 'weekly':
            period_str = "주봉 (약 3년)"
        elif period_type == 'daily':
            period_str = f"일봉 ({months}개월)" if months and months < 12 else "일봉 (1년)"
        elif period_type == 'hourly':
            period_str = "시봉 (3개월)"
        else:
            period_str = "분봉 (1분, 당일)"
        ax1.set_title(f"차트 분석 [{period_str}]: {name}", fontsize=16, pad=20)
        ax1.set_ylabel("지수" if is_index else "가격")
        ax1.legend(loc='upper left', ncol=4, fontsize=9)
        ax1.grid(True, alpha=0.2)
        try: ax1.yaxis.set_major_locator(MaxNLocator(nbins=30, prune='both'))
        except ImportError: pass 

        # [1-1] Volume Overlay (가격 차트 하단에 거래량 바 추가)
        ax1v = ax1.twinx()
        # 전일 대비 상승이면 빨강, 하락이면 파랑
        vol_colors = ['red' if c > o else 'blue' for c, o in zip(df['close'], df['close'].shift(1).fillna(df['close']))]
        ax1v.bar(df.index, df['volume'], color=vol_colors, alpha=0.15, width=0.6)
        ax1v.set_ylim(0, df['volume'].max() * 5) # 거래량이 캔들을 가리지 않도록 높이 조절
        ax1v.axis('off') # 축 눈금 숨김

        # [수정] 가격 Y축을 오른쪽으로 명시적 이동 (twinx 생성 후 적용)
        ax1.yaxis.tick_right()
        ax1.yaxis.set_label_position("right")

        # [2] MACD (위치 변경: RSI 위로 이동)
        ax2.plot(df.index, df['MACD'], label='MACD', color='gray', linewidth=1.2)
        ax2.plot(df.index, df['MACD_Signal'], label='Signal', color='orange', linewidth=1.0)
        
        # [변경] 히스토그램 색상 4단계 세분화
        hist_vals = df['MACD_Hist'].values
        hist_colors = []
        for i in range(len(hist_vals)):
            val = hist_vals[i]
            prev = hist_vals[i-1] if i > 0 else val
            
            if val >= 0:
                hist_colors.append('#FF0000' if val >= prev else '#FFAAAA') # 양수: 상승(진한빨강) / 하락(연한빨강)
            else:
                hist_colors.append('#0000FF' if val < prev else '#AAAAFF')  # 음수: 하락(진한파랑) / 상승(연한파랑-반등)
            
        ax2.bar(df.index, df['MACD_Hist'], color=hist_colors, alpha=0.8, label='Hist')
        ax2.set_ylabel("MACD")
        ax2.set_title("MACD (Moving Average Convergence Divergence)", fontsize=10, loc='right')
        ax2.grid(True, alpha=0.2); ax2.yaxis.tick_right(); ax2.yaxis.set_label_position("right")
        ax2.legend(loc='upper left', fontsize=8)

        # [3] ADX & DMI (RSI 위로 이동)
        ax3.plot(df.index, df['PLUS_DI'], label='+DI', color='red', linewidth=1.35, alpha=0.7)
        ax3.plot(df.index, df['MINUS_DI'], label='-DI', color='blue', linewidth=1.35, alpha=0.7)
        ax3.plot(df.index, df['ADX'], label='ADX', color='black', linewidth=1.5, alpha=0.8)
        for level in [15, 20, 30, 40]: ax3.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax3.fill_between(df.index, 40, df['ADX'], where=(df['ADX'] >= 40), color='purple', alpha=0.3)
        ax3.fill_between(df.index, 30, df['ADX'], where=((df['ADX'] >= 30) & (df['ADX'] < 40)), color='red', alpha=0.1)
        ax3.fill_between(df.index, 20, df['ADX'], where=((df['ADX'] >= 20) & (df['ADX'] < 30)), color='orange', alpha=0.1)
        ax3.fill_between(df.index, 15, df['ADX'], where=((df['ADX'] >= 15) & (df['ADX'] < 20)), color='yellow', alpha=0.1)
        ax3.set_ylabel("ADX & DMI")
        ax3.set_title("ADX & DMI", fontsize=10, loc='right')
        ax3.set_ylim(0, 100); ax3.grid(True, alpha=0.2); ax3.yaxis.tick_right(); ax3.yaxis.set_label_position("right")
        ax3.legend(loc='upper left', fontsize=8)

        # [4] RSI
        ax4.plot(df.index, df['RSI'], label='RSI(14)', color='gray', linewidth=1.2)
        rsi_up = config.INDICATOR_PARAMS["RSI_UPPER"]
        rsi_low = config.INDICATOR_PARAMS["RSI_LOWER"]
        for level in [rsi_low, 45, 55, rsi_up]: ax4.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax4.fill_between(df.index, rsi_up, df['RSI'], where=(df['RSI']>=rsi_up), color='purple', alpha=0.4)
        ax4.fill_between(df.index, 55, df['RSI'], where=((df['RSI'] >= 55) & (df['RSI'] < rsi_up)), color='red', alpha=0.1)
        ax4.fill_between(df.index, 45, df['RSI'], where=((df['RSI'] >= 45) & (df['RSI'] < 55)), color='orange', alpha=0.1)
        ax4.fill_between(df.index, rsi_low, df['RSI'], where=((df['RSI'] >= rsi_low) & (df['RSI'] < 45)), color='yellow', alpha=0.1)
        ax4.fill_between(df.index, rsi_low, df['RSI'], where=(df['RSI'] <= rsi_low), color='blue', alpha=0.4)
        
        ax4.set_ylabel("RSI")
        ax4.set_title("RSI (Relative Strength Index)", fontsize=10, loc='right')
        ax4.set_yticks([0, 10, 30, 50, 70, 90]); ax4.set_ylim(0, 100); ax4.grid(True, alpha=0.2)
        ax4.yaxis.tick_right(); ax4.yaxis.set_label_position("right")

        # [5] CCI
        ax5.plot(df.index, df['CCI'], label='CCI(20)', color='gray', linewidth=1.2)
        cci_up = config.INDICATOR_PARAMS["CCI_UPPER"]
        cci_low = config.INDICATOR_PARAMS["CCI_LOWER"]
        for level in [cci_up, 0, cci_low]: ax5.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax5.fill_between(df.index, cci_up, df['CCI'], where=(df['CCI'] >= cci_up), color='red', alpha=0.4)
        ax5.fill_between(df.index, 0, df['CCI'], where=((df['CCI'] > 0) & (df['CCI'] < cci_up)), color='orange', alpha=0.1)
        ax5.fill_between(df.index, cci_low, df['CCI'], where=((df['CCI'] > cci_low) & (df['CCI'] <= 0)), color='yellow', alpha=0.1)
        ax5.fill_between(df.index, cci_low, df['CCI'], where=(df['CCI'] <= cci_low), color='blue', alpha=0.4)
        ax5.set_ylabel("CCI")
        ax5.set_title("CCI (Commodity Channel Index)", fontsize=10, loc='right')
        ax5.grid(True, alpha=0.2); ax5.yaxis.tick_right(); ax5.yaxis.set_label_position("right")

        # [6] 이격도 (Disparity, 5/10/20/60일)
        ax6.plot(df.index, df['DISP5'], label='이격도 5', color='green', linewidth=1.0, alpha=0.8)
        ax6.plot(df.index, df['DISP10'], label='이격도 10', color='blue', linewidth=1.0, alpha=0.8)
        ax6.plot(df.index, df['DISP20'], label='이격도 20', color='red', linewidth=1.2, alpha=0.8)
        ax6.plot(df.index, df['DISP60'], label='이격도 60', color='orange', linewidth=1.2, alpha=0.8)
        ax6.axhline(100, color='black', linestyle='-', linewidth=0.8, alpha=0.5)
        for level in [95, 105]: ax6.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax6.set_ylabel("이격도")
        ax6.set_title("Disparity (이격도)", fontsize=10, loc='right')
        ax6.grid(True, alpha=0.2); ax6.yaxis.tick_right(); ax6.yaxis.set_label_position("right")
        ax6.legend(loc='upper left', ncol=4, fontsize=8)

        # [X축 설정]
        tick_indices = np.linspace(0, len(df) - 1, 15, dtype=int)
        
        def format_date(date_val):
            # Timestamp 객체 등 날짜형식인 경우 strftime 사용
            if hasattr(date_val, 'strftime'):
                if period_type in ['intraday', 'hourly']:
                    return date_val.strftime("%m-%d %H:%M")
                return date_val.strftime("%Y-%m-%d")
            
            s_date = str(date_val).split('.')[0]
            if period_type in ['intraday', 'hourly']:
                if len(s_date) >= 16: return s_date[5:16] # MM-DD HH:MM
            if len(s_date) == 8: return f"{s_date[:4]}-{s_date[4:6]}-{s_date[6:]}"
            elif '-' in s_date: return s_date[:10]
            return s_date

        formatted_labels = [format_date(df['date'].iloc[i]) for i in tick_indices]
        
        for ax in [ax1, ax2, ax3, ax4, ax5, ax6]:
            ax.set_xticks(tick_indices)
            ax.set_xticklabels(formatted_labels, rotation=0, ha='center', fontsize=9)
            # [수정] 모든 그래프의 X축 날짜 표시
            ax.tick_params(axis='x', labeltop=False, labelbottom=True)

        # 우측 여백 확보 — 박스상단/하단 등 우측 라벨이 잘리지 않도록 (sharex라 ax1만 조정)
        ax1.set_xlim(right=(len(df) - 1) + max(len(df) * 0.06, 4))

        plt.tight_layout()
        safe_code = re.sub(r'[=\-\.\^]', '', code)
        file_name = f"analysis_{safe_code}_{period_type}.png"
        file_path = os.path.join(config.CHART_DIR, file_name)
        plt.savefig(file_path, dpi=dpi); plt.close()
        
    if not quiet:
        config.console.print(f"\n[bold green]차트가 생성되었습니다: {file_name}[/bold green]")
    if open_file:
        try:
            if platform.system() == "Windows": os.startfile(file_path)
            elif platform.system() == "Darwin": os.system(f"open {file_path}")
            else: os.system(f"xdg-open {file_path}")
        except Exception: pass

    # [AI 분석] 생성된 차트 PNG 경로를 반환해 Gemini 비전 분석 등 후속 처리에 활용할 수 있게 한다.
    return file_path

def generate_monte_carlo_histogram(returns, name, code, open_file=True):
    """Monte Carlo 시뮬레이션 수익률 분포 히스토그램 생성"""
    setup_korean_font()
    
    plt.figure(figsize=(10, 6))
    # 히스토그램 그리기
    n, bins, patches = plt.hist(returns, bins=30, color='skyblue', edgecolor='black', alpha=0.7)
    
    # 제목 및 레이블
    plt.title(f"Monte Carlo 시뮬레이션 수익률 분포 (1,000회): {name}", fontsize=14)
    plt.xlabel("수익률 (%)", fontsize=12)
    plt.ylabel("빈도수 (Frequency)", fontsize=12)
    plt.grid(True, alpha=0.3, linestyle='--')
    
    # 통계선 표시
    avg_ret = np.mean(returns)
    plt.axvline(avg_ret, color='red', linestyle='dashed', linewidth=1.5, label=f'평균: {avg_ret:.2f}%')
    
    var_95 = np.percentile(returns, 5)
    plt.axvline(var_95, color='orange', linestyle='dashed', linewidth=1.5, label=f'VaR(95%): {var_95:.2f}%')
    
    plt.legend(loc='upper right')
    
    # 파일 저장
    safe_code = re.sub(r'[=\-\.\^]', '', code)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_name = f"mc_dist_{safe_code}_{timestamp}.png"
    file_path = os.path.join(config.CHART_DIR, file_name)
    
    plt.savefig(file_path, dpi=100)
    plt.close()
    
    config.console.print(f"\n[bold green]수익률 분포 히스토그램이 저장되었습니다: {file_name}[/bold green]")
    
    if open_file:
        try:
            if platform.system() == "Windows": os.startfile(file_path)
            elif platform.system() == "Darwin": os.system(f"open {file_path}")
            else: os.system(f"xdg-open {file_path}")
        except Exception: pass
