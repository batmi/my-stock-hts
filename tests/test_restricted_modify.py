"""제한 종목을 '해제 후 재등록' 없이 고칠 수 있어야 한다.

[배경 · 2026-09-04] 사유 한 줄을 고치는 수단이 없었다. 해제 후 다시 등록하면
 · **등록일이 오늘로 밀린다** — 언제부터 막아 둔 종목인지가 사라진다
 · 다시 등록하는 사이 자동매매가 그 종목을 살 수 있는 창이 열린다
게다가 add_restricted_stock 은 메모를 ', ' 로 이어붙이므로(여러 번 걸린 이유를 잃지
않으려는 설계) 오타를 지우지도 못했다. 변경은 이어붙이지 않고 그 자리를 대체한다.
"""
import pytest

import config
from modules import auto_trade
from modules.auto_trade import menu as at_menu


@pytest.fixture
def store(tmp_path):
    """제한 목록 파일이 임시 경로로 격리돼 있는지 **확인한다**.

    격리 자체는 conftest 의 autouse 픽스처가 RESTRICTED_FILE 을 tmp_path 로 돌려 해 준다.
    여기서 다시 patch 하면 이름을 틀렸을 때(예: RESTRICTED_STOCKS_FILE) raising=False 에
    묻혀 조용히 **운영 파일에 쓰게 된다** — 그래서 patch 하지 않고 검증만 한다.
    """
    path = auto_trade.common.RESTRICTED_FILE
    assert str(tmp_path) in str(path), f"제한 목록이 격리되지 않았다: {path}"
    return path


@pytest.fixture
def seeded(store):
    auto_trade.add_restricted_stock("005930", "삼성전자", "오타있는 사유")
    auto_trade.add_restricted_stock("000660", "SK하이닉스", "계좌 사유",
                                    cano="12345678", acnt="01", account_type="한투-자동")
    return auto_trade.load_restricted_stocks()


# ─────────────────────────────────────────────
# 1. 사유 교체
# ─────────────────────────────────────────────

def test_memo_is_replaced_not_appended(seeded):
    """추가 경로는 이어붙인다 — 변경은 그러면 안 된다(오타가 영원히 남는다)."""
    auto_trade.update_restricted_stock("005930", "고친 사유")
    assert auto_trade.load_restricted_stocks()["005930"]["memo"] == "고친 사유"


def test_add_still_appends(seeded):
    """추가의 이어붙이기는 의도된 동작이다 — 변경을 넣었다고 바뀌면 안 된다."""
    auto_trade.add_restricted_stock("005930", "삼성전자", "두 번째 사유")
    memo = auto_trade.load_restricted_stocks()["005930"]["memo"]
    assert "오타있는 사유" in memo and "두 번째 사유" in memo


def test_the_original_registration_date_survives(seeded):
    """등록일이 밀리면 '언제부터 막아 둔 종목인가'를 잃는다."""
    before = seeded["005930"]["date"]
    auto_trade.update_restricted_stock("005930", "고친 사유")
    assert auto_trade.load_restricted_stocks()["005930"]["date"] == before


def test_account_scope_memo_is_replaced(seeded):
    auto_trade.update_restricted_stock("000660", "새 사유",
                                       old_cano="12345678", old_acnt="01",
                                       new_cano="12345678", new_acnt="01")
    acc = auto_trade.load_restricted_stocks()["000660"]["accounts"]["12345678-01"]
    assert acc["memo"] == "새 사유" and acc["type"] == "한투-자동"


# ─────────────────────────────────────────────
# 2. 적용 범위 이동
# ─────────────────────────────────────────────

def test_global_to_account_moves_and_keeps_the_date(seeded):
    before = seeded["005930"]["date"]
    auto_trade.update_restricted_stock("005930", "계좌만 제한",
                                       new_cano="99999999", new_acnt="01",
                                       account_type="토스")
    info = auto_trade.load_restricted_stocks()["005930"]
    assert info["memo"] == "", "전체 계좌 제한이 남아 있으면 범위를 옮긴 게 아니다"
    acc = info["accounts"]["99999999-01"]
    assert acc["memo"] == "계좌만 제한" and acc["type"] == "토스"
    assert acc["date"] == before, "범위를 옮긴 것이지 새로 건 제한이 아니다"


def test_account_to_global_moves(seeded):
    auto_trade.update_restricted_stock("000660", "전체로 확대",
                                       old_cano="12345678", old_acnt="01")
    info = auto_trade.load_restricted_stocks()["000660"]
    assert info["memo"] == "전체로 확대"
    assert "12345678-01" not in info.get("accounts", {})


def test_moving_between_accounts_does_not_touch_other_scopes(store):
    """다계좌 운영에서 다른 계좌의 제한까지 함께 옮기면 안 된다."""
    auto_trade.add_restricted_stock("005930", "삼성전자", "A계좌", cano="11111111", acnt="01")
    auto_trade.add_restricted_stock("005930", "삼성전자", "B계좌", cano="22222222", acnt="01")

    auto_trade.update_restricted_stock("005930", "A계좌(수정)",
                                       old_cano="11111111", old_acnt="01",
                                       new_cano="33333333", new_acnt="01")
    accounts = auto_trade.load_restricted_stocks()["005930"]["accounts"]
    assert set(accounts) == {"22222222-01", "33333333-01"}
    assert accounts["22222222-01"]["memo"] == "B계좌"


# ─────────────────────────────────────────────
# 3. 경계
# ─────────────────────────────────────────────

def test_unknown_code_is_reported_not_silently_created(store):
    assert auto_trade.update_restricted_stock("999999", "없는 종목") is False
    assert "999999" not in auto_trade.load_restricted_stocks()


def test_emptying_every_reason_removes_the_stock(seeded):
    """사유가 통째로 비면 해제와 같은 뜻이다 — 빈 껍데기를 남기지 않는다."""
    auto_trade.update_restricted_stock("005930", "")
    assert "005930" not in auto_trade.load_restricted_stocks()


# ─────────────────────────────────────────────
# 4. 메뉴 배선
# ─────────────────────────────────────────────

def test_menu_offers_modify_as_item_three():
    import inspect

    src = inspect.getsource(at_menu.manage_restricted_stocks_menu)
    assert '("3", "제한 종목 변경", "Modify")' in src
    assert '("4", "제한 종목 해제", "Remove")' in src
    assert "_modify_restricted_stock()" in src


def test_the_ui_refuses_to_empty_the_reason(seeded, monkeypatch):
    """빈 사유는 해제와 같으므로 변경 화면에서 받지 않는다(해제 메뉴로 안내)."""
    monkeypatch.setattr(at_menu, "_print_restricted_entry_table", lambda *a, **k: None)
    answers = iter(["1", "   "])          # 번호 → 빈 메모
    monkeypatch.setattr(at_menu.Prompt, "ask", lambda *a, **k: next(answers))

    assert at_menu._modify_restricted_stock() is False
    assert auto_trade.load_restricted_stocks()["005930"]["memo"] == "오타있는 사유"


# ─────────────────────────────────────────────
# 5. 계좌 범위 선택 — 토스 분기가 덮이던 버그
# ─────────────────────────────────────────────

def test_current_account_choice_respects_toss_mode(monkeypatch):
    """종전에는 토스 분기 바로 다음 줄이 한투-자동으로 덮어써서, 토스 모드인데
    한투 계좌번호가 제한에 박혔다(elif 가 아니라 나란한 대입이었다)."""
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    monkeypatch.setattr(config.session, "cano", "77777777", raising=False)
    monkeypatch.setattr(config.session, "acnt_prdt_cd", "01", raising=False)
    monkeypatch.setattr(config.session, "auto_cano", "88888888", raising=False)
    monkeypatch.setattr(at_menu.Prompt, "ask", lambda *a, **k: "2")

    cano, acnt, acc_type = at_menu._ask_restriction_scope()
    assert (cano, acc_type) == ("77777777", "토스")


def test_current_account_choice_uses_auto_account_on_kis(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(config.session, "auto_cano", "88888888", raising=False)
    monkeypatch.setattr(config.session, "auto_acnt_prdt_cd", "01", raising=False)
    monkeypatch.setattr(at_menu.Prompt, "ask", lambda *a, **k: "2")

    cano, acnt, acc_type = at_menu._ask_restriction_scope()
    assert (cano, acc_type) == ("88888888", "한투-자동")
