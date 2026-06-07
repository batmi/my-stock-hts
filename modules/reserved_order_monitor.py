import time
import threading
import logging
from datetime import datetime
import api
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
            if order['condition_type'] in ['SCORE_UP', 'SCORE_DOWN', 'RSI_UP', 'RSI_DOWN', 'EMA_UP', 'EMA_DOWN']:
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
                start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0900")
                end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1530")
                
                if not is_overseas and (now_time_str_short >= end_time or now_time_str_short < start_time):
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
                            score, _ = analysis.calculate_score(df=df, ind=ind)
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
                
    def _execute_order(self, order, reason):
        db_manager.db.update_reserved_order_status(order['id'], 'PROCESSING')
        market_str = "domestic" if order['market'] == 'KR' else "overseas"
        
        order_price = float(order['order_price'])
        if order_price == 0:
            if order['market'] == 'KR':
                ord_dvsn = "01"
                price_str = "0"
            else:
                ord_dvsn = "00"
                curr_price = api.get_current_price(order['code'], is_overseas=True)
                if curr_price > 0:
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
            
            # [추가] 해당 종목의 나머지 예약 주문 일괄 취소 및 알림
            canceled_orders = db_manager.db.cancel_other_reserved_orders(order['id'], order['cano'], order['acnt'], order['code'])
            for co in canceled_orders:
                t_type = "매수" if co['order_type'] == 'buy' else "매도"
                cond_str = co['condition_type']
                api.send_telegram_message(f"🗑️ [예약 일괄 취소]\n종목: {co['name']}({co['code']})\n사유: 동일 종목의 다른 예약 매매 발동으로 인한 일괄 자동 취소\n조건: {cond_str} ({t_type})")
                
        else:
            fail_msg = res.get('msg1', '알 수 없는 오류')
            db_manager.db.update_reserved_order_status(order['id'], 'FAILED', fail_reason=fail_msg)
            api.send_telegram_message(f"🚨 [예약 주문 실패]\n종목: {order['name']}\n사유: {fail_msg}")