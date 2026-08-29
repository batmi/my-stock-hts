"""거래 내역(/history) 주문 출처 꼬리표 테스트.

증상: 시스템 트레이딩이 낸 주문도, 사용자가 낸 수동 주문도 전부 '매수(외부) / 사유: [외부]'로
표시됐다(실측 2026-07-28 /history 주간).

원인: _get_refined_trades_cached가 집계 편의를 위해 레코드의 type을 'buy'/'sell'로 통째로
덮어써, DB에 저장된 출처 꼬리표((AUTO)/(수동)/(예약)/(외부))가 화면에 닿기 전에 사라졌다.
표시 코드는 '자동도 수동도 예약도 아니면 외부'로 단정했기 때문에 전부 (외부)가 됐다.

DB의 꼬리표 표기(실측):
  시스템 = 'buy(AUTO)'/'sell(AUTO)', 수동 = '매수(수동)', 예약 = '매수취소(예약)',
  외부 = '현금매수(외부)'
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from modules.telegram_bot import TelegramCommander


def _row(type_str, odno="0001", reason="사유", status="체결", days_ago=1,
         code="005930", name="삼성전자", qty=9, price=100000, seq=0):
    """DB trades 레코드 한 행(get_trades 반환 형태)."""
    t = datetime.now() - timedelta(days=days_ago, seconds=seq)
    return {
        'id': 1000 + seq, 'time': t.strftime("%Y-%m-%d %H:%M:%S"),
        'type': type_str, 'code': code, 'name': name, 'qty': qty, 'price': price,
        'odno': odno, 'org_odno': None, 'order_status': status, 'reason': reason,
        'profit_amt': 0, 'profit_rate': 0.0, 'strategy_score': 0, 'stop_loss_rate': 0.0,
    }


@pytest.fixture
def bot():
    b = TelegramCommander()
    with b._trade_cache_lock:
        b._trade_cache.clear()
    yield b
    with b._trade_cache_lock:
        b._trade_cache.clear()


def _history(bot, rows, days=7):
    """get_trades는 id DESC(최신 우선)로 반환한다 — 실제 순서를 그대로 흉내낸다."""
    ordered = sorted(rows, key=lambda r: r['id'], reverse=True)
    with patch('modules.telegram_bot.db_manager.db.get_trades', return_value=ordered), \
         patch('modules.telegram_bot.db_manager.db.get_all_stock_strategies', return_value=[]), \
         patch('modules.telegram_bot.auto_trade.get_restricted_stocks', return_value={}):
        return bot._get_trade_history(days=days)


# ==========================================================
# 출처별 꼬리표
# ==========================================================

@pytest.mark.parametrize("db_type, expect_type, expect_reason_tag", [
    ("buy(AUTO)", "매수(자동)", "[자동]"),          # 시스템 트레이딩
    ("sell(AUTO)", "매도(자동)", "[자동]"),
    ("매수(수동)", "매수(수동)", "[수동]"),          # 운영자 수동 주문
    ("매도(수동)", "매도(수동)", "[수동]"),
    ("현금매수(외부)", "매수(외부)", "[외부]"),      # 앱/HTS 외부 주문
])
def test_history_shows_origin_tag(bot, db_type, expect_type, expect_reason_tag):
    msg = _history(bot, [_row(db_type, reason="[추세매수] 조건 만족")])
    assert f"| {expect_type} |" in msg, msg
    assert expect_reason_tag in msg, msg


def test_reserved_order_tagged_as_reserved(bot):
    """예약 주문은 종전에 (외부)로 표시됐다 — tag_disp에 예약 분기가 없었다."""
    msg = _history(bot, [_row("매수취소(예약)", reason="조건: TIME (09:05)", status="취소")])
    assert "매수취소(예약)" in msg, msg
    assert "(외부)" not in msg, msg


def test_unknown_tag_is_not_labeled_external(bot):
    """출처를 알 수 없으면 (외부)로 단정하지 않는다."""
    msg = _history(bot, [_row("매수", reason="레거시 기록")])
    assert "| 매수 |" in msg, msg
    assert "외부" not in msg, msg


# ==========================================================
# 접수→체결 병합 (사용자 실측 케이스)
# ==========================================================

def test_merged_receipt_and_fill_keeps_receipt_tag(bot):
    """접수(시스템)와 체결(원주문 조회 실패로 외부 태깅)이 병합될 때 접수 꼬리표가 이긴다.

    실측 증상: 사유는 시스템 것('[추세매수] 조건 만족 …')인데 유형만 '매수(외부)'로 찍혔다.
    체결 확인 시점에 get_trade_by_odno가 원주문을 못 찾으면 그 레코드에 (외부)가 붙는데,
    병합이 type을 갱신하지 않아 먼저 자리잡은 쪽이 남는다 → '접수' 원본이 정답이다.
    """
    rows = [
        _row("buy(AUTO)", odno="0100", reason="[추세매수] 조건 만족 [점수:8.0]",
             status="접수", seq=0),
        _row("현금매수(외부)", odno="0100", reason="체결 확인 (앱/HTS 외부 주문)",
             status="체결", seq=1),
    ]
    rows[1]['id'] = rows[0]['id'] + 1      # 체결이 나중에 적재됨
    msg = _history(bot, rows)

    assert "| 매수(자동) |" in msg, msg
    assert "[자동]" in msg, msg
    assert "[외부]" not in msg, msg
    assert "[추세매수] 조건 만족" in msg, msg   # 사유는 접수 원본 유지


def test_external_only_fill_stays_external(bot):
    """접수 원본이 없는 진짜 외부 주문은 그대로 (외부)여야 한다."""
    msg = _history(bot, [_row("현금매수(외부)", odno="0200",
                              reason="체결 확인 (앱/HTS 외부 주문)", status="체결")])
    assert "| 매수(외부) |" in msg, msg
    assert "[외부]" in msg, msg


# ==========================================================
# 캐시 레코드 스키마
# ==========================================================

def test_refined_cache_keeps_both_type_forms(bot):
    """집계용 단순 type과 표시용 원본 type_full이 함께 남아야 한다."""
    rows = [_row("buy(AUTO)", odno="0300"), _row("매도(수동)", odno="0301", seq=1)]
    with patch('modules.telegram_bot.db_manager.db.get_trades',
               return_value=sorted(rows, key=lambda r: r['id'], reverse=True)):
        refined = bot._get_refined_trades_cached()

    by_odno = {r['odno']: r for r in refined}
    assert by_odno['0300']['type'] == 'buy'          # 집계 코드가 쓰는 형태
    assert by_odno['0300']['type_full'] == 'buy(AUTO)'
    assert by_odno['0301']['type'] == 'sell'
    assert by_odno['0301']['type_full'] == '매도(수동)'


def test_refine_records_fills_missing_type_full():
    """병합 시 먼저 자리잡은 꼬리표를 유지하되, 비어 있으면 채운다."""
    from modules.auto_trade import AutoTrader
    trader = AutoTrader()
    base = _row("buy", odno="0400", seq=0)
    base.pop('type_full', None)
    later = _row("buy", odno="0400", seq=1)
    later['type_full'] = 'buy(AUTO)'
    later['id'] = base['id'] + 1

    merged = trader._refine_trade_records([base, later])
    assert len(merged) == 1
    assert merged[0]['type_full'] == 'buy(AUTO)'


def test_refine_records_prefers_first_type_full():
    from modules.auto_trade import AutoTrader
    trader = AutoTrader()
    first = _row("buy", odno="0500", seq=0)
    first['type_full'] = 'buy(AUTO)'
    second = _row("buy", odno="0500", seq=1)
    second['type_full'] = '현금매수(외부)'
    second['id'] = first['id'] + 1

    merged = trader._refine_trade_records([first, second])
    assert merged[0]['type_full'] == 'buy(AUTO)'


# ==========================================================
# 피라미딩(추가매수) 사유 태그
# ==========================================================

def test_pyramiding_buy_tagged_as_additional_buy(bot):
    """피라미딩 매수는 '[추가매수]'로 가른다.

    실측 증상(2026-08-26): 접수 상태의 피라미딩 주문이 '[자동] [미체결] 피라미딩 1차 …'로
    찍혔다. 매수 사유 분류에 피라미딩 분기가 없어 태그가 비었고, 그 빈자리를 '접수(미체결)'
    상태 태그가 채운 것이다. 상태는 이미 '접수' 열이 말하고 있으니, 사유 자리에는
    무엇을 왜 샀는지가 와야 한다.
    """
    reason = "피라미딩 1차 (수익률:+10.2%, 점수:9.5, 상태:강매수)"
    msg = _history(bot, [_row("buy(AUTO)", reason=reason, status="접수")])

    assert "[추가매수]" in msg, msg
    assert "[미체결]" not in msg, msg
    assert "[자동]" in msg, msg


def test_pyramiding_fill_also_tagged(bot):
    """체결된 피라미딩도 같은 태그를 단다(접수 상태에만 붙는 태그가 아니다)."""
    reason = "피라미딩 2차 (수익률:+15.0%, 점수:9.0, 상태:강매수)"
    msg = _history(bot, [_row("buy(AUTO)", reason=reason, status="체결")])
    assert "[추가매수]" in msg, msg


# ==========================================================
# [단일 소스] core.trade_tags — 잔고 화면(메뉴 9)과 텔레그램 /history 가 공유한다
# ==========================================================
def test_buy_tag_vocabulary():
    """매수 사유 어휘. 피라미딩이 가장 먼저 걸려야 한다(증액 ≠ 신규 진입)."""
    from core import trade_tags as tt
    assert tt.classify_buy_reason("피라미딩 2차 매수") == "추가매수"
    assert tt.classify_buy_reason("PYRAMID add") == "추가매수"
    assert tt.classify_buy_reason("슈퍼모멘텀 돌파") == "돌파매수"
    assert tt.classify_buy_reason("역매수 진입") == "눌림목"
    assert tt.classify_buy_reason("조건 만족 (SCORE 5.2)") == "추세매수"
    assert tt.classify_buy_reason("수동 매수") == "수동매수"
    assert tt.classify_buy_reason("알 수 없는 사유") == ""
    assert tt.classify_buy_reason("") == ""
    assert tt.classify_buy_reason(None) == ""


def test_sell_tag_vocabulary():
    """매도 사유 어휘. ATR손절이 손절보다 먼저여야 세부 사유가 뭉개지지 않는다."""
    from core import trade_tags as tt
    assert tt.classify_sell_reason("ATR 손절선 이탈") == "ATR손절"
    assert tt.classify_sell_reason("손절선 이탈") == "손절"
    assert tt.classify_sell_reason("반익절 실행") == "반익절"
    assert tt.classify_sell_reason("트레일링 스탑 청산") == "트레일링스탑"
    assert tt.classify_sell_reason("시간 청산") == "시간청산"
    assert tt.classify_sell_reason("점수 하락 매도진입") == "추세이탈"


def test_buy_tag_merges_after_state_tag():
    """`[강매수]` 등 스냅샷 상태 태그가 앞에 있으면 그 **뒤에** 끼운다."""
    from core import trade_tags as tt
    assert tt.apply_buy_tag("[강매수] 슈퍼모멘텀 돌파") == "[강매수] [돌파매수] 슈퍼모멘텀 돌파"
    assert tt.apply_buy_tag("조건 만족") == "[추세매수] 조건 만족"
    # 이미 붙어 있으면 두 번 붙이지 않는다.
    assert tt.apply_buy_tag("[추가매수] 피라미딩 2차") == "[추가매수] 피라미딩 2차"


def test_sell_tag_leaves_prefixed_reason_alone():
    """매도 사유의 선행 대괄호는 사유 그 자체인 경우가 많아 덧붙이지 않는다."""
    from core import trade_tags as tt
    assert tt.apply_sell_tag("[익절] 목표 도달") == "[익절] 목표 도달"
    assert tt.apply_sell_tag("트레일링 스탑") == "[트레일링스탑] 트레일링 스탑"


def test_history_screens_share_one_tag_source():
    """두 화면이 각자 사다리를 복붙하지 않는지 고정한다.

    [회귀 · 2026-08-26] 같은 if-elif 사다리가 account.py 와 telegram_bot.py 에 글자
    그대로 복제돼 있어 피라미딩 태그 누락을 두 곳에서 똑같이 고쳐야 했다. 어휘가 하나
    늘 때마다 한쪽이 빠질 자리가 남는다.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("modules/account.py", "modules/telegram_bot.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "trade_tags" in src, f"{name} 이 단일 소스를 쓰지 않는다"
        assert 'buy_tag = "돌파매수"' not in src, f"{name} 에 매수 태그 사다리가 복제돼 있다"
        assert 'sell_tag = "ATR손절"' not in src, f"{name} 에 매도 태그 사다리가 복제돼 있다"
