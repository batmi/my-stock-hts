# modules/chart.py
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager, rc
import platform
import os
import re
from matplotlib.ticker import MaxNLocator
import config
import api
import indicators

def setup_korean_font():
    current_os = platform.system()
    try:
        if current_os == "Windows": rc('font', family='Malgun Gothic')
        elif current_os == "Darwin": rc('font', family='AppleGothic')
        else: rc('font', family='NanumGothic')
    except: pass
    plt.rcParams['axes.unicode_minus'] = False

def generate_visual_chart(code, name, is_overseas):
    setup_korean_font()
    
    # [로그] 차트 생성 요청 시작
    if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 차트 생성 요청: {name} ({code}) | Overseas: {is_overseas}[/dim cyan]")

    with config.console.status(f"[bold green]{name} 맞춤형 분석 차트 생성 중...[/]"):
        df = api.get_chart_data(code, is_overseas)
        
        if df is None or df.empty:
            if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] 차트 데이터 수신 실패 (Empty)[/dim red]")
            config.console.print(f"[red]{name} 데이터를 불러올 수 없습니다.[/]")
            return
        
        # [로그] 데이터 수신 확인
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim magenta][DEBUG] 차트 데이터 수신 완료: {len(df)}행[/dim magenta]")
            if not df.empty:
                tail_str = df.tail(2).to_string().replace('\n', '\n[DEBUG]      ')
                config.console.print(f"[dim magenta][DEBUG]      Data Tail:\n[DEBUG]      {tail_str}[/dim magenta]")
        
        df = df.reset_index(drop=True)
        
        if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
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
        ax1.set_title(f"차트 분석: {name}", fontsize=16, pad=20)
        ax1.set_ylabel("지수" if is_index else "가격")
        ax1.legend(loc='upper left', ncol=4, fontsize=9)
        ax1.grid(True, alpha=0.2)
        ax1.yaxis.tick_right()
        ax1.yaxis.set_label_position("right")
        try: ax1.yaxis.set_major_locator(MaxNLocator(nbins=30, prune='both'))
        except ImportError: pass 

        # [2] RSI
        ax2.plot(df.index, df['RSI'], label='RSI(14)', color='black', linewidth=1.2)
        for level in [30, 45, 55, 70]: ax2.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax2.fill_between(df.index, 70, df['RSI'], where=(df['RSI']>=70), color='purple', alpha=0.2)
        ax2.fill_between(df.index, 55, df['RSI'], where=((df['RSI'] >= 55) & (df['RSI'] < 70)), color='red', alpha=0.2)
        ax2.fill_between(df.index, 45, df['RSI'], where=((df['RSI'] >= 45) & (df['RSI'] < 55)), color='darkorange', alpha=0.2)
        ax2.fill_between(df.index, 30, df['RSI'], where=((df['RSI'] >= 30) & (df['RSI'] < 45)), color='yellow', alpha=0.2)
        ax2.fill_between(df.index, 30, df['RSI'], where=(df['RSI'] <= 30), color='blue', alpha=0.2)
        
        ax2.set_ylabel("RSI")
        ax2.set_title("RSI (Relative Strength Index)", fontsize=10, loc='right')
        ax2.set_yticks([0, 10, 30, 50, 70, 90]); ax2.set_ylim(0, 100); ax2.grid(True, alpha=0.2)
        ax2.yaxis.tick_right(); ax2.yaxis.set_label_position("right")

        # [3] ADX
        ax3.plot(df.index, df['ADX'], label='ADX(14)', color='darkgreen', linewidth=1.2)
        for level in [15, 20, 30, 40]: ax3.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax3.fill_between(df.index, 40, df['ADX'], where=(df['ADX'] >= 40), color='purple', alpha=0.2)
        ax3.fill_between(df.index, 30, df['ADX'], where=((df['ADX'] >= 30) & (df['ADX'] < 40)), color='red', alpha=0.2)
        ax3.fill_between(df.index, 20, df['ADX'], where=((df['ADX'] >= 20) & (df['ADX'] < 30)), color='darkorange', alpha=0.2)
        ax3.fill_between(df.index, 15, df['ADX'], where=((df['ADX'] >= 15) & (df['ADX'] < 20)), color='yellow', alpha=0.2)
        ax3.set_ylabel("ADX")
        ax3.set_title("ADX (Average Directional Index)", fontsize=10, loc='right')
        ax3.set_ylim(0, 100); ax3.grid(True, alpha=0.2); ax3.yaxis.tick_right(); ax3.yaxis.set_label_position("right")

        # [4] CCI
        ax4.plot(df.index, df['CCI'], label='CCI(20)', color='brown', linewidth=1.2)
        for level in [100, 0, -100]: ax4.axhline(level, color='gray', linestyle=':', linewidth=0.8, alpha=0.5)
        ax4.fill_between(df.index, 100, df['CCI'], where=(df['CCI'] >= 100), color='red', alpha=0.2)
        ax4.fill_between(df.index, 0, df['CCI'], where=((df['CCI'] > 0) & (df['CCI'] < 100)), color='darkorange', alpha=0.2)
        ax4.fill_between(df.index, -100, df['CCI'], where=((df['CCI'] > -100) & (df['CCI'] <= 0)), color='yellow', alpha=0.2)
        ax4.fill_between(df.index, -100, df['CCI'], where=(df['CCI'] <= -100), color='blue', alpha=0.2)
        ax4.set_ylabel("CCI")
        ax4.set_title("CCI (Commodity Channel Index)", fontsize=10, loc='right')
        ax4.grid(True, alpha=0.2); ax4.yaxis.tick_right(); ax4.yaxis.set_label_position("right")
        
        # [5] OBV
        ax5.plot(df.index, df['OBV'], label='OBV', color='blue', linewidth=1.2)
        ax5.set_ylabel("OBV")
        ax5.set_title("OBV (On-Balance Volume)", fontsize=10, loc='right')
        ax5.grid(True, alpha=0.2); ax5.yaxis.tick_right(); ax5.yaxis.set_label_position("right")

        # [X축 설정]
        tick_indices = np.linspace(0, len(df) - 1, 15, dtype=int)
        
        def format_date(date_val):
            s_date = str(date_val).split('.')[0]
            if len(s_date) == 8: return f"{s_date[:4]}-{s_date[4:6]}-{s_date[6:]}"
            elif '-' in s_date: return s_date[:10]
            return s_date

        formatted_labels = [format_date(df['date'].iloc[i]) for i in tick_indices]
        
        for ax in [ax1, ax2, ax3, ax4, ax5]:
            ax.set_xticks(tick_indices)
            ax.set_xticklabels(formatted_labels, rotation=0, ha='center', fontsize=9)
        
        ax1.tick_params(axis='x', labeltop=False, labelbottom=True)
        ax5.tick_params(axis='x', labeltop=False, labelbottom=True)
        plt.setp(ax2.get_xticklabels(), visible=False)
        plt.setp(ax3.get_xticklabels(), visible=False)
        plt.setp(ax4.get_xticklabels(), visible=False)

        plt.tight_layout()
        safe_code = re.sub(r'[=\-\.\^]', '', code)
        file_name = f"analysis_{safe_code}.png"
        plt.savefig(file_name, dpi=300); plt.close()
        
    config.console.print(f"\n[bold green]차트가 생성되었습니다: {file_name}[/bold green]")
    try:
        if platform.system() == "Windows": os.startfile(file_name)
        elif platform.system() == "Darwin": os.system(f"open {file_name}")
        else: os.system(f"xdg-open {file_name}")
    except: pass

