"""보유 종목의 거래 내역을 증권사 체결 기록에서 DB로 복원한다.

[왜 필요한가] 시스템 DB와 실제 계좌를 대조할 경로가 없었다. HTS·MTS로 직접 산 종목,
시스템 도입 이전의 포지션, 또는 DB를 새 기계로 옮기면서 잃은 기록은 DB에 매수 이력이
없다. 그러면 실현손익·보유일수·평단이 전부 빈 채로 남고, 실계좌 투입 뒤 무엇이
어긋났는지 확인할 기준선 자체가 없다.

[무엇을 복원하나] 지금 보유 중인 수량을 설명하는 데 필요한 만큼만이다.
최근 체결부터 과거로 거슬러 올라가며 수량을 역산해, **현재 포지션이 열린 시점**까지의
체결을 고른다. 그보다 과거는 이미 청산된 다른 포지션이므로 건드리지 않는다.

  예) 현재 10주 보유
      08-05 매수 4  →  남은 설명 10-4 = 6
      08-03 매도 4  →  남은 설명  6+4 = 10   (팔기 전에는 더 들고 있었다)
      07-28 매수 10 →  남은 설명 10-10 = 0   ← 여기서 포지션이 열렸다. 멈춘다.
      07-10 매수 3  →  (이미 청산된 과거 포지션 — 복원 대상 아님)

수량 역산은 매도를 '되돌리는' 방향이라 분할 매수·부분 매도가 섞여도 성립한다.
조회 구간(기본 12개월) 안에서 0까지 못 내려가면 그 종목은 '부분 복원'으로 보고한다 —
없는 기록을 지어내지 않는다.
"""
import logging

import api
import config
from core import utils
from modules import db_manager
from core import trading_cost

logger = logging.getLogger(__name__)

BACKFILL_REASON = "보유분 복원(증권사 체결내역)"
#  '우리가 낸 주문인데 체결 기록만 없는' 경우의 복원. 외부 매수와 구분해야 한다 —
#  외부로 잘못 판정하면 그 포지션이 제한 종목으로 올라가 손절·트레일링이 통째로 멈춘다.
OWN_FILL_REASON = "체결 기록 복원(시스템 주문)"


def supports_broker_history():
    """증권사 체결 이력으로 복원할 수 있는 모드인가.

    가상투자(mode 1)는 KIS 실전 시세를 쓰지만 체결은 가상이라 증권사에 이력이 없다.
    체결 원장은 paper DB(paper_fills)에 따로 있으므로 복원할 것도, 복원할 곳도 없다.
    토스는 KIS 체결조회 TR 자체가 없다.

    [주의] 이 가드가 없으면 조회가 빈 값을 돌려주는 것을 '진입이 조회 구간보다 과거'로
      오해해 전 종목이 '부분 복원'으로 표시된다(2026-08-10 관측).
    """
    return not (getattr(config.session, 'is_paper', False)
                or getattr(config.session, 'is_toss', False))


def select_explaining_executions(executions, current_qty):
    """현재 보유수량을 설명하는 체결만 고른다(시간 오름차순 반환).

    executions: 시간 오름차순 체결 dict 목록 (api._fetch_period_executions 형식)
    반환: (선택된 체결 목록, 남은 미설명 수량)
      남은 수량 0  = 포지션이 열린 시점까지 온전히 복원됨
      남은 수량 >0 = 조회 구간보다 과거에 진입 — 부분 복원
    """
    remaining = int(current_qty or 0)
    if remaining <= 0 or not executions:
        return [], remaining

    picked = []
    for tx in reversed(executions):          # 최신 → 과거
        picked.append(tx)
        remaining -= tx['qty'] if tx['is_buy'] else -tx['qty']
        if remaining <= 0:
            break

    picked.reverse()                          # 시간 오름차순으로 되돌린다
    return picked, max(0, remaining)


def build_records(executions, is_overseas=False):
    """선택된 체결을 DB 기록 형태로 만든다. 평단을 전진 재생해 매도의 실현손익까지 채운다.

    복원 구간은 '포지션이 열린 시점'부터이므로 평단을 0에서 다시 쌓을 수 있다. 그래야
    매도 기록의 실현손익이 실제와 맞는다(빈 값으로 두면 대조의 의미가 없다).
    """
    records, qty, avg = [], 0, 0.0

    for tx in executions:
        price = float(tx['price'] or 0)
        label = tx.get('type_name') or ("매수" if tx['is_buy'] else "매도")
        rec = {
            'type': f"{label}(외부)", 'code': tx['code'], 'name': tx['name'],
            'qty': tx['qty'], 'price': price, 'odno': tx['odno'],
            'time': _fmt_time(tx['date'], tx['time']),
            'buy_price': 0.0, 'profit_amt': 0, 'profit_rate': 0.0,
        }

        if tx['is_buy']:
            total = avg * qty + price * tx['qty']
            qty += tx['qty']
            avg = total / qty if qty else 0.0
        else:
            sell_qty = min(tx['qty'], qty) if qty else 0
            if avg > 0 and sell_qty > 0:
                amt, rate = trading_cost.net_realized_profit(avg, price, sell_qty, is_overseas)
                rec['buy_price'] = avg
                rec['profit_amt'] = int(amt)
                rec['profit_rate'] = rate
            qty = max(0, qty - tx['qty'])
            if qty == 0:
                avg = 0.0

        records.append(rec)

    return records


def _fmt_time(date, tmd):
    tmd = (tmd or "000000").zfill(6)
    return f"{date[:4]}-{date[4:6]}-{date[6:]} {tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}"


def plan(holdings, cano=None, acnt_prdt_cd=None, months=12):
    """복원 계획을 만든다(DB 쓰기 없음). [{code, name, qty, records, missing, already}] 반환.

    holdings: KIS 국내 잔고 output1 형식

    체결 내역 조회에 실패하면 **RuntimeError 를 올린다** — 빈 계획을 돌려주면 호출부가
    그것을 '복원할 것이 없다'로 읽는다. 모르는 것으로 계획을 세우지 않는다.
    """
    positions = {}
    for h in holdings or []:
        try:
            q = int(float(h.get('hldg_qty') or 0))
        except (TypeError, ValueError):
            continue
        if q > 0:
            positions[str(h.get('pdno') or '').strip()] = (q, str(h.get('prdt_name') or '').strip())
    if not positions:
        return []

    fetched = api.get_period_executions(list(positions), cano=cano,
                                        acnt_prdt_cd=acnt_prdt_cd, months=months)
    #  [Fix 2026-09-05] None = 조회 실패. 빈 dict('체결 없음')와 갈라야 한다.
    #   섞으면 보유분 전체가 '진입이 조회 구간보다 과거(부분 복원)'라는 **틀린 진단**으로
    #   보고되고(이 모듈의 다짐은 "없는 기록을 지어내지 않는다"이다), 외부 매수를 하나도
    #   못 찾아 제한 등록을 건너뛴다 — 운용자가 직접 산 종목을 시스템이 자기 포지션으로
    #   관리하게 된다. 모르면 계획을 세우지 않는다.
    if fetched is None:
        raise RuntimeError("증권사 체결 내역을 조회하지 못했습니다 — '체결 없음'이 아닙니다")

    out = []
    for code, (qty, name) in positions.items():
        picked, missing = select_explaining_executions(fetched.get(code, []), qty)
        records = build_records(picked)
        for r in records:
            r['name'] = r['name'] or name
        already = sum(1 for r in records if _exists(r['odno']))
        out.append({'code': code, 'name': name, 'qty': qty,
                    'records': records, 'missing': missing, 'already': already})
    return out


def _exists(odno, on_date=None):
    """이 체결이 이미 기록돼 있는가.

    [날짜와 함께 본다 · 2026-09-05] 증권사 주문번호는 **당일 채번**이라 매일 0부터 다시
     올라간다(core.utils.order_age_seconds·db_manager.check_trade_exists 주석 참조).
     복원 구간은 최대 12개월이라 그 안에서 같은 번호가 여러 번 나온다 — 전체 이력에서
     찾으면 **다른 날의 같은 번호 때문에 진짜 체결이 '이미 있음'으로 건너뛰어진다**.
     그러면 그 종목의 평단·진입일·손절률이 복원되지 않는다.
     on_date 는 그 체결이 실제로 저장될 날짜('YYYY-MM-DD')다.
    """
    if not odno:
        return False
    try:
        return bool(db_manager.db.check_trade_exists(odno, "체결", on_date=on_date))
    except Exception as e:
        #  [Fix 2026-09-06] 종전에는 False('없다')였다 — 같은 체결을 한 번 더 적어
        #   평단·실현손익이 이중 계상된다. 모르면 '이미 있다'로 보고 건너뛴다.
        #   복원은 기동 때마다 다시 도므로 다음 기회에 채워진다.
        logger.warning(f"[복원] {odno} 중복 여부를 확인하지 못해 이번 복원에서 건너뜁니다: {e}")
        return True


def our_order_row(odno):
    """이 주문번호를 **시스템이 낸 적이 있는가** — 있으면 그 접수 기록을 돌려준다.

    [왜 '체결 기록 없음'으로 판정하면 안 되나] 체결 기록(order_status='체결')은 체결
    통보를 받은 뒤에 따로 INSERT 된다. 주문을 내고 체결 전에 프로세스가 죽으면(라즈베리
    파이 OOM·재기동) 접수 기록만 남는데, 그것만 보고 '외부 매수'로 판정하면 시스템이
    **자기 포지션을 제한 종목으로 올려** 손절·트레일링에서 제외한다. 경보는 나가지만
    청산은 사람이 손대기 전까지 멈춘다.

    외부 앱(MTS/HTS) 주문도 접수 기록이 생기지만 type 에 '(외부)'가 붙는다 — 그것은
    우리 주문이 아니다.
    """
    if not odno:
        return None
    try:
        #  [의도적으로 날짜를 주지 않는다] 여기는 **과거를 채워 넣는** 경로라 애초에
        #  옛 행을 찾는 것이 목적이다. 날짜로 좁히면 백필이 아무것도 못 찾는다
        #  (check_trade_exists 가 같은 이유로 on_date=None 을 남겨 둔 것과 같다).
        row = db_manager.db.get_trade_by_odno(odno)
    except Exception:
        return None
    if not row:
        return None
    return None if "(외부)" in str(row.get('type') or '') else row


def apply(plans, cano=None, acnt_prdt_cd=None):
    """계획을 DB에 기록한다. (기록 건수, 건너뛴 건수) 반환.

    이미 같은 주문번호의 '체결' 기록이 있으면 건너뛴다 — 여러 번 실행해도 중복되지 않는다.
    계좌 귀속이 어긋나면 복원 자체가 무의미하므로 계좌 컨텍스트를 명시 고정한다.
    """
    written = skipped = 0
    target = cano or config.session.cano

    with utils.AccountContext(target):
        for p in plans:
            for r in p['records']:
                #  r['time'] 은 'YYYY-MM-DD HH:MM:SS' — 저장될 날짜와 같은 값이다.
                if _exists(r['odno'], on_date=str(r.get('time') or '')[:10] or None):
                    skipped += 1
                    continue
                # 우리가 낸 주문인데 체결 기록만 빠진 경우 — '(외부)'로 적으면 통계도
                #  틀리고, 무엇보다 진입 시 손절률이 사라져 청산 기준이 폴백으로 내려간다.
                own = our_order_row(r['odno'])
                rec_type = r['type'].replace("(외부)", "") if own else r['type']
                try:
                    own_sl = float((own or {}).get('stop_loss_rate') or 0.0)
                except (TypeError, ValueError):
                    own_sl = 0.0
                ok = db_manager.db.insert_trade(
                    rec_type, r['code'], r['name'], r['qty'], str(int(r['price'])), r['odno'],
                    order_status="체결",
                    reason=OWN_FILL_REASON if own else BACKFILL_REASON,
                    custom_time=r['time'],
                    profit_amt=r['profit_amt'], profit_rate=r['profit_rate'],
                    buy_price=r['buy_price'], stop_loss_rate=own_sl,
                    score=(own or {}).get('strategy_score') or 0)
                if ok:
                    written += 1
                else:
                    skipped += 1
                    logger.warning(f"[보유분 복원] DB 기록 실패: {r['code']} {r['odno']}")
    return written, skipped


def sync_account(cano=None, acnt_prdt_cd=None, months=12, register_restrictions=False,
                 holdings=None):
    """보유분 기록을 증권사 체결 내역과 맞춘다(기동 시 자동 호출용). 요약 dict 반환.

    [조회 범위] 날짜 창이 아니라 **보유수량 역산**으로 정한다. 포지션이 열린 시점까지만
      거슬러 올라가고 거기서 멈추므로, 시스템이 얼마나 오래 꺼져 있었든 스스로 필요한
      만큼만 조회한다. '마지막 기록일 이후'로 잡으면 오래 쉰 뒤엔 창이 무한정 넓어지는데,
      정작 필요한 건 지금 들고 있는 포지션의 이력뿐이다. months 는 안전 상한이다.

    [한계] 시스템이 꺼진 사이에 사서 그 사이에 판 종목(왕복)은 잡지 못한다. 지금 보유가
      없으므로 역산의 출발점이 없기 때문이다. 그 포지션은 이미 닫혀 있어 시스템이 잘못
      관리할 위험은 없고, 통계상 누락으로만 남는다.

    register_restrictions: 자동매매 계좌에만 True. 시스템이 꺼진 사이 운용자가 그 계좌에서
      직접 산 종목을 시스템이 '자기 포지션'으로 알고 관리하는 것을 막는다. 실시간 경로
      (ConclusionMonitor)는 이미 같은 처리를 하는데, 기동 경로에만 이 방어가 없었다.
    """
    summary = {'written': 0, 'skipped': 0, 'restricted': [], 'partial': [], 'error': None}
    if not supports_broker_history():
        return summary
    try:
        if holdings is None:
            holdings, _ = api.get_domestic_balance(cano, acnt_prdt_cd)
        #  plan() 은 체결 내역 조회 실패를 RuntimeError 로 올린다(그쪽 독스트링) — 빈 계획을
        #  '복원할 것이 없다'로 읽히지 않게 하려는 것이다. 그런데 **잔고** 조회 실패는 그 앞에서
        #  None 으로 들어와 `holdings or []` 에 삼켜져, 똑같이 빈 계획이 됐다. 같은 규칙을 세운다.
        if holdings is None:
            raise RuntimeError("보유 잔고를 조회하지 못했습니다 — '복원할 종목이 없다'가 아닙니다")
        plans = plan(holdings, cano=cano, acnt_prdt_cd=acnt_prdt_cd, months=months)
        if not plans:
            return summary

        summary['partial'] = [(p['code'], p['name'], p['missing']) for p in plans if p['missing'] > 0]

        # [외부 판정] '체결 기록이 없다'가 아니라 '우리가 낸 주문이 아니다'가 기준이다.
        #  our_order_row 주석 참조 — 접수 기록만 남은 자기 주문을 외부로 몰면 그 포지션의
        #  손절이 멈춘다(제한 종목 등록).
        new_buy_codes = {(p['code'], p['name']) for p in plans
                         for r in p['records']
                         if '매수' in r['type'] and not _exists(r['odno'])
                         and our_order_row(r['odno']) is None}

        summary['written'], summary['skipped'] = apply(plans, cano=cano, acnt_prdt_cd=acnt_prdt_cd)

        if register_restrictions and new_buy_codes:
            summary['restricted'] = _restrict_external_buys(new_buy_codes, cano, acnt_prdt_cd)
    except Exception as e:
        summary['error'] = str(e)
        #  [2026-09-05] 실패를 '부분 복원' 목록으로 흘려보내지 않는다 — 그러면 조회
        #   실패가 "진입이 12개월보다 과거"라는 진단으로 화면에 나간다.
        summary['partial'] = []
        logger.warning(f"[보유분 복원] 동기화 실패 — 복원하지 않았습니다: {e}")
    return summary


def _restrict_external_buys(codes_names, cano, acnt_prdt_cd):
    """외부에서 산 종목을 시스템 매매 대상에서 뺀다(수동매매 제한).

    지연 임포트다 — auto_trade 가 이 모듈을 부르므로 모듈 수준에서 참조하면 순환된다.
    """
    from modules.auto_trade import add_restricted_stock, get_restricted_stocks
    from modules.auto_trade.common import _current_account_type

    done = []
    try:
        already = get_restricted_stocks(cano, acnt_prdt_cd)
    except Exception:
        already = {}

    for code, name in sorted(codes_names):
        if code in already:
            continue
        try:
            add_restricted_stock(code, name, "수동매매", cano=cano, acnt=acnt_prdt_cd,
                                 account_type=_current_account_type(cano, acnt_prdt_cd))
            done.append(code)
        except Exception as e:
            logger.warning(f"[보유분 복원] 제한 등록 실패 {code}: {e}")
    return done
