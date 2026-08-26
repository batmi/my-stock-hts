import pytest
from unittest.mock import patch, MagicMock
from modules import account
import config

import api

@patch('api.get_domestic_balance')
def test_display_balance_details_domestic(mock_balance):
    """국내 잔고 상세 출력 테스트"""
    mock_balance.return_value = (
        [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}],
        [{'scts_evlu_amt': '600000', 'tot_evlu_amt': '1000000', 'evlu_pfls_smtl_amt': '100000'}]
    )

    with patch('config.console.print') as mock_print:
        account._display_balance_details("12345678", "01")
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('api.get_overseas_balance')
@patch('api.get_domestic_balance')
def test_display_balance_details_overseas(mock_dom, mock_ovs):
    """해외 잔고 상세 출력 테스트"""
    mock_dom.return_value = ([], [])
    mock_ovs.return_value = [
        {'ovrs_item_name': 'Apple', 'ovrs_pdno': 'AAPL', 'ovrs_cblc_qty': '10', 'pchs_avg_pric': '150.0', 'ovrs_now_pric': '160.0', 'frcr_evlu_pfls_amt': '100.0', 'evlu_pfls_rt': '6.6', '_exchange': 'NASD'}
    ]
    
    with patch('config.console.print') as mock_print:
        account._display_balance_details("12345678", "01")
        assert mock_print.call_count > 0

@patch('modules.account._display_balance_details')
def test_get_account_balance_ui(mock_display):
    """계좌 잔고 조회 메뉴 테스트"""
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = "12345678"
    config.session.auto_acnt_prdt_cd = "01"

    with patch('config.console.print'):
        account.get_account_balance()
        mock_display.assert_called_with("12345678", "01")


# ==========================================================
# 모드별 계좌 표시 대상 (중복 출력 회귀)
# ==========================================================

@pytest.fixture
def session_snapshot():
    """config.session의 계좌·모드 필드를 원복한다."""
    s = config.session
    keys = ('cano', 'acnt_prdt_cd', 'auto_cano', 'auto_acnt_prdt_cd', 'is_toss', 'is_paper')
    saved = {k: getattr(s, k, None) for k in keys}
    yield s
    for k, v in saved.items():
        setattr(s, k, v)


def test_toss_mode_shows_single_account(session_snapshot):
    """토스는 주식계좌가 하나뿐이다 — 같은 계좌가 두 번 찍히면 안 된다.

    실측 증상(2026-08-26): 토스 모드에서 자산 현황이 '토스증권'과 '한투증권 (수동)'으로
    같은 계좌를 두 번 출력했다. '한투증권 (수동)' 줄이 모드와 무관하게 무조건 들어갔다.
    """
    s = session_snapshot
    s.is_toss, s.is_paper = True, False
    s.cano, s.acnt_prdt_cd = "18901501685", ""
    s.auto_cano, s.auto_acnt_prdt_cd = s.cano, s.acnt_prdt_cd

    targets = account._display_account_targets()
    assert targets == [("18901501685", "", "토스증권")], targets


def test_paper_mode_shows_single_virtual_account(session_snapshot):
    """가상투자(mode 1)는 가상 계좌 하나로 돈다."""
    s = session_snapshot
    s.is_toss, s.is_paper = False, True
    s.cano, s.acnt_prdt_cd = "PAPER", ""
    s.auto_cano, s.auto_acnt_prdt_cd = s.cano, s.acnt_prdt_cd

    targets = account._display_account_targets()
    assert len(targets) == 1, targets
    assert targets[0][2] == "가상투자", targets


def test_real_mode_splits_manual_and_auto(session_snapshot):
    """실전(mode 2)에서 자동매매 계좌가 따로면 수동/자동 둘로 가른다."""
    s = session_snapshot
    s.is_toss, s.is_paper = False, False
    s.cano, s.acnt_prdt_cd = "12345678", "01"
    s.auto_cano, s.auto_acnt_prdt_cd = "87654321", "01"

    targets = account._display_account_targets()
    assert targets == [("12345678", "01", "한투증권 (수동)"),
                       ("87654321", "01", "한투증권 (자동)")], targets


def test_real_mode_single_account_not_labeled_manual(session_snapshot):
    """자동매매 계좌를 따로 두지 않았으면 나눌 것이 없다 — '(수동)'도 붙이지 않는다."""
    s = session_snapshot
    s.is_toss, s.is_paper = False, False
    s.cano, s.acnt_prdt_cd = "12345678", "01"
    s.auto_cano, s.auto_acnt_prdt_cd = "12345678", "01"

    targets = account._display_account_targets()
    assert targets == [("12345678", "01", "한투증권")], targets


@patch('modules.account._display_asset_status')
def test_deposit_balance_prints_one_panel_in_toss(mock_status, session_snapshot):
    """자산 현황(메뉴 9)도 토스에서 한 번만 조회한다."""
    s = session_snapshot
    s.is_toss, s.is_paper = True, False
    s.cano, s.acnt_prdt_cd = "18901501685", ""
    s.auto_cano, s.auto_acnt_prdt_cd = s.cano, s.acnt_prdt_cd

    with patch('config.console.print'), patch('modules.account.time.sleep'):
        account.get_deposit_balance()
    assert mock_status.call_count == 1, mock_status.call_args_list
