# modules/chart.py
import matplotlib
matplotlib.use('Agg') # [추가] GUI 백엔드 미사용 (스레드 안전성 확보)
import matplotlib.pyplot as plt
import numpy as np
import logging
from matplotlib import rc
import platform
import os
import re
from matplotlib.ticker import MaxNLocator
import config
import api
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import indicators
from datetime import datetime
from contextlib import nullcontext

def setup_korean_font():
    current_os = platform.system()
    try:
        if current_os == "Windows": rc('font', family='Malgun Gothic')
        elif current_os == "Darwin": rc('font', family='AppleGothic')
        else: rc('font', family='NanumGothic')
    except: pass
    plt.rcParams['axes.unicode_minus'] = False

def generate_visual_chart(code, name, is_overseas, open_file=True, dpi=300, quiet=False, period_type='daily'):
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
            progress.add_task(f"[bold green]{name} 맞춤형 분석 차트 생성 중...[/]", total=None)
            
        df = api.get_chart_data(code, is_overseas, period_type)
        
        if df is None or df.empty:
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] 차트 데이터 수신 실패 (Empty)[/dim red]")
            else:
                logging.error(f"[Chart] {name}({code}) 데이터 수신 실패 (Empty DataFrame). Period: {period_type}")
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
        df['ADX'] = indicators.get_adx_full_series(df)
        df['RSI'] = indicators.get_rsi_full_series(df)
        df['CCI'] = indicators.get_cci_full_series(df)
        df['OBV'] = indicators.get_obv_full_series(df)
        df['MACD'], df['MACD_Signal'], df['MACD_Hist'] = indicators.get_macd_full_series(df)

        # [변경] 서브플롯 5개로 조정 (OBV 삭제)
        fig, (ax1, ax2, ax3, ax4, ax5) = plt.subplots(5, 1, figsize=(20, 22), sharex=True, gridspec_kw={'height_ratios': [4, 1, 1, 1, 1]})

        # [1] Price Chart
        ax1.plot(df.index, df['BB_up'], color='gray', linestyle=':', linewidth=1, alpha=0.3)
        ax1.plot(df.index, df['BB_low'], color='gray', linestyle=':', linewidth=1, alpha=0.3)
        ax1.fill_between(df.index, df['BB_low'], df['BB_up'], color='gray', alpha=0.05)
        ax1.plot(df.index, df['close'], label='종가', color='black', linewidth=1.5, alpha=0.5)
        ax1.plot(df.index, df['EMA5'], label='EMA 5', color='green', linewidth=1.2)
        ax1.plot(df.index, df['EMA20'], label='EMA 20', color='red', linewidth=1.2)
        ax1.plot(df.index, df['EMA60'], label='EMA 60', color='orange', linewidth=1.2)
        ax1.plot(df.index, df['EMA120'], label='EMA 120', color='purple', linewidth=1.2)
        
        sar_color = np.where(df['close'] > df['SAR'], 'red', 'blue')
        ax1.scatter(df.index, df['SAR'], s=5, c=sar_color, alpha=0.4, label='SAR')
        
        is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or 'DX-Y' in code)
        
        period_str = "일봉 (1년)" if period_type == 'daily' else ("시봉 (3개월)" if period_type == 'hourly' else "분봉 (1분, 당일)")
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

        # [3] RSI
        ax3.plot(df.index, df['RSI'], label='RSI(14)', color='gray', linewidth=1.2)
        rsi_up = config.INDICATOR_PARAMS["RSI_UPPER"]
        rsi_low = config.INDICATOR_PARAMS["RSI_LOWER"]
        for level in [rsi_low, 45, 55, rsi_up]: ax3.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax3.fill_between(df.index, rsi_up, df['RSI'], where=(df['RSI']>=rsi_up), color='purple', alpha=0.4)
        ax3.fill_between(df.index, 55, df['RSI'], where=((df['RSI'] >= 55) & (df['RSI'] < rsi_up)), color='red', alpha=0.1)
        ax3.fill_between(df.index, 45, df['RSI'], where=((df['RSI'] >= 45) & (df['RSI'] < 55)), color='orange', alpha=0.1)
        ax3.fill_between(df.index, rsi_low, df['RSI'], where=((df['RSI'] >= rsi_low) & (df['RSI'] < 45)), color='yellow', alpha=0.1)
        ax3.fill_between(df.index, rsi_low, df['RSI'], where=(df['RSI'] <= rsi_low), color='blue', alpha=0.4)
        
        ax3.set_ylabel("RSI")
        ax3.set_title("RSI (Relative Strength Index)", fontsize=10, loc='right')
        ax3.set_yticks([0, 10, 30, 50, 70, 90]); ax3.set_ylim(0, 100); ax3.grid(True, alpha=0.2)
        ax3.yaxis.tick_right(); ax3.yaxis.set_label_position("right")

        # [4] ADX
        ax4.plot(df.index, df['ADX'], label='ADX(14)', color='gray', linewidth=1.2)
        for level in [15, 20, 30, 40]: ax4.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax4.fill_between(df.index, 40, df['ADX'], where=(df['ADX'] >= 40), color='purple', alpha=0.4)
        ax4.fill_between(df.index, 30, df['ADX'], where=((df['ADX'] >= 30) & (df['ADX'] < 40)), color='red', alpha=0.1)
        ax4.fill_between(df.index, 20, df['ADX'], where=((df['ADX'] >= 20) & (df['ADX'] < 30)), color='orange', alpha=0.1)
        ax4.fill_between(df.index, 15, df['ADX'], where=((df['ADX'] >= 15) & (df['ADX'] < 20)), color='yellow', alpha=0.1)
        ax4.set_ylabel("ADX")
        ax4.set_title("ADX (Average Directional Index)", fontsize=10, loc='right')
        ax4.set_ylim(0, 100); ax4.grid(True, alpha=0.2); ax4.yaxis.tick_right(); ax4.yaxis.set_label_position("right")

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
        
        for ax in [ax1, ax2, ax3, ax4, ax5]:
            ax.set_xticks(tick_indices)
            ax.set_xticklabels(formatted_labels, rotation=0, ha='center', fontsize=9)
            # [수정] 모든 그래프의 X축 날짜 표시
            ax.tick_params(axis='x', labeltop=False, labelbottom=True)

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
        except: pass

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
        except: pass
