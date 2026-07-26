"""설정 메뉴 '전체보기' 동선 테스트.

- 메인메뉴 0 > 9(전체 조회)는 그룹을 되묻지 않고 바로 전체를 출력한다.
- 하위 메뉴 1~5에는 '9. 전체보기'가 있고, 기본 선택지가 9다.
"""
import inspect
import re

from modules import settings as S


def _menu_src():
    for name in dir(S):
        fn = getattr(S, name)
        if inspect.isfunction(fn) and 'menu_items = [' in (inspect.getsource(fn) if fn.__module__ == S.__name__ else ''):
            src = inspect.getsource(fn)
            if '"시스템 설정 전체 조회"' in src:
                return src
    raise AssertionError("설정 메뉴 함수를 찾지 못했습니다")


def test_view_all_item_constant():
    assert S._VIEW_ALL_KEY == "9"
    assert S._VIEW_ALL_ITEM == ("9", "전체보기", "View All")


def test_top_level_view_config_has_no_group_prompt():
    """0>9는 '조회할 설정 그룹 선택'을 묻지 않고 바로 전체를 출력한다."""
    src = _menu_src()
    assert "조회할 설정 그룹 선택" not in src
    assert "view_system_config(None)" in src


def test_all_five_submenus_offer_view_all():
    src = _menu_src()
    assert src.count("_VIEW_ALL_ITEM") == 5
    assert src.count("default_choice=_VIEW_ALL_KEY") == 5
    # 하위 메뉴 1~5가 각자 자기 그룹을 조회한다
    assert sorted(re.findall(r"view_system_config\((\d)\)", src)) == ["1", "2", "3", "4", "5"]


def test_view_all_branch_precedes_section_dispatch():
    """'9'가 sub_map에 들어 있으므로 편집 분기보다 먼저 걸러져야 한다."""
    src = _menu_src()
    for grp in ("1", "3", "4"):
        blk = src.split(f'elif choice == "{grp}":' if grp != "1" else 'if choice == "1":')[1]
        i_view = blk.index("if sub_choice == _VIEW_ALL_KEY:")
        i_edit = blk.index("_edit_section(")
        assert i_view < i_edit, f"choice {grp}: 전체보기 분기가 편집 분기보다 뒤에 있음"


def test_view_system_config_accepts_group_and_none():
    sig = inspect.signature(S.view_system_config)
    assert sig.parameters['group'].default is None
