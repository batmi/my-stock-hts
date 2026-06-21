import time
import threading
import logging
from datetime import datetime
import json
import api
import utils
from modules import analysis
import indicators
from modules import db_manager
import config

logger = logging.getLogger(__name__)

class ReservedOrderMonitor:
    """백그라운드 예약 주문(Stop, Limit, Breakout, Time) 감시 스레드"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ReservedOrderMonitor, cls).__new__(cls)
            cls._instance.is_running = False
            cls._instance.monitor_thread = None
            cls._instance.chart_cache = {} # {code: {'df': df, 'time': timestamp}}
        return cls._instance

    def start(self):
        if self.is_running: return
        self.is_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="ReservedOrderMonitor")
        self.monitor_thread.start()
        logger.info("[Reserve] 예약 주문 3초 주기 감시 스레드 시작")

    def stop(self):
        self.is_running = False
        
    def _monitor_loop(self):
        while self.is_running:
            try:
                self._check_orders()
            except Exception as e:
                logger.error(f"[Reserve] 예약 주문 감시 에러: {e}")
            time.sleep(10.0)  # 스윙 투자에 적합한 서버 최적화 주기 (10초)
            
    def _check_orders(self):
        try:
            pending_orders = db_manager.db.get_pending_reserved_orders()
        except AttributeError:
            return # DB 미구현 상태이면 스킵
            
        if not pending_orders: return
            
        now = datetime.now()
        now_time_str_short = now.strftime("%H%M")
        now_time_str_full = now.strftime("%Y%m%d%H%M")
        today_str = now.strftime("%Y%m%d")
        current_prices = {}
        
        # 차트 조회가 필요한 종목 식별 (RATE LIMIT 방어용 캐시 예열)
        chart_required_codes = set()
        for order in pending_orders:
            if order['condition_type'] in ['SCORE_UP', 'SCORE_DOWN', 'RSI_UP', 'RSI_DOWN', 'EMA_UP', 'EMA_DOWN',
                                           'STATE_STRONGBUY', 'STATE_BUY', 'STATE_MR', 'NEW_HIGH', 'COMPOSITE']:
                code = order['code']
                now_ts = time.time()
                # 캐시가 1시간 지났거나 없으면 업데이트
                if code not in self.chart_cache or (now_ts - self.chart_cache[code]['time']) > 3600:
                    chart_required_codes.add((code, order['market'] == 'US'))
                    
        # 필요한 차트 데이터 갱신
        for code, is_ovs in chart_required_codes:
            df = api.get_chart_data(code, is_overseas=is_ovs)
            if df is not None and not df.empty:
                self.chart_cache[code] = {'df': df, 'time': time.time()}
            time.sleep(0.3)
        
        for order in pending_orders:
            code, condition_type = order['code'], order['condition_type']
            target_price = float(order.get('target_price', 0.0))
            order_type = order['order_type']
            is_overseas = (order['market'] == 'US')
            
            expire_dt = order.get('expire_dt')
            if expire_dt and expire_dt != "20991231" and today_str > expire_dt:
                db_manager.db.update_reserved_order_status(order['id'], 'EXPIRED')
                api.send_telegram_message(f"🗑 [예약 주문 만료]\n종목: {order['name']}({order['code']})\n사유: 설정된 유효 기간({expire_dt[:4]}-{expire_dt[4:6]}-{expire_dt[6:8]}) 경과로 자동 취소되었습니다.")
                
                # [추가] 만료(취소) 내역 거래내역에 기록
                t_type = f"{'매수' if order['order_type'] == 'buy' else '매도'}취소(예약)"
                db_manager.db.insert_trade(
                    t_type, order['code'], order['name'], order['qty'], 
                    order.get('order_price', 0), f"RES_EXP_{order['id']}", 
                    order_status="취소", reason="유효 기간 만료로 인한 예약 자동 취소"
                )
                continue

            trigger, reason = False, ""
            
            if condition_type == 'TIME':
                tt = order['target_time']
                if tt:
                    if len(tt) >= 12 and now_time_str_full >= tt[:12]:
                        trigger, reason = True, f"지정 시간({tt}) 도달"
                    elif len(tt) <= 4 and now_time_str_short >= tt:
                        trigger, reason = True, f"지정 시간({tt}) 도달"
            else:
                # [수정] 장 마감 시간대 API 자원 최적화 (시스템 설정 시간 연동)
                start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0800")
                end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "2000")
                
                if not is_overseas and (now_time_str_short >= end_time or now_time_str_short < start_time):
                    continue
                    
                # [추가] 정규장 단일가 매매 동기화를 위한 대체거래소 휴게 시간은 발동 스킵
                if not is_overseas and (("0850" <= now_time_str_short < "0900") or ("1525" <= now_time_str_short < "1530")):
                    continue
                # 해외 주식은 한국 시간 기준 주간(08:00 ~ 16:00)에 감시 생략 (서머타임 넉넉히 고려)
                if is_overseas and ("0800" <= now_time_str_short < "1600"):
                    continue

                if code not in current_prices:
                    current_prices[code] = api.get_current_price(code, is_overseas)
                curr_price = current_prices[code]
                if curr_price <= 0: continue
                
                if condition_type in ['SCORE_UP', 'SCORE_DOWN', 'RSI_UP', 'RSI_DOWN', 'EMA_UP', 'EMA_DOWN']:
                    cached = self.chart_cache.get(code)
                    if cached:
                        # [핵심] 원본 캐시를 복사 후 마지막 행(오늘)의 종가만 현재가로 교체하여 지표 계산
                        df = cached['df'].copy()
                        df.iloc[-1, df.columns.get_loc('close')] = curr_price
                        ind = indicators.calculate_indicators(df)
                        
                        if 'SCORE' in condition_type:
                            custom_rule = db_manager.db.get_stock_strategy(code)
                            weights = config.SCORING_WEIGHTS
                            if custom_rule and custom_rule.get('weights'):
                                try:
                                    w_data = custom_rule['weights']
                                    if isinstance(w_data, str): weights = json.loads(w_data)
                                    elif isinstance(w_data, dict): weights = w_data
                                except: pass
                            sm_flag, _ = analysis.check_smart_money_turnaround(code, is_overseas=is_overseas)
                            score, _ = analysis.calculate_score(df=df, ind=ind, weights=weights, smart_money=sm_flag)
                            
                            if condition_type == 'SCORE_UP' and score >= target_price:
                                trigger, reason = True, f"목표 점수 도달 ({score}점 >= {target_price}점)"
                            elif condition_type == 'SCORE_DOWN' and score <= target_price:
                                trigger, reason = True, f"목표 점수 하락 ({score}점 <= {target_price}점)"
                        elif 'RSI' in condition_type:
                            rsi_val = ind.get('rsi')
                            if rsi_val is not None:
                                if condition_type == 'RSI_UP' and rsi_val >= target_price:
                                    trigger, reason = True, f"RSI 도달 ({rsi_val:.1f} >= {target_price})"
                                elif condition_type == 'RSI_DOWN' and rsi_val <= target_price:
                                    trigger, reason = True, f"RSI 하락 ({rsi_val:.1f} <= {target_price})"
                        elif 'EMA' in condition_type:
                            ema_key = f"ema_{int(target_price)}"
                            ema_val = ind.get(ema_key)
                            if ema_val is not None:
                                if condition_type == 'EMA_UP' and curr_price >= ema_val:
                                    trigger, reason = True, f"EMA {int(target_price)}선 상향돌파 (현재가: {curr_price:,.2f})"
                                elif condition_type == 'EMA_DOWN' and curr_price <= ema_val:
                                    trigger, reason = True, f"EMA {int(target_price)}선 하향이탈 (현재가: {curr_price:,.2f})"

                elif condition_type in ('SMART_MONEY', 'STATE_STRONGBUY', 'STATE_BUY', 'STATE_MR', 'NEW_HIGH', 'COMPOSITE'):
                    # [차별화 조건] 우리 시스템 고유 엔진(수급/상태/신고가/복합)을 트리거로 활용
                    df, ind = self._get_indicators_for(code, curr_price)  # SMART_MONEY는 ind 불필요
                    ctx = {'curr_price': curr_price, 'df': df, 'ind': ind, 'code': code,
                           'is_overseas': is_overseas, 'now_hhmm': now_time_str_short}

                    if condition_type == 'SMART_MONEY':
                        if self._eval_atomic('SMART_MONEY', None, ctx):
                            trigger, reason = True, "스마트머니(외국인/기관) 순매수 전환 포착"
                    elif condition_type.startswith('STATE_'):
                        target_state = {'STATE_STRONGBUY': '강매수', 'STATE_BUY': '매수', 'STATE_MR': '역매수'}[condition_type]
                        if self._eval_atomic('STATE', target_state, ctx):
                            trigger, reason = True, f"시스템 상태 '{target_state}' 진입"
                    elif condition_type == 'NEW_HIGH':
                        if self._eval_atomic('NEW_HIGH', target_price, ctx):
                            hi_label = "사상 최고가" if target_price == 0 else "52주 신고가"
                            trigger, reason = True, f"{hi_label} 경신 (현재가: {curr_price:,.2f})"
                    elif condition_type == 'COMPOSITE':
                        trigger, reason = self._eval_composite(order, ctx)

                elif condition_type == 'TRAILING_BUY':
                    lowest = float(order.get('lowest_price', 0.0))
                    if lowest <= 0.0 or curr_price < lowest:
                        db_manager.db.update_reserved_order_lowest(order['id'], curr_price)
                        lowest = curr_price
                        
                    if lowest > 0:
                        rebound_rate = ((curr_price - lowest) / lowest) * 100
                        if rebound_rate >= target_price:
                            trigger, reason = True, f"바닥({lowest:,.0f}) 대비 {rebound_rate:.1f}% 반등 매수"
                            
                elif condition_type == 'TRAILING_SELL':
                    highest = float(order.get('highest_price', 0.0))
                    if highest <= 0.0 or curr_price > highest:
                        db_manager.db.update_reserved_order_highest(order['id'], curr_price)
                        highest = curr_price
                        
                    if highest > 0:
                        drop_rate = ((highest - curr_price) / highest) * 100
                        if drop_rate >= target_price:
                            trigger, reason = True, f"고점({highest:,.0f}) 대비 {drop_rate:.1f}% 하락 매도"
                    
                if condition_type == 'STOP':
                    if curr_price <= target_price:
                        trigger, reason = True, f"스탑/하향 이탈 (현재가: {curr_price})"
                elif condition_type == 'BREAKOUT':
                    if curr_price >= target_price:
                        trigger, reason = True, f"상향 돌파 (현재가: {curr_price})"
                elif condition_type == 'LIMIT':
                    if (order_type == 'buy' and curr_price <= target_price) or \
                       (order_type == 'sell' and curr_price >= target_price):
                        trigger, reason = True, f"지정가 도달 (현재가: {curr_price})"
                        
            if trigger:
                logger.info(f"[Reserve] 예약 발동: {order['name']} - {reason}")
                self._execute_order(order, reason)
                
    def _get_indicators_for(self, code, curr_price):
        """캐시된 차트에 현재가를 반영하여 지표 계산. (df, ind) 반환 — 캐시 없으면 (None, None)."""
        cached = self.chart_cache.get(code)
        if not cached:
            return None, None
        df = cached['df'].copy()
        df.iloc[-1, df.columns.get_loc('close')] = curr_price
        ind = indicators.calculate_indicators(df)
        return df, ind

    def _compute_score(self, ctx):
        """ctx 기준 퀀트 점수 계산 (개별 룰 가중치/스마트머니 반영)."""
        code = ctx['code']
        custom_rule = db_manager.db.get_stock_strategy(code)
        weights = config.SCORING_WEIGHTS
        if custom_rule and custom_rule.get('weights'):
            try:
                w = custom_rule['weights']
                weights = json.loads(w) if isinstance(w, str) else (w if isinstance(w, dict) else weights)
            except Exception:
                pass
        if '_sm' not in ctx:
            ctx['_sm'], _ = analysis.check_smart_money_turnaround(code, is_overseas=ctx['is_overseas'])
        score, _ = analysis.calculate_score(df=ctx['df'], ind=ctx['ind'], weights=weights, smart_money=ctx['_sm'])
        return round(score, 1)

    def _compute_state(self, ctx):
        """ctx 기준 시스템 상태(강매수/매수/역매수/...) 분류."""
        code = ctx['code']
        df = ctx['df']
        if '_sm' not in ctx:
            ctx['_sm'], _ = analysis.check_smart_money_turnaround(code, is_overseas=ctx['is_overseas'])
        prev_rsi = None
        try:
            if len(df) >= 16:
                delta = df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                prev_rsi = (100 - (100 / (1 + gain / loss))).iloc[-2]
        except Exception:
            pass
        w52_pos = 0.0
        try:
            recent = df.tail(250)
            h52, l52 = recent['high'].max(), recent['low'].min()
            if h52 > l52:
                w52_pos = (ctx['curr_price'] - l52) / (h52 - l52) * 100
        except Exception:
            pass
        state, _, _ = analysis.classify_stock_state(
            df=df, ind=ctx['ind'], prev_rsi=prev_rsi, w52_pos=w52_pos, smart_money=ctx['_sm']
        )
        return state

    def _eval_atomic(self, ctype, value, ctx):
        """원자 조건 1개 평가 → bool. 신규 단일조건과 복합(COMPOSITE) 서브조건이 공유한다."""
        cp = ctx['curr_price']
        ind = ctx.get('ind')

        if ctype == 'SMART_MONEY':
            if '_sm' not in ctx:
                ctx['_sm'], _ = analysis.check_smart_money_turnaround(ctx['code'], is_overseas=ctx['is_overseas'])
            return bool(ctx['_sm'])
        if ctype in ('PRICE_UP', 'PRICE_DOWN'):
            return cp >= value if ctype == 'PRICE_UP' else cp <= value
        if ctype == 'TIME_AFTER':
            now_hhmm = ctx.get('now_hhmm')
            return now_hhmm is not None and value is not None and now_hhmm >= str(value)
        if ctype == 'NEW_HIGH':
            df = ctx.get('df')
            if df is None or 'high' not in df.columns or len(df) < 20:
                return False
            # value(거래일 룩백)>0 이면 직전 N거래일, 아니면 전체기간(사상) 기준. 오늘 봉은 제외하고 직전 최고가와 비교.
            lookback = int(value) if value else len(df)
            prior = df['high'].iloc[-(lookback + 1):-1]
            if prior.empty:
                return False
            prior_high = prior.max()
            return prior_high > 0 and cp >= prior_high

        # 이하 조건은 지표(차트) 필요
        if ind is None:
            return False
        if ctype == 'STATE':
            if '_state' not in ctx:
                ctx['_state'] = self._compute_state(ctx)
            return ctx['_state'] == value
        if ctype in ('SCORE_UP', 'SCORE_DOWN'):
            if '_score' not in ctx:
                ctx['_score'] = self._compute_score(ctx)
            s = ctx['_score']
            return s >= value if ctype == 'SCORE_UP' else s <= value
        if ctype in ('RSI_UP', 'RSI_DOWN'):
            r = ind.get('rsi')
            if r is None:
                return False
            return r >= value if ctype == 'RSI_UP' else r <= value
        if ctype in ('EMA_UP', 'EMA_DOWN'):
            ev = ind.get(f'ema_{int(value)}')
            if ev is None:
                return False
            return cp >= ev if ctype == 'EMA_UP' else cp <= ev
        return False

    def _atomic_label(self, st, sv):
        """복합조건 발동 사유 표기용 라벨."""
        return {
            'SMART_MONEY': '수급전환',
            'STATE': f"상태={sv}",
            'SCORE_UP': f"점수≥{sv}", 'SCORE_DOWN': f"점수≤{sv}",
            'RSI_UP': f"RSI≥{sv}", 'RSI_DOWN': f"RSI≤{sv}",
            'EMA_UP': f"{int(sv) if sv is not None else ''}일선상회", 'EMA_DOWN': f"{int(sv) if sv is not None else ''}일선하회",
            'PRICE_UP': f"가격≥{sv}", 'PRICE_DOWN': f"가격≤{sv}",
            'TIME_AFTER': f"시각≥{str(sv)[:2]}:{str(sv)[2:]}" if sv else "시각",
            'NEW_HIGH': "사상최고가경신" if not sv else "52주신고가경신",
        }.get(st, st)

    def _eval_composite(self, order, ctx):
        """복합(AND) 조건 평가. 모든 서브조건 충족 시 (True, 사유) 반환."""
        raw = order.get('composite_json')
        if not raw:
            return False, ""
        try:
            subs = json.loads(raw)
        except Exception:
            return False, ""
        if not subs:
            return False, ""
        labels = []
        for sub in subs:
            st = sub.get('type')
            sv = sub.get('value')
            if not self._eval_atomic(st, sv, ctx):
                return False, ""
            labels.append(self._atomic_label(st, sv))
        return True, "복합조건 충족: " + " AND ".join(labels)

    def _execute_order(self, order, reason):
        db_manager.db.update_reserved_order_status(order['id'], 'PROCESSING')
        market_str = "domestic" if order['market'] == 'KR' else "overseas"
        
        order_price = float(order['order_price'])
        if order_price == 0:
            if order['market'] == 'KR':
                now_time = datetime.now().strftime("%H%M")
                if ("1530" <= now_time <= "2000") or ("0800" <= now_time <= "0850"):
                    ord_dvsn = "00"
                    curr = api.get_current_price(order['code'], is_overseas=False)
                    if curr > 0:
                        slippage = getattr(config, 'SLIPPAGE_RATE', 0.002)
                        adj_price = curr * (1 + slippage) if order['order_type'] == 'buy' else curr * (1 - slippage)
                        curr = utils.adjust_to_tick(adj_price, is_overseas=False)
                    price_str = str(int(curr)) if curr > 0 else "0"
                else:
                    ord_dvsn = "01"
                    price_str = "0"
            else:
                ord_dvsn = "00"
                curr_price = api.get_current_price(order['code'], is_overseas=True)
                if curr_price > 0:
                    slippage = getattr(config, 'SLIPPAGE_RATE', 0.002)
                    adj_price = curr_price * (1 + slippage) if order['order_type'] == 'buy' else curr_price * (1 - slippage)
                    curr_price = utils.adjust_to_tick(adj_price, is_overseas=True)
                    
                    if curr_price >= 1.0: price_str = f"{curr_price:.2f}"
                    else: price_str = f"{curr_price:.4f}"
                else:
                    price_str = "0"
        else:
            ord_dvsn = "00"
            price_str = str(int(order_price)) if order['market'] == 'KR' else str(order_price)
        
        res = api.place_order(market_str, order['order_type'], order['code'], order['qty'], price_str, ord_dvsn)
        if res.get('rt_cd') == '0':
            odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
            db_manager.db.update_reserved_order_status(order['id'], 'TRIGGERED', odno)
            display_price = "시장가" if order_price == 0 else (f"{int(order_price):,}원" if order['market'] == 'KR' else f"${order_price:,.2f}")
            api.send_telegram_message(f"🔔 [예약 {'매수' if order['order_type']=='buy' else '매도'} 실행]\n종목: {order['name']}({order['code']})\n단가: {display_price}\n조건: {reason}\n주문번호: {odno}")
            
            # [추가] 예약 발동 내역을 거래 내역(trades)에 기록 (접수 상태)
            t_type = f"{'매수' if order['order_type'] == 'buy' else '매도'}(예약)"
            snapshot = analysis.get_snapshot(order['code'], is_overseas=(order['market'] == 'US'))
            db_manager.db.insert_trade(
                t_type, order['code'], order['name'], order['qty'], 
                order_price, odno, snapshot=snapshot, reason=f"예약발동: {reason}"
            )
            
            # [추가] 미체결 추적 및 모의투자 체결 보정을 위해 OrderManager에 등록
            from modules import auto_trade
            trader = auto_trade.AutoTrader()
            if hasattr(trader, 'order_manager'):
                trader.order_manager.register_manual_order(order['code'], odno)
                
            # [추가] 즉각적인 체결 확인을 위해 ConclusionMonitor 트리거
            auto_trade.ConclusionMonitor().check_now()

            # [추가] 해당 종목의 나머지 예약 주문 일괄 취소 및 알림
            canceled_orders = db_manager.db.cancel_other_reserved_orders(order['id'], order['cano'], order['acnt'], order['code'])
            for co in canceled_orders:
                t_type = "매수" if co['order_type'] == 'buy' else "매도"
                cond_str = co['condition_type']
                api.send_telegram_message(f"🗑️ [예약 일괄 취소]\n종목: {co['name']}({co['code']})\n사유: 동일 종목의 다른 예약 매매 발동으로 인한 일괄 자동 취소\n조건: {cond_str} ({t_type})")
                
                # [추가] 일괄 취소 내역 거래내역에 기록
                db_manager.db.insert_trade(
                    f"{t_type}취소(예약)", co['code'], co['name'], co['qty'], 
                    co.get('order_price', 0), f"RES_CAN_{co['id']}", 
                    order_status="취소", reason=f"일괄 자동 취소 (조건: {cond_str})"
                )
                
        else:
            fail_msg = res.get('msg1', '알 수 없는 오류')
            db_manager.db.update_reserved_order_status(order['id'], 'FAILED', fail_reason=fail_msg)
            api.send_telegram_message(f"🚨 [예약 주문 실패]\n종목: {order['name']}\n사유: {fail_msg}")