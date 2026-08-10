"""관심종목 메모 관리(7-5) 진입 경로 회귀 테스트.

[배경 2026-08-10] 저장된 메모가 0건이면 목록 출력 직후 곧바로 return 해버려,
그 아래 있던 '추가' 안내까지 함께 사라졌다. 첫 메모를 만들 수단이 없어지는 상태였다.
9-5(포지션 분석)는 같은 상황을 '저장분이 없어도 작업 메뉴는 띄운다'로 이미 해결해 두었고,
메모 화면도 그 방식(작업 선택 0:조회/1:추가/2:삭제)에 맞췄다.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.manage import watchlist


@pytest.fixture
def no_sleep():
    with patch('modules.manage.watchlist.time.sleep'):
        yield


def test_empty_memo_list_still_offers_add(no_sleep):
    """메모가 0건이어도 '추가'로 들어갈 수 있어야 한다 (첫 메모를 만들 길)."""
    with patch('utils.get_all_stock_memos', return_value=[]), \
         patch('modules.manage.watchlist.add_new_stock_memo') as mock_add, \
         patch('rich.prompt.Prompt.ask', side_effect=["1", "b"]), \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        watchlist.manage_stock_memos_by_mode('view')

    mock_add.assert_called_once()


def test_empty_memo_list_can_leave_without_adding(no_sleep):
    """추가하지 않고 그냥 나가는 길도 있어야 한다 (메뉴를 잘못 눌렀을 때)."""
    with patch('utils.get_all_stock_memos', return_value=[]), \
         patch('modules.manage.watchlist.add_new_stock_memo') as mock_add, \
         patch('rich.prompt.Prompt.ask', side_effect=["b"]), \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        res = watchlist.manage_stock_memos_by_mode('view')

    assert res == 'back'
    mock_add.assert_not_called()


def test_delete_mode_leaves_immediately_when_empty(no_sleep):
    """삭제 모드는 지울 대상이 없으면 그대로 나간다 (작업 메뉴를 띄우지 않는다)."""
    with patch('utils.get_all_stock_memos', return_value=[]), \
         patch('rich.prompt.Prompt.ask') as mock_ask, \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        res = watchlist.manage_stock_memos_by_mode('delete')

    assert res == 'back'
    mock_ask.assert_not_called()


def test_add_is_reachable_when_memos_exist(no_sleep):
    """메모가 있을 때도 추가 경로는 살아 있어야 한다 (1번)."""
    memos = [{'id': 1, 'code': '005930', 'name': '삼성전자',
              'memo': 'Test', 'updated_at': '2026-08-10 10:00:00'}]
    with patch('utils.get_all_stock_memos', return_value=memos), \
         patch('modules.manage.watchlist.add_new_stock_memo') as mock_add, \
         patch('rich.prompt.Prompt.ask', side_effect=["1", "b"]), \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        watchlist.manage_stock_memos_by_mode('view')

    mock_add.assert_called_once()


def test_view_selects_stock_by_number(no_sleep):
    """조회(0)를 고르면 그 다음에 종목 번호를 묻는다."""
    memos = [{'id': 1, 'code': '005930', 'name': '삼성전자',
              'memo': 'Test', 'updated_at': '2026-08-10 10:00:00'}]
    with patch('utils.get_all_stock_memos', return_value=memos), \
         patch('modules.manage.watchlist._manage_specific_stock_memos',
               return_value='quit_to_menu') as mock_detail, \
         patch('rich.prompt.Prompt.ask', side_effect=["0", "1"]), \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        watchlist.manage_stock_memos_by_mode('view')

    mock_detail.assert_called_once_with('005930', '삼성전자', 'view')


def test_delete_action_enters_delete_mode(no_sleep):
    """삭제(2)를 고르면 삭제 모드 목록으로 들어간다."""
    memos = [{'id': 1, 'code': '005930', 'name': '삼성전자',
              'memo': 'Test', 'updated_at': '2026-08-10 10:00:00'}]
    with patch('utils.get_all_stock_memos', return_value=memos), \
         patch('modules.manage.watchlist._manage_specific_stock_memos',
               return_value='deleted') as mock_detail, \
         patch('rich.prompt.Prompt.ask', side_effect=["2", "1", "b"]), \
         patch('config.console.print'), \
         patch('utils.clear_screen'):
        watchlist.manage_stock_memos_by_mode('view')

    mock_detail.assert_called_once_with('005930', '삼성전자', 'delete')
