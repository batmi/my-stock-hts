"""성과 보고 계산 — 매매 기록의 정제와 통계(순수 함수).

[왜 분리했나 · 2026-10-16] 이 계산은 AutoTrader(7,800줄)의 메서드였고, 트레이더 상태
 (self.trade_records)와 DB 조회 한 군데에 묶여 있어 통계 규칙 하나를 고치려 해도 트레이더
 인스턴스를 세워야 했다. 상태는 인자(records)로, DB 조회는 호출부가 넘기는 함수
 (past_trades_of)로 받는다. 계산 규칙은 옮기기 전과 같다(같은 입력에서 같은 결과 —
 AutoTrader._refine_trade_records / _calculate_statistics 는 이 함수에 위임한다).

[주의] profit_amt 는 실거래에서 총차익(비용 제외)이다 — 비용은 cost_amt 로 따로 남는다
 ([[trading-cost-single-source]]). 이 통계는 종전과 같이 profit_amt 를 합산한다.
"""
from collections import Counter
from datetime import datetime

import api


def refine_trade_records(records):
    """거래 내역 중복 제거 및 우선순위 적용 (전략 사유 > 체결 확인)"""
    unique_records = {}
    
    for r in records:
        odno = r.get('odno')
        # odno가 없으면 고유 키 생성하여 포함
        if not odno:
            key = f"NO_ODNO_{r.get('time', '')}_{r.get('code', '')}_{r.get('type', '')}_{len(unique_records)}"
            unique_records[key] = r
            continue

        # [수정] KIS 주문번호(odno)는 영업일 단위로 채번되어 날짜가 다르면 재사용된다.
        #  키를 odno만 쓰면 서로 다른 날짜의 거래가 같은 odno로 병합되어 한쪽이
        #  소실(누락)되므로, (거래일 + odno)를 키로 사용해 날짜 충돌을 막는다.
        #  (같은 날의 접수→체결 병합은 그대로 유지된다)
        date_key = str(r.get('time', ''))[:10]
        key = f"{date_key}_{odno}"

        if key not in unique_records:
            unique_records[key] = dict(r) # 복사본 저장
        else:
            existing = unique_records[key]
            
            # 새 레코드(r)가 더 최신 정보(체결 등)를 담고 있을 때 병합
            if (api.safe_float(r.get('price'), default=0.0) > 0
                        and api.safe_float(existing.get('price'), default=0.0) <= 0):
                existing['price'] = r['price']
                
            if r.get('profit_amt'):
                existing['profit_amt'] = r['profit_amt']
                existing['cost_amt'] = r.get('cost_amt') or 0.0   # 손익과 같은 행의 비용을 함께 따라가야 순손익이 맞다
            if r.get('profit_rate'):
                existing['profit_rate'] = r['profit_rate']
                
            old_reason = str(existing.get('reason', ''))
            new_reason = str(r.get('reason', ''))
            
            if "체결 확인" in old_reason and "체결 확인" not in new_reason:
                existing['reason'] = new_reason
            elif "체결 확인" not in old_reason and "체결 확인" in new_reason:
                pass # 기존 구체적 사유 유지
            else:
                existing['reason'] = new_reason # 최신 사유로 덮어씀

            existing['time'] = r.get('time', existing.get('time'))
            if r.get('order_status'):
                existing['order_status'] = r['order_status']

            # [추가] 주문 출처 꼬리표(type_full)는 '접수' 원본이 정확하다. 레코드는 시간
            #  오름차순으로 들어오므로 먼저 자리잡은 값(=접수)을 유지하고, 비어 있을 때만 채운다.
            #  (체결 확인 시점에 원주문 조회가 실패하면 그 레코드에는 (외부)가 붙는다)
            if not existing.get('type_full') and r.get('type_full'):
                existing['type_full'] = r['type_full']
    
    return list(unique_records.values())


def calculate_statistics(records, past_trades_of=None):
    """매매 기록 → 성과 통계(순수 계산). past_trades_of(code) 는 기간 밖 매수 기록 조회(없으면 생략)."""
    
    # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
    # 수동 매매('체결 확인')도 통계에 포함
    
    total_trades = len(records)
    buy_trades = [r for r in records if r['type'] == 'buy']
    sell_trades = [r for r in records if r['type'] == 'sell']
    
    win_trades = 0
    loss_trades = 0
    total_profit = 0
    total_profit_rate = 0.0
    total_buy_amt_for_sell = 0
    
    # [추가] Best/Worst 및 사유 분석 변수
    best_trade = None
    worst_trade = None
    sell_reasons = Counter()
    buy_reasons = Counter()
    
    # [추가] 보유 기간 계산
    total_holding_seconds = 0
    holding_count = 0
    buy_times = {} # code -> list of datetime

    # 시간순 처리를 위해 전체 기록 순회
    for r in records:
        code = r['code']
        try:
            dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
        except Exception: continue

        if r['type'] == 'buy':
            if code not in buy_times: buy_times[code] = []
            buy_times[code].append(dt)
            
            reason_raw = r.get('reason', '')
            reason_key = "기타"
            if "조건 만족" in reason_raw:
                if "슈퍼모멘텀" in reason_raw:
                    reason_key = "추격 매수 (돌파)"
                else:
                    reason_key = "스코어 진입 (조건 만족)"
            elif "역매수" in reason_raw or "역추세" in reason_raw:
                reason_key = "역추세 매수 (낙폭과대)"
            elif "수동" in reason_raw or "사용자 수동 주문" in reason_raw:
                reason_key = "수동 매수"
            elif "예약" in reason_raw:
                reason_key = "예약 매수"
            buy_reasons[reason_key] += 1
        elif r['type'] == 'sell':
            # 매도 시 매수 기록과 매칭 (FIFO: 먼저 산 것을 먼저 판다고 가정)
            if code in buy_times and buy_times[code]:
                buy_dt = buy_times[code].pop(0)
                diff = (dt - buy_dt).total_seconds()
                total_holding_seconds += diff
                holding_count += 1
            else:
                # [추가] 기간 검색으로 인해 주어진 레코드에 매수 기록이 없는 경우 DB에서 과거 내역 조회
                try:
                    past_trades = past_trades_of(code) if past_trades_of else []
                    for pt in past_trades:
                        pt_type = pt.get('type', '').lower()
                        if "buy" in pt_type or "매수" in pt_type:
                            pt_dt = datetime.strptime(pt['time'], "%Y-%m-%d %H:%M:%S")
                            if pt_dt < dt:
                                diff = (dt - pt_dt).total_seconds()
                                total_holding_seconds += diff
                                holding_count += 1
                                break
                except Exception:
                    pass
    
    for t in sell_trades:
        profit = t.get('profit_amt', 0)
        rate = t.get('profit_rate', 0.0)
        total_profit += profit
        total_profit_rate += rate
        
        qty = api.safe_int(t.get('qty'))
        price = api.safe_float(t.get('price'), default=0.0)
        sell_amt = qty * price
        buy_amt = sell_amt - profit
        total_buy_amt_for_sell += buy_amt
        
        if profit > 0: win_trades += 1
        else: loss_trades += 1
        
        # [추가] Best/Worst 갱신
        if best_trade is None or profit > best_trade.get('profit_amt', 0):
            best_trade = t
        if worst_trade is None or profit < worst_trade.get('profit_amt', 0):
            worst_trade = t
        
        # [추가] 매도 사유 분석
        reason = t.get('reason', '기타')
        reason_key = "기타"
        if "반익절" in reason: reason_key = "반익절"
        elif "과열" in reason: reason_key = "과열매도"
        elif "익절" in reason: reason_key = "익절"
        elif "ATR손절" in reason: reason_key = "ATR손절"
        elif "손절" in reason: reason_key = "손절"
        elif "트레일링" in reason: reason_key = "트레일링스탑"
        elif "시간청산" in reason: reason_key = "시간청산"
        elif "추세" in reason or "점수하락" in reason or "매도진입" in reason: reason_key = "추세이탈"
        elif "수동" in reason: reason_key = "수동매도"
        sell_reasons[reason_key] += 1
        
    avg_profit_rate = (total_profit_rate / len(sell_trades)) if sell_trades else 0.0
    win_rate = (win_trades / len(sell_trades) * 100) if sell_trades else 0.0
    total_realized_rate = (total_profit / total_buy_amt_for_sell * 100) if total_buy_amt_for_sell > 0 else 0.0

    # [추가] 평균 보유 기간 포맷팅
    avg_holding_str = "-"
    if holding_count > 0:
        avg_sec = total_holding_seconds / holding_count
        if avg_sec < 60: avg_holding_str = f"{int(avg_sec)}초"
        elif avg_sec < 3600: avg_holding_str = f"{int(avg_sec//60)}분 {int(avg_sec%60)}초"
        else: avg_holding_str = f"{int(avg_sec//3600)}시간 {int((avg_sec%3600)//60)}분"

    return {
        "total_trades": total_trades,
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "win_trades": win_trades,
        "loss_trades": loss_trades,
        "total_profit": total_profit,
        "total_realized_rate": total_realized_rate,
        "total_buy_amt_for_sell": total_buy_amt_for_sell, # [추가] 투자 원금 기준 알파 계산용
        "avg_profit_rate": avg_profit_rate,
        "win_rate": win_rate,
        "avg_holding_str": avg_holding_str,
        "sell_trades_exist": len(sell_trades) > 0,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "sell_reasons": sell_reasons,
        "buy_reasons": buy_reasons
    }
