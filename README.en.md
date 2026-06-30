# MyStock HTS (Home Trading System)

[Korean](README.md) | [English](README.en.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Python-based personal stock auto-trading and analysis system utilizing the Korea Investment & Securities (KIS) and Toss Securities Open APIs.
It operates in a terminal (Console) environment and provides real-time market quotes, precise technical analysis, and strategy-based auto-trading features.

## Disclaimer

> **[IMPORTANT] Please read this before using the software.**

1. **Responsibility for Investment Results**: This software was written for development and educational purposes, and it is merely a supplementary tool for investment. All investment losses and damages incurred by using this software are entirely the **user's responsibility**, and the developer assumes no legal or financial liability.
2. **System Risks**: Due to API server failures, network instability, program bugs, or logic errors, orders may be omitted, duplicated, or executed at unintended prices. Users must continuously monitor the system and, in case of emergency, immediately halt the program and respond directly via the broker's HTS/MTS.
3. **Verification Recommended**: Before real trading, please thoroughly verify the system's stability and the validity of your strategy in a **mock investment (simulation)** environment for a sufficient period.

---

## Table of Contents
1. [Overview & Objective](#1-overview--objective)
2. [Trading Strategy](#2-trading-strategy)
3. [Configuration](#3-configuration)
4. [Architecture & Stability](#4-architecture--stability)
5. [Project Structure](#5-project-structure)
6. [Prerequisites](#6-prerequisites)
7. [Installation & Execution](#7-installation--execution)
8. [Telegram Bot](#8-telegram-bot)
9. [Disclosure Integration (OpenDART)](#9-disclosure-integration)
10. [AI-Powered Assistant](#10-ai-powered-assistant)
11. [Reserved Order System](#11-reserved-order-system)
12. [Known Issues](#12-known-issues)
13. [License](#13-license)

## 1. Overview & Objective

This program is an all-in-one stock tool based on a CLI (Command Line Interface) designed for **Quant and System Traders who value technical analysis**.
Without the need for a heavy HTS (Home Trading System), you can quickly and intuitively check real-time quotes, balances, and execute orders in a terminal environment. It helps you catch trading timing based on **data and indicators (EMA alignment, RSI, MACD, etc.)** rather than relying on intuition.

### Key Features
*   **HTS/MTS Replacement:** Real-time quote inquiry and buy/sell/modify/cancel order execution from the terminal.
*   **Full Support for NXT (Alternative Trading System) & SOR (Smart Order Routing):** Fully supports real-time quote integration and auto/manual/reserved trading during the regular session (KRX) as well as the Nextrade (NXT) operating hours (08:00~08:50, 15:30~20:00). (Note: Mock trading does not support this due to KIS API specs.)
*   **Enterprise-grade Reserved Orders:** Surpasses HTS limitations by supporting 24-hour background reserved trading based on quant scores, RSI, and trailing stops.
*   **Technical Analysis Automation:** Automates complex supplementary indicator calculations to provide intuitive investment judgment signals such as **'Buy/Rise/Interest/Watch/Caution/Sell'**.
*   **Individual Stock Strategy Settings:** Allows setting different buy/sell criteria (score, RSI) and take-profit/stop-loss/trailing-stop ratios individually per stock.
*   **In-depth Index Analysis:** Provides detailed charts for market indices such as KOSPI and NASDAQ, along with AI in-depth reports combined with macro environments.
*   **AI Investment Assistant:** Utilizes Google Gemini LLM to provide in-depth stock diagnostics, analysis of market-leading themes, interactive Q&A, and pre-market briefings.
*   **DART (Electronic Disclosure) Integration:** Utilizes OpenDART API for interest stock **disclosure monitoring** (importance classification + AI good/bad news interpretation), **dividend/earnings calendar** (ex-dividend dates by dividend cycle, earnings submission deadlines), and **real-time Telegram alerts for major disclosures** (capital increase, capital reduction, treasury stock, administrative issue designation, etc.).
*   **Market Index Filtering:** Risk management feature that analyzes the trend of KOSPI/KOSDAQ indices and automatically suspends buying in a downtrend.
*   **Portfolio De-synchronization (Correlation Filtering):** Prevents duplicate purchases of stocks that have similar price movements (correlation coefficient of 0.7 or higher) to currently held stocks, inducing portfolio diversification.
*   **Real-time Configuration Changes:** The ability to immediately change and permanently apply buy/sell conditions and investment weights while system trading is running.
*   **Trading Restricted Stock Management:** Exclude specific stocks from system trading (buy/sell restricted) and set to only receive alerts when buy signals occur.
*   **Integrated Management for Domestic/Overseas:** Integrates management of domestic and US stocks into a single interface using the KIS API.
*   **Support for Real/Mock Trading:** Easily switch between mock investment and real investment by just changing settings.
*   **Strategy Backtesting:** Validate the current trading strategy based on historical data and simulate expected returns.
*   **Enterprise-grade Stability & Performance Optimization:** 
    *   Strict dynamic configuration validation using **Pydantic** and a Thread-safe architecture.
    *   **Global Thread Pool reuse mechanism** preventing API bottlenecks and OS resource waste, driven by an independent background scheduler.
    *   Introduces a DB proxy architecture based on a worker Queue to **fundamentally block SQLite Lock issues** in a multi-threaded environment.

## 2. Trading Strategy

> **Notice:** System trading (auto-trading) only operates on the **'Domestic Stocks (국내주식)' list** registered in the 'Interest Stock Management' menu.

This system is based on a **Trend Following** strategy, catching high-probability entry points through multi-faceted technical analysis and executing strict risk management.

### 1. Buy Strategy
Buying is executed when both the composite score calculated through the **Quant Multi-Factor Model** and the filtering conditions are satisfied.

*   **Entry Conditions (AND condition)**:
    1.  **Composite Score**: **7.5 points or higher** (out of 10)
    2.  **Overheating Prevention**: RSI **under 65** (zone with upside potential)
    3.  **Supply & Demand Check**: Volume strength **100% or higher** (buying pressure dominance)
    4.  **Market Filter**: KOSPI/KOSDAQ index located above the 50-day moving average (avoiding downtrends)

*   **Scoring Model (Total 10 points)**:
    *   **Trend (Trend)**: Moving average alignment, MACD golden cross, Parabolic SAR uptrend
    *   **Momentum (Momentum)**: RSI bullish zone (50+) and upside potential zone (40~50), CCI uptrend
    *   **Strength (Strength)**: ADX trend strength (20+), OBV volume uptrend, Smart Money (Foreign/Institutional turnaround)
    *   **Bonus (Bonus)**: Additional points awarded for synergy between indicators

*   **Downtrend Exclusive: Oversold Mean Reversion**:
    *   A strategy aiming for a technical rebound in a temporary oversold (depressed) zone during a major downtrend or sudden drop due to individual bad news.
    *   Even if the composite score is insufficient, it enters as a **'Mean Reversion Buy'** if all following conditions are met:
        *   **Condition 1**: Disparity compared to the 20-day MA is very low (default `90%` or less)
        *   **Condition 2**: RSI turns upward after reaching oversold & **closes as a bullish candle today** (default `40` or less & higher than previous day & current > open)
        *   **Condition 3**: OBV uptrend or Smart Money inflow confirmed (cumulative buying pressure verified)
        *   **Condition 4**: Volume strength `120%` or higher during auto-trading execution (real-time buying pressure re-verified)
        *   *(Note: To prevent whipsaws (fake rebounds) in panic selling zones with ADX 45+, entry is blocked regardless of the score.)*

*   **Leading Stock Following: Super Momentum**:
    *   For leading stocks making strong rallies near 52-week highs, standard overheating criteria are relaxed to follow the trend longer.
    *   **Trigger Conditions**: Composite score **8.5+** & current price at **90%+** of 52-week high.
    *   **Strategy Change**: Maximum allowed buy RSI is raised to **75**, and overheating sell RSI is raised to **85** to follow the market's strong trend to the end.

### 2. Sell Strategy
To preserve profits and limit losses, the following sell logic is monitored in real-time. (Applied in order of priority)

1.  **Take Profit**: Immediate profit realization when the return reaches the target (**+50.0%**).
2.  **Half Take-Profit**: To respond to highly volatile markets, **50% of the holding amount is pre-sold** when reaching half the target take-profit return to secure early profits and defend the win rate.
3.  **Stop Loss**: Immediate sell to lock in losses when the loss rate reaches the limit (**-7.0%**).
4.  **Time-based Stop**: To reduce opportunity costs for sideways stocks, forcefully liquidate if the target minimum return (e.g., +3.0%) is not reached within a set period (default 20 days) after purchase. (Postponed if today's indicator state maintains a 'Buy' or 'Rise' trend.)
5.  **Trailing Stop**: After reaching a return of **+10.0%** or more, sell to preserve profit if it drops **-4.0%** from the peak (or ATR-linked dynamic drop).
6.  **Break Even Stop**: When the **maximum return achieved** since purchase reaches **+7.0%** (or ATR dynamic stop loss width) or more, forcefully raise the existing stop-loss line to the break-even profit zone (**+0.5%**) to secure minimal profit and defend against a loss.
7.  **Defensive Half Sell**: Take out 50% to preserve profits early when a downward reversal signal (SAR sell reversal + dropping below 5-day MA) occurs while the stock is rising.
8.  **Overheating Sell**: Preemptive sell judging as an overbought state if RSI exceeds **85**.
9.  **Trend Broken**: Sell if the composite score drops below **5.0 points** or major support lines collapse.
10. **ATR Dynamic Stop Loss**: When `USE_ATR_STOP` is set, a different stop loss rate is applied to each stock based on the volatility (ATR) at the time of purchase. (e.g., ATR * 2.0)

### 3. Scoring System
The composite score determining whether to buy is calculated based on the **Quant Multi-Factor Model**. (Total 10 points, 0.5 point increments)
*The weight of each factor can be adjusted via settings (`SCORING_WEIGHTS`); below are default values.*

1.  **Trend Factor [Default 4.0]**
    *   **Moving Average**: Current > 20MA (+0.5), 20/60/120MA Alignment (+1.0), 5MA > 20MA (+0.5)
    *   **Early Trend Reversal**: If 20MA <= 60MA, current price crosses above 60MA (+0.5)
    *   **MACD**: MACD > Signal Golden Cross (+0.5), MACD Histogram positive or rising (+0.5)
    *   **SAR**: Current price > SAR (Uptrend +0.5)

2.  **Momentum Factor [Default 2.5]**
    *   **RSI**: RSI >= 50 Bullish (+0.5), RSI >= 60 Momentum expansion (+0.5), 40 <= RSI < 50 Upside potential (+0.5)
    *   **CCI**: CCI > 0 Uptrend (+0.5), CCI > -100 Escaping oversold (+0.5)
    *   **DMI**: +DI > -DI Cross (+0.5)

3.  **Strength & Volume Factor [Default 1.5]**
    *   **ADX**: ADX >= 20 Trend formation confirmed (+0.5)
    *   **Volume**: Volume explosion compared to 20-day average (200%+) & Bullish candle (+0.5)
    *   **OBV & Smart Money**: OBV rising & Major supply turnaround (+0.5)

4.  **Synergy Bonus [Default 2.0]**
    *   **Trend Start**: Current > 60MA AND MACD positive (or expanding) AND ADX >= 20 (+1.0)
    *   **Momentum Explosion**: MACD positive (or expanding) AND (RSI >= 60) AND (OBV rising) (+1.0)

#### Scoring Guide
*   **8.5 ~ 10.0 points (Strong Buy)**: All indicators point to an uptrend with perfect correlation. Good to enter with a high weight.
*   **7.0 ~ 8.5 points (Buy)**: The trend is clear, but some secondary indicators haven't followed yet. (Default buy threshold `BUY_SCORE` = 7.0) Good for split buying.
*   **6.0 ~ 7.0 points (Rise)**: The trend is aligned and alive, but the score falls slightly short of the buy threshold. (`RISE_SCORE` = 6.0)
*   **Interest / Nascent (regardless of score)**: The trend alignment is **not yet complete**, but **early trend-reversal signals** (short-term golden cross, MACD improvement, +DI dominance, RSI crossing above 50, CCI improvement, supply inflow, MA60 proximity) are detected in **3 or more** counts (`INTEREST_SIGNAL_MIN`) with no clear risk signals (MACD dead cross, -DI dominance, RSI overheating/depletion, etc.). It is **detected even below the 120-day line**, intended for **manual swing (short-term) trading monitoring** to quickly recognize whether it may develop into an actual buy stage. Note: it is not an automatic-buy target.
*   **Below 5.0 points (Sell/Avoid)**: Downtrend or sideways market with no clear direction.

## 3. Configuration

You can set the **Global Strategy** in the `config.py` file, and apply individual settings per stock via the **'System Trading > Per-Stock Trading Rules'** menu in the program.
Also, you can modify global settings in real-time during execution via the **'Main Menu > [0] System Settings'** menu (persists even upon restart).

### 1. Technical Indicator Settings (`INDICATOR_PARAMS`)
*   **Chart Lookback Days (`CHART_LOOKBACK_DAYS`)**: 730 days (2 years). Retrieves sufficient past data for accurate calculation of moving averages (e.g., 120-day line).
*   **RSI (Relative Strength Index)**:
    *   Period: 14 days (`RSI_PERIOD`)
    *   Overbought limit: 70 (`RSI_UPPER`) / Oversold limit: 30 (`RSI_LOWER`)
*   **Parabolic SAR**:
    *   Acceleration Factor (AF): Start 0.02 (`SAR_AF_START`), Step 0.02 (`SAR_AF_STEP`), Max 0.2 (`SAR_AF_MAX`)
    *   Increasing the value makes reversal signals faster, but may increase fake signals.
*   **CCI (Commodity Channel Index)**:
    *   Period: 20 days (`CCI_WINDOW`)
    *   Overbought/Oversold limit: ±100 (`CCI_UPPER`, `CCI_LOWER`)
*   **ADX (Average Directional Index)**: Period 14 days (`ADX_PERIOD`). A stable trend is considered formed when it's 20 or higher.

### 2. Buy/Analysis Thresholds (`ANALYSIS_THRESHOLDS`)
*   **Buy Score (`BUY_SCORE`)**: Default **7.0 points**. A buy signal is generated when the composite score combining various technical indicators is equal to or higher than this value.
*   **Rise Score (`RISE_SCORE`)**: Default **6 points**. It doesn't meet the buy criteria, but there is an upward flow.
*   **Interest Signal Minimum (`INTEREST_SIGNAL_MIN`)**: Default **3**. When trend alignment is incomplete but early trend-reversal signals are detected in this many counts or more (with no risk signals), the stock is classified as **'Interest' (nascent)**. Detected even below the 120-day line, intended for manual swing-trade monitoring. (0 disables it.)
*   **Interest MA60 Proximity Ratio (`INTEREST_MA60_NEAR`)**: Default **0.97**. If the current price is at or above this ratio of the 60-day line (e.g., 97%), it counts as an 'MA60 breakout attempt' early signal even while still below the 60-day line.
*   **Maximum Buy Allowed RSI (`BUY_RSI_MAX`)**: Default **70**. Even if the buy score is met, we do not enter if the RSI is above this value, considering it already overheated.
*   **Buy Volume Strength (`BUY_VOL_STRENGTH`)**: Default **100.0%**. The volume strength at the time of purchase must be at least this value (buying pressure dominance).
*   **Mean Reversion (`USE_MEAN_REVERSION`)**: Catches the point where indicators rebound after reaching oversold in a downtrend or sudden drop.
    *   `MR_RSI_MAX`: Maximum allowed RSI for mean reversion entry (Default 40.0)
    *   `MR_DISPARITY_MAX`: Disparity limit compared to 20-day MA (Default 90.0% or less)
    *   `MR_VOL_STRENGTH`: High volume strength to confirm buying pressure at the bottom (Default 120.0%)
*   **Super Momentum (`SUPER_MOMENTUM_USE`)**: Relaxes the buy/sell RSI thresholds for powerful leading stocks (new high rally) to follow the trend longer.
    *   `SUPER_MOMENTUM_SCORE`: Minimum trigger composite score (Default 8.5 points)
    *   `SUPER_MOMENTUM_W52_POS`: Minimum 52-week high position (Default 90.0% or higher)
    *   `SUPER_BUY_RSI_MAX`: Maximum allowed buy RSI relaxed upon trigger (Default 75.0)

### 3. Sell Strategy (`SELL_STRATEGY`)
*   **Take Profit**: Realize profit when the return reaches **+50.0%** (`TAKE_PROFIT_RATE`).
*   **Half Take-Profit**: If `HALF_TAKE_PROFIT_USE` is True, 50% of the holdings are pre-sold when reaching half the target take-profit to secure early profit.
*   **Stop Loss**: Confirm loss when the loss rate reaches **-7.0%** (`STOP_LOSS_RATE`).
*   **Grace Period Stop Loss (`MR_GRACE_LOSS_RATE`)**: The maximum allowable loss rate during the grace period for stocks entered via mean reversion. (Default -7.0%)
*   **Time-based Stop**: If `TIME_STOP_USE` is True, sell to secure opportunity cost if the target return (`TIME_STOP_MIN_PROFIT_RATE`, default 3.0%) is not met within the set days (`TIME_STOP_DAYS`, default 20 days) after purchase. (Postponed if uptrend is maintained)
*   **Trailing Stop**:
    *   **Trigger Condition**: Start monitoring upon reaching **+10.0%** (`TRAILING_STOP_ACTIVATION_RATE`) return.
    *   **Sell Condition**: Sell if it drops **-4.0%** (`TRAILING_STOP_CALLBACK_RATE`) from the peak. (If `USE_ATR_STOP` is True, dynamic drop rate based on ATR is applied.)
*   **Break Even Stop**: When the highest return achieved reaches `BREAK_EVEN_PROFIT_RATE` (default 7.0%), raise the stop loss to `BREAK_EVEN_STOP_RATE` (default +0.5%) to defend profits.
*   **Defensive Half Sell**: If `DEFENSIVE_HALF_SELL_USE` is True, sell 50% on a downward reversal signal (SAR Sell + 5MA breakdown).
*   **Overheating Sell**: If RSI exceeds **85** (`TAKE_PROFIT_RSI`), it is considered an overbought zone, so a preemptive sell is executed.
*   **Trend Broken Sell**: Sell if the composite score falls below **5 points** (`SELL_SCORE`).
*   **ATR Stop Loss**: If `USE_ATR_STOP` is True, use ATR * `ATR_STOP_MULTIPLIER` at the time of purchase as the stop loss rate instead of a fixed rate.
*   **Max ATR Stop Loss Rate**: `MAX_ATR_STOP_LOSS_RATE` is a safety mechanism to prevent the stop loss width from becoming abnormally large due to data errors or excessive volatility. (Default -15.0%)

### 4. Risk Management & Filtering
*   **Slippage Adjustment**:
    *   **Overview**: An adjustment to account for the difference between the price at the time of the order and the actual execution price (unfavorable execution).
    *   **Backtesting**: Conservatively reflects actual trading costs (spread, execution delays) by assuming a higher purchase price and a lower selling price.
    *   **Real/Auto Trading**: Adjusts the order price to a more favorable direction (Buy: +0.1%, Sell: -0.1%) when placing market orders to increase the execution probability in rapidly changing markets.
    *   **Settings**: Default `0.002` (0.2%). Can be changed in `config.py` or the settings menu.
    *   **Recommendation**:
        *   **Large Caps/ETFs**: 0.1% ~ 0.2% (abundant liquidity)
        *   **Mid/Small Caps**: 0.3% ~ 0.5%
        *   **High Volatility**: 0.5% ~ 1.0%

*   **Monte Carlo Simulation**:
    *   **Overview**: Adds noise and repeatedly tests to verify the robustness of a strategy.
    *   **Method**: Injects ±1% random noise into the price data, reflects uncertainties like slippage variation and execution failures (1%), and runs 1000 times.
    *   **Result**: Validates whether the strategy is based on luck or skill via average returns, worst case (VaR 95%), standard deviation, etc.

*   **Market Index Filtering**: Automatically suspends new buys if the KOSPI/KOSDAQ index falls below a moving average (default 30 days), treating it as a 'downtrend'.
*   **Correlation Filtering**: Prevents new buys if the candidate stock shows high correlation (e.g., 0.7 or above) with currently held stocks to avoid concentration risk.
*   **Technical Filtering**: 
    *   **General Filter**: Suspends new buys if the trend breaks (MACD death cross, major MA breakdown).
    *   **Absolute Defense Filter**: Blocks new buys completely regardless of the score during "Super Panic" selling zones (ADX 45+ with strong sell pressure) to avoid fake rebounds.
*   **Daily Loss Limit**: Automatically pauses the system if the daily estimated asset drops beyond a set limit (e.g., -10%).
*   **Max Holding Stocks**: Limits the maximum number of stocks to hold in the portfolio (default 5).
*   **Kill Switch**: Transitions to standby mode to protect the account if continuous API or system errors occur (default 5 times).
*   **Order State Machine**: Strictly manages the order lifecycle to prevent duplicate buys or phantom positions.
*   **Risk-based Position Sizing**: Limits the maximum loss width (default 5%) per trade against the total account, automatically reducing buy weight for highly volatile stocks.
*   **Volatility Targeting**: Increases cash ratio when market volatility is high and expands investments when low.

### 5. Scoring Weights Optimization
*   **Overview**: Allows users to manually configure or optimize the weights for each factor (Trend, Momentum, Strength, Synergy) in the buy score.
*   **Settings**: `TREND` (4.0), `MOMENTUM` (2.5), `STRENGTH` (1.5), `SYNERGY` (2.0).
*   **Optimization**: Use the 'Weight Optimization' feature in the backtesting menu to find the best combination based on historical data.

### 6. Adaptive Thresholds
*   **Overview**: Dynamically adjusts the buy criteria score (`BUY_SCORE`) by analyzing the market phase (Bull/Bear/Sideways) in real-time.
*   **Operation**:
    *   **Bull**: Lowers buy criteria (e.g., -0.5 points) for aggressive entry.
    *   **Bear**: Raises buy criteria (e.g., +0.5 points) for conservative entry.
    *   **Sideways**: Maintains the default score.

## 4. Architecture & Stability

This system applies a robust backend architecture to solve concurrency issues and API communication bottlenecks that can occur in multi-threaded auto-trading environments.

*   **DB Worker Queue Proxy & SQLite Lock Prevention**: 
    *   **Problem**: Simultaneous write attempts to a local SQLite database by multiple threads cause `database is locked` errors, leading to critical order omissions.
    *   **Solution**: Routes all DB writes through a single worker queue proxy (`db_queue.py`), ensuring data integrity and completely preventing DB locks.
*   **Global Thread Pool**: 
    *   Prevents memory leaks and context switching overhead from indiscriminate thread creation by centrally managing a system-wide thread pool in `executors.py`.
*   **Dynamic Configuration Validation & Thread-Safe Architecture**: 
    *   Applies a `threading.RLock` mechanism to prevent crashes when trading strategy settings are changed in real-time. Validates parameters using `Pydantic`.
*   **Order State Machine & Kill Switch**: 
    *   Strictly tracks the order lifecycle from reception to execution/cancellation. A Kill Switch pauses the system if continuous network/API errors occur.
*   **Unified Real-time Price**: 
    *   To ensure the analysis screens and the system trader compute indicators from the **same intraday price**, the logic that overlays the latest real-time price onto the unconfirmed daily candle (close/high/low) is unified into a single entry point (`indicators.apply_realtime_price`), structurally eliminating score mismatches between the menu analysis and auto-trading.
    *   Domestic price/trade-strength is fetched in **a single call covering both KRX and NXT (alternative exchange)**; in real-trading mode, NXT session quotes are reflected in real time, sharing the same cache key as the system trader to cut redundant calls.
*   **Memory Protection (Raspberry Pi OOM guard)**: 
    *   For long-running operation on constrained devices (e.g., Raspberry Pi 1GB), the quote micro-cache and chart cache enforce a **maximum item count** and evict the oldest entries when exceeded, so memory does not grow unbounded even during full-market scans.
*   **Interrupt-safe Exceptions**: 
    *   Bare `except:` clauses were normalized to `except Exception:` so that `KeyboardInterrupt`/`SystemExit` propagate correctly (preserving Ctrl+C responsiveness and clean shutdown).

## 5. Project Structure

```text
my-stock-hts/
├── run.sh                # [Mac/Linux] Execution script
├── run.bat               # [Windows] Execution script
├── main.py               # Main execution file (Menu & Routing)
├── config.py             # Settings, Env vars, Data load
├── api.py                # KIS API, yfinance, OpenDART communication
├── constants.py          # Constant definitions (TR ID, field mapping, etc.)
├── indicators.py         # Technical indicators calculation (RSI, ADX, MACD, etc.)
├── utils.py              # Common utilities (dates, formatting, etc.)
├── session.py            # Session & Token management
├── context.py            # Global thread states & Lock management
├── requirements.txt      # Python dependencies list
├── pytest.ini            # Pytest test configuration file
├── .env.example          # Env var setup example file
├── LICENSE.md            # License file
├── db/                   # [Auto-generated] SQLite DB file storage
├── json/                 # [Auto-generated] Dynamic settings & state/cache file storage
│   ├── stock.json              # Interest/monitoring stock list
│   ├── restricted_stocks.json  # Trading restricted stock list
│   ├── daily_asset_state.json  # Initial starting asset record for the day (for daily loss limit)
│   ├── dynamic_config.json     # Backup of system settings changed during program execution
│   └── dart_corp_map.json      # [Auto-generated] DART stock code ↔ unique number (corp_code) mapping cache
├── logs/                 # [Auto-generated] Log file storage
├── chart/                # [Auto-generated] Chart image storage
├── data/                 # [Auto-generated] Excel/CSV export storage
├── tools/                # Various diagnostics & utility tools
│   ├── stock-hts               # [Linux server] tmux session auto-configuration script
│   ├── get_telegram_chat_id.py # Telegram Chat ID confirmation tool
│   ├── clear_trade_history.py  # Trading history & DB initialization tool
│   ├── check_execution.py      # Execution history confirmation tool
│   ├── check_unfilled_orders.py# Unfilled orders confirmation tool
│   ├── check_deposit_apis.py   # Deposit-related API confirmation tool
│   ├── check_simulation_balance.py  # Mock investment balance confirmation tool
│   ├── benchmark_api_tps.py    # API TPS (calls per second) benchmark tool
│   ├── simulate_thread_workers.py   # Thread worker performance simulation
│   ├── verify_autotrader_cancel.py  # Auto-trading cancellation logic verification tool
│   ├── search_indices_yfinance.py   # yfinance-based overseas index search tool
│   ├── gemini_tool.py          # Gemini AI feature direct test tool
│   └── get_google_genai.py     # Google GenAI SDK connection confirmation tool
├── tests/                # Pytest unit/integration test codes (750+)
└── modules/              # Feature-specific module folders
    ├── db_manager.py     # DB connection & query management
    ├── db_queue.py       # Single worker queue proxy for SQLite concurrency control
    ├── telegram_bot.py   # Telegram bot integration & notifications
    ├── scheduler.py      # Dedicated worker for background scheduling & timers
    ├── executors.py      # Central management of system-wide Thread Pool
    ├── prompts.py        # External management of prompt templates for AI assistant
    ├── settings.py       # [0] System Settings management
    ├── market.py         # [1] Market Indices inquiry
    ├── analysis.py       # [2] Stock price & technical analysis
    ├── chart.py          # [3] Chart visualization & analysis
    ├── backtest.py       # [4] Strategy Backtesting
    ├── auto_trade.py     # [5] System Trading (Auto Trading)
    ├── theme_analysis.py # [6] Stock trend analysis + AI (Gemini) analysis/disclosure summary
    ├── manage.py         # [7] Interest Stock Management
    ├── calendar_events.py# [7-6] Dividend/Earnings Calendar (DART + yfinance)
    ├── disclosure.py     # [7-7/7-8] Disclosure monitoring/earnings tracking + Telegram alerts (DART)
    ├── trading.py        # [8] Stock Order Management (Buy/Sell/Modify/Cancel)
    ├── reserved_order_monitor.py # Background reserved order (Stop loss, Trailing, etc.) monitoring thread
    └── account.py        # [9] Asset & Balance Management
```

## 6. Prerequisites

This program operates based on the Open APIs of **Korea Investment & Securities (KIS)** and **Toss Securities**.
You need an account and API access key from the brokerages to run the program normally. (You can register both or choose just one.)

### Korea Investment & Securities (KIS)
1.  **Open Account**: Via smartphone app ('Korea Investment').
2.  **Apply for KIS Developers**: Apply on the KIS Developers website.
3.  **Mock Investment (Recommended)**: Apply for mock trading via the website/HTS.
4.  **Issue API Key**: My Page > My Services > Issue Key (Real/Mock separately).

### Toss Securities
1.  **Open Account**: Via the Toss app.
2.  **Toss Developer Center**: Apply on the Toss Securities Open API website.
3.  **Issue API Key**: Issue App Key & Secret from the Developer Center.

### Common
5.  **Environment Variables**: Register the issued Keys and Account Numbers as System Environment Variables.

## 7. Installation & Execution

### 1. Download Source Code
```bash
git clone https://github.com/your-username/my-stock-hts.git
cd my-stock-hts
```

### 2. Create and Activate Virtual Environment
**[Recommended]** It is highly recommended to use a virtual environment.
```bash
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows
```

### 3. Install Libraries
```bash
pip install -r requirements.txt
```

### 4. Configuration
Register sensitive information like API Keys as **environment variables**:
*   `SIM_APP_KEY`, `SIM_APP_SECRET`, `SIM_ACC_NUM`: KIS Mock Investment
*   `REAL_APP_KEY`, `REAL_APP_SECRET`, `REAL_ACC_NUM`: KIS Real Investment
*   `AUTO_APP_KEY`, `AUTO_APP_SECRET`, `AUTO_ACC_NUM`: KIS Auto Trading Only (Optional)
*   `TOSS_APP_KEY`, `TOSS_APP_SECRET`, `TOSS_ACC_NUM`: Toss Securities
*   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram Bot (Optional)
*   `GEMINI_API_KEY`: Google Gemini API Key for AI features (Optional)
*   `DART_API_KEY`: OpenDART API Key for disclosures (Optional)

### 5. Execution
```bash
chmod +x run.sh
./run.sh

# Auto-trading mode right away
./run.sh --mode 1 --auto
```

## 8. Telegram Bot

We recommend integrating a Telegram Bot to receive trading history notifications and remotely control the system.

### 1. Create Bot (BotFather)
1. Search **@BotFather** on Telegram.
2. Type `/newbot` and follow the guide.
3. Copy the **API Token**.

### 2. Check Chat ID
1. Send a message to your newly created bot (e.g., "start").
2. Run `python tools/get_telegram_chat_id.py` and enter your token to get the **Chat ID**.

### 3. Environment Variables
Register `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `config.py` or as environment variables.

### 4. Main Commands
*   **Status**: `/status`, `/balance`, `/holdings`, `/profit`
*   **Control**: `/start`, `/stop`, `/restart`
*   **Analysis**: `/market`, `/signal`, `/analyze`, `/curate`, `/closing`
*   **Scan & Config**: `/scan`, `/config`, `/preset`, `/rules`, `/stocks`

## 9. Disclosure Integration (OpenDART)

By integrating the Financial Supervisory Service's **DART OpenAPI**, you can use features like disclosure monitoring, dividend/earnings calendars, and real-time Telegram alerts for major disclosures. (Menu `[7]`)
> The DART API Key is **optional**.

1.  **Issue API Key (Free)**: Apply on the OpenDART website.
2.  **Environment Variable**: Register `DART_API_KEY`.
3.  **Features**:
    *   `[6] Dividend/Earnings Calendar`: Automatically calculates expected ex-dividend dates and earnings deadlines.
    *   `[7] Disclosure Monitoring`: Filters recent disclosures and provides Gemini AI summaries of Good/Bad news.
    *   **Telegram Alerts**: Sends instant pushes for major disclosures (capital increase, administrative issues, etc.).

## 10. AI-Powered Assistant

*   **Overview**: Combines Google Gemini LLM's real-time web search capabilities with precise technical analysis to provide deep investment insights.
*   **Key Services**:
    1.  **AI In-depth Stock Diagnostics (`/analyze`)**: Writes future stock price prospect reports based on quant scores and news.
    2.  **Market-Leading Theme Analysis**: Analyzes top 5 themes and leading stocks.
    3.  **AI Pre-market Briefing**: Daily morning briefings summarizing global markets and hot issues.
    4.  **Interactive Q&A (`/ask`)**: Ask free-form questions about stocks/economy based on the latest news.
    5.  **AI Backtesting Diagnostics**: Proposes optimal buy/sell parameters evaluating backtest results.
    6.  **AI Trading Autopsy**: Analyzes your trading results after selling and advises on future strategies.
    7.  **AI Closing Briefing (`/closing`)**: Daily market review and analysis of held stocks.
    8.  **AI Curation (`/curate`)**: Discovers leading themes and recommends stocks based on real-time macro indicators.

**Google AI Studio Integration**: Get a free API Key from [Google AI Studio](https://aistudio.google.com/) and register it as `GEMINI_API_KEY`.

## 11. Reserved Order System

Supports **quant score and technical indicator-based reserved orders**, fully utilizing local computer resources.
Background monitoring runs every 3 seconds and is persistently saved in SQLite DB.

### Trigger Conditions (9 types)
1.  **STOP**: Sell when price drops below target.
2.  **BREAKOUT**: Chase buy when breaking resistance.
3.  **LIMIT**: Execute at a specific price.
4.  **TIME**: Execute unconditionally at a set time.
5.  **SCORE**: Triggered by quant score thresholds.
6.  **RSI**: Triggered when RSI crosses limits.
7.  **TRAILING_BUY**: Chase buy after a rebound from the lowest point.
8.  **TRAILING_SELL**: Sell to preserve profits after dropping from a peak.
9.  **EMA**: Execute when crossing moving averages.

### Smart Protection Logic
*   **Expiration Date**: Auto-cancels upon expiry (Today, This week, This month, Indefinite).
*   **Duplicate Prevention**: If a stock with a reserved sell is sold manually, the reserved sell is auto-cancelled.
*   **Network Optimization**: API quote queries are completely blocked during off-hours to prevent traffic waste and mock trading rate limit exhaustion.

## 12. Known Issues

*   **Unfilled Order Query Error (KIS Mock)**: Due to a KIS API bug, unfilled order queries may return empty lists even if the order was successfully placed. The system handles this via local order state tracking and blind cancellations.
*   **NXT and SOR Unsupported (KIS Mock)**: NXT real-time quotes and SOR unified orders are not supported in the KIS mock trading environment; only KRX regular-session trading is available. As a result, trading attempts during the post-15:30 NXT session raise errors, and **the price on the analysis screen stays frozen at the regular-session close and does not update** (because NXT quotes cannot be retrieved). They only work in the real investment mode.

## 13. License

This project is licensed under the Apache License 2.0.
