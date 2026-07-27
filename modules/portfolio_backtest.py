"""다종목 포트폴리오 백테스트 (슬롯 경쟁·현금 제약·히트 캡 재현).

기존 ``backtest.simulate_strategy``는 '한 종목 × 계좌 전액'을 가정해 종목별 전략 검증에는
맞지만, 실제 운용에서 성과를 좌우하는 세 가지를 재현하지 못한다.

  1) 슬롯 경쟁(기회비용) — 동시에 N종목만 보유하므로 좋은 신호가 나도 자리가 없으면 못 산다.
  2) 현금 제약 — 피라미딩에 쓴 현금은 다른 종목 신규 진입에 못 쓴다.
  3) 포트폴리오 히트 캡 — 보유 전체의 오픈 리스크 합이 한도를 넘으면 신규 매수가 막힌다.

이 모듈은 하나의 계좌로 N슬롯을 굴리며 위 셋을 모두 반영한다. 진입·청산 판정은
``backtest.calculate_daily_status``(= ``analysis.classify_stock_state``/``calculate_score``)를
그대로 쓰고, 청산 체인(ATR 손절 → BEP → 시간청산 → 샹들리에 TS → 점수매도)과 사이징 3층
결합은 ``simulate_strategy``·``engine.RiskManager.allocate_budget``과 동일 순서·조건으로 맞췄다.

정합성 검증: 슬롯 1·기초비중 25%·히트캡 OFF로 돌려 종목별로 ``simulate_strategy``와 비교하면
관심종목 50개 기준 수익률 상관 0.9988(평균차 -0.12%p), MDD 평균 동일, 청산 805건 vs 803건이다.
"""
import math

import pandas as pd
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

import config
import context
import utils
from modules import backtest


# ==========================================================
# 시뮬레이션 코어
# ==========================================================
def precompute_status(dfs, thresholds):
    """종목별·일자별 (raw_score, sell_check_score, can_buy_state, state, reason)을 미리 계산한다.

    상태 판정은 하루 단위로 결정되므로 조합(슬롯 수·피라미딩 차수)을 바꿔 반복 실행할 때
    매번 다시 계산할 필요가 없다. 50종목 3년 기준 1초 남짓이며 이후 실행은 순수 루프가 된다.
    """
    out = {}
    for code, df in dfs.items():
        status, prev = {}, None
        for row in df.to_dict("records"):
            try:
                status[str(row["date"])] = backtest.calculate_daily_status(row, prev, thresholds=thresholds)
            except Exception:
                status[str(row["date"])] = (0.0, 0.0, False, "-", "데이터 부족")
            prev = row
        out[code] = status
    return out


def _atr_stop_rate(atr, price, atr_mult):
    """진입 시점 ATR 손절률(%). simulate_strategy와 동일하게 MAX_ATR_STOP_LOSS_RATE로 캡한다."""
    if not (atr and atr > 0 and price > 0):
        return 0.0
    rate = -((atr * atr_mult / price) * 100)
    cap = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
    if cap != 0 and rate < cap:
        rate = cap
    return rate


def _weighted_sl(position, default_sl):
    """보유 lot들의 수량가중 평균 ATR 손절률. 실매매의 매수기록 가중평균과 같은 규칙."""
    total_qty = weighted = 0.0
    for lot in position["lots"]:
        if lot["qty"] > 0 and lot["sl"] != 0.0:
            total_qty += lot["qty"]
            weighted += lot["qty"] * lot["sl"]
    return (weighted / total_qty, True) if total_qty > 0 else (default_sl, False)


def allocate_amount(equity, cash, invest_ratio, sl_rate, atr, price):
    """engine.RiskManager.allocate_budget과 동일한 3층 min 결합(기초비중·리스크·변동성)."""
    base_amt = int(equity * invest_ratio)
    amount = base_amt

    risk_per_trade = getattr(config, "SYSTEM_RISK_PER_TRADE", 4.0)
    if risk_per_trade > 0 and sl_rate and abs(sl_rate) > 0:
        params = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
        try:
            gap_buffer = max(1.0, float(params.get("GAP_RISK_BUFFER", 1.2)))
        except (TypeError, ValueError):
            gap_buffer = 1.2
        max_loss = equity * (risk_per_trade / 100.0)
        amount = min(amount, int(max_loss / ((abs(sl_rate) / 100.0) * gap_buffer)))

    if getattr(config, "USE_VOLATILITY_TARGETING", True) and atr and price > 0:
        annual_vol = (atr / price) * math.sqrt(252)
        if annual_vol > 0:
            scale = getattr(config, "TARGET_VOLATILITY", 0.25) / annual_vol
            scale = max(getattr(config, "VOLATILITY_SCALING_MIN", 0.4),
                        min(getattr(config, "VOLATILITY_SCALING_MAX", 2.0), scale))
            amount = min(amount, min(int(base_amt * scale), base_amt))

    return max(0, min(amount, cash))


def run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=4,
                  pyramiding_max=None, heat_cap_pct=None, invest_ratio=None,
                  atr_mult=None, market_filter_dates=None, reserved_cash=0.0):
    """N슬롯 포트폴리오 시뮬레이션.

    Args:
        dfs: {code: DataFrame} 지표 계산이 끝난 일봉(날짜 오름차순).
        status: precompute_status 결과.
        dates: 시뮬레이션 대상 거래일(오름차순 'YYYYMMDD' 문자열).
        slots: 동시 보유 종목 수. invest_ratio 미지정 시 기초 비중은 1/slots.
        market_filter_dates: {code: set(차단일)} 신규 진입 차단일. 실매매와 동일하게 매도·피라미딩엔 무관.
        reserved_cash: 운용자가 수동 운용 등으로 묶어둔 금액. 같은 계좌에 있으므로
            사이징 기준 자산(_equity)에는 포함되지만 시스템이 집행할 수는 없다.

    하루 처리 순서는 실매매와 같다: 매도 → 피라미딩 → 신규 매수(점수 높은 순).
    """
    sell_cfg, thr = config.SELL_STRATEGY, config.ANALYSIS_THRESHOLDS
    invest_ratio = invest_ratio if invest_ratio is not None else (1.0 / max(1, slots))
    atr_mult = atr_mult if atr_mult is not None else sell_cfg.get("ATR_STOP_MULTIPLIER", 2.0)
    if heat_cap_pct is None:
        heat_cap_pct = getattr(config, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0)

    pyr_max = pyramiding_max if pyramiding_max is not None else thr.get("PYRAMIDING_MAX_COUNT", 1)
    pyr_use = thr.get("PYRAMIDING_USE", True) and pyr_max > 0
    pyr_trigger = thr.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)
    pyr_ratio = thr.get("PYRAMIDING_RATIO", 0.5)

    use_atr = sell_cfg.get("USE_ATR_STOP", True)
    default_sl = sell_cfg["STOP_LOSS_RATE"]
    sell_score_limit = sell_cfg["SELL_SCORE"]
    ts_act = sell_cfg.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_callback = sell_cfg.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    ts_atr_mult = sell_cfg.get("TRAILING_ATR_MULTIPLIER", 3.0)
    time_stop_days = sell_cfg.get("TIME_STOP_DAYS", 20)
    use_time_stop = sell_cfg.get("TIME_STOP_USE", True) and time_stop_days > 0
    bep_stop = sell_cfg.get("BREAK_EVEN_STOP_RATE", 0.5)
    bep_default = sell_cfg.get("BREAK_EVEN_PROFIT_RATE", 5.0)

    buy_score = thr["BUY_SCORE"]
    buy_rsi = thr["BUY_RSI_MAX"]
    super_use = thr.get("SUPER_MOMENTUM_USE", True)
    super_score = thr.get("SUPER_MOMENTUM_SCORE", 8.0)
    super_w52 = thr.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_rsi = thr.get("SUPER_BUY_RSI_MAX", 80.0)
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)

    rows = {code: {str(r["date"]): r for r in df.to_dict("records")} for code, df in dfs.items()}
    parsed = {d: pd.to_datetime(d, format="%Y%m%d") for d in dates}

    reserved_cash = float(reserved_cash or 0.0)
    cash = float(initial_capital) - reserved_cash
    positions, trades, equity_curve, cash_ratios = {}, [], [], []
    peak, mdd, slot_usage = initial_capital, 0.0, 0
    # [소액 시드 진단] 배분액이 1주 값에 못 미쳐 버려진 기회. 시드가 작을수록 급증한다.
    skipped_qty0, pyramid_blocked_qty0 = 0, 0

    def _equity(day):
        return cash + reserved_cash + sum(
            p["qty"] * rows[c][day]["close"] for c, p in positions.items() if day in rows[c])

    def _effective_sl(position):
        """현재 유효 손절률(BEP 상향 반영)."""
        sl, applied = _weighted_sl(position, default_sl)
        max_profit = (position["high"] - position["avg"]) / position["avg"] * 100
        activation = abs(sl) if (applied and sl < 0) else bep_default
        if max_profit >= activation and sl < bep_stop:
            return bep_stop, applied, True
        return sl, applied, False

    for day in dates:
        equity_curve.append(_equity(day))
        if equity_curve[-1] > 0:
            cash_ratios.append(cash / equity_curve[-1] * 100)
        peak = max(peak, equity_curve[-1])
        if peak > 0:
            mdd = min(mdd, (equity_curve[-1] - peak) / peak * 100)

        # ---------- 1) 매도 ----------
        for code in list(positions.keys()):
            row = rows[code].get(day)
            if row is None:
                continue
            price = row["close"]
            if price is None or (isinstance(price, float) and math.isnan(price)) or price <= 0:
                continue

            pos = positions[code]
            raw_score, sell_check, _can_buy, state, state_reason = status[code][day]
            pos["high"] = max(pos["high"], row["high"])
            loss_rate = (price - pos["avg"]) / pos["avg"] * 100
            max_profit = (pos["high"] - pos["avg"]) / pos["avg"] * 100
            sl_rate, atr_applied, is_bep = _effective_sl(pos)
            holding_days = (parsed[day] - pos["buy_dt"]).days

            sell, reason = False, ""
            if sl_rate != 0 and loss_rate <= sl_rate:
                sell = True
                reason = "본전청산" if is_bep else ("ATR손절" if (use_atr and atr_applied) else "손절")
            elif use_time_stop and holding_days >= time_stop_days and loss_rate < 0:
                # 시간청산 유예: 매수 계열 상태 유지 + 상방 모멘텀(최근 5일 고점 ≥ 10일 고점)
                grace = state in ("매수", "강매수", "역매수", "상승", "대기") and \
                    row.get("roll_high_5", 0) >= row.get("roll_high_10", 0)
                if not grace:
                    sell, reason = True, "시간청산"
            elif pos["high"] > 0 and max_profit >= ts_act:
                drop = (pos["high"] - price) / pos["high"] * 100
                callback = ts_callback
                atr_val = row.get("ATR", 0)
                if use_atr and atr_val and atr_val > 0:
                    callback = max(ts_callback, (atr_val * ts_atr_mult / pos["high"]) * 100)
                if drop >= callback:
                    sell, reason = True, "트레일링스탑"

            if not sell:
                # 점수 매도는 추세 구조 훼손(주가<60일선 또는 '매도' 상태) 동시 충족 시에만
                ema60 = row.get("EMA60")
                structure_broken = (state == "매도") or ema60 is None or price < ema60
                if sell_check < sell_score_limit and structure_broken:
                    sell = True
                    reason = state_reason if (sell_check == 0 and raw_score > 0) else "점수하락"

            if sell:
                sell_price = utils.adjust_to_tick(price * (1 - slippage), False) or price
                amount = pos["qty"] * sell_price
                amount -= int(amount * 0.0023)
                profit = amount - pos["qty"] * pos["avg"]
                cash += amount
                trades.append({
                    "code": code, "date": day, "reason": reason, "profit_amt": profit,
                    "profit": profit / (pos["qty"] * pos["avg"]) * 100, "days": holding_days,
                })
                del positions[code]

        # ---------- 히트(총 오픈 리스크) 예산 ----------
        heat_budget = None
        if heat_cap_pct and heat_cap_pct > 0:
            heat = 0.0
            for code, pos in positions.items():
                row = rows[code].get(day)
                if row is None:
                    continue
                sl_rate, _applied, _bep = _effective_sl(pos)
                if sl_rate < 0:
                    heat += pos["qty"] * row["close"] * (abs(sl_rate) / 100.0)
            heat_budget = _equity(day) * heat_cap_pct / 100.0 - heat

        # ---------- 2) 피라미딩 (수익 포지션 증액) ----------
        if pyr_use:
            for code, pos in list(positions.items()):
                row = rows[code].get(day)
                if row is None or pos["pyr"] >= pyr_max:
                    continue
                _raw, _chk, _can, state, _reason = status[code][day]
                price = row["close"]
                if (price - pos["avg"]) / pos["avg"] * 100 < pyr_trigger or state not in ("매수", "강매수"):
                    continue

                add_qty = int(pos["qty"] * pyr_ratio)
                if add_qty < 1:
                    # 보유 수량이 적으면(1주 등) 증액 비율 0.5로는 1주도 안 나온다 = 피라미딩 불발
                    pyramid_blocked_qty0 += 1
                    continue
                add_price = utils.adjust_to_tick(price * (1 + slippage), False) or price
                add_qty = min(add_qty, int(cash / add_price))
                add_sl = _atr_stop_rate(row.get("ATR", 0), add_price, atr_mult) if use_atr else default_sl
                if heat_budget is not None and add_sl:
                    affordable = heat_budget / (add_price * (abs(add_sl) / 100.0))
                    add_qty = min(add_qty, int(max(0, affordable)))
                if add_qty < 1:
                    continue

                cost = add_qty * add_price
                cash -= cost
                if heat_budget is not None:
                    heat_budget -= cost * (abs(add_sl) / 100.0)
                pos["avg"] = (pos["qty"] * pos["avg"] + cost) / (pos["qty"] + add_qty)
                pos["qty"] += add_qty
                pos["lots"].append({"qty": add_qty, "sl": add_sl})
                pos["pyr"] += 1
                pos["buy_dt"] = parsed[day]  # 실매매와 동일하게 시간청산 기준일 갱신
                trades.append({"code": code, "date": day, "reason": f"피라미딩{pos['pyr']}차",
                               "profit_amt": 0, "profit": 0, "days": 0})

        # ---------- 3) 신규 매수 (점수 높은 순) ----------
        slot_usage += len(positions)
        if len(positions) < slots:
            candidates = []
            for code, stock_rows in rows.items():
                if code in positions:
                    continue
                row = stock_rows.get(day)
                if row is None:
                    continue
                raw_score, _chk, can_buy, state, _reason = status[code][day]
                if not can_buy or not (raw_score >= buy_score or state == "역매수"):
                    continue
                is_super = super_use and raw_score >= super_score and row.get("w52_pos", 0) >= super_w52
                if row["RSI"] >= (super_rsi if is_super else buy_rsi):
                    continue
                if market_filter_dates and day in market_filter_dates.get(code, ()):
                    continue
                candidates.append((raw_score, code, row))
            candidates.sort(reverse=True, key=lambda item: item[0])

            for _score, code, row in candidates:
                if len(positions) >= slots:
                    break
                buy_price = utils.adjust_to_tick(row["close"] * (1 + slippage), False) or row["close"]
                sl_rate = _atr_stop_rate(row.get("ATR", 0), buy_price, atr_mult) if use_atr else default_sl
                amount = allocate_amount(_equity(day), cash, invest_ratio, sl_rate,
                                         row.get("ATR", 0), buy_price)
                if heat_budget is not None and sl_rate:
                    amount = min(amount, max(0, heat_budget / (abs(sl_rate) / 100.0)))
                qty = int(amount / buy_price)
                if qty < 1:
                    # 배분액 < 1주 값 → 매수 불가. 고가주는 소액 시드에서 아예 살 수 없다.
                    skipped_qty0 += 1
                    continue

                cash -= qty * buy_price
                if heat_budget is not None:
                    heat_budget -= qty * buy_price * (abs(sl_rate) / 100.0)
                positions[code] = {"qty": qty, "avg": buy_price, "lots": [{"qty": qty, "sl": sl_rate}],
                                   "high": row["high"], "buy_dt": parsed[day], "pyr": 0}
                trades.append({"code": code, "date": day, "reason": "매수",
                               "profit_amt": 0, "profit": 0, "days": 0})

    final_asset = _equity(dates[-1]) if dates else initial_capital
    sells = [t for t in trades if t["reason"] in
             ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락") or t["profit_amt"] != 0]
    gross_profit = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    gross_loss = abs(sum(t["profit_amt"] for t in sells if t["profit_amt"] < 0))
    return {
        "final_asset": final_asset,
        "total_return": (final_asset - initial_capital) / initial_capital * 100,
        "mdd": mdd,
        "pf": (gross_profit / gross_loss) if gross_loss else float("inf"),
        "win": sum(1 for t in sells if t["profit_amt"] > 0),
        "loss": sum(1 for t in sells if t["profit_amt"] <= 0),
        "trades": trades,
        "sells": sells,
        "pyramid_count": sum(1 for t in trades if "피라미딩" in t["reason"]),
        "avg_slots": slot_usage / len(dates) if dates else 0.0,
        "avg_cash_ratio": (sum(cash_ratios) / len(cash_ratios)) if cash_ratios else 0.0,
        "skipped_qty0": skipped_qty0,                    # 1주도 못 사서 넘긴 진입 기회
        "pyramid_blocked_qty0": pyramid_blocked_qty0,    # 보유 수량이 적어 불발된 증액 기회
        "equity": equity_curve,
    }


# ==========================================================
# 데이터 준비
# ==========================================================
def prepare_universe(targets, days, progress_cb=None):
    """대상 종목의 일봉·지표·시장필터 차단일을 준비한다.

    Returns: (dfs, market_filter_dates, dates, failed)
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    dfs, mf_dates, failed = {}, {}, []
    for code, name in targets:
        try:
            df = backtest.get_backtest_data(code, False, days)
            if df is None or df.empty:
                failed.append(name)
                continue
            df = backtest._append_smart_money_signal(df, code, False)
            df = backtest.compute_price_indicators(df)
            df["roll_high_5"] = df["high"].rolling(5, min_periods=1).max()
            df["roll_high_10"] = df["high"].rolling(10, min_periods=1).max()

            mask = df["date"].astype(str) >= cutoff
            start_idx = mask.idxmax() if mask.any() else 0
            if len(df) - start_idx < 100:
                failed.append(name)
                continue
            dfs[code] = df.iloc[start_idx:].reset_index(drop=True)

            backtest.prepare_market_filter(code, False, days)
            mf_dates[code] = set(backtest._MARKET_FILTER_STATE.get("dates") or set())
        except Exception:
            failed.append(name)
        if progress_cb:
            progress_cb(name)

    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    return dfs, mf_dates, dates, failed


# ==========================================================
# CLI
# ==========================================================
def run_portfolio_backtest():
    """관심종목 전체를 N슬롯 포트폴리오로 굴리는 백테스트 (메뉴 진입점)."""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        utils.clear_screen()
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"),
            ("2", "국내 ETF", "Domestic ETF"),
            ("3", "국내 전체 (주식+ETF)", "All Domestic"),
        ]
        choice = utils.show_menu("포트폴리오 백테스팅 (Portfolio Backtest)", menu_items, default_choice="3")
        if choice.lower() in ["b", "q"]:
            return False
        if choice not in ("1", "2", "3"):
            continue

        keys = {"1": ["stocks_kr"], "2": ["etfs_kr"], "3": ["stocks_kr", "etfs_kr"]}[choice]
        targets = []
        for key in keys:
            for item in config.session.stock_data.get(key, []):
                targets.append((item["code"], item["name"]))
        if not targets:
            config.console.print("[yellow]대상 종목이 없습니다. 관심종목을 먼저 등록하세요.[/yellow]")
            utils.pause()
            continue

        max_holdings = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
        pyr_default = config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_MAX_COUNT", 1)
        config.console.print(f"\n[dim]대상 {len(targets)}종목 · 현재 설정: 슬롯 {max_holdings} · 피라미딩 {pyr_default}차[/dim]")

        val = Prompt.ask("분석 기간(일)", default="1095")
        if val.lower() in ["b", "q"]:
            continue
        days = max(200, int(val))
        val = Prompt.ask("동시 보유 슬롯 수 [dim](쉼표로 여러 개 비교 가능: 4,6)[/dim]", default=str(max_holdings))
        if val.lower() in ["b", "q"]:
            continue
        slot_list = [int(s) for s in val.replace(" ", "").split(",") if s.isdigit()] or [max_holdings]
        val = Prompt.ask("피라미딩 차수 [dim](쉼표로 여러 개 비교 가능: 1,2)[/dim]", default=str(pyr_default))
        if val.lower() in ["b", "q"]:
            continue
        pyr_list = [int(s) for s in val.replace(" ", "").split(",") if s.isdigit()] or [pyr_default]
        val = Prompt.ask("초기 자본(원)", default="10000000")
        if val.lower() in ["b", "q"]:
            continue
        initial = max(1_000_000, int(val))

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), console=config.console, transient=True) as progress:
            task = progress.add_task(f"[cyan]데이터 준비 중 (0/{len(targets)})[/cyan]", total=len(targets))
            done = {"n": 0}

            def _tick(_name):
                done["n"] += 1
                progress.update(task, advance=1,
                                description=f"[cyan]데이터 준비 중 ({done['n']}/{len(targets)})[/cyan]")

            dfs, mf_dates, dates, failed = prepare_universe(targets, days, progress_cb=_tick)

        if not dfs or not dates:
            config.console.print("[red]사용 가능한 데이터가 없습니다.[/red]")
            utils.pause()
            continue

        thresholds = {
            "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
            "WEIGHTS": config.SCORING_WEIGHTS,
        }
        with config.console.status("[cyan]일별 상태·점수 계산 중...[/cyan]"):
            status = precompute_status(dfs, thresholds)

        table = Table(title=f"\n포트폴리오 백테스팅 — {len(dfs)}종목 · {len(dates)}거래일 · 초기자본 {initial:,}원",
                      box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("슬롯", justify="center")
        table.add_column("피라미딩", justify="center")
        table.add_column("최종자산", justify="right")
        table.add_column("총수익률", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("PF", justify="right")
        table.add_column("승률", justify="right")
        table.add_column("청산", justify="right")
        table.add_column("증액", justify="right")
        table.add_column("평균 슬롯", justify="right")

        for slots in slot_list:
            for pyr in pyr_list:
                res = run_portfolio(dfs, status, dates, initial_capital=initial, slots=slots,
                                    pyramiding_max=pyr, market_filter_dates=mf_dates)
                n_sell = len(res["sells"])
                win_rate = res["win"] / n_sell * 100 if n_sell else 0.0
                ret_color = "red" if res["total_return"] > 0 else "blue"
                table.add_row(
                    str(slots), f"{pyr}차",
                    f"{res['final_asset']:,.0f}원",
                    f"[{ret_color}]{res['total_return']:+.2f}%[/]",
                    f"[blue]{res['mdd']:.2f}%[/]",
                    f"{res['pf']:.2f}",
                    f"{win_rate:.1f}%",
                    f"{n_sell}건",
                    f"{res['pyramid_count']}건",
                    f"{res['avg_slots']:.2f}/{slots}",
                )

        config.console.print(table)
        if failed:
            config.console.print(f"[dim]※ 데이터 부족으로 제외: {len(failed)}종목 ({', '.join(failed[:5])}"
                                 f"{' 외' if len(failed) > 5 else ''})[/dim]")
        config.console.print("[dim]※ 슬롯 경쟁·현금 제약·포트폴리오 히트 캡이 모두 반영된 단일 경로 결과입니다. "
                             "종목 구성이 바뀌면 값이 크게 달라질 수 있습니다.[/dim]")
        utils.pause()
    return True
