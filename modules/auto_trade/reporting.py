"""AutoTrader 의 화면·보고 — 상태 화면·운영 관제·성과 보고서·로그 보기·텔레그램 상태/관제 문구.

[왜 나눴나 · 2026-10-24 C1 1단계] AutoTrader(7,659줄) 중 약 2,100줄이 매매 판단과 무관한 화면·보고였다.
 이 코드는 트레이더 상태를 주로 읽는다. 쓰는 곳은 두 가지다 — 보고서가 채우는 trade_records, 그리고
 상태 화면·텔레그램 상태가 initial_asset(오늘 시작 자산)이 비어 있을 때 당일 저장분으로 되살리는 자리
 (기동 경로 initialize 와 같은 복원). 매매 루프가 부르는 것은 장 전환 알림의 보유 목록(_get_holdings_message)
 하나뿐이다. 한 파일에 섞여 있으면 매수·매도 코드를 고칠 때 화면 코드까지 함께 읽어야 하고, 화면 문구
 하나를 고친 변경이 매매 파일의 변경으로 보인다.

[어떻게 나눴나] 메서드 본문은 한 글자도 바꾸지 않고 옮겼다. AutoTrader 가 이 믹스인을 상속하므로
 self.print_status() · AutoTrader.get_health_message · patch.object(AutoTrader, ...) 가 전과 같이 동작한다.
 모듈 전역 이름(config·api·console·Table …)은 trader.py 와 **같은 객체**를 가리키게 import 했다
 (tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도 trader 와 같은 이름을 쓴다 — 로그 파일·
 필터가 그 이름으로 묶여 있다. 동등성 기준은 주기 재생 기록(tests/test_cycle_replay.py)이다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.reporting.datetime). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import concurrent.futures
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable

from rich import box
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

import api
import config
from core import context
from core import indicators
from core import utils
from modules import account, analysis
from modules import db_manager
from modules import telegram_notify
from modules.auto_trade import performance
from modules.auto_trade.common import (DISK_FREE_WARN_MB, _enrich_rules_with_weights, _get_trade_account,
                                       is_single_price_break, load_daily_initial_asset, trade_account_key)

console = config.console

#  트레이더 로그와 같은 이름 — 화면·보고 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class TraderReportingMixin:
    """AutoTrader 의 화면·보고 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.logs·self.order_manager …)를 읽는다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의는 AutoTrader). 값 없는 선언이라 실행 시에는 AutoTrader 의
    #  것이 그대로 쓰인다. 화면 코드가 매매 쪽 무엇에 기대는지 한눈에 보는 목록이다(getattr 기본값으로 읽는
    #  상태는 빠져 있다).
    logs: list
    trade_records: list
    is_running: bool
    start_time: Any
    initial_asset: Any
    last_cycle_at: Any
    last_success_at: Any
    last_error_at: Any
    last_error_message: str
    code_error_streaks: int
    buy_halt_reason: str
    market_index_status: dict
    skipped_by_market_filter_count: dict
    order_manager: Any
    log: Callable[..., None]
    is_market_open: Callable[..., bool]
    daily_pnl_base: Callable[..., Any]
    transfer_note: Callable[..., str]
    _refine_trade_records: Callable[..., list]
    _get_stock_market_type: Callable[..., Any]
    _update_market_indices_status: Callable[..., None]
    _format_market_filter_basis: Callable[..., str]
    _health_memory: Callable[..., Any]

    def log_current_holdings(self):
        """현재 보유 종목 현황을 조회하여 로그에 출력합니다 (체결 후 호출용)"""
        try:
            target_cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
                valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                
                def get_display_width(s):
                    return len(s) + sum(1 for c in s if ord(c) > 127)

                def pad(s, width, align='>'):
                    real_len = get_display_width(s)
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                max_name_width = 20
                if valid_holdings:
                    for item in valid_holdings:
                        name = f"{item['prdt_name']} ({item['pdno']})"
                        w = get_display_width(name)
                        if w > max_name_width:
                            max_name_width = w
                
                name_col_width = max(30, max_name_width + 2)
                line_length = name_col_width + 95

                header = (
                    f"{pad('종목명', name_col_width, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("")
                self.log("─" * line_length)
                self.log(header)
                self.log("─" * line_length)
                
                if not valid_holdings:
                    self.log(f"{pad('보유 종목 없음', name_col_width, '<')} ")
                else:
                    for item in valid_holdings:
                        name = f"{item['prdt_name']} ({item['pdno']})"
                        qty = api.safe_int(item.get('hldg_qty'))
                        buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                        cur_price = api.safe_int(item.get('prpr'))
                        # 매입금액: 실전 잔고(INQR_DVSN=01)·토스 어댑터는 pchs_amt가 0/누락으로 오므로
                        # 합계 줄·잔고 화면과 동일하게 평단×수량으로 복원한다.
                        pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)
                        eval_amt = api.safe_int(item.get('evlu_amt'))
                        profit = api.safe_int(item.get('evlu_pfls_amt'))
                        rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)

                        row_str = f"{pad(name, name_col_width, '<')} {pad(f'{qty:,}주', 10, '>')} {pad(f'{buy_price:,.0f}원', 12, '>')} {pad(f'{cur_price:,.0f}원', 12, '>')} {pad(f'{pchs_amt:,}원', 15, '>')} {pad(f'{eval_amt:,}원', 15, '>')} {pad(f'{profit:+,}원', 14, '>')} {pad(f'{rate:.2f}%', 10, '>')}"
                        self.log(row_str)
                self.log("─" * line_length)
                self.log("")
        except Exception as e:
            self.log(f"보유 종목 로깅 실패: {e}")

    def get_status_message(self):
        """텔레그램 전송용 상태 요약 메시지 생성"""
        status_text = "STOPPED"
        status_icon = "🔴"
        if self.is_running:
            if self.is_market_open():
                status_text = "RUNNING"
                status_icon = "🟢"
            else:
                if is_single_price_break():
                    status_text = "WAITING (휴게 시간 대기)"
                elif api.is_holiday_today() or datetime.now().weekday() > 4:
                    status_text = "WAITING (공휴일/주말 휴장)"
                else:
                    status_text = "WAITING"
                status_icon = "🟡"
        
        msg = f"{status_icon} [시스템 상태: {status_text}]\n"

        # [안전장치] 방어 모드 표시 — 청산은 계속 돌고 신규 진입만 막혀 있음을 명확히 알린다.
        if self.is_running and getattr(self, 'buy_halted', False):
            msg += f"🛑 방어 모드: 신규 매수 중단 ({self.buy_halt_reason})\n   └ 매도·손절·트레일링 스탑 감시는 정상 동작 중\n"

        # 자산 정보 조회
        current_asset = None
        deposit = 0
        holdings = []

        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            try:
                acnt = config.session.auto_acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                res = api.get_deposit_balance(target_cano, acnt)
                if res:
                    d2_val = res.get('d2_real', 0)
                    if d2_val == 0: d2_val = res.get('d2_deposit', 0)
                    deposit = d2_val
                
                # [수정] 보유 종목 개별 합산으로 평가금액 직접 계산 (데이터 정합성 보장)
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                    if valid_holdings:
                        tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                elif summary:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                
                current_asset = deposit + tot_evlu
            except Exception: pass

        if current_asset is not None:
            tot_profit = 0
            tot_pchs = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
                    tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
            
            if self.initial_asset > 0:
                msg += f"오늘 시작 자산: {self.initial_asset:,}원\n"
            else:
                msg += f"오늘 시작 자산: - (미설정)\n"
                
            msg += f"오늘 현재 자산: {current_asset:,}원\n"
            
            if self.initial_asset > 0:
                _base = self.daily_pnl_base()
                daily_profit = int(round(current_asset - _base))   # 기준선은 float 다 (원 단위로 표시)
                daily_profit_rate = (daily_profit / _base) * 100 if _base > 0 else 0.0
                msg += (f"오늘 현재 손익: {daily_profit:+,}원 "
                        f"({daily_profit_rate:+.2f}%){self.transfer_note()}\n")
                
            realized_profit = 0
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                
                target_account = None
                if config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                today_trades = db_manager.db.get_trades(
                    start_date=today_str, end_date=today_str, 
                    is_sim=False, account=target_account
                )
                
                today_trades_parsed = []
                for r in reversed(today_trades):
                    type_str = r['type']
                    simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                    parsed_r = dict(r)
                    parsed_r['type'] = simple_type
                    today_trades_parsed.append(parsed_r)
                
                today_trades_refined = self._refine_trade_records(today_trades_parsed)
                # [추가] 체결된 내역만 당일 매매 요약에 포함
                today_trades_refined = [r for r in today_trades_refined if not r.get('order_status') or "체결" in r.get('order_status', '')]
                sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                realized_profit = sum(int(t.get('profit_amt') or 0) for t in sell_trades)
            except Exception: pass
            
            _base = self.daily_pnl_base()
            realized_rate = (realized_profit / _base * 100) if _base > 0 else 0.0
            msg += f"오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)\n"
            msg += f"주문 가능 금액: {deposit:,}원\n"
            msg += f"증권 평가 자산: {tot_evlu:,}원\n"
            msg += f"증권 평가 손익: {tot_profit:+,}원 ({rate:+.2f}%)\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        # [시장 지수 및 필터링 데이터 준비]
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_str = "ON" if use_filter else "OFF"
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 80)
        filter_band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
        band_txt = f" ±{filter_band:g}%" if filter_band else ""

        is_healthy_k = True
        is_healthy_q = True
        
        market_idx_msgs = []
        market_flt_msgs = []

        try:
            for name, m_type in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = df.iloc[-1]['close']
                    prev = df.iloc[-2]['close'] if len(df) > 1 else curr
                    rate = ((curr - prev) / prev) * 100
                    market_idx_msgs.append(f"• {name}: {curr:,.2f} ({rate:+.2f}%)")
                    
                    filter_status = "허용"
                    det = None
                    # 시스템 루프의 상태 캐시(market_index_status)를 우선 적용
                    cached_stat = self.market_index_status.get(m_type)
                    
                    if isinstance(cached_stat, dict) and cached_stat.get('unknown'):
                        is_healthy = False
                        filter_status = "보류 (판단불가)"
                    elif cached_stat and isinstance(cached_stat, dict) and cached_stat.get('current', 0) > 0:
                        is_healthy = cached_stat.get('is_healthy', True)
                        filter_status = "허용" if is_healthy else "보류"
                        det = cached_stat.get('filter')   # 루프가 판정할 때 같이 남긴 근거
                    else:
                        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
                        if indicators.market_filter_ready(df['close'], ma_period):
                            det = indicators.get_market_filter_detail(
                                df['close'], ma_period,
                                getattr(config, 'MARKET_FILTER_BAND', 1.0))
                            if 'date' in df.columns:
                                det['asof'] = str(df['date'].iloc[-1])
                            det['source'] = analysis.index_source(df)
                            is_healthy = not bool(det['blocked'])
                            filter_status = "허용" if is_healthy else "보류"
                        else:
                            is_healthy = False
                            filter_status = "보류 (데이터부족)"
                            
                    if m_type == "KOSPI":
                        is_healthy_k = is_healthy
                    elif m_type == "KOSDAQ":
                        is_healthy_q = is_healthy
                        
                    market_flt_msgs.append(f"• {name}: {filter_status}")
                    #  상태만으로는 기다릴지 알 수 없다 — 무엇과 무엇을 비교해 그렇게 됐는지,
                    #  어느 선을 넘어야 풀리는지, 며칠째인지까지 적는다.
                    market_flt_msgs.extend(f"  {line}" for line in self._format_market_filter_basis(det))
                else:
                    # [fail-closed] 지수를 못 읽으면 표시도 '보류(판단불가)'다. 종전에는
                    #  화면만 '확인 불가'로 찍고 is_healthy_* 는 기본값 True 로 남아,
                    #  아래 보류 종목 수 집계와 이 줄이 서로 다른 말을 했다.
                    if m_type == "KOSPI":
                        is_healthy_k = False
                    elif m_type == "KOSDAQ":
                        is_healthy_q = False
                    market_idx_msgs.append(f"• {name}: 확인 불가")
                    market_flt_msgs.append(f"• {name}: 보류 (판단불가)")
        except Exception: pass
        
        # [시장 지수] 섹션 출력
        msg += "\n[시장 지수]\n"
        if market_idx_msgs:
            msg += "\n".join(market_idx_msgs) + "\n"
        else:
            msg += "• 지수 데이터 확인 불가\n"

        # [시장 상황] 섹션 출력
        msg += "\n[시장 상황]\n"
        rp = config.MARKET_REGIME_PARAMS
        ema_desc = f"EMA {rp.get('REGIME_EMA_FAST', 9)}/{rp.get('REGIME_EMA_SLOW', 41)}"

        for m_type, label in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
            try:
                info = analysis.get_market_regime_detail(m_type)
                # 이모지·라벨 모두 analysis 단일 소스에서 받는다(메뉴 헤더·텔레그램 버튼과 공유).
                #  판정 불가(지수를 못 읽음)는 '판정 보류'(실제 횡보)와 다른 글자로 나온다.
                emoji, label_txt = analysis.describe_regime(info)
                regime_str = f"{emoji} {label_txt}"
                if info.get('unknown'):
                    msg += f"• {label}: {regime_str} (지수 데이터를 확인할 수 없습니다)\n"
                    continue
                msg += f"• {label}: {regime_str} ({info['moved_pct']:+.1f}%, {ema_desc} 기준)\n"
            except Exception:
                msg += f"• {label}: 확인 불가\n"

        # [시장 필터링] 섹션 출력
        #  [2026-09-23] 필터가 꺼져 있어도 판정 결과를 함께 적는다 — 지수가 지금 그 선의
        #   어느 쪽에 있는지, 몇 봉째인지, 무슨 값·출처로 본 것인지는 필터를 켤지 말지를
        #   정하는 근거이므로 OFF 라고 가릴 이유가 없다. 다만 '보류'가 매수를 막고 있다는
        #   말로 읽히면 안 되므로(종전에 감춘 이유가 그것이었다) 종목 줄 끝에 (미적용)을
        #   붙인다. 근거 줄(└ ...)은 상태가 아니라 숫자라 그대로 둔다.
        msg += f"\n[시장 필터링] ({filter_str}, SMA {filter_ma}일{band_txt} 기준)\n"
        if not use_filter:
            msg += "• 필터 비활성 — 시장 상태와 무관하게 매수를 허용합니다\n"
            if market_flt_msgs:
                for line in market_flt_msgs:
                    msg += (f"{line} (미적용)\n" if line.startswith("• ") else f"{line}\n")
            else:
                msg += "• 필터링 상태 확인 불가\n"
        elif market_flt_msgs:
            msg += "\n".join(market_flt_msgs) + "\n"
        else:
            msg += "• 필터링 상태 확인 불가\n"

        if use_filter:
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0:
                skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0:
                skip_msg.append(f"KOSDAQ {skip_q}종목")
                
            if skip_msg:
                msg += f"⚠️ 하락장 방어 중 (현재 {', '.join(skip_msg)} 신규 매수 보류)\n"

        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

        if valid_holdings:
            from modules import account
            analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
            msg += "\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
        else:
            msg += "\n📋 [보유 종목] 없음"
            
        return msg

    @staticmethod
    def _health_time(value):
        """관제 메시지용 시각 포맷. 값이 없을 때도 상태를 명확히 표시한다.

        [2026-10-24] 계좌 차단기 점검 시각(circuit_breaker_ran_at)은 time.time() 실수다. 종전엔 datetime 만
         시각으로 바꿔 '최근 점검 1761290000.12' 처럼 원시 타임스탬프가 /health·관제 화면에 그대로 찍혔다
         (주기 재생 하네스 tests/cycle_replay.py 가 찾음). 숫자 시각도 같은 형식으로 보인다.
        """
        if not value:
            return "기록 없음"
        if isinstance(value, datetime):
            return value.strftime("%H:%M:%S")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value).strftime("%H:%M:%S")
        return str(value)

    def _health_cycle_text(self):
        """관제용 주기 소요 시간 문구와 '실제 감시 간격(초)'을 돌려준다.

        Returns: (표시 문자열, 감시 간격 초 또는 None)
        """
        last = getattr(self, 'last_cycle_secs', None)
        if last is None:
            return "미측정 (루프 1회 실행 후 표시)", None
        hist = getattr(self, 'cycle_secs_history', None) or [last]
        avg = sum(hist) / len(hist)
        interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
        gap = avg + interval
        peak = getattr(self, 'cycle_secs_peak', 0.0) or last
        return (f"분석 {avg:.1f}초 (최근 {len(hist)}회 평균, 최대 {peak:.1f}초) + 대기 {interval}초 "
                f"= 청산 감시 간격 {gap:.0f}초", gap)

    def get_health_message(self):
        """외부 API 호출 없이 운영 상태를 요약한 관제 메시지를 만든다.

        /status는 잔고·지수 조회까지 수행하는 상세 화면이고, /health는 장애 상황에서도
        응답할 수 있도록 메모리·로컬 DB·실시간 피드 상태만 사용한다.
        """
        now = datetime.now()
        warnings = []
        risks = []

        if not self.is_running:
            state = "중지"
            icon = "🔴"
            warnings.append("자동매매가 실행 중이 아닙니다")
        elif getattr(self, "buy_halted", False):
            state = "방어 모드"
            icon = "🟠"
            warnings.append(f"신규 매수 중단: {self.buy_halt_reason or '사유 미기록'}")
        elif self.is_market_open():
            state = "운영 중"
            icon = "🟢"
        else:
            state = "대기"
            icon = "🟡"

        max_err = int(getattr(config, "SYSTEM_MAX_CONSECUTIVE_ERRORS", 5) or 5)
        errors = int(getattr(self, "consecutive_errors", 0) or 0)
        if errors:
            (risks if errors >= max_err else warnings).append(
                f"자동매매 루프 연속 오류 {errors}/{max_err}회"
            )

        # 체결 모니터 오류는 주문 안전성과 직결되므로 별도로 노출한다.
        monitor_errors = 0
        try:
            monitor_errors = int(getattr(_pkg().ConclusionMonitor(), "consecutive_errors", 0) or 0)
        except Exception:
            warnings.append("체결 감시 상태를 읽지 못했습니다")
        if monitor_errors:
            (risks if monitor_errors >= max_err else warnings).append(
                f"체결 감시 연속 오류 {monitor_errors}/{max_err}회"
            )

        # 로컬 주문 상태는 API 장애 중에도 확인 가능하다.
        with getattr(self.order_manager, "_lock", threading.RLock()):
            pending_orders = sum(len(v) for v in self.order_manager.pending_orders.values())
        try:
            reserved_orders = len(db_manager.db.get_pending_reserved_orders())
        except Exception:
            reserved_orders = 0
            warnings.append("예약 주문 DB를 읽지 못했습니다")

        # 당일 주문 이력도 로컬 DB에서만 집계한다. 주문번호 기준으로 중복된
        # 접수→체결 행은 한 건으로 정리해 운영자가 실제 주문 흐름을 오해하지 않게 한다.
        today_order_count = today_fill_count = today_cancel_count = 0
        try:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            today_trades = db_manager.db.get_trades(
                start_date=now.strftime("%Y-%m-%d"), end_date=now.strftime("%Y-%m-%d"),
                is_sim=False, account=target_account,
            )
            refined = self._refine_trade_records(today_trades)
            today_order_count = len(refined)
            today_fill_count = sum("체결" in str(t.get("order_status") or "") for t in refined)
            today_cancel_count = sum("취소" in str(t.get("order_status") or "") for t in refined)
        except Exception:
            warnings.append("당일 주문 이력을 읽지 못했습니다")

        feed_text = "REST 폴링"
        try:
            from brokers import realtime
            feed = realtime.get_feed()
            if getattr(config.session, "is_toss", False):
                #  [2026-09-09] 토스 WS는 **주문 이벤트 전용**이다. 시세는 구독하지 않으므로
                #   현재가·호가는 여전히 REST 폴링이다. 둘을 한 줄에 같이 적지 않으면
                #   "WebSocket 연결됨"이 시세까지 실시간이라는 뜻으로 읽힌다.
                thread = getattr(feed, "_thread", None)
                alive = bool(thread and thread.is_alive())
                subscribed = bool(getattr(feed, "_subscribed", False))
                if alive and subscribed:
                    feed_text = "토스: 시세 REST 폴링 + 주문 이벤트 WS 구독 중"
                else:
                    feed_text = "토스: 시세 REST 폴링 (주문 이벤트 WS 연결 대기)"
                    if getattr(config, "USE_WEBSOCKET", True):
                        warnings.append("토스 주문 이벤트 WebSocket이 연결 또는 구독 대기 상태입니다")
            elif not getattr(config, "USE_WEBSOCKET", True):
                feed_text = "REST 폴링 (WebSocket 비활성)"
            else:
                thread = getattr(feed, "_thread", None)
                alive = bool(thread and thread.is_alive())
                got_data = bool(getattr(feed, "_got_data", False))
                coverage = feed.coverage() if hasattr(feed, "coverage") else None
                feed_text = "WebSocket 연결·수신 중" if alive and got_data else "WebSocket 재연결/REST 폴백"
                if not (alive and got_data):
                    warnings.append("실시간 시세 WebSocket이 연결 또는 수신 대기 상태입니다")
                if coverage:
                    feed_text += f" (시세 {coverage.get('price_covered', 0)}/{coverage.get('priority', 0)}종목)"
        except Exception:
            warnings.append("실시간 피드 상태를 읽지 못했습니다")

        account = config.session.auto_cano
        #  [Fix 2026-09-05] 마지막 줄이 분기 **밖**에 있어 무엇으로 떴든 항상 "KIS 실전"으로
        #   덮였다. scheduler._heartbeat_context 에서 같은 형태를 2026-09-04 에 고쳤는데
        #   쌍둥이인 이 자리가 남아 있었다 — 관제 첫 화면이 가상투자·토스에서도 'KIS 실전'
        #   이라고 말한다. 두 인스턴스를 함께 돌리는 운용에서 화면이 계좌 성격을 거짓말하면
        #   장애 때 실계좌부터 뒤지게 된다.
        if getattr(config.session, "is_paper", False):
            mode = "가상투자"
        elif getattr(config.session, "is_toss", False):
            mode = "토스 실전"
        else:
            mode = "KIS 실전"

        if self.last_success_at and isinstance(self.last_success_at, datetime):
            age = (now - self.last_success_at).total_seconds()
            if self.is_running and age > max(120, getattr(config, "SYSTEM_TRADING_INTERVAL", 60) * 4):
                warnings.append(f"정상 루프 갱신 지연 {int(age)}초")
        elif self.is_running:
            warnings.append("아직 정상 루프 완료 기록이 없습니다")

        # 언제부터 돌고 있는지는 장애 판단의 기준 시각이라 관제 첫 화면에 함께 둔다.
        if self.is_running and self.start_time:
            elapsed = str(now - self.start_time).split(".")[0]
            run_text = f"{self.start_time.strftime('%Y-%m-%d %H:%M:%S')} (경과 {elapsed})"
        else:
            run_text = "미실행"

        rss_mb, avail_mb, peak_mb = self._health_memory()
        resource_parts = []
        if rss_mb:
            # 피크를 함께 보여야 '지금 여유롭다'가 안심의 근거가 되지 않는다 — OOM은
            #  분석이 몰리는 순간에 나고, 그 순간의 값은 여기 찍히지 않는다.
            mem_text = f"프로세스 메모리 {rss_mb:,.0f}MB"
            if peak_mb > rss_mb:
                mem_text += f" (피크 {peak_mb:,.0f}MB)"
            resource_parts.append(mem_text)
        if avail_mb:
            resource_parts.append(f"가용 메모리 {avail_mb:,.0f}MB")
            #  1GB 라즈베리파이 기준으로 가용 메모리가 이 아래로 떨어지면 OOM 종료 위험이 커진다.
            #  [2026-09-08] 문턱은 config 가 정본이다 — 능동 경보(_warn_if_memory_low)와
            #  이 화면이 다른 숫자를 쓰면 "화면은 괜찮다는데 알림이 온다"가 된다.
            _warn_at = float(getattr(config, 'SYSTEM_MEMORY_WARN_MB', 120))
            _risk_at = float(getattr(config, 'SYSTEM_MEMORY_RISK_MB', 60))
            if avail_mb < _warn_at:
                (risks if avail_mb < _risk_at else warnings).append(
                    f"가용 메모리 부족 {avail_mb:,.0f}MB (OOM 위험)"
                )
        resource_text = " · ".join(resource_parts) if resource_parts else "확인 불가"

        #  [추가 2026-09-08] **감시자가 도는지도 관제에 올린다.** 프로세스가 통째로 죽는
        #   실패(OOM 킬)에서 알릴 수 있는 것은 밖의 cron 감시자뿐인데, 그 감시자가 멈춘
        #   상태는 파일상 '정상'과 구분되지 않았다(heartbeat.watchdog_status 주석).
        #   여기 한 줄이면 사람이 /health 를 볼 때마다 부재가 드러난다.
        try:
            from modules import heartbeat
            wd_state, wd_detail = heartbeat.watchdog_status()
            if wd_state == "unknown":
                warnings.append("프로세스 중단 감시(cron)가 실행된 기록이 없습니다 (crontab 등록 확인 필요)")
            elif wd_state == "stale":
                warnings.append(wd_detail)
        except Exception as e:      # noqa: BLE001 - 관제 화면이 이것 때문에 죽지는 않는다
            logger.debug(f"[Health] 감시자 상태 확인 실패: {e}")

        # [운영 관제] 주기 소요 시간 — 관심종목을 늘릴 때의 실질 상한 지표.
        #  SYSTEM_TRADING_INTERVAL은 '주기가 끝난 뒤 쉬는 시간'이므로 실제 감시 간격은
        #  (소요 시간 + interval)이다. 종목이 늘면 소요 시간만 길어져 손절·트레일링 확인이
        #  그만큼 늦어진다. 추세추종에서 청산은 생명줄이라 여기서 한계를 잡아야 한다.
        cycle_text, cycle_gap = self._health_cycle_text()
        if cycle_gap:
            if cycle_gap >= 300:
                risks.append(f"청산 감시 간격 {int(cycle_gap)}초 (관심종목 축소 또는 주기 간격 단축 필요)")
            elif cycle_gap >= 180:
                warnings.append(f"청산 감시 간격 {int(cycle_gap)}초 (종목 추가 시 주의)")

        # [운영 관제] 리스크 4종은 '지금 얼마나 위험한가'를 판단하는 핵심 수치다. 숫자만 나열하면
        #  운용자가 기준을 외우고 있어야 읽히므로, 각 값을 판단 기준(슬롯 수·히트 한도·기본 배수)
        #  대비로 함께 적는다. 값의 의미(무엇을 뜻하는 금액인지)도 짧게 덧붙인다.
        #  주의: 텔레그램과 공용 문자열이라 rich 마크업을 넣지 않는다(색은 _add_health_rows에서 부여).
        #        또한 CLI 표는 " · "로 줄을 나누므로 각 항목 내부에는 " · "를 쓰지 않는다.
        tracked_cnt = len(getattr(self, 'trailing_stop_cache', {}) or {})
        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 4) or 4
        equity = getattr(self, 'current_total_asset', 0) or 0
        heat_amt = getattr(self, 'portfolio_heat_amt', 0.0) or 0.0
        heat_cap_pct = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0) or 0.0
        risk_scale = getattr(self, 'risk_scale', 1.0) or 1.0
        risk_scale_reason = getattr(self, 'risk_scale_reason', "") or ""

        risk_parts = [f"추적 포지션 {tracked_cnt}/{max_holdings}종목 (손절·트레일링 감시 중)"]
        if equity > 0:
            risk_parts.append(f"현재 자산 {equity:,.0f}원 (예수금+주식평가)")
        else:
            risk_parts.append("현재 자산 미조회 (루프 1회 실행 후 표시)")

        # 히트는 절대 금액보다 '한도를 얼마나 썼는가 / 얼마나 남았는가'가 판단 기준이다.
        #  한도는 실제 매수 게이트가 쓰는 값과 같게 계산한다
        #  (자산 × 한도% × 리스크 배수 — engine.effective_portfolio_cap 참조).
        if heat_cap_pct <= 0:
            risk_parts.append(f"포트폴리오 히트 {heat_amt:,.0f}원 (한도 미사용)")
            risk_parts.append("동시 손절 시 최대손실")
        elif equity <= 0:
            risk_parts.append(f"포트폴리오 히트 {heat_amt:,.0f}원 (한도 {heat_cap_pct:.1f}%)")
            risk_parts.append("동시 손절 시 최대손실, 한도액은 자산 조회 후 산출")
        else:
            heat_limit = equity * heat_cap_pct * min(1.0, risk_scale) / 100.0
            used_pct = (heat_amt / heat_limit * 100.0) if heat_limit > 0 else 0.0
            room = heat_limit - heat_amt
            risk_parts.append(
                f"포트폴리오 히트 {heat_amt:,.0f}원 / 한도 {heat_limit:,.0f}원 ({used_pct:.0f}% 소진)"
            )
            if used_pct >= 100:
                risk_parts.append("동시 손절 시 최대손실, 한도 초과로 신규 매수·피라미딩 차단")
                risks.append(
                    f"포트폴리오 히트 한도 초과 {heat_amt:,.0f}/{heat_limit:,.0f}원 (신규 매수·피라미딩 차단 중)"
                )
            elif used_pct >= 80:
                risk_parts.append(f"동시 손절 시 최대손실, 한도까지 {room:,.0f}원 (임박)")
                warnings.append(f"포트폴리오 히트 한도 {used_pct:.0f}% 소진 (신규 매수 여력 축소)")
            else:
                risk_parts.append(f"동시 손절 시 최대손실, 한도까지 {room:,.0f}원 여유")

        if risk_scale >= 1.0:
            risk_parts.append("리스크 배수 x1.00 (기본 사이징 100%)")
        else:
            risk_parts.append(f"리스크 배수 x{risk_scale:.2f} (신규 매수 예산 {risk_scale * 100:.0f}%로 축소)")
            if risk_scale_reason:
                risk_parts.append(f"축소 사유: {risk_scale_reason[:60]}")

        # [동적 손절 캡 · 2026-10-24] 지금 적용 중인 ATR 손절 캡과 그 근거. 도입(08-09) 이후 실매매 캡이 한 번도
        #  움직이지 않았는데 어디에도 보이지 않아 두 달 넘게 몰랐다 — 고정값으로 도는 것도 그렇다고 적는다.
        try:
            _ss = config.SELL_STRATEGY
            if _ss.get("MAX_ATR_STOP_LOSS_RATE", -15.0) and _ss.get("ATR_CAP_DYNAMIC", True):
                _cap = _pkg().effective_atr_stop_cap()
                _vi = getattr(self, 'vol_regime_info', None)
                if _vi:
                    risk_parts.append(f"ATR 손절 캡 {_cap:.1f}% (지수 변동성 x{_vi['ratio']:.2f}, "
                                      f"{_vi['asof'][4:6]}/{_vi['asof'][6:8]} 확정 종가 기준)")
                elif getattr(self, '_vol_regime_tried', False):
                    risk_parts.append(f"ATR 손절 캡 {_cap:.1f}% 고정 (지수 장기 이력 미확보)")
                else:
                    risk_parts.append(f"ATR 손절 캡 {_cap:.1f}% (루프 실행 후 지수 변동성 반영)")
        except Exception:
            pass

        # [표시 정직성] SYSTEM_RISK_PER_TRADE(명목 한도)는 변동성 타겟팅이 켜져 있는 한
        #  사이징 min 결합에서 한 번도 구속되지 않는다(config.py의 2026-07-27 실측 주석 참조).
        #  명목값만 보이면 "1회 4% 리스크로 돌고 있다"는 오해를 부르므로, 실제로 금액을 결정하는
        #  변동성층 기준의 실효 비중을 함께 적는다. 타겟팅을 끄면 명목 한도가 실효 한도가 된다.
        try:
            _nominal_risk = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0) or 0.0
            if getattr(config, 'USE_VOLATILITY_TARGETING', True):
                _eff_ratio = (config.resolve_invest_ratio()
                              * getattr(config, 'VOLATILITY_SCALING_MIN', 0.4))
                _part = f"1회 사이징 기준 비중 약 {_eff_ratio * 100:.1f}% (변동성 타겟팅이 결정)"
                if _nominal_risk > 0:
                    _part += f", 명목 한도 {_nominal_risk:g}%는 미발동"
                risk_parts.append(_part)
            elif _nominal_risk > 0:
                risk_parts.append(f"변동성 타겟팅 OFF — 1회 리스크 한도 {_nominal_risk:g}%가 실효 제한")
        except Exception:
            pass

        lines = [
            f"{icon} [운영 관제 /health: {state}]",
            f"• 모드/계좌: {mode} / {account or '-'}",
            f"• 실행 시간: {run_text}",
            f"• 자동매매 루프: 최근 시작 {self._health_time(self.last_cycle_at)} · 정상 완료 {self._health_time(self.last_success_at)}",
            f"• 주기 소요: {cycle_text}",
            f"• 최근 오류: {self._health_time(self.last_error_at)}" + (f" — {self.last_error_message[:160]}" if self.last_error_message else ""),
            f"• Kill Switch: 자동매매 {errors}/{max_err} · 체결 감시 {monitor_errors}/{max_err}"
            + (f" · [bold red]서버 정상인데 루프 오류 {self.code_error_streaks}회 "
               f"— 원인 확인 필요[/bold red]" if getattr(self, 'code_error_streaks', 0) else ""),
            f"• 주문 감시: 미체결 {pending_orders}건 · 예약 대기 {reserved_orders}건 · 오늘 주문/체결/취소 {today_order_count}/{today_fill_count}/{today_cancel_count}건",
            f"• 시세 연결: {feed_text}",
            f"• 알림 발신: {self._health_telegram_text()}",
            f"• 저장 상태: {self._health_storage_text()}",
            f"• 계좌 차단기: {self._health_circuit_breaker_text()}",
            "• 리스크: " + " · ".join(risk_parts),
            f"• 시스템 자원: {resource_text}",
        ]
        if risks:
            lines.append("\n🚨 [위험]\n" + "\n".join(f"• {item}" for item in risks))
        if warnings:
            lines.append("\n⚠️ [주의]\n" + "\n".join(f"• {item}" for item in warnings))
        if not risks and not warnings:
            lines.append("\n✅ 관제상 즉시 조치가 필요한 신호가 없습니다.")
        return "\n".join(lines)

    def _health_telegram_text(self):
        """알림 발신 상태. 전송은 비동기라 호출부가 성공 여부를 모르므로 여기서 드러낸다.

        '알림이 조용하다'가 '이상 없음'인지 '경로가 죽었다'인지 구분되어야 한다.
        """
        try:
            h = telegram_notify.get_delivery_health()
        except Exception:
            return "상태 확인 불가"
        if not h['sent'] and not h['failed']:
            return "발신 이력 없음"
        text = f"성공 {h['sent']}건 · 실패 {h['failed']}건"
        if h['consecutive_failed'] > 0:
            text += (f" · [bold red]연속 실패 {h['consecutive_failed']}건 — 알림이 도착하지 "
                     f"않고 있습니다[/bold red]")
            if h['last_error']:
                text += f" ({h['last_error'][:60]})"
        if h['lost']:
            text += f" · 미전달 {len(h['lost'])}건(최근: {h['lost'][-1][0]} {h['lost'][-1][1][:40]})"
        return text

    def _health_circuit_breaker_text(self):
        """일일 손실 한도 감시가 살아 있는가. '조용함'이 '정상'과 구분되어야 한다."""
        limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
        if limit <= 0:
            return "미사용 (SYSTEM_DAILY_LOSS_LIMIT=0)"
        fails = getattr(self, 'circuit_breaker_fails', 0)
        ran = getattr(self, 'circuit_breaker_ran_at', 0.0)
        text = f"한도 -{limit}% · 최근 점검 {self._health_time(ran) if ran else '없음'}"
        if getattr(self, 'buy_halted', False):
            text += f" · [bold yellow]방어 모드 작동 중({self.buy_halt_reason or ''})[/bold yellow]"
        if fails:
            text += f" · [bold red]연속 실패 {fails}회 — 한도가 감시되지 않는다[/bold red]"
        return text

    def _health_storage_text(self):
        """디스크 여유와 DB 쓰기 실패. 둘 다 조용히 진행되는 고장이라 눈에 띄어야 한다."""
        try:
            free_mb = db_manager.db.disk_free_mb()
            h = db_manager.db.get_write_failures()
        except Exception:
            return "상태 확인 불가"

        if free_mb < 0:
            text = "디스크 여유 확인 불가"
        elif free_mb < DISK_FREE_WARN_MB:
            text = f"[bold red]디스크 여유 {free_mb:,.0f}MB — 쓰기 실패 임박[/bold red]"
        else:
            text = f"디스크 여유 {free_mb:,.0f}MB"

        if h['count']:
            text += (f" · [bold red]DB 쓰기 실패 {h['count']}건[/bold red]"
                     f" (최근: {h['last_op']} — {h['last_error'][:60]})")
        return text

    def _add_health_rows(self, table, skip_labels=()):
        """기존 CLI 테이블 끝에 운영 관제 행을 추가한다.

        텔레그램은 한 화면에 읽기 쉬운 줄글을 쓰되, 터미널은 기존 ``print_status``와
        같은 표 형식을 사용해 운용 중 수치를 빠르게 비교할 수 있게 한다.
        ``skip_labels``에 넣은 항목은 상위 표에 이미 있는 값이라 중복 출력하지 않는다.
        """
        message_lines = self.get_health_message().splitlines()

        def _cli_text(text):
            """텔레그램용 상태 기호를 터미널 테이블에서는 제거한다."""
            for mark in ("🟢", "🟡", "🟠", "🔴", "🚨", "⚠️", "⚠", "✅"):
                text = text.replace(mark, "")
            # 오류 메시지의 대괄호가 rich 마크업으로 해석돼 글자가 사라지는 것을 막는다.
            return escape(text.strip())

        # '리스크' 셀에서 바로 앞 값을 부연하는 줄(무엇을 뜻하는 금액인지·축소 사유).
        #  텔레그램 한 줄 표기에서는 들여쓰기 기호가 어색하므로 CLI 표에서만 붙인다.
        risk_sub_prefixes = ("동시 손절 시 최대손실", "축소 사유:")

        def _compact_detail(label, detail):
            # 한 줄에 모든 지표를 나열하면 표가 화면 전체 폭으로 늘어난다. 관련 값은
            # 셀 안에서 줄바꿈해 기존 상태 표의 폭과 가독성을 맞춘다.
            if label in ("자동매매 루프", "Kill Switch", "주문 감시", "리스크"):
                rows = detail.split(" · ")
                if label == "리스크":
                    rows = [f"└ {r}" if r.startswith(risk_sub_prefixes) else r for r in rows]
                return "\n".join(rows)
            return detail

        def _styled(label, detail):
            """기존 상태 표와 같은 색 규칙(정상=dim green, 경고=yellow, 위험=red)을 적용한다."""
            if label == "Kill Switch":
                counts = re.findall(r"(\d+)/(\d+)", detail)
                if counts and any(int(cur) > 0 for cur, _ in counts):
                    color = "red" if any(int(cur) >= int(mx) for cur, mx in counts) else "yellow"
                    return f"[{color}]{detail}[/]"
                return f"[dim green]{detail}[/]"
            if label == "최근 오류":
                return f"[dim green]{detail}[/]" if detail.startswith("기록 없음") else f"[yellow]{detail}[/]"
            if label == "시세 연결":
                if "연결·수신" in detail:
                    return f"[green]{detail}[/]"
                if "재연결" in detail or "폴백" in detail:
                    return f"[yellow]{detail}[/]"
            if label == "리스크":
                # 히트 한도 소진과 사이징 축소는 '지금 매수가 막혔는지'를 결정하므로 색으로 먼저 보이게 한다.
                if "한도 초과" in detail:
                    return f"[red]{detail}[/]"
                if "한도 임박" in detail or "축소" in detail:
                    return f"[yellow]{detail}[/]"
            if label == "주기 소요":
                # 청산 감시가 늦어지면 손절이 늦게 걸린다 — 관심종목 확대의 실질 상한 지표.
                m = re.search(r"청산 감시 간격 (\d+)초", detail)
                if m:
                    gap = int(m.group(1))
                    if gap >= 300:
                        return f"[red]{detail}[/]"
                    if gap >= 180:
                        return f"[yellow]{detail}[/]"
                    return f"[dim green]{detail}[/]"
            return detail

        section = None
        section_messages = []

        def _flush_section_messages():
            """주의/위험 항목을 한 셀에 모아 라벨 반복을 피한다."""
            nonlocal section_messages
            if section and section_messages:
                color = "bold red" if section == "위험 신호" else "bold orange3"
                table.add_row(section, f"[{color}]" + "\n".join(section_messages) + "[/]")
                section_messages = []

        # 상태 제목은 기존 테이블 제목에 이미 있으므로 건너뛴다.
        for raw_line in message_lines[1:]:
            line = raw_line.strip()
            if not line:
                continue
            if line in ("🚨 [위험]", "⚠️ [주의]"):
                _flush_section_messages()
                section = "위험 신호" if line.startswith("🚨") else "주의 신호"
                table.add_section()
                continue
            if line.startswith("✅ "):
                _flush_section_messages()
                table.add_section()
                table.add_row("관제 결과", f"[dim green]{_cli_text(line)}[/]")
                continue

            # get_health_message의 표준 항목(• 구분: 내용)을 터미널 표의 두 열로 분리한다.
            if line.startswith("• "):
                body = line[2:]
                if section:
                    section_messages.append(_cli_text(body))
                elif ": " in body:
                    label, detail = body.split(": ", 1)
                    if label in skip_labels:
                        continue
                    table.add_row(label, _styled(label, _compact_detail(label, _cli_text(detail))))
                else:
                    table.add_row("상태", _cli_text(body))
            else:
                if section:
                    section_messages.append(_cli_text(line))
                else:
                    table.add_row("상태", _cli_text(line))

        _flush_section_messages()

    def print_health(self):
        """CLI용 운영 관제 단독 화면(하위 호환용)."""
        utils.clear_screen()
        utils.print_breadcrumb()

        table = Table(
            title="운영 관제",
            title_justify="center",
            title_style="",
            box=box.HORIZONTALS,
            show_header=True,
            header_style="dim",
            border_style="dim",
        )
        table.add_column("구분", justify="left", style="cyan", width=15, no_wrap=True)
        table.add_column("상세 내용", justify="left")
        self._add_health_rows(table)

        console.print()
        console.print(table)
        console.print()

    def _get_skipped_stocks_count(self, holdings):
        """현재 관심 종목 중 미보유 종목을 대상으로 시장별 대기 종목 수를 계산합니다."""
        targets = config.session.stock_data.get("stocks_kr", [])
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            targets += config.session.stock_data.get("etfs_kr", [])
        holding_codes = {h['pdno'] for h in holdings if api.safe_int(h.get('hldg_qty')) > 0} if holdings else set()
        
        count_k = 0
        count_q = 0
        for item in targets:
            code = item['code']
            if code in holding_codes:
                continue
            m_type = self._get_stock_market_type(code)
            if m_type == "KOSDAQ": count_q += 1
            else: count_k += 1
            
        return count_k, count_q

    def get_recent_logs(self):
        """최근 로그 반환 (텔레그램용)"""
        if not self.logs:
            return "📭 로그가 없습니다."
        
        final_logs = []
        current_len = 0
        max_len = 3800 # 텔레그램 제한(4096자) 고려하여 여유 있게 설정
        
        header = "📜 [최근 시스템 로그]\n"
        current_len += len(header)

        for log in reversed(self.logs):
            if current_len + len(log) + 1 > max_len:
                break
            final_logs.append(log)
            current_len += len(log) + 1
        
        final_logs.reverse()
        return header + "\n".join(final_logs)

    def print_status(self):
        utils.clear_screen()
        utils.print_breadcrumb()
        
        if not self.is_running:
            status_text = "STOPPED"
            status_color = "red"
        elif self.is_market_open():
            status_text = "RUNNING"
            status_color = "green"
        else:
            status_text = "WAITING"
            status_color = "yellow"
        
        #  국면 dict 를 그대로 들고 간다 — 표시는 analysis.describe_regime 하나를 지나야
        #   '판정 불가'(지수를 못 읽음)와 '판정 보류'(실제 횡보)가 갈린다. 조회 자체가
        #   던진 경우(None)만 '확인 불가'로 적는다.
        kospi_info = kosdaq_info = None
        kospi_adj = kosdaq_adj = 0.0

        # 3. 자산 및 손익 현황 (안전성 핵심)
        current_asset = None
        deposit = 0
        holdings = []
        
        # [추가] 상태 조회 시에도 시스템 트레이딩 컨텍스트 사용
        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]자산/시장 정보 병렬 조회 중...[/cyan]", total=None)
                acnt = config.session.auto_acnt_prdt_cd

                # [최적화] 잔고+예수금 / 시장 국면 / 지수 상태를 병렬 조회 (기존 순차 → 동시)
                def _fetch_asset():
                    _holdings, _summary, _deposit = [], [], 0
                    try:
                        _holdings, _summary = api.get_domestic_balance(target_cano, acnt)
                    except Exception:
                        _holdings, _summary = [], []
                    # 예수금 조회 (매수 여력 확인용) — 잔고 결과에 의존하므로 같은 태스크에서 수행
                    try:
                        if _summary and len(_summary) > 0:
                            _deposit = api.safe_int(_summary[0].get('dnca_tot_amt', 0))

                        # [수정] 예수금은 항상 상세 조회로 확인한다 (정확도 우선)
                        res = api.get_deposit_balance(target_cano, acnt)
                        if res:
                            _deposit = res.get('d2_real', 0)
                            if _deposit == 0: _deposit = res.get('d2_deposit', 0)
                    except Exception: pass
                    return _holdings, _summary, _deposit

                def _fetch_regimes():
                    try:
                        k = analysis.get_market_regime_detail("KOSPI")
                        q = analysis.get_market_regime_detail("KOSDAQ")
                        return k, q
                    except Exception:
                        return None, None

                def _update_indices():
                    # [추가] 지수 상태 정보가 없으면 업데이트 시도 (시장 필터링 사용 시)
                    # 시스템이 정지 상태이거나 장 시작 전이라도 상태 조회 시에는 최신 정보를 보여주기 위함
                    if not getattr(config, 'USE_MARKET_FILTER', True):
                        return
                    need_update = False
                    if "KOSPI" not in self.market_index_status or "KOSDAQ" not in self.market_index_status:
                        need_update = True
                    elif self.market_index_status.get("KOSPI", {}).get("current", 0) == 0 or \
                         self.market_index_status.get("KOSDAQ", {}).get("current", 0) == 0:
                        need_update = True
                    if need_update:
                        try:
                            self._update_market_indices_status(notify=False)
                        except Exception: pass

                summary = []
                #  [계좌 컨텍스트] at_init 과 같은 이유로 제출 스레드에서 감싼다.
                #   시세만 보는 작업(_fetch_regimes·_update_indices)도 함께 감싼다 —
                #   앱키가 갈리면 TPS 버킷도 갈려, 자동 계좌 몫으로 잡아야 할 호출이
                #   수동 버킷을 깎는다(그쪽이 좁아 주문 경로가 먼저 막힌다).
                _st_task = utils.inherit_account_context
                with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_status") as executor:
                    fut_asset = executor.submit(_st_task(_fetch_asset))
                    fut_regime = executor.submit(_st_task(_fetch_regimes))
                    fut_indices = executor.submit(_st_task(_update_indices))

                    holdings, summary, deposit = fut_asset.result()
                    _k, _q = fut_regime.result()
                    if _k: kospi_info, kospi_adj = _k, _k.get('score_adj', 0.0)
                    if _q: kosdaq_info, kosdaq_adj = _q, _q.get('score_adj', 0.0)
                    fut_indices.result()

                # [수정] 중복 API 호출 방지 및 동일 스냅샷 기반 현재 자산 일괄 계산
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                    tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                elif summary and len(summary) > 0:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))

                current_asset = deposit + tot_evlu

        console.print()
        table = Table(title=f"시스템 트레이딩 상태 ({status_text})", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="left", style="cyan", width=15)
        table.add_column("상세 내용", justify="left")

        # 1. 실행 정보
        if self.is_running and self.start_time:
            start_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split('.')[0]
            table.add_row("실행 시간", f"{start_str} (경과: {elapsed_str})")
        
        # 2. 마켓 상태
        if self.is_market_open():
            market_status = "장 운영 중 (거래 가능)"
        else:
            if is_single_price_break():
                market_status = "휴게 시간 (단일가 매매 동기화 대기 중)"
            elif api.is_holiday_today():
                market_status = "공휴일 휴장 (대기 중)"
            else:
                market_status = "장 마감 (대기 중)"
                
        if datetime.now().weekday() > 4: market_status = "주말 휴장 (대기 중)"
        table.add_row("마켓 상태", market_status)

        # [안전장치] 방어 모드 — 신규 매수만 차단, 청산 감시는 계속됨을 명시
        if self.is_running and getattr(self, 'buy_halted', False):
            table.add_row("방어 모드",
                          f"[bold red]🛑 신규 매수 중단[/] ({self.buy_halt_reason})\n"
                          f"[dim]매도·손절·트레일링 스탑 감시는 정상 동작 중 (날짜 변경 시 자동 해제)[/]")

        # [추가] 시장 국면 상태 표시
        #  [Fix 2026-09-08] 종전에는 format_regime(국면문자열) 이었다. 지수를 못 읽으면
        #   get_market_regime 이 'Sideways' 를 돌려주므로, 이 표는 지수가 끊긴 동안에도
        #   🟡 '판정 보류'라고 적었다 — 시장이 실제로 어느 쪽도 아니라는 뜻이라 전혀 다른
        #   말이다. /status·텔레그램은 2026-09-08에 고쳤는데 이 콘솔 표만 남아 있었다.
        def _regime_cell(info):
            if info is None:
                return "확인 불가"
            #  markup=True 는 종전 format_regime 호출과 같은 표기(rich 색 태그, 이모지 없음).
            return analysis.describe_regime(info, markup=True)[1]
        k_regime_str = _regime_cell(kospi_info)
        q_regime_str = _regime_cell(kosdaq_info)
        rp = config.MARKET_REGIME_PARAMS
        regime_desc = f"EMA {rp.get('REGIME_EMA_FAST', 9)}/{rp.get('REGIME_EMA_SLOW', 41)} 교차 + {rp.get('REGIME_CONFIRM_PCT', 5.0):g}% 확인"
        table.add_row("시장 국면", f"KOSPI: {k_regime_str} (보정: {kospi_adj:+.1f}점) / KOSDAQ: {q_regime_str} (보정: {kosdaq_adj:+.1f}점) [dim]({regime_desc})[/]")

        # [추가] 지수 추세 상태 표시 (시장 필터링 사용 시)
        if getattr(config, 'USE_MARKET_FILTER', True):
            # ... existing code ...
            kospi_stat = self.market_index_status.get("KOSPI")
            kosdaq_stat = self.market_index_status.get("KOSDAQ")
            
            def get_stat_msg(stat):
                # [Fix] 판단 불가(조회 실패)는 '확인 중'이 아니라 '매수 보류' 상태임을 명시한다.
                if isinstance(stat, dict) and stat.get('unknown'):
                    return "[yellow]판단 불가 (신규 매수 보류)[/]"
                if not stat or not isinstance(stat, dict) or stat.get('current', 0) == 0:
                    return "[dim]확인 중[/]"

                is_healthy = stat.get('is_healthy', True)
                current = stat.get('current', 0)
                trend_icon = "(상승)" if is_healthy else "(하락)"
                color = "red" if is_healthy else "blue"
                return f"[{color}]{current:,.0f} {trend_icon}[/]"
            
            table.add_row("지수 추세", f"KOSPI: {get_stat_msg(kospi_stat)} / KOSDAQ: {get_stat_msg(kosdaq_stat)}")
            
            # [추가] 필터링 보류 개수 표시
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            is_healthy_k = kospi_stat.get('is_healthy', True) if isinstance(kospi_stat, dict) else True
            is_healthy_q = kosdaq_stat.get('is_healthy', True) if isinstance(kosdaq_stat, dict) else True
            
            # [추가] 분석 루프가 돌지 않았을 경우(0건) stock.json 기준으로 실제 보류 대상 개수 산출
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0: skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0: skip_msg.append(f"KOSDAQ {skip_q}종목")
            
            if skip_msg:
                filter_ma = getattr(config, 'MARKET_FILTER_MA', 80)
                filter_band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
                band_txt = f" -{filter_band:g}%" if filter_band else ""
                bear_txt = ", 확정 Bear 해제" if getattr(config, 'MARKET_FILTER_RELEASE_ON_BEAR', False) else ""
                table.add_row("시장 필터링", f"[bold blue]{', '.join(skip_msg)} 매수 보류[/] [dim](SMA {filter_ma}일{band_txt} 이탈{bear_txt})[/]")

        table.add_section()
        
        # [추가] 개별 종목 룰 설정 현황
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [Fix] 가중치 JSON 파싱
        rule_table = None
        rule_summary = None
        
        # 보유 종목 코드 집합 생성 (강조 표시용)
        held_codes = set()
        if holdings:
            for h in holdings:
                if api.safe_int(h.get('hldg_qty')) > 0:
                    held_codes.add(h.get('pdno'))

        if custom_rules:
            rule_summary = f"총 {len(custom_rules)}개 종목 개별 설정됨"
            
            # 별도 테이블로 상세 표시
            rule_table = Table(title="종목별 개별 트레이딩 룰 목록", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            rule_table.add_column("종목명(코드)", justify="left")
            rule_table.add_column("매수(점수/RSI/체결/비대칭)", justify="center")
            rule_table.add_column("청산(익절/TS/RSI/기한)", justify="center")
            rule_table.add_column("리스크(비중/손절)", justify="center")
            rule_table.add_column("가중치", justify="center") # [추가]
            rule_table.add_column("수정일", justify="center", style="dim")
            
            for i, r in enumerate(custom_rules):
                # 보유 중인 종목이면 종목명 강조 (bold cyan)
                name_disp = f"{r['name']}({r['code']})"
                if r['code'] in held_codes:
                    name_disp = f"[bold cyan]{name_disp}[/]"
                
                w_str = "기본"
                if r.get('weights'):
                    w = r['weights']
                    w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

                sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
                ratio_str = config.format_invest_ratio(r.get('invest_ratio'))

                rule_table.add_row(
                    name_disp,
                    f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / {_pkg().rule_value(r, 'buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배",
                    f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
                    f"{ratio_str} / {sl_str}",
                    w_str,
                    r.get('updated_at', '-')
                )
                if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
                    rule_table.add_section()

        # 금일 매매 & 실현 손익 계산 (상단 이동)
        today_profit = 0
        buy_cnt = 0
        sell_cnt = 0

        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            target_account = None
            if config.session.auto_cano:
                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            today_trades = db_manager.db.get_trades(
                start_date=today_str, end_date=today_str, 
                is_sim=False, account=target_account
            )
            
            today_trades_parsed = []
            for r in reversed(today_trades):
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                today_trades_parsed.append(parsed_r)
            
            # 중복 제거 및 정제
            today_trades_refined = self._refine_trade_records(today_trades_parsed)
            
            # [추가] 체결된 내역만 당일 매매 요약에 포함
            today_trades_refined = [r for r in today_trades_refined if "체결" in r.get('order_status', '')]
            
            buy_trades = [x for x in today_trades_refined if x['type'] == 'buy']
            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
            
            buy_cnt = len(buy_trades)
            sell_cnt = len(sell_trades)
            
            # 금일 실현 손익 합산
            for t in sell_trades:
                today_profit += int(t.get('profit_amt') or 0)
                
        except Exception:
            # DB 조회 실패 시 메모리 값 사용 (Fallback)
            buy_cnt = len([x for x in self.trade_records if x['type'] == 'buy'])
            sell_cnt = len([x for x in self.trade_records if x['type'] == 'sell'])
            for r in self.trade_records:
                if r['type'] == 'sell':
                    today_profit += int(r.get('profit_amt') or 0)

        # 3. 자산 현황
        if current_asset is not None:
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
                    
            tot_profit = 0
            tot_pchs = 0
            tot_evlu = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
                    tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
                    tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            color = "[red]" if tot_profit > 0 else ("[blue]" if tot_profit < 0 else "[white]")
            
            table.add_row("증권 매입 금액", f"{tot_pchs:,}원")
            table.add_row("증권 평가 금액", f"{tot_evlu:,}원")
            table.add_row("증권 평가 손익", f"{color}{tot_profit:+,}원 ({rate:+.2f}%)[/]")
            table.add_row("주문 가능 금액", f"{deposit:,}원")
            
            table.add_section()

            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                
                _base = self.daily_pnl_base()
                daily_profit = int(round(current_asset - _base))   # 기준선은 float 다 (원 단위로 표시)
                daily_profit_rate = (daily_profit / _base) * 100 if _base > 0 else 0.0
                dp_color = "[red]" if daily_profit > 0 else ("[blue]" if daily_profit < 0 else "[white]")
                table.add_row("오늘 현재 손익",
                              f"{dp_color}{daily_profit:+,}원 ({daily_profit_rate:+.2f}%)[/]"
                              f"{self.transfer_note()}")
                
                realized_rate = (today_profit / _base) * 100 if _base > 0 else 0.0
                rp_color = "[red]" if today_profit > 0 else ("[blue]" if today_profit < 0 else "[white]")
                table.add_row("오늘 실현 손익", f"{rp_color}{today_profit:+,}원 ({realized_rate:+.2f}%)[/]")
            else:
                table.add_row("오늘 시작 자산", "- (미설정)")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                table.add_row("오늘 현재 손익", "-")
                table.add_row("오늘 실현 손익", "-")
        else:
            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")

        table.add_section()

        # 4. 설정 및 상태 정보 (재구성)
        # 매수 조건
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        buy_ask_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
        auto_adj = "ON" if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else "OFF"
        if config.session.is_toss:
            table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 매도잔량비 {buy_ask_ratio}배↑ (체결강도 미제공→매도잔량비 대체)")
        else:
            table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 체결강도 {buy_vol}%↑ / 비대칭 {buy_ask_ratio}배↑ (자동연동: {auto_adj})")

        # [추가] 역추세 매수 표시
        use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False)
        mr_status = "[green]ON[/]" if use_mr else "[red]OFF[/]"
        mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
        table.add_row("", f"역매수 (RSI {mr_rsi}↓ / 20일선 이격도 {mr_disp}%↓ / 체결 {mr_vol}%↑) {mr_status}")

        # 매도 조건
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        ts_act = _pkg().ts_activation_label()
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
        
        use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]
        time_stop_min = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0)

        # [수정] 0=미사용 규칙은 OFF로 명시 (활성 조건처럼 보이던 표시 모순 해소)
        overheat_str = f"과열 매도 (RSI {tp_rsi} 초과)" if tp_rsi > 0 else "과열 매도 [red]OFF[/]"
        table.add_row("매도 조건", f"추세이탈 ({sell_score}점 미만 + 60일선 이탈) / {overheat_str}")

        # 익절 / 반익절
        if tp > 0:
            half_tp_status = "[green]ON[/]" if use_half_tp else "[red]OFF[/]"
            tp_str = f"익절 (+{tp}%) / 반익절 (+{tp/2:.1f}%, 50%) {half_tp_status}"
        else:
            tp_str = "익절/반익절 [red]OFF[/] (추세추종: 트레일링 스탑 주청산)"
        table.add_row("", tp_str)
        
        # ATR손절 / 고정손절
        atr_status = "[green]ON[/]" if use_atr else "[red]OFF[/]"
        sl_str = f"ATR손절 (x{atr_mult}) {atr_status}"
        fixed_sl_status = "[red]OFF[/]" if use_atr else "[green]ON[/]"
        sl_str += f" / 고정손절 ({sl}%) {fixed_sl_status}"
        table.add_row("", sl_str)

        time_stop_status = "[green]ON[/]" if use_time_stop else "[red]OFF[/]"
        table.add_row("", f"시간청산 ({time_stop_days}일 경과 & 수익률 +{time_stop_min}% 미만) {time_stop_status}")
        
        ts_atr_mult = config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.5)
        act_mult = _pkg().ts_activation_atr_mult()
        if "손익분기" in ts_act:
            ts_act = f"동적 손익분기 (ATR x{act_mult})"
            
        table.add_row("", f"샹들리에 트레일링스탑 (발동: {ts_act} / 이탈: 고점대비 ATR x{ts_atr_mult}, 하한 -{ts_call}%)")

        # 투자 설정
        max_holdings = config.SYSTEM_MAX_HOLDINGS
        include_etf = getattr(config, 'SYSTEM_INCLUDE_ETF', False)
        etf_str = "포함" if include_etf else "제외"
        table.add_row("투자 설정", f"비중 {config.format_invest_ratio()} (최대 {max_holdings}종목, ETF {etf_str})")

        # 손실 제한
        loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
        if loss_limit > 0:
            safety_msg = "[green]안전[/green]"
            if current_asset is not None and self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                
                if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
            table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg})")
        else:
            table.add_row("손실 제한", "미사용")

        # 연속 에러는 아래 운영 관제 섹션의 'Kill Switch' 행이 자동매매·체결 감시를
        # 함께 보여 주므로 여기서 중복 출력하지 않는다.
        table.add_row("오늘 매매", f"[red]매수 {buy_cnt}건[/] / [blue]매도 {sell_cnt}건[/]")
        
        if rule_summary:
            table.add_section()
            table.add_row("개별 룰 설정", rule_summary)

        # 운영 관제는 별도 표가 아닌 상태 표의 마지막 섹션으로 이어서 보여 준다.
        # 기존 상태/설정 정보와 관제 데이터를 수평 구분선으로 명확히 나눈다.
        table.add_section()
        # 표 상단에 이미 출력한 항목(실행 시간)은 관제 섹션에서 생략한다.
        self._add_health_rows(table, skip_labels={"실행 시간"} if (self.is_running and self.start_time) else ())

        console.print(table)
        
        if rule_table:
            console.print()
            console.print(rule_table)
            console.print()
        
        # [추가] 보유 종목 리스트 출력
        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

        if valid_holdings:
            holding_rows = []
            # [추가] 시장 구분 등 추가 정보를 가져오는 지연 시간에 대응하기 위한 프로그레스 바 적용
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]보유 종목 세부 정보 조회 중...[/cyan]", total=len(valid_holdings))
                for item in valid_holdings:
                    name = item['prdt_name']
                    code = item['pdno']
                    market_type = self._get_stock_market_type(code)
                    #  [Fix 2026-09-06] 하드 서브스크립트는 한 종목의 빈 값 하나로 **표 전체**를 죽인다.
                    #   (배치 49 에서 '그 줄이 깨질 뿐'이라 판단했던 것은 틀렸다 — 루프를 감싸는
                    #    행 단위 try 가 없어 예외가 표 밖으로 나간다.) 안전 변환으로 바꾼다.
                    qty = api.safe_int(item.get('hldg_qty'))
                    buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                    cur_price = api.safe_int(item.get('prpr'))
                    profit = api.safe_int(item.get('evlu_pfls_amt'))
                    rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)
                    holding_rows.append((name, code, market_type, qty, buy_price, cur_price, profit, rate))
                    progress.advance(task)

            console.print()
            h_table = Table(title="보유 종목 리스트", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            h_table.add_column("종목명(코드)", justify="left")
            h_table.add_column("시장", justify="center")
            h_table.add_column("수량", justify="right")
            h_table.add_column("매입가", justify="right")
            h_table.add_column("현재가", justify="right")
            h_table.add_column("평가손익", justify="right")
            h_table.add_column("수익률", justify="right")
            
            for name, code, market_type, qty, buy_price, cur_price, profit, rate in holding_rows:
                p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                h_table.add_row(
                    f"{name}({code})", 
                    market_type,
                    f"{qty:,}주", 
                    f"{buy_price:,.0f}원",
                    f"{cur_price:,}원", 
                    f"{p_color}{profit:+,}원[/]", 
                    f"{p_color}{rate:+.2f}%[/]"
                )
            console.print(h_table)
        else:
            console.print("\n[dim]현재 보유 중인 종목이 없습니다.[/dim]")

        console.print()

    def print_report(self, target_account=None):
        menu_items = [
            ("1", "일간 (오늘)", "Daily"),
            ("2", "주간 (최근 7일)", "Weekly"),
            ("3", "월간 (최근 30일)", "Monthly"),
            ("4", "기간 직접 입력", "Custom Days")
        ]
        choice = utils.show_menu("시스템 트레이딩 평가 리포트 (Trading Report)", menu_items, default_choice="4")
        if choice.lower() == 'q': return False
        
        menu_map = {"1": "일간", "2": "주간", "3": "월간", "4": "직접 입력"}
        if choice in menu_map:
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
            
        days = None
        if choice == "1": days = 0
        elif choice == "2": days = 7
        elif choice == "3": days = 30
        elif choice == "4":
            utils.print_breadcrumb()
            val = Prompt.ask("조회할 기간(일) 입력 [dim](Enter: 전체 내역, 이전: b, 메인: q)[/dim]", default="")
            console.print()
            if val.lower() in ['b', 'q']: return False
            
            if val.strip() and val.isdigit():
                days = int(val)
                context.USER_ACTION_BREADCRUMB.append(f"[{days}일]")
            else:
                days = None # 전체 내역
                context.USER_ACTION_BREADCRUMB.append("[전체]")

        # [추가] 토스 등 체결감시 미가동 상태에서도 평가 전에 당일 체결을 DB에 동기화한다.
        # (토스 CLOSED 주문 = 체결 데이터. 수동 주문 체결이 누락되어 리포트가 비던 문제 해결)
        if config.session.is_toss:
            try:
                _pkg().ConclusionMonitor()._check_conclusions()
            except Exception as e:
                logger.debug(f"[Report] 토스 체결 동기화 실패: {e}")

        self._load_trade_records(days=days, target_account=target_account)

        if not self.trade_records:
            console.print("\n[yellow]선택한 기간에 해당하는 매매 기록이 없습니다.[/yellow]")
            return
            
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]리포트 통계 분석 및 시장 데이터 수집 중...[/cyan]", total=None)
            
            stats = self._calculate_statistics()
            
            # [추가] 자산 증감 및 시장 성과 통계 추가
            now = datetime.now()
            end_dt = now.strftime("%Y-%m-%d")
            
            if days is not None:
                start_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            else:
                start_dt = self.trade_records[0]['time'][:10] if self.trade_records else end_dt
                
            if target_account:
                # 토스 계좌번호엔 '-'가 여러 개라(예: 189-01-501685-) 마지막 '-' 기준으로 분리
                target_cano, acnt = target_account.rsplit('-', 1)
            else:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                if not target_cano:
                    target_cano = config.session.cano
                    acnt = config.session.acnt_prdt_cd
                    
                target_account = f"{target_cano}-{acnt}"
            
            current_asset = 0
            try:
                with utils.AccountContext(target_cano):
                    asset_data = account.get_asset_status_data(target_cano, acnt)
                    if asset_data:
                        current_asset = asset_data.get('tot_asset', 0)
            except Exception: pass
            
            initial_asset = db_manager.db.get_daily_asset(start_dt, target_account)
            stats['current_asset'] = current_asset
            stats['initial_asset'] = initial_asset
            
            kospi_rate = 0.0
            try:
                kospi_df = analysis.get_domestic_index_data("KOSPI")
                if kospi_df is not None and not kospi_df.empty:
                    s_dt = start_dt.replace('-', '')
                    e_dt = end_dt.replace('-', '')
                    
                    if 'date' in kospi_df.columns:
                        def to_yyyymmdd(x):
                            if hasattr(x, 'strftime'): return x.strftime('%Y%m%d')
                            return str(x).replace('-', '')[:8]
                        
                        dates = kospi_df['date'].apply(to_yyyymmdd)
                        mask = (dates >= s_dt) & (dates <= e_dt)
                        period_df = kospi_df[mask]
                        if not period_df.empty:
                            first_idx = kospi_df.index.get_loc(period_df.index[0])
                            last_idx = kospi_df.index.get_loc(period_df.index[-1])
                            
                            if first_idx > 0:
                                start_val = kospi_df.iloc[first_idx - 1]['close']
                            else:
                                start_val = kospi_df.iloc[first_idx]['close']
                                
                            end_val = kospi_df.iloc[last_idx]['close']
                            if start_val > 0:
                                kospi_rate = ((end_val - start_val) / start_val) * 100
            except Exception: pass
            stats['kospi_rate'] = kospi_rate

            # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
            holdings_summary = None
            try:
                with utils.AccountContext(target_cano):
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_pchs = 0
                    tot_profit = 0
                    tot_evlu = 0
                    
                    if summary and len(summary) > 0:
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt'))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl'))
                        tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt'))
                    
                    if tot_pchs == 0 and holdings:
                        tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in holdings if api.safe_int(h.get('hldg_qty')) > 0)
                        tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in holdings if api.safe_int(h.get('hldg_qty')) > 0)
                        tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in holdings if api.safe_int(h.get('hldg_qty')) > 0)
                    
                    if tot_pchs > 0 or tot_profit != 0:
                        rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
            except Exception: pass

        self._print_summary_table(stats, holdings_summary)
        self._print_current_holdings(target_cano, acnt)
        self._print_stock_details()

    def _load_trade_records(self, days=None, target_account=None):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
                BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]DB에서 매매 내역 조회 및 분석 중...[/cyan]", total=None)
            
            # [수정] DB에서 매매 내역 조회 (수동 매매 포함을 위해 is_auto 필터 제거)
            # 시스템 매매와 수동 매매를 모두 포함하여 평가
            limit = 500
            start_date = None
            
            if days is not None:
                limit = None
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            # [수정] 자동매매 계좌 번호로 필터링 (시스템 트레이딩 내역만 조회)
            if not target_account:
                if config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비 (메모리 필터링)
            try:
                db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date, account=target_account)
            except TypeError:
                db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date)
                if target_account:
                    db_records = [r for r in db_records if r.get('account') == target_account]
            
            # DB 레코드를 내부 포맷으로 변환
            self.trade_records = []
            for r in reversed(db_records): # DB는 최신순이므로 시간순(과거->최신)으로 뒤집음
                # type 파싱: "buy(AUTO)" -> "buy"
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                self.trade_records.append(parsed_r)
            
            # [추가] 중복 제거 및 정제 (시스템 주문과 체결 확인 병합)
            self.trade_records = self._refine_trade_records(self.trade_records)
            
            # [추가] 성과 평가(Report)에서는 체결된 내역만 포함하도록 필터링 (미체결/접수/취소 등 제외)
            self.trade_records = [r for r in self.trade_records if "체결" in r.get('order_status', '')]
            
            # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
            if self.trade_records:
                self.trade_records.sort(key=lambda x: x['time'])

    def get_performance_report(self, days=None):
        """텔레그램용 성과 리포트 생성"""
        # DB에서 조회 (로그 출력 없이)
        # [수정] 수동 매매 포함을 위해 is_auto 필터 제거
        limit = 500
        start_date = None
        period_msg = "전체 (최근 500건)"
        
        if days is not None:
            limit = None
            start_dt = datetime.now() - timedelta(days=days)
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            period_msg = f"{start_date} ~ {end_date}"
        
        # [수정] 자동매매 계좌 번호로 필터링
        target_account = None
        if config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비
        try:
            db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date, account=target_account)
        except TypeError:
            db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date)
            if target_account:
                db_records = [r for r in db_records if r.get('account') == target_account]
        
        temp_records = []
        for r in reversed(db_records):
            type_str = r['type']
            simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
            
            parsed_r = dict(r)
            parsed_r['type'] = simple_type
            temp_records.append(parsed_r)
            
        # [추가] 중복 제거 및 정제
        refined_records = self._refine_trade_records(temp_records)
        
        # [추가] 성과 평가(Report)에서는 체결된 내역만 포함하도록 필터링
        refined_records = [r for r in refined_records if "체결" in r.get('order_status', '')]
        
        # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
        if refined_records:
            refined_records.sort(key=lambda x: x['time'])
            
        msg = "📊 [시스템 트레이딩 성과 리포트]\n"

        if not refined_records:
            msg += f"기간: {period_msg}\n\n"
            msg += "매매 기록이 없습니다."
            return msg
            
        stats = self._calculate_statistics(refined_records)
        
        # [추가] 기간 정보
        if refined_records:
            start_date = refined_records[0]['time'][:10]
            end_date = refined_records[-1]['time'][:10]
            msg += f"기간: {start_date} ~ {end_date}\n\n"
        
        # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
        holdings_summary = None
        try:
            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                tot_pchs = 0
                tot_profit = 0
                tot_evlu = 0
                
                # [수정] API 요약 데이터 대신 보유 종목 합산
                if holdings:
                    valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                    if valid_holdings:
                        tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
                        tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
                        tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                
                if tot_pchs > 0 or tot_profit != 0:
                    rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                    holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
        except Exception: pass
        
        if stats['sell_trades_exist']:
            win_rate = stats['win_rate']
            total_profit = stats['total_profit']
            avg_profit_rate = stats['avg_profit_rate']
            
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += f"승률: {win_rate:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)\n"
            msg += f"건당 평균 수익률: {avg_profit_rate:+.2f}%\n"
            msg += f"건당 평균 보유: {stats['avg_holding_str']}\n"
            total_realized_rate = stats.get('total_realized_rate', 0.0)
            msg += f"총 실현 손익: {total_profit:+,}원 (매매원금 대비 {total_realized_rate:+.2f}%)\n"
            
            msg += f"\n[최고 / 최다 손익]\n"
            if stats.get('best_trade'):
                b = stats['best_trade']
                msg += f"최고 수익: {b['name']} ({b['profit_amt']:+,}원 / {b['profit_rate']:+.2f}%)\n"
            if stats.get('worst_trade'):
                w = stats['worst_trade']
                msg += f"최다 손실: {w['name']} ({w['profit_amt']:+,}원 / {w['profit_rate']:+.2f}%)\n"
            
            msg += f"\n[매수 사유 분포]\n"
            buy_reasons = stats.get('buy_reasons', {})
            total_buys = stats['buy_count']
            if total_buys > 0:
                for r, count in buy_reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_buys*100:.1f}%)\n"
            else:
                msg += "• 매수 내역 없음\n"

            msg += f"\n[매도 사유 분포]\n"
            reasons = stats.get('sell_reasons', {})
            total_sells = stats['sell_count']
            if total_sells > 0:
                for r, count in reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_sells*100:.1f}%)\n"
            else:
                msg += "• 매도 내역 없음\n"
                    
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
        else:
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += "(청산된 내역이 없어 수익률 산출 불가)\n"
            
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
            
        return msg.strip()

    def _calculate_statistics(self, records=None):
        """성과 통계 — performance.calculate_statistics 에 위임(기간 밖 매수 기록은 DB 에서)."""
        if records is None: records = self.trade_records
        return performance.calculate_statistics(
            records, past_trades_of=lambda code: db_manager.db.get_trades(code=code, limit=100))

    def _print_summary_table(self, stats, holdings_summary=None):
        summary_table = Table(title="트레이딩 성과 요약", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        # [추가] 조회 기간 표시
        period_str = "전체"
        if getattr(self, 'trade_records', None) and len(self.trade_records) > 0:
            start_date = self.trade_records[0]['time'][:10]
            end_date = self.trade_records[-1]['time'][:10]
            period_str = f"{start_date} ~ {end_date}"
            
        summary_table.add_row("조회 기간", period_str)
        summary_table.add_row("총 매매 실행", f"{stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})")
        
        if stats['sell_trades_exist']:
            summary_table.add_row("승률 (Win Rate)", f"{stats['win_rate']:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)")
            
            # [추가] 시작 자산 및 현재 자산 비교 표시
            initial_asset = stats.get('initial_asset', 0)
            current_asset = stats.get('current_asset', 0)
            if initial_asset and current_asset > 0:
                asset_profit = current_asset - initial_asset
                asset_roi = (asset_profit / initial_asset) * 100
                summary_table.add_row("총 계좌 시작 자산", f"{int(initial_asset):,}원")
                summary_table.add_row("총 계좌 현재 자산", f"{current_asset:,}원")
                ap_color = "[red]" if asset_profit > 0 else ("[blue]" if asset_profit < 0 else "[white]")
                summary_table.add_row("총 계좌 자산 증감", f"{ap_color}{int(asset_profit):+,}원 ({asset_roi:+.2f}%)[/]")
                
            tp = stats['total_profit']
            tr_rate = stats.get('total_realized_rate', 0.0)
            summary_table.add_row("총 실현 손익", f"[red]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]" if tp > 0 else f"[blue]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]")
            
            total_buy = stats.get('total_buy_amt_for_sell', 0)
            sec_pl = holdings_summary['tot_profit'] if holdings_summary else 0
            sec_buy = holdings_summary['tot_pchs'] if holdings_summary else 0
            total_invested = total_buy + sec_buy
            total_net_profit = tp + sec_pl
            
            strategy_roi = 0.0
            if total_invested > 0:
                strategy_roi = (total_net_profit / total_invested) * 100
            
            sp_color = "[red]" if total_net_profit > 0 else ("[blue]" if total_net_profit < 0 else "[white]")
            summary_table.add_row("현재 전략 손익", f"{sp_color}{total_net_profit:+,}원 (실현+평가 손익 {strategy_roi:+.2f}%)[/]")

            apr = stats['avg_profit_rate']
            summary_table.add_row("건당 평균 수익률", f"[red]{apr:+.2f}%[/]" if apr > 0 else f"[blue]{apr:+.2f}%[/]")
            summary_table.add_row("건당 평균 보유", stats['avg_holding_str'])
            
            # [추가] 시장 대비 성과 표시
            kospi_rate = stats.get('kospi_rate', 0.0)
            k_color = "[red]" if kospi_rate > 0 else ("[blue]" if kospi_rate < 0 else "[white]")
            market_perf_str = f"코스피 지수: {k_color}{kospi_rate:+.2f}%[/]"

            if total_invested > 0:
                alpha = strategy_roi - kospi_rate
                a_color = "[red]" if alpha > 0 else ("[blue]" if alpha < 0 else "[white]")
                # [수정] 가독성 향상을 위해 초과/부진 여부 명시
                if alpha > 0:
                    alpha_label = "시장 대비 초과 수익 (Outperform)"
                else:
                    alpha_label = "시장 대비 성과 (Underperform)"
                market_perf_str += f" / {alpha_label}: {a_color}{alpha:+.2f}%[/]"
            summary_table.add_row("시장 대비 성과", market_perf_str)
        
        if holdings_summary:
            summary_table.add_section()
            summary_table.add_row("총 매입금액", f"{holdings_summary['tot_pchs']:,}원")
            summary_table.add_row("총 평가금액", f"{holdings_summary['tot_evlu']:,}원")
            hp = holdings_summary['tot_profit']
            hr = holdings_summary['rate']
            hc = "[red]" if hp > 0 else ("[blue]" if hp < 0 else "[white]")
            summary_table.add_row("총 평가손익", f"{hc}{hp:+,}원 ({hr:+.2f}%)[/]")
            
        console.print(summary_table)

    def _print_current_holdings(self, target_cano=None, target_acnt=None):
        try:
            # 컨텍스트 설정 (시스템 트레이딩 계좌 조회)
            if not target_cano:
                target_cano = config.session.auto_cano
                target_acnt = config.session.auto_acnt_prdt_cd
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, target_acnt)
                
                if holdings:
                    console.print()
                    h_table = Table(title="현재 보유 종목 현황", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
                    h_table.add_column("종목명(코드)", justify="left")
                    h_table.add_column("보유수량", justify="right")
                    h_table.add_column("매입단가", justify="right")
                    h_table.add_column("현재가", justify="right")
                    h_table.add_column("평가손익", justify="right")
                    h_table.add_column("수익률", justify="right")
                    
                    for item in holdings:
                        name = item['prdt_name']
                        code = item['pdno']
                        qty = api.safe_int(item.get('hldg_qty'))
                        buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                        cur_price = api.safe_int(item.get('prpr'))
                        profit = api.safe_int(item.get('evlu_pfls_amt'))
                        rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)
                        
                        p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                        
                        h_table.add_row(f"{name}({code})", f"{qty:,}주", f"{buy_price:,.0f}원", f"{cur_price:,}원", f"{p_color}{profit:+,}원[/]", f"{p_color}{rate:+.2f}%[/]")
                    console.print(h_table)
        except Exception: pass

    def _print_stock_details(self):
        stock_stats = {}
        buy_times_per_stock = {} # 종목별 매수 시간 추적 (FIFO)

        # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
        filtered_records = self.trade_records

        for r in filtered_records:
            code = r['code']
            if code not in stock_stats:
                stock_stats[code] = {
                    'name': r['name'], 
                    'buy': 0, 'sell': 0, 
                    'profit': 0, 'rates': [], 'wins': 0,
                    'reasons': [], # 매도 사유 리스트
                    'holding_secs': [], # 보유 기간 리스트
                    'max_rate': -999.0, 'min_rate': 999.0,
                    'total_buy_amt': 0 # [추가] 총 매수 금액
                }
            if code not in buy_times_per_stock:
                buy_times_per_stock[code] = []
            
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except Exception: dt = datetime.now()
            
            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
                stock_stats[code]['total_buy_amt'] += int(float(r.get('price', 0) or 0) * float(r.get('qty', 0) or 0)) # [추가] 매수 금액 누적
                buy_times_per_stock[code].append(dt)
            elif r['type'] == 'sell':
                stock_stats[code]['sell'] += 1
                p = r.get('profit_amt', 0)
                rate = r.get('profit_rate', 0.0)
                
                stock_stats[code]['profit'] += p
                stock_stats[code]['rates'].append(rate)
                if p > 0: stock_stats[code]['wins'] += 1
                
                # 사유 분석 (익절/손절/추세 등 키워드 추출)
                reason_raw = r.get('reason', '')
                reason_simple = "기타"
                if "익절" in reason_raw: reason_simple = "익절"
                elif "손절" in reason_raw: reason_simple = "손절"
                elif "추세" in reason_raw: reason_simple = "추세이탈"
                stock_stats[code]['reasons'].append(reason_simple)
                
                # 최대/최소 수익률 갱신
                if rate > stock_stats[code]['max_rate']: stock_stats[code]['max_rate'] = rate
                if rate < stock_stats[code]['min_rate']: stock_stats[code]['min_rate'] = rate
                
                # 보유 기간 계산 (FIFO)
                if buy_times_per_stock[code]:
                    buy_dt = buy_times_per_stock[code].pop(0)
                    hold_sec = (dt - buy_dt).total_seconds()
                    stock_stats[code]['holding_secs'].append(hold_sec)

        if stock_stats:
            console.print()
            s_table = Table(title="종목별 성과 분석", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            s_table.add_column("종목명(코드)", justify="left")
            s_table.add_column("매매(매수/매도)", justify="center")
            s_table.add_column("승률", justify="right")
            s_table.add_column("총 손익", justify="right")
            s_table.add_column("평균 수익률", justify="right")
            # [추가] 상세 정보 컬럼
            s_table.add_column("최대/최소", justify="right")
            s_table.add_column("주요 사유", justify="center")
            s_table.add_column("평균 보유", justify="right")

            for i, (code, stat) in enumerate(stock_stats.items()):
                s_cnt = stat['sell']
                win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
                avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0
                
                p_color = "[red]" if stat['profit'] > 0 else ("[blue]" if stat['profit'] < 0 else "[white]")
                r_color = "[red]" if avg_rate > 0 else ("[blue]" if avg_rate < 0 else "[white]")
                
                # 최대/최소 수익률 포맷팅
                #  색상은 '최대/최소'라는 자리가 아니라 값의 부호를 따른다(+는 빨강, -는 파랑).
                #  자리 고정 색상은 전 구간 손실 종목의 최대 수익률(-4.9%)까지 빨강으로 보여
                #  같은 표의 총 손익·평균 수익률 색상과 어긋났다.
                max_r = stat['max_rate'] if stat['max_rate'] != -999.0 else 0.0
                min_r = stat['min_rate'] if stat['min_rate'] != 999.0 else 0.0
                max_c = "[red]" if max_r > 0 else ("[blue]" if max_r < 0 else "[white]")
                min_c = "[red]" if min_r > 0 else ("[blue]" if min_r < 0 else "[white]")
                range_str = f"{max_c}{max_r:+.1f}%[/] / {min_c}{min_r:+.1f}%[/]" if s_cnt > 0 else "-"
                
                # 주요 매도 사유 (최빈값)
                reason_str = "-"
                if stat['reasons']:
                    c = Counter(stat['reasons'])
                    most_common = c.most_common(1)[0] # (사유, 횟수)
                    reason_str = f"{most_common[0]}({most_common[1]}회)"
                
                # 평균 보유 시간 포맷팅
                hold_str = "-"
                if stat['holding_secs']:
                    avg_sec = sum(stat['holding_secs']) / len(stat['holding_secs'])
                    if avg_sec < 60: hold_str = f"{int(avg_sec)}초"
                    elif avg_sec < 3600: hold_str = f"{int(avg_sec//60)}분"
                    else: hold_str = f"{int(avg_sec//3600)}시간"
                
                s_table.add_row(
                    f"{stat['name']} ({code})",
                    f"{stat['buy']} / {stat['sell']}",
                    f"{win_rate:.1f}%",
                    f"{p_color}{stat['profit']:+,}원[/]",
                    f"{r_color}{avg_rate:+.2f}%[/]",
                    range_str,
                    reason_str,
                    hold_str
                )
                
                # [추가] 5개마다 실선 추가
                if (i + 1) % 5 == 0 and (i + 1) < len(stock_stats):
                    s_table.add_section()
            console.print(s_table)
        
        # 상세 내역 테이블
        console.print()
        detail_table = Table(title="상세 매매 내역 (최신순)", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        detail_table.add_column("시간", justify="center")
        detail_table.add_column("구분", justify="center")
        detail_table.add_column("종목명", justify="left")
        detail_table.add_column("수량", justify="right")
        detail_table.add_column("단가", justify="right")
        detail_table.add_column("매매금액", justify="right") # [추가]
        detail_table.add_column("손익(수익률)", justify="right")
        detail_table.add_column("사유", justify="left")
        
        # [수정] 필터링된 레코드 사용 (최신순 정렬)
        records = list(reversed(filtered_records))
        
        for i, r in enumerate(records):
            type_str = "[red]매수[/]" if r['type'] == 'buy' else "[blue]매도[/]"
            
            # [수정] 단가 포맷팅 (시장가 0원 처리)
            price_val = float(r.get('price', 0) or 0)
            qty_val = float(r.get('qty', 0) or 0)
            if price_val <= 0:
                price_str = "시장가"
                amt_str = "-"
            else:
                if price_val.is_integer():
                    price_str = f"{int(price_val):,}"
                else:
                    price_str = f"{price_val:,.2f}"
                
                trade_amt = int(price_val * qty_val)
                amt_str = f"{trade_amt:,}"
            
            profit_display = "-"
            reason_display = r.get('reason', '-')

            if r['type'] == 'sell':
                p_amt = r.get('profit_amt', 0)
                p_rate = r.get('profit_rate', 0.0)
                color = "[red]" if p_amt > 0 else "[blue]"
                profit_display = f"{color}{p_amt:+,}원 ({p_rate:+.2f}%)[/]"
            
            detail_table.add_row(
                r['time'][5:], # MM-DD HH:MM:SS
                type_str,
                f"{r['name']}",
                f"{r['qty']}",
                price_str,
                amt_str, # [수정]
                profit_display,
                reason_display
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(records):
                detail_table.add_section()
            
        console.print(detail_table)

    def view_log_file(self):
        """현재 날짜의 시스템 트레이딩 로그 파일을 실시간으로 출력합니다."""
        utils.clear_screen()
        utils.print_breadcrumb()
        
        log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
        filename = "autotrade.log" # [수정] 고정 파일명 사용
        filepath = os.path.join(log_dir, filename)

        # [추가] 파일이 생성될 때까지 잠시 대기 (최대 10초) - 부팅 직후 실행 시 필요
        for _ in range(10):
            if os.path.exists(filepath): break
            time.sleep(1)

        if not os.path.exists(filepath):
            console.print(f"\n[yellow]로그 파일({filename})이 없습니다.[/yellow]")
            return

        console.print(f"\n[bold cyan]━━━ 실시간 로그 모니터링 ({filename}) ━━━[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]로그 파일 로딩 중...[/cyan]", total=None)

        f = None
        try:
            f = open(filepath, 'r', encoding='utf-8')
            # 초기 출력: 최근 50줄
            lines = f.readlines()
            for line in lines[-50:]:
                console.print(escape(line.strip()))
            
            # 현재 파일의 inode 저장 (파일 교체 감지용)
            current_inode = os.fstat(f.fileno()).st_ino
            
            # 실시간 모니터링
            while True:
                # [추가] 로그 뷰어 실행 중에도 토큰 만료 체크 및 갱신 수행
                api.check_and_refresh_token_if_expired()

                line = f.readline()
                if line:
                    console.print(escape(line.strip()))
                else:
                    time.sleep(0.1)
                    # 파일 교체(로테이션) 감지
                    try:
                        if os.path.exists(filepath):
                            new_inode = os.stat(filepath).st_ino
                            if new_inode != current_inode:
                                # 파일이 교체됨 (자정 로테이션 등)
                                f.close()
                                f = open(filepath, 'r', encoding='utf-8')
                                current_inode = new_inode
                                console.print("\n[dim yellow]>>> 로그 파일이 교체되었습니다 (Log Rotation) <<<[/dim yellow]\n")
                    except Exception:
                        pass
        except KeyboardInterrupt:
            console.print("\n[yellow]로그 모니터링을 종료합니다.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]로그 파일 읽기 오류: {e}[/red]")
        finally:
            if f and not f.closed:
                f.close()

    def _get_holdings_message(self, target_cano):
        """보유 종목 현황 메시지 생성 (장 시작/마감 알림용)"""
        msg = ""
        try:
            acnt = config.session.auto_acnt_prdt_cd
            holdings, _ = api.get_domestic_balance(target_cano, acnt)
            
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

            if valid_holdings:
                from modules import account
                analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
            else:
                msg += "\n\n📋 [보유 종목] 없음"
        except Exception as e:
            logger.error(f"보유 종목 조회 실패: {e}")
            msg += "\n\n(보유 종목 조회 실패)"
            
        return msg
