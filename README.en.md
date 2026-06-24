# MyStock HTS (Home Trading System)

[🇰🇷 Korean](README.md) | [🇺🇸 English](README.en.md)

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
*   **Technical Analysis Automation:** Automates complex supplementary indicator calculations to provide intuitive investment judgment signals such as **'Buy/Rise/Watch/Caution/Sell'**.
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
    *   **Trend**: Moving average alignment, MACD golden cross, Parabolic SAR uptrend
    *   **Momentum**: RSI bullish zone (50+) and upside potential zone (40~50), CCI uptrend
    *   **Strength**: ADX trend strength (20+), OBV volume uptrend, Smart Money (Foreign/Institutional turnaround)
    *   **Bonus**: Additional points awarded for synergy between indicators

*   **Downtrend Exclusive: Oversold Mean Reversion**:
    *   A strategy aiming for a technical rebound in a temporary oversold (depressed) zone during a major downtrend or sudden drop due to individual bad news.
    *   Even if the composite score is insufficient, it enters as a **'Mean Reversion Buy'** if all following conditions are met:
        *   **Condition 1**: Disparity compared to the 20-day MA is very low (default `90%` or less)
        *   **Condition 2**: RSI turns upward after reaching oversold & **closes as a bullish candle today** (default `40` or less & higher than previous day & current > open)
        *   **Condition 3**: OBV uptrend or Smart Money inflow confirmed
        *   **Condition 4**: Volume strength `120%` or higher during auto-trading execution
        *   *(Note: To prevent whipsaws (fake rebounds) in panic selling zones with ADX 45+, entry is blocked.)*

*   **Leading Stock Following: Super Momentum**:
    *   For leading stocks making strong rallies near 52-week highs, standard overheating criteria are relaxed to follow the trend longer.
    *   **Trigger Conditions**: Composite score **8.5+** & current price at **90%+** of 52-week high.
    *   **Strategy Change**: Maximum allowed buy RSI is raised to **75**, and overheating sell RSI is raised to **85**.

### 2. Sell Strategy
To preserve profits and limit losses, the following sell logic is monitored in real-time. (Applied in order of priority)

1.  **Take Profit**: Immediate profit realization when the return reaches the target (**+50.0%**).
2.  **Half Take-Profit**: To respond to highly volatile markets, **50% of the holding amount is pre-sold** when reaching half the target take-profit return to secure early profits.
3.  **Stop Loss**: Immediate sell to lock in losses when the loss rate reaches the limit (**-7.0%**).
4.  **Time-based Stop**: To reduce opportunity costs for sideways stocks, forcefully liquidate if the target minimum return (e.g., +3.0%) is not reached within a set period (default 20 days) after purchase. (Postponed if today's indicator state maintains 'Buy' or 'Rise' trend.)
5.  **Trailing Stop**: After reaching a return of **+10.0%** or more, sell to preserve profit if it drops **-4.0%** from the peak (or ATR-linked dynamic drop).
6.  **Break Even Stop**: When the **maximum return achieved** since purchase reaches **+7.0%** (or ATR dynamic stop loss width) or more, forcefully raise the existing stop-loss line to the break-even profit zone (**+0.5%**) to secure minimal profit.
7.  **Defensive Half Sell**: Take out 50% to preserve profits early when a downward reversal signal (SAR sell reversal + dropping below 5-day MA) occurs while the stock is rising.
8.  **Overheating Sell (RSI)**: Preemptive sell judging as an overbought state if RSI exceeds **85**.
9.  **Trend Broken**: Sell if the composite score drops below **5.0 points** or major support lines collapse.
10. **ATR Dynamic Stop Loss**: When `USE_ATR_STOP` is set, a different stop loss rate is applied to each stock based on the volatility (ATR) at the time of purchase. (e.g., ATR * 2.0)

### 3. Scoring System
The composite score determining whether to buy is calculated based on the **Quant Multi-Factor Model**. (Total 10 points, 0.5 point increments)
*The weight of each factor can be adjusted via settings (`SCORING_WEIGHTS`); below are default values.*

1.  **Trend Factor [Default 4.0]**: MA alignment, early trend reversal, MACD golden cross, SAR uptrend.
2.  **Momentum Factor [Default 2.5]**: RSI bullish zone, CCI uptrend, DMI cross.
3.  **Strength & Volume Factor [Default 1.5]**: ADX trend strength, trading volume explosion, OBV/Smart Money turnaround.
4.  **Synergy Bonus [Default 2.0]**: Additional points for synergistic indicator combinations.

#### Scoring Guide
*   **8.5 ~ 10.0 (Strong Buy)**: All indicators point to an uptrend with perfect correlation. Good to enter with a high weight.
*   **7.0 ~ 8.0 (Buy)**: The trend is clear, but some secondary indicators haven't followed yet. Good for split buying.
*   **5.5 ~ 6.5 (Watch)**: Early stage of an uptrend or weakening trend. Watch if it goes up to the 7-point range rather than entering hastily.
*   **Below 5.0 (Sell/Avoid)**: Downtrend or sideways market with no clear direction.

## 3. Configuration

You can set the **Global Strategy** in the `config.py` file, and apply individual settings per stock via the **'System Trading > Per-Stock Trading Rules'** menu in the program.
Also, you can modify global settings in real-time during execution via the **'Main Menu > [0] System Settings'** menu (persists even upon restart).

*(For detailed explanations of parameters such as `INDICATOR_PARAMS`, `ANALYSIS_THRESHOLDS`, `SELL_STRATEGY`, and `Risk Management`, please refer to `config.py`.)*

## 4. Architecture & Stability

This system applies a robust backend architecture to solve concurrency issues and API communication bottlenecks that can occur in multi-threaded auto-trading environments.

*   **DB Worker Queue Proxy & SQLite Lock Prevention**: Solves `database is locked` errors by centralizing all DB write tasks into a Single Worker Queue Proxy.
*   **Global Thread Pool**: Manages a system-wide thread pool to prevent memory leaks and context switching overhead from indiscriminate thread creation.
*   **Dynamic Configuration Validation & Thread-Safe Architecture**: Applies a `threading.RLock` mechanism to prevent crashes when trading strategy settings are changed in real-time, and strictly validates parameter values using `Pydantic`.
*   **Order State Machine & Kill Switch**: Strictly tracks the order lifecycle to prevent duplicate orders or phantom positions. A Kill Switch pauses the system if continuous errors occur.

## 5. Project Structure
*(See Korean README for directory tree)*

## 6. Prerequisites

This program operates based on the Open APIs of **Korea Investment & Securities (KIS)** and **Toss Securities**.
You need an account and API access key from the brokerages to run the program normally. (You can register both or choose just one.)

### Korea Investment & Securities (KIS)
1.  Open an account (non-face-to-face via smartphone app).
2.  Apply for KIS Developers service.
3.  Apply for Mock Investment (highly recommended).
4.  Issue API Key (Real/Mock).

### Toss Securities
1.  Open a Toss Securities account via the Toss app.
2.  Apply for service on the Toss Securities Developer Center.
3.  Issue API Key (App Key and Secret).

### Common
5.  Register the issued Keys and Account Numbers as System Environment Variables.

## 7. Installation & Execution

### 1. Download Source Code
```bash
git clone https://github.com/your-username/my-stock-hts.git
cd my-stock-hts
```

### 2. Create and Activate Virtual Environment
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
*   `TOSS_APP_KEY`, `TOSS_APP_SECRET`, `TOSS_ACC_NUM`: Toss Securities
*   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram Bot (Optional)
*   `GEMINI_API_KEY`: Google Gemini API Key for AI features (Optional)
*   `DART_API_KEY`: OpenDART API Key for disclosures (Optional)

### 5. Execution
```bash
chmod +x run.sh
./run.sh
```

## 8. Telegram Bot

We recommend integrating a Telegram Bot to receive trading history notifications and remotely control the system.
Create a bot via **@BotFather**, obtain the API Token, find your Chat ID using `tools/get_telegram_chat_id.py`, and register them as environment variables.

### Main Commands
*   `/status`, `/balance`, `/holdings`, `/profit`
*   `/start`, `/stop`, `/restart`
*   `/market`, `/signal`, `/analyze`, `/curate`, `/closing`

## 9. Disclosure Integration (OpenDART)

By integrating the Financial Supervisory Service's **DART OpenAPI**, you can use features like disclosure monitoring, dividend/earnings calendars, and real-time Telegram alerts for major disclosures.
Issue a free API Key from the OpenDART website and set it as `DART_API_KEY`.

## 10. AI-Powered Assistant

*   **AI In-depth Stock Diagnostics**: Analyzes technical indicators and real-time news to write future stock price prospect reports.
*   **Market-Leading Theme Analysis**: Summarizes the top 5 leading themes, their backgrounds, and supply/demand trends.
*   **AI Pre-market Briefing**: Daily morning briefings on global market data and hot issues before the Korean market opens.
*   **Interactive Q&A (`/ask`)**: Ask free-form questions about stocks/economy and get accurate answers based on the latest news.
*   **AI Trading Autopsy**: Analyzes your trading results after selling and advises on future strategies.

Issue a free API Key from [Google AI Studio](https://aistudio.google.com/) and register it as `GEMINI_API_KEY`.

## 11. Reserved Order System

Supports **quant score and technical indicator-based reserved orders**, fully utilizing local computer resources.
Triggers include Stop Loss, Breakout, Limit Price, Specific Time, Quant Score, RSI, Trailing Buy, Trailing Sell, and EMA Cross. Features smart protection logic to prevent double execution and optimize API traffic during off-hours.

## 12. Known Issues

*   **Unfilled Order Query Error (KIS Mock)**: Due to a KIS API bug, unfilled order queries may return empty lists even if the order was successfully placed. The system handles this via local order state tracking and blind cancellations.
*   **NXT and SOR Unsupported (KIS Mock)**: NXT real-time quotes and SOR unified orders are not supported in the KIS mock trading environment. They only work in the real investment mode.

## 13. License

This project is licensed under the Apache License 2.0.
