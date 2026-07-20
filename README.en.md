# MyStock HTS (Home Trading System)

[Korean](README.md) | [English](README.en.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Python-based personal stock auto-trading and analysis system utilizing the Korea Investment & Securities (KIS) and Toss Securities Open APIs.
It operates in a terminal (Console) environment and provides real-time market quotes, precise technical analysis, and strategy-based auto-trading features.

> **The fundamental principle of this trading system is Trend Following.** Losses are cut fast and profits run until the trend breaks (no fixed take profit; the Chandelier trailing stop is the primary exit), and only strong, durable trends are bought. Settings that violate this principle (fixed take profit, half take-profit, RSI overheating sell, defensive half sell, mean-reversion buying) are preserved in code but disabled by default and hidden from the settings menus.

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
*   **Technical Analysis Automation:** Automates complex supplementary indicator calculations to provide intuitive investment judgment signals such as **'Buy/Wait/Rise/Interest/Watch/Caution/Sell'**.
*   **Individual Stock Strategy Settings:** Allows setting different buy/sell criteria (score, RSI) and take-profit/stop-loss/trailing-stop ratios individually per stock.
*   **In-depth Index Analysis:** Provides detailed charts for market indices such as KOSPI and NASDAQ, along with AI in-depth reports combined with macro environments.
*   **AI Investment Assistant:** Utilizes Google Gemini LLM to provide in-depth stock diagnostics, analysis of market-leading themes, interactive Q&A, and pre-market briefings.
*   **DART (Electronic Disclosure) Integration:** Utilizes OpenDART API for watchlist **disclosure monitoring** (importance classification + AI good/bad news interpretation + auto-extracted details for supply contracts, treasury stock, bonus issues, etc.), **dividend/earnings calendar** (confirmed record-date parsing, estimated earnings dates), **supply-demand & overhang signals** (treasury stock decisions, mezzanine overhang, insider/5% reports), **financial snapshot** (standalone quarterly earnings, ROE, debt ratio), and **real-time Telegram alerts for major disclosures**.
*   **Market Index Filtering:** Risk management feature that analyzes the trend of KOSPI/KOSDAQ indices and automatically suspends buying in a downtrend.
*   **Relative Strength (RS) Filter:** Automatically excludes new buys whose 6-month return trails their home index (KOSPI/KOSDAQ) — blocking entries into weak trends that cannot even beat the market.
*   **Real-time Market Halt Alerts (Circuit Breaker / VI):** Detects market-wide circuit breakers (CB) and per-stock Volatility Interruptions (VI) based on actual exchange status flags and instantly notifies via Telegram. (CB on by default; VI is optional.)
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

> **Trend-Following Doctrine (Core Principles)**: ① Cut losses short, let profits run — there is no fixed take-profit ceiling; the **trailing stop (Chandelier Exit)** is the primary exit. ② Buy strong, durable trends — buy candidates are prioritized by **trend quality (regression momentum)** → score → 52-week-high proximity → volume strength, and the **Relative Strength (RS) filter** blocks entries that trail their home index outright. Only positions validated by profit are **pyramided (scaled up)**; averaging down into losers is structurally impossible. ③ The trend is the market — new buys are suspended when the index is below its reference moving average.

> This section explains the **concepts** of the strategy. For the trigger thresholds (defaults) and parameter details of each condition, see **[3. Configuration](#3-configuration)**. (All values are configurable.)

### 1. Buy Strategy
Buying is executed when both the composite score calculated through the **Quant Multi-Factor Model** and the filtering conditions are satisfied.

*   **Entry Conditions (AND condition)**:
    1.  **Composite Score**: At or above the buy threshold (`BUY_SCORE`) — see [3. Scoring System](#3-scoring-system) below for how the score is built
    2.  **Overheating Prevention**: RSI under the allowed ceiling (`BUY_RSI_MAX`) (relaxed when Super Momentum triggers)
    3.  **Supply & Demand Check**: Volume strength at or above the threshold (`BUY_VOL_STRENGTH`) (buying pressure dominance)
    4.  **Market Filter**: KOSPI/KOSDAQ index located above the reference moving average (`MARKET_FILTER_MA`) (avoiding downtrends)
    5.  **Relative Strength (RS) Filter**: The stock's 6-month (`MOMENTUM_LOOKBACK`) return must **exceed** that of its home index (KOSPI/KOSDAQ) (`USE_RS_FILTER`) — a +15% stock in a +20% market is a laggard; stocks with no excess return over the index are not considered "clear trends" and are excluded. (Passes automatically on index-data failure or insufficient stock history.)

*   **Buy Priority (Gate vs. Ranking separation)**: When multiple candidates pass the entry conditions (the gate), they are bought in order of **① trend quality → ② composite score → ③ 52-week-high proximity → ④ volume strength**.
    *   **Trend Quality (Regression Momentum)**: The **annualized slope × R²** of a linear regression over the last 90 days (`TREND_QUALITY_LOOKBACK`) of log closes (Clenow momentum). The slope measures trend strength and R² measures smoothness (a proxy for persistence), so stocks that stumbled into alignment through wild swings rank behind steadily rising leaders. Since the score is a sum of binary signals with frequent ties, it serves only as the entry gate, while the continuous trend-quality value decides priority among candidates.

*   **Leading Stock Following: Super Momentum**:
    *   For leading stocks making strong rallies near 52-week highs, the standard overheating criteria (allowed buy/sell RSI) are relaxed to follow the market's strong trend to the end.

*   **Pyramiding: Scaling into Winners**:
    *   For held positions whose return is at or above the trigger (`PYRAMIDING_PROFIT_TRIGGER`, default +10%) with the buy signal (trend) still intact, the position is increased by a ratio of the held quantity (`PYRAMIDING_RATIO`, default 50%), limited to once per position by default.
    *   The exact opposite of averaging down — **it never fires on losing positions.** The stop-loss rate for the added tranche is recalculated from the ATR at the time of scaling and feeds into the weighted-average stop.

*   **Downtrend Exclusive: Oversold Mean Reversion** — **disabled by default (hidden from menus)**:
    *   A strategy aiming for a technical rebound in oversold zones. As a counter-trend edge that conflicts with the trend-following doctrine (it needs short reversion targets and tight stops, mismatching the system's wide trend-following exits) and competes for scarce position slots, it is **disabled in the defaults and in every preset and is not exposed in the settings menus**. (Re-enabling is only possible by editing `json/dynamic_config.json` directly.)

### 2. Sell Strategy
Following the trend-following doctrine — **"cut losses fast, keep the upside open until the trend breaks"** — the exit conditions are monitored in the following priority order, with no fixed take-profit ceiling.

1.  **Stop Loss**: Immediate sell when the loss rate reaches the limit. With `USE_ATR_STOP`, a dynamic stop based on the volatility (ATR) at purchase time is applied per stock.
    *   **Break Even Stop**: When the maximum return achieved reaches the trigger level, the stop-loss line is automatically raised to the break-even profit zone (+0.5%) so a winner cannot turn into a loser.
2.  **Time-based Stop**: Only stocks that are **still at a loss** after the set period and have lost upward momentum are liquidated. (Profitable positions are never sold on time; postponed while the uptrend holds.)
3.  **Trailing Stop — Primary Exit (Chandelier Exit)**: After the activation return is reached, sell when the price drops from the peak by a dynamic callback based on the dedicated trailing ATR multiplier. Volatile leaders get a proportionally wider callback so the trend can be followed to the end. (The default `TS_MAX_GIVEBACK_RATIO=0` removes the giveback cap — a pure Chandelier; set it to a positive ratio to cap how much of the maximum gain can be given back.)
4.  **Trend Broken**: Full liquidation if the composite score drops below the sell threshold or the state is classified as 'Sell'.

> **Disabled-by-default options (upside limiters, hidden from menus)**: Fixed take profit (`TAKE_PROFIT_RATE=0`), half take-profit (`HALF_TAKE_PROFIT_USE=False`), RSI overheating sell (`TAKE_PROFIT_RSI=0`), and defensive half sell (`DEFENSIVE_HALF_SELL_USE=False`) cut off the fat tail of profits, so they are turned off in the defaults and in every preset, and **are not exposed in the settings menus to protect the trend-following principle**. Re-enabling is only possible by editing `json/dynamic_config.json` directly or via per-stock custom rules (auto-trading menu).

### 3. Scoring System
The composite score determining whether to buy is calculated based on the **Quant Multi-Factor Model**. (Total 10 points, 0.5 point increments)
*The weight of each factor can be adjusted via settings (`SCORING_WEIGHTS`); below are default values.*

1.  **Trend Factor [Default 4.0]**
    *   **Moving Average**: Current > 20MA (+0.5), 20/60/120MA Alignment (+1.0), 5MA > 20MA (+0.5) — the highly correlated EMA signal cluster is **capped at 2.0 points** in total (prevents over-crediting)
    *   **Early Trend Reversal**: If 20MA <= 60MA, current price crosses above 60MA (+0.5)
    *   **Trend Persistence**: Close was above the 60MA for ≥ 70% (`TREND_PERSIST_MIN`) of the last 120 days (`TREND_PERSIST_LOOKBACK`) (+0.5) — a durability signal measuring "how long the trend has held" rather than the current snapshot, separating freshly crossed, unproven trends from long-sustained ones.
    *   **MACD**: MACD > Signal Golden Cross (+0.5), MACD Histogram positive or rising (+0.5)
    *   **SAR**: Current price > SAR (Uptrend +0.5)

2.  **Momentum Factor [Default 2.5]**
    *   **RSI**: RSI >= 50 Bullish (+0.5), RSI >= 60 Momentum expansion (+0.5), 40 <= RSI < 50 Upside potential (+0.5)
    *   **CCI**: CCI > 0 Uptrend (+0.5), CCI > -100 Escaping oversold (+0.5)
    *   **DMI**: +DI > -DI Cross (+0.5)
    *   **Price Momentum (Absolute Momentum)**: 6-month (`MOMENTUM_LOOKBACK`) return positive AND 52-week position ≥ 80% (`MOMENTUM_W52_NEAR`) — bonus for leaders near their highs (+0.5).
        *   **Multi-horizon Alignment Gate**: The bonus is withheld if the 1-month or 3-month (`MOMENTUM_LOOKBACK_1M/3M`) return is negative — blocking top-of-trend entries into **"cooling trends"** whose 6-month figure looks good but whose recent momentum has rolled over.

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
*   **Wait (score ≥ `BUY_SCORE`)**: The score already meets the buy threshold, but the short-term **RSI is overheated** (at or above `BUY_RSI_MAX`, yet below the overheat-caution line), so entry is merely deferred. This is a *"too strong, wait for a pullback"* **buy-the-dip standby** — the opposite of "Caution" (which signals weakness/danger). It is treated as a sibling of "Rise" (same color, time-stop grace, and screening visibility). It automatically flips to "Buy" once RSI cools. (If RSI climbs further to the overheat-caution line, it is demoted to "Caution".)
*   **6.0 ~ 7.0 points (Rise)**: The trend is aligned and alive, but the score falls slightly short of the buy threshold. (score-accumulation standby, `RISE_SCORE` = 6.0)
*   **Interest / Nascent (regardless of score)**: The trend alignment is **not yet complete**, but **early trend-reversal signals are detected in a minimum count or more** (`INTEREST_SIGNAL_MIN`) with no clear risk signals. Intended for **manual swing (short-term) trading monitoring** to quickly recognize whether it may develop into an actual buy stage; it is not an automatic-buy target. (See 3-2 for the seven early signals and detailed conditions.)
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
*   **Interest Signal Minimum (`INTEREST_SIGNAL_MIN`)**: Default **3**. When trend alignment is incomplete but early trend-reversal signals — seven kinds: short-term golden cross, MACD improvement, +DI dominance, RSI crossing above 50, CCI improvement, supply inflow, MA60 proximity — are detected in this many counts or more with no risk signals (MACD dead cross, -DI dominance, RSI overheating/depletion, etc.), the stock is classified as **'Interest' (nascent)**. Detected even below the 120-day line, intended for manual swing-trade monitoring. (0 disables it.)
*   **Interest MA60 Proximity Ratio (`INTEREST_MA60_NEAR`)**: Default **0.97**. If the current price is at or above this ratio of the 60-day line (e.g., 97%), it counts as an 'MA60 breakout attempt' early signal even while still below the 60-day line.
*   **Maximum Buy Allowed RSI (`BUY_RSI_MAX`)**: Default **70**. Even if the buy score is met, we do not enter if the RSI is above this value, considering it already overheated.
*   **Buy Volume Strength (`BUY_VOL_STRENGTH`)**: Default **100.0%**. The volume strength at the time of purchase must be at least this value (buying pressure dominance).
*   **Mean Reversion (`USE_MEAN_REVERSION`)**: Catches the point where indicators rebound after reaching oversold in a downtrend or sudden drop. **(Disabled in defaults and all presets per the trend-following doctrine and hidden from the settings menus; re-enabling requires editing `json/dynamic_config.json` directly.)**
    *   `MR_RSI_MAX`: Maximum allowed RSI for mean reversion entry (Default 40.0)
    *   `MR_DISPARITY_MAX`: Disparity limit compared to 20-day MA (Default 90.0% or less)
    *   `MR_VOL_STRENGTH`: High volume strength to confirm buying pressure at the bottom (Default 120.0%)
*   **Super Momentum (`SUPER_MOMENTUM_USE`)**: Relaxes the buy/sell RSI thresholds for powerful leading stocks (new high rally) to follow the trend longer.
    *   `SUPER_MOMENTUM_SCORE`: Minimum trigger composite score (Default 8.0 points)
    *   `SUPER_MOMENTUM_W52_POS`: Minimum 52-week high position (Default 90.0% or higher)
    *   `SUPER_BUY_RSI_MAX`: Maximum allowed buy RSI relaxed upon trigger (Default 80.0)
*   **Pyramiding (`PYRAMIDING_USE`)**: Scales up only held positions validated by profit. (The opposite of averaging down — never fires on losing positions.)
    *   `PYRAMIDING_PROFIT_TRIGGER`: Minimum return to trigger scaling (Default +10.0%)
    *   `PYRAMIDING_RATIO`: Added quantity as a ratio of the held quantity (Default 0.5 = 50%)
    *   `PYRAMIDING_MAX_COUNT`: Maximum scale-ups per position (Default 1)

### 3. Sell Strategy (`SELL_STRATEGY`)
*   **Stop Loss**: Confirm loss when the loss rate reaches **-7.0%** (`STOP_LOSS_RATE`).
*   **ATR Stop Loss**: If `USE_ATR_STOP` is True (default), use ATR × `ATR_STOP_MULTIPLIER` (default 2.0) at the time of purchase as the stop loss rate instead of a fixed rate.
*   **Max ATR Stop Loss Rate**: `MAX_ATR_STOP_LOSS_RATE` is a safety mechanism to prevent the stop loss width from becoming abnormally large due to data errors or excessive volatility. (Default -15.0%)
*   **Break Even Stop**: When the highest return achieved reaches `BREAK_EVEN_PROFIT_RATE` (default 5.0%, dynamically linked when ATR is in use), raise the stop loss to `BREAK_EVEN_STOP_RATE` (default +0.5%) to defend profits.
*   **Time-based Stop**: If `TIME_STOP_USE` is True, sell when — after the set days (`TIME_STOP_DAYS`, default 20 days) — the return is below `TIME_STOP_MIN_PROFIT_RATE` (default **0.0%**, i.e., only positions still at a loss) and upward momentum has been lost. (Postponed if uptrend is maintained)
*   **Trailing Stop — Primary Exit (Chandelier Exit)**:
    *   **Trigger Condition**: Start monitoring upon reaching **+10.0%** (`TRAILING_STOP_ACTIVATION_RATE`) maximum return.
    *   **Sell Condition**: Effective callback = max(`TRAILING_ATR_MULTIPLIER` (default 3.0) × ATR ÷ peak, minimum `TRAILING_STOP_CALLBACK_RATE` (default 5.0%)). Volatile leaders get a proportionally wider callback to follow the trend longer. (The trailing ATR multiplier is separate from the stop-loss `ATR_STOP_MULTIPLIER`. `TS_MAX_GIVEBACK_RATIO` (default 0 = cap removed, pure Chandelier) can be set to a positive ratio to cap the giveback of the maximum gain.)
*   **Trend Broken Sell**: Sell if the composite score falls below **5 points** (`SELL_SCORE`) or the state is classified as 'Sell'.
*   **Grace Period Stop Loss (`MR_GRACE_LOSS_RATE`)**: The maximum allowable loss rate during the grace period for stocks entered via mean reversion. (Default -7.0%; irrelevant while mean reversion is disabled)
*   **Disabled-by-default options (upside limiters — hidden from menus; enable only via direct `dynamic_config.json` edits or per-stock custom rules)**:
    *   **Take Profit**: `TAKE_PROFIT_RATE` = **0 (unused)**. If set, full liquidation at that return.
    *   **Half Take-Profit**: `HALF_TAKE_PROFIT_USE` = **False**. If enabled, 50% is pre-sold at half the take-profit target.
    *   **Overheating Sell**: `TAKE_PROFIT_RSI` = **0 (unused)**. If set, preemptive sell when RSI exceeds it. (`SUPER_TAKE_PROFIT_RSI` applies under Super Momentum.)
    *   **Defensive Half Sell**: `DEFENSIVE_HALF_SELL_USE` = **False**. If enabled, sell 50% on a downward reversal signal (SAR Sell + 5MA breakdown).

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

*   **Market Index Filtering**: Automatically suspends new buys if the KOSPI/KOSDAQ index falls below a moving average (default 60 days, `MARKET_FILTER_MA`), treating it as a 'downtrend'. **The same rule (using your current settings) is also modeled in backtests**, so new entries on index-weak days are blocked in simulations as well (live/backtest parity).
*   **Relative Strength (RS) Filter**: Suspends new buys whose 6-month (126 trading days) return is at or below that of their home index (KOSPI/KOSDAQ) — an implementation of the trend-following principle that a stock unable to beat its own index is not a "clear trend". Reuses the shared index-data cache (no extra API load) and fails open on index-data failure or insufficient stock history so a data outage never halts buying entirely. (`USE_RS_FILTER`)
*   **Correlation Filtering**: Prevents new buys if the candidate stock shows high correlation (e.g., 0.7 or above) with currently held stocks to avoid concentration risk.
*   **Technical Filtering**: 
    *   **General Filter**: Suspends new buys if the trend breaks (MACD death cross, major MA breakdown).
    *   **Absolute Defense Filter**: Blocks new buys completely regardless of the score during "Super Panic" selling zones (ADX 45+ with strong sell pressure) to avoid fake rebounds.
*   **Daily Loss Limit**: Automatically pauses the system if the daily estimated asset drops beyond a set limit (e.g., -10%).
*   **Max Holding Stocks**: Limits the maximum number of stocks to hold in the portfolio (default 4 × 25% per stock — sized so that each slot clears the minimum tradable unit and keeps pyramiding functional; consider expanding to 5–6 slots for larger seeds).
*   **Kill Switch**: Transitions to standby mode to protect the account if continuous API or system errors occur (default 5 times).
*   **Order State Machine**: Strictly manages the order lifecycle to prevent duplicate buys or phantom positions.
*   **Risk-based Position Sizing**: Limits the maximum loss width (default 4%) per trade against the total account, automatically reducing buy weight for highly volatile stocks.
*   **Portfolio Heat Cap (Total Open-Risk Limit)**: Caps the combined potential loss of all holdings — measured from current price down to each position's effective stop (including break-even/trailing uplifts) — at a set percentage of the account (default 10%, `SYSTEM_MAX_PORTFOLIO_RISK`). While the per-trade risk limit controls each position individually, this cap controls the 'simultaneous stop-out' scenario in aggregate: new buys are shrunk or held to fit the remaining risk budget, and pyramiding add-ons are also held when over budget. As existing positions' stops rise to break-even/trailing levels, the budget naturally recovers and buying resumes.
*   **Volatility Targeting**: Normalizes each stock's annualized volatility toward a target (default 20%, `TARGET_VOLATILITY`) using ATR — shrinking size for high-volatility names and expanding for low-volatility ones. Crucially, **the expansion is clamped so it never exceeds the per-stock nominal cap (base weight)**, preventing over-concentration in a single name. (Sizing respects the risk limits even on the final slot.)
*   **Dynamic Risk Scaling**: Following the trend-following principle of "capping risk and volatility relative to capital," the risk limits for *new* entries (per-trade risk and portfolio heat cap) are automatically reduced based on market regime and account state. Only entry *size* is adjusted; exit logic (trailing stops, etc.) is never affected.
    *   **Bear-regime reduction**: If either KOSPI or KOSDAQ is in a bear regime (index < MA), risk limits are scaled by a factor (default ×0.75, `BEAR_RISK_SCALE`).
    *   **Drawdown step-down (Turtle-style)**: Risk limits are reduced in steps based on the account's drawdown from its high-water mark (HWM). (Default: ≥5% → ×0.75, ≥10% → ×0.5.) Betting shrinks automatically during losing streaks to smooth the max-drawdown curve, and restores on recovery. (Regime and drawdown factors combine multiplicatively.)
    *   **Gap-risk buffer**: Since the soft stop (periodic-check stop-loss) can fill below the stop price on a gap-down, risk-based sizing multiplies the stop distance by a buffer (default ×1.2, `GAP_RISK_BUFFER`) for conservative sizing.
    *   **Pyramiding market gate**: In a bear market where new buys are blocked (index < SMA), even a validated profitable position is held from adding on (no exposure expansion).

### 5. Scoring Weights Optimization
*   **Overview**: Allows users to manually configure or optimize the weights for each factor (Trend, Momentum, Strength, Synergy) in the buy score.
*   **Settings**: `TREND` (4.0), `MOMENTUM` (2.5), `STRENGTH` (1.5), `SYNERGY` (2.0).
*   **Optimization**: Use the 'Weight Optimization' feature in the backtesting menu to find the best combination based on historical data. The take-profit/stop-loss sweep in the single backtest run includes a **"no take profit (trailing-stop driven)" baseline** — fixed take-profit combinations are shown for reference against the trend-following doctrine.

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
    *   **NXT closing price retained after hours**: In real-trading mode, after the NXT sessions end (pre 08:00–09:00, after 15:30–20:00) — overnight, weekends, holidays — the live NXT quote is used if KIS provides one; otherwise the **last NXT close is remembered and shown until the next trading day's open** (persisted to disk, survives restart). This exposes the more recent NXT close (20:00) rather than the KRX regular-session close (15:30). (Simulation always shows the KRX close, since the KIS API does not support NXT there.)
*   **Real-time WebSocket Quotes & Execution Notices**:
    *   Korea Investment & Securities (real/simulation) **WebSocket push** is used to receive the current price/trade-strength of held and candidate stocks without REST polling (`realtime.py`). The read paths (`get_current_price`/trade-strength) prefer the WS cache and **automatically fall back to REST** when a symbol is unsubscribed, disconnected, or the feature is off — so behavior is always guaranteed. Freeing the TPS budget for order/balance calls **improves system-trading throughput**.
    *   **System-trading symbols subscribed first**: KIS limits a single connection to 41 registrations (symbol×TR) and one concurrent connection per approval_key. System-trading symbols (holdings → buy candidates) are **always subscribed**, and remaining slots rotate the other watchlist symbols. Whether domestic ETFs are included in system trading (`SYSTEM_INCLUDE_ETF`) is also honored in the subscription priority.
    *   **Real-time execution notices (H0STCNI0/H0STCNI9)**: Order fills are detected instantly over WebSocket (AES256-CBC decryption), immediately waking the proven conclusion-confirmation logic (`ConclusionMonitor`). The simulation account's fill-estimation delay (up to minutes when idle) is **cut to near-instant**. On missing notices, decryption failure, or unset HTS ID, it **fully falls back to the existing periodic polling**. (Requires the **HTS login ID** as the subscription key — see environment variables below.)
    *   **Runtime toggle**: The on/off setting (`USE_WEBSOCKET`) can be changed under **Main Menu `[0] System Settings > 5-1`** and applies **without restarting** (off → REST fallback, on → auto-reconnect). WebSocket connection/subscription/execution-notice status is logged at **INFO level to the file log** for monitoring. (Toss Securities has no official WS support, so REST polling is retained.)
*   **Memory Protection (Raspberry Pi OOM guard)**: 
    *   For long-running operation on constrained devices (e.g., Raspberry Pi 1GB), the quote micro-cache and chart cache enforce a **maximum item count** and evict the oldest entries when exceeded, so memory does not grow unbounded even during full-market scans.
*   **Interrupt-safe Exceptions**: 
    *   Bare `except:` clauses were normalized to `except Exception:` so that `KeyboardInterrupt`/`SystemExit` propagate correctly (preserving Ctrl+C responsiveness and clean shutdown).
*   **API Call Efficiency & Speed (TPS Optimization)**:
    *   KIS/Toss OpenAPIs enforce a per-second transaction (TPS) limit, and every quote/order call passes serially through a single global TPS gate. Analysis time is therefore "total calls ÷ TPS", so the following improvements target both factors.
    *   **Adaptive Dynamic TPS (AIMD)**: Instead of a fixed margin (effective 18 TPS), the effective TPS starts from a margin and is additively raised as successes accumulate, then multiplicatively backed off the moment `EGW00201` (rate exceeded) occurs — **self-converging to the optimal TPS** for current server/network conditions.
    *   **NXT Time-window Gating**: During the regular session (09:00–15:30) KRX is the representative price, so the auxiliary NXT (alternative exchange) quote call is skipped and only fetched during NXT-only sessions (pre/after market), **roughly halving per-stock calls during regular hours**.
    *   **Persistent Daily-chart Cache**: Daily candles change once per day, so they are persisted to disk (SQLite) per trading day. After a restart they are restored instantly **without re-fetching over the network for the same trading day** (eliminating startup bursts); stale-date entries are auto-pruned.
    *   **Skip Unneeded Order-book Calls**: The order book (ask/bid ratio) is fetched only for stocks whose buy supply/demand gate is active, removing order-book calls for stocks with the gate disabled.
    *   **TPS-aligned Worker Pools**: Per-stock parallel analysis worker counts are aligned to TPS, preventing excess threads that merely wait at the gate (wasting memory).

## 5. Project Structure

```text
my-stock-hts/
├── run.sh                # [Mac/Linux] Execution script
├── run.bat               # [Windows] Execution script
├── main.py               # Main execution file (Menu & Routing)
├── config.py             # Settings, Env vars, Data load
├── api.py                # KIS API communication, yfinance integration, quote/chart data
├── toss_api.py           # Toss Securities Open API client (quotes/assets/orders in Toss mode)
├── realtime.py           # KIS WebSocket real-time quote & execution-notice feed (REST fallback when uncovered)
├── constants.py          # Constant definitions (TR ID, field mapping, etc.)
├── indicators.py         # Technical indicators calculation (RSI, ADX, MACD, etc.)
├── utils.py              # Common utilities (dates, formatting, etc.)
├── jsonio.py             # Shared JSON file load/save helper (lowest-level utility)
├── caching.py            # Shared in-memory TTL cache (size cap & auto-eviction)
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
│   ├── token_cache.json        # [Auto-generated] API access token cache
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
├── tests/                # Pytest unit/integration test codes (870+)
└── modules/              # Feature-specific module folders
    ├── db_manager.py     # DB connection & query management
    ├── db_queue.py       # Single worker queue proxy for SQLite concurrency control
    ├── telegram_bot.py   # Telegram bot inbound command handling
    ├── telegram_notify.py# Telegram outbound layer (message/photo sending)
    ├── dart_api.py       # OpenDART (disclosure) API integration (dividends/earnings/disclosures)
    ├── scheduler.py      # Dedicated worker for background scheduling & timers
    ├── market_halt.py    # Circuit breaker (CB) / VI market-halt detection & Telegram alerts
    ├── executors.py      # Central management of system-wide Thread Pool
    ├── prompts.py        # External management of prompt templates for AI assistant
    ├── settings.py       # [0] System Settings management
    ├── market.py         # [1] Market Indices inquiry
    ├── analysis.py       # [2] Stock price & technical analysis
    ├── chart.py          # [3] Chart visualization & analysis
    ├── backtest.py       # [4] Strategy Backtesting
    ├── auto_trade/       # [5] System Trading (Auto Trading) package
    │   ├── common.py     #   ├ Shared helpers (restricted stocks/daily asset/market hours/OrderStatus)
    │   ├── engine.py     #   ├ Trading engine (DefaultStrategy·OrderManager·RiskManager)
    │   ├── conclusion.py #   ├ Fill monitoring/confirmation (ConclusionMonitor)
    │   ├── trader.py     #   ├ AutoTrader main loop (analyze→buy/sell→report)
    │   └── menu.py       #   └ Trading rules/restricted stocks menu UI
    ├── theme_analysis.py # [6] Discovery & Financials + AI (Gemini) analysis/disclosure summary
    ├── manage/           # [7] Watchlist Management + [6-5~8] fundamentals package
    │   ├── watchlist.py  #   ├ Watchlist add/delete/view & menu UI
    │   ├── events.py     #   ├ Dividend/Earnings Calendar (DART + yfinance)
    │   └── disclosure.py #   └ Disclosure monitoring/earnings tracking + Telegram alerts (DART)
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
5.  **(Optional) HTS login ID**: To use real-time **execution-notice WebSocket**, your KIS HTS login ID is required as the subscription key. (If unset, fill detection works via the existing REST polling.)

### Toss Securities
1.  **Open Account**: Via the Toss app.
2.  **Toss Developer Center**: Apply on the Toss Securities Open API website.
3.  **Issue API Key**: Issue App Key & Secret from the Developer Center.

### Toss Mode (mode 3) Data Basis (Caution)

Toss and KIS provide the "closing price" of domestic (KR) stocks on different bases. **A different change amount/rate in Toss mode compared to KIS mode is not a bug but a data-source difference.**

| Item | KIS (mode 1/2) | Toss (mode 3) |
| --- | --- | --- |
| Current/last price | KRX regular-session close (mode 1) or NXT close (mode 2, merged) | Unified last trade price including NXT extended hours (~20:00) |
| Daily candle close | KRX regular-session (15:30) close | Unified last price including NXT extended hours |
| Base for change amount/rate (previous close) | Reference price (`stck_sdpr`) = previous KRX regular-session close | Previous daily candle close = previous unified (NXT-inclusive) last price |
| Price limit (upper/lower) base | KRX reference price | Unified (NXT-inclusive) last price |

- Example (Samsung Electronics, as of 2026-07-10): Thursday's close differs — KRX 278,000 vs Toss (NXT-inclusive) 282,500 — so even though both modes show the same Friday last price (286,500), the change displays as +8,500 (+3.06%) in KIS vs +4,000 (+1.42%) in Toss.
- The Toss API does not expose the KRX regular-session close at all, so change rates in Toss mode are computed **self-consistently on the unified-price basis** (the same family of figures the Toss app shows). Chart-derived indicators (52-week high/low, moving averages, etc.) are also unified-price based and may differ slightly from KIS mode.
- The program notes this difference at Toss-mode startup.

### Common
5.  **Environment Variables**: Register the issued Keys and Account Numbers as System Environment Variables.
6.  **IP Allowlist (Whitelist)**:
    - **Toss Securities (required)**: The Toss Open API **requires registering allowed IPs**. Running from an unregistered network (mobile tethering, VPN, a line whose IP changed, etc.) causes token issuance to be rejected with `IP address not allowed`. Register the **public IP of the server/PC that runs the program** in the developer console (App Settings → Allowed IP).
    - **KIS (optional)**: KIS does not require IP registration by default. It only restricts access if you have set a "Customer IP" restriction on the API key. (Connection-refused / timeout errors are usually **KIS server maintenance/outage or a network issue** — check the server status and your network first.)
    - For always-on operation (e.g. a Raspberry Pi), a **static IP** line is recommended. If the public IP changes, you must re-register it.
    - If token issuance fails, the program **prints your current public IP and cause-specific guidance during the pre-flight check** — follow it.

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
*   `REAL_HTS_ID`, `SIM_HTS_ID`: Subscription key (KIS HTS login ID) for real-time **execution-notice WebSocket** (Optional). Set per real/mock; if identical, a single `KIS_HTS_ID` (or `HTS_ID`) suffices. **If unset, fill detection falls back to REST polling.**
*   `TOSS_APP_KEY`, `TOSS_APP_SECRET`, `TOSS_ACC_NUM`: Toss Securities
*   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram Bot (Optional)
*   `GEMINI_API_KEY`: Google Gemini API Key for AI features (Optional)
*   `GEMINI_MODEL`: Gemini model name to use (Optional, default: `gemini-flash-latest`)
*   `GEMINI_FALLBACK_MODEL`: Lighter fallback model automatically retried when the free-tier quota is exceeded (429) (Optional, default: `gemini-flash-lite-latest`)
*   `DART_API_KEY`: OpenDART API Key for disclosures (Optional)

**Example (`export` in a shell profile such as `~/.htsrc`):**
```sh
# Korea Investment & Securities (real/mock)
export REAL_APP_KEY="..."  ; export REAL_APP_SECRET="..."  ; export REAL_ACC_NUM="12345678-01"
export SIM_APP_KEY="..."   ; export SIM_APP_SECRET="..."   ; export SIM_ACC_NUM="50012345-01"

# (Optional) real-time execution-notice WebSocket key = KIS HTS login ID
export REAL_HTS_ID="myhtsid"   # real
export SIM_HTS_ID="myhtsid"    # mock (a single KIS_HTS_ID covers both if identical)
```
> After adding/changing env vars, reload the shell (`source ~/.htsrc`) and **restart the program** for them to take effect.

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
*   **System Control**: `/start`, `/stop`, `/restart`, `/status`, `/config` (strategy settings), `/preset <phase>` (market preset, b/r/s/d)
*   **Account & Assets**: `/balance`, `/holdings`, `/pending` (unfilled orders), `/reserves` (reserved orders), `/profit [period]` (realized P&L, d/w/m/n), `/history [period]` (trade history), `/report [period]` (performance report), `/stats [stock]` (per-stock performance)
*   **Market & Stock Analysis**: `/market [group]` (indices, k/u/s/r/g/c/b), `/signal <stock/index>` (technical diagnosis), `/analyze <stock/index>` (AI in-depth diagnosis), `/chart [period] <stock/index>` (chart, d/h/m), `/briefing` (on-demand AI market briefing), `/closing` (AI closing briefing), `/curate` (AI leading-stock curation), `/scan [market]` (TradingView scan, k/u), `/news <stock>` (AI latest news), `/ask <question>` (free-form AI Q&A)
*   **Management & Misc**: `/stocks` (watchlist), `/rules [stock]` (per-stock trading rules), `/restrict` (restricted stocks), `/addrestrict <stock> [reason]`, `/delrestrict <stock>`, `/memo [a/d/stock]`, `/log` (recent logs), `/help`

> Commands may be added or changed over time. **Type `/help` in the bot for the latest full list.**

## 9. Disclosure Integration (OpenDART)

By integrating the Financial Supervisory Service's **DART OpenAPI**, you can use features like disclosure monitoring, dividend/earnings calendars, supply-demand & overhang signals, financial snapshots, and real-time Telegram alerts for major disclosures. (Under Menu `[6] Discovery & Financials`)
> The DART API Key is **optional**.

1.  **Issue API Key (Free)**: Apply on the OpenDART website.
2.  **Environment Variable**: Register `DART_API_KEY`.
3.  **Features** (Menu `[6]` Discovery & Financials):
    *   `[5] Dividend/Earnings Calendar`: Estimates the next ex-dividend date per dividend cycle, and when a cash/in-kind dividend decision disclosure exists, parses the document to replace the estimate with the **confirmed record date and dividend per share**. Also shows estimated Korean earnings announcement dates (based on last year's provisional-earnings filing pattern) and the next statutory report deadline.
    *   `[6] Disclosure Monitoring`: Classifies recent disclosures by importance with Gemini AI good/bad-news summaries. Auto-extracts details: provisional earnings (revenue/OP/NP with YoY), paid-in capital increases (dilution), CB/BW terms, **supply contracts (amount, % of revenue, counterparty)**, **treasury stock decisions (amount, period)**, **bonus issues (allotment ratio)**, and **capital reductions (ratio)**.
    *   `[7] Supply-Demand & Overhang Signals`: (1) **Treasury stock acquisition/disposal/trust decisions** (company-level supply signal), (2) **mezzanine (CB/BW/EB) overhang watch** — conversion price vs. current price, potential conversion volume vs. shares outstanding, recent conversion-exercise filings, (3) **bonus issue decisions**, (4) insider (elestock) and 5% holder (majorstock) net buy/sell summary.
    *   `[8] Financial Snapshot`: Revenue/operating profit/net profit with YoY from the latest periodic report, **standalone quarterly operating profit** (cumulative-difference method), and DART-computed **ROE / debt ratio**.
    *   **Telegram Alerts**: Sends instant pushes for major disclosures (capital increase, administrative issues, etc.).

## 10. AI-Powered Assistant

*   **Overview**: Combines Google Gemini LLM's real-time web search capabilities with precise technical analysis to provide deep investment insights.
*   **Key Services**:
    1.  **AI In-depth Stock Diagnostics (`/analyze`)**: Writes future stock price prospect reports based on quant scores and news.
    2.  **Market-Leading Theme Analysis**: Analyzes top 5 themes and leading stocks.
    3.  **AI Pre-market Briefing**: Daily morning briefings summarizing global markets and hot issues.
    4.  **Interactive Q&A (`/ask`)**: Ask free-form questions about stocks/economy based on the latest news.
    5.  **AI Backtesting Diagnostics**: Proposes optimal parameters (entry hurdles, stop width, trailing multiplier, weights — within the trend-following framework) evaluating backtest results. (All trading-advisory prompts state the trend-following doctrine so counter-trend advice such as fixed take profits is not generated.)
    6.  **AI Trading Autopsy**: Analyzes your trading results after selling and advises on future strategies.
    7.  **AI Closing Briefing (`/closing`)**: Daily market review and analysis of held stocks.
    8.  **AI Curation (`/curate`)**: Discovers leading themes and recommends stocks based on real-time macro indicators.
    9.  **AI Chart Image Reading (Gemini Vision)**: The chart image generated by Chart Analysis (menu `[3]`, weekly/daily supported) is read directly by the Gemini vision model, providing an in-depth diagnosis of candle patterns, trendlines, and volume profiles — just as a human would read the chart.

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
