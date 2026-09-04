"""/scan 은 프리셋 하나가 실패해도 나머지를 돌려주고, 고른 지표를 실제로 보여준다.

[배경 · 2026-09-04 감사]
 1. 프리셋 루프가 타임아웃 외의 예외를 `raise e` 로 올려보냈다. 10개 중 하나가 실패하면
    그때까지 쌓아 둔 응답이 통째로 버려지고 사용자는 오류 문구만 받았다.
    (메뉴 6-2 스크리너도 같은 코드였다 — 함께 고쳤다)
 2. PER/ROE/배당을 덧붙이는 분기 조건이 "ValueRebound" 였는데 그런 프리셋 키는 없다
    (정식 키는 ValueTurnaround). 이름이 바뀌며 죽은 분기라, PER 1~12 · ROE>15% 로
    고른 '저평가 우량주'인데 정작 그 수치가 결과에 안 붙었다(메뉴 표에는 붙는다).
"""
import inspect

import pytest

from modules import telegram_bot as tb
from modules import theme_analysis as ta


def _scan_source():
    return inspect.getsource(tb.TelegramCommander._execute_scan)


# ─────────────────────────────────────────────
# 1. 한 프리셋 실패가 전체를 버리지 않는가
# ─────────────────────────────────────────────

def test_a_failing_preset_does_not_discard_the_whole_reply():
    src = _scan_source()
    loop = src[src.index("for attempt in range(3):"):src.index("if df is not None")]
    assert "raise e" not in loop, "프리셋 실패가 여전히 응답 전체를 버린다"
    assert "break" in loop


def test_skipped_presets_are_named_in_the_reply():
    """조용히 빠지면 '조건에 맞는 종목 없음'과 구분되지 않는다."""
    src = _scan_source()
    assert "failed.append" in src
    assert "건너뛴 조건" in src


def test_timeout_still_retries():
    src = _scan_source()
    assert "time.sleep(1.5)" in src and "attempt < 2" in src


# ─────────────────────────────────────────────
# 2. 죽은 프리셋 키
# ─────────────────────────────────────────────

def _scan_code_only():
    """주석을 뺀 실행 코드만 — 왜 고쳤는지 적은 주석에 옛 이름이 남아 있어도 되게."""
    out = []
    for line in _scan_source().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


def test_no_dead_preset_key_remains():
    assert "ValueRebound" not in _scan_code_only(), "존재하지 않는 프리셋 키로 분기하고 있다"


def test_value_and_dividend_presets_get_their_numbers():
    """이 두 프리셋은 PER·ROE·배당으로 고른 것이라, 그 수치가 결과의 알맹이다."""
    src = _scan_source()
    assert 'tg_to_id[preset_key] in ("value", "dividend")' in src


def test_every_telegram_key_maps_to_a_real_preset():
    """정식 ID 로 판정하므로, 매핑이 어긋나면 KeyError 로 즉시 드러난다."""
    src = _scan_source()
    keys = set(__import__("re").findall(r'\("(\w+)", "', src))
    mapping = dict(__import__("re").findall(r'"(\w+)": "(\w+)"', src))
    for k in keys:
        if k in mapping:
            assert mapping[k] in ta.SCREENER_PRESETS, f"{k} → {mapping[k]} 는 없는 프리셋"


def test_screener_presets_cover_both_surfaces():
    """메뉴와 텔레그램이 같은 프리셋 집합을 쓴다(단일 관리 지점)."""
    menu_ids = {pid for pids in ta.SCREENER_MENU_TO_ID.values() for pid in pids}
    assert menu_ids == set(ta.SCREENER_PRESETS)
