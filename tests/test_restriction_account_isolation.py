"""수동/자동 계좌가 분리된 mode 2에서 트레이딩 제한 종목이 계좌를 넘지 않는지 검증한다.

[왜 중요한가] 제한 종목의 목적은 '운용자가 직접 산 포지션을 시스템이 제 손절 기준으로
팔아치우지 않게 하는 것'이다. 그런데 REAL_ACC_NUM(수동)과 AUTO_ACC_NUM(자동매매)이
분리되면 시스템은 자동 계좌의 잔고만 읽으므로, 수동 계좌의 포지션을 건드릴 방법이
애초에 없다.

따라서 계좌를 넘는 차단은 **순손실**이다. 운용자가 개인 계좌에서 삼성전자를 샀다는
이유로 시스템이 자기 계좌에서 삼성전자를 매매하지 못하면, 보호받는 것은 없고 기회만
사라진다. 반대로 운용자가 **자동 계좌에서** 직접 매수했다면 그 포지션은 시스템의
청산 대상과 같은 자리에 있으므로 반드시 차단돼야 한다.

전역 제한(계좌 미지정)은 종전대로 모든 계좌에 걸린다 — 운용자가 종목 자체를 배제한
것이므로 계좌와 무관하다.
"""
import pytest

import config
from modules import auto_trade
from modules.auto_trade import common

MANUAL = ("68029263", "01")     # REAL_ACC_NUM 계열(운용자 수동 계좌)
AUTO = ("44048158", "01")       # AUTO_ACC_NUM 계열(시스템 트레이딩 계좌)
CODE, NAME = "005930", "삼성전자"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """제한 종목 저장소를 임시 파일로 돌린다(운영 json 오염 방지)."""
    monkeypatch.setattr(common, 'RESTRICTED_FILE', str(tmp_path / "restricted.json"), raising=False)
    yield


@pytest.fixture
def separated_accounts(monkeypatch):
    s = config.session
    for k, v in (('is_simulation', False), ('is_toss', False), ('is_paper', False),
                 ('cano', MANUAL[0]), ('acnt_prdt_cd', MANUAL[1]),
                 ('auto_cano', AUTO[0]), ('auto_acnt_prdt_cd', AUTO[1])):
        monkeypatch.setattr(s, k, v, raising=False)
    yield s


def test_manual_buy_in_the_personal_account_does_not_block_the_system(isolated_store, separated_accounts):
    """운용자가 개인 계좌에서 산 종목은 시스템(자동 계좌) 매매를 막지 않는다."""
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=MANUAL[0], acnt=MANUAL[1])

    assert CODE in auto_trade.get_restricted_stocks(*MANUAL), "수동 계좌에서는 제한이 보여야 한다"
    assert CODE not in auto_trade.get_restricted_stocks(*AUTO), (
        "개인 계좌의 수동 매수가 자동 계좌의 매매까지 막았다 — 시스템은 그 포지션을 "
        "볼 수도 팔 수도 없으므로 보호되는 것은 없고 기회만 사라진다")


def test_manual_buy_inside_the_system_account_does_block_it(isolated_store, separated_accounts):
    """운용자가 자동 계좌에서 직접 매수하면 그 종목은 시스템 매매에서 빠져야 한다."""
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=AUTO[0], acnt=AUTO[1])

    assert CODE in auto_trade.get_restricted_stocks(*AUTO), (
        "자동 계좌의 수동 매수분이 제한되지 않았다 — 시스템이 제 손절 기준으로 "
        "운용자의 포지션을 청산한다")
    assert CODE not in auto_trade.get_restricted_stocks(*MANUAL)


def test_global_restriction_covers_both_accounts(isolated_store, separated_accounts):
    """계좌를 지정하지 않은 전역 제한은 종목 자체의 배제이므로 양쪽 모두에 걸린다."""
    auto_trade.add_restricted_stock(CODE, NAME, "급등주")

    assert CODE in auto_trade.get_restricted_stocks(*MANUAL)
    assert CODE in auto_trade.get_restricted_stocks(*AUTO)


def test_removing_one_account_leaves_the_other(isolated_store, separated_accounts):
    """한쪽 계좌에서 전량 매도해도 다른 계좌의 제한은 남는다."""
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=MANUAL[0], acnt=MANUAL[1])
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=AUTO[0], acnt=AUTO[1])

    auto_trade.remove_restricted_stock(CODE, cano=MANUAL[0], acnt=MANUAL[1])

    assert CODE not in auto_trade.get_restricted_stocks(*MANUAL)
    assert CODE in auto_trade.get_restricted_stocks(*AUTO), (
        "수동 계좌를 정리했다고 자동 계좌의 제한까지 풀렸다")


def test_removing_the_last_account_scope_drops_the_entry(isolated_store, separated_accounts):
    """마지막 스코프가 사라지면 종목 항목 자체가 저장소에서 제거된다(잔여물 방지)."""
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=AUTO[0], acnt=AUTO[1])
    auto_trade.remove_restricted_stock(CODE, cano=AUTO[0], acnt=AUTO[1])

    assert CODE not in auto_trade.load_restricted_stocks()


def test_account_removal_does_not_touch_the_global_scope(isolated_store, separated_accounts):
    """계좌 스코프 해제가 전역 제한을 함께 풀면 안 된다."""
    auto_trade.add_restricted_stock(CODE, NAME, "급등주")                     # 전역
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=AUTO[0], acnt=AUTO[1])

    auto_trade.remove_restricted_stock(CODE, cano=AUTO[0], acnt=AUTO[1])

    assert CODE in auto_trade.get_restricted_stocks(*AUTO), "전역 제한까지 함께 풀렸다"


def test_trade_account_resolver_points_at_the_auto_account(separated_accounts):
    """매매 판정 경로가 조회하는 계좌가 자동매매 계좌여야 한다.

    _get_trade_account()가 수동 계좌를 가리키면 위 격리가 모두 무의미해진다 —
    시스템이 남의 계좌 제한 목록을 보고 자기 매매를 결정하게 된다.
    """
    assert common._get_trade_account() == AUTO


def test_simulation_mode_collapses_to_the_session_account(separated_accounts, monkeypatch):
    """모의투자는 계좌가 하나뿐이므로 세션 계좌로 떨어진다."""
    monkeypatch.setattr(config.session, 'is_simulation', True, raising=False)
    assert common._get_trade_account() == MANUAL


def test_trader_never_looks_up_restrictions_without_an_account():
    """trader.py에서 계좌 없이 get_restricted_stocks()를 부르지 않는다.

    인자를 생략하면 config.session.cano — 즉 **운용자 수동 계좌**로 폴백한다.
    매매 판정에서 그러면 남의 계좌 제한으로 자기 매매를 결정하게 되고, 표시에서
    그러면 자동 계좌의 보유 종목을 수동 계좌 기준으로 주석 달아 '시스템이 손절을
    관리 중'인 것처럼 보이게 한다(실제로는 자동매도 제외 상태).

    2026-08-09: 표시 경로 4곳이 이 폴백을 타고 있어 계좌를 명시하도록 고쳤다.
    """
    import re
    src = open('modules/auto_trade/trader.py', encoding='utf-8').read()
    bare = re.findall(r'get_restricted_stocks\(\s*\)', src)
    assert not bare, (
        f"계좌를 지정하지 않은 get_restricted_stocks() 호출이 {len(bare)}곳 남아 있다 — "
        "_get_trade_account()를 넘길 것")
