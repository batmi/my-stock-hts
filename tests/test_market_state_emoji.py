"""메뉴 헤더·텔레그램 버튼·/status 가 공유하는 시장 국면 이모지.

[왜 고정하나 · 2026-08-29]
 ① 같은 if-elif 사다리가 세 곳에 복제돼 있었고, 텔레그램 버튼 매핑은 그 출력과 손으로
    맞춰야 했다 — 한 곳만 빠지면 버튼이 조용히 안 먹는다.
 ② 헤더 이모지는 utils.render_menu 안의 브레드크럼에 붙어 **모든 메뉴 화면**에서 불린다.
    지수 조회가 한 번도 성공하지 못하면 화면마다 폴백 체인(KIS→tvDatafeed→yfinance)이
    통째로 돌았다(실측 20회 렌더 → 20회 조회).
"""
import time

import pytest

import config
from modules import analysis


# ==========================================================
# 1. 단일 소스
# ==========================================================
def test_regime_emoji_covers_every_regime():
    """국면 표(REGIME_DISPLAY)와 이모지 표가 갈라지지 않는다."""
    assert set(analysis.REGIME_EMOJI) == set(analysis.REGIME_DISPLAY), \
        "국면이 한쪽에만 추가됐다 — 이모지 또는 라벨이 빠진다"
    for regime in analysis.REGIME_DISPLAY:
        assert analysis.regime_emoji(regime), regime


def test_unknown_regime_is_distinguishable():
    """조회 실패(⚪)와 판정 보류(🟡)를 구분한다."""
    assert analysis.regime_emoji("존재하지 않는 국면") == analysis.REGIME_EMOJI_UNKNOWN
    assert analysis.regime_emoji("Sideways") != analysis.REGIME_EMOJI_UNKNOWN


def test_no_duplicate_emoji_ladder_in_ui_layers():
    """[회귀] 같은 사다리가 다시 복제되면 잡는다."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("main.py", "modules/telegram_bot.py", "modules/auto_trade/trader.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert 'regime == \'Bull\'' not in src and '"Bull": "🔴"' not in src, \
            f"{rel} 에 국면→이모지 매핑이 복제돼 있다 — analysis.regime_emoji 를 쓴다"


def test_status_button_map_covers_all_emojis():
    """버튼 매핑이 이모지 목록 전체를 덮는다 — 하나라도 빠지면 그 국면에서 버튼이 죽는다."""
    from modules import telegram_bot
    emojis = telegram_bot._status_button_emojis()
    assert set(analysis.all_regime_emojis()) <= set(emojis)
    for regime in list(analysis.REGIME_DISPLAY) + ["알 수 없음"]:
        assert analysis.regime_emoji(regime) in emojis, regime


# ==========================================================
# 2. 브레드크럼이 지수 조회를 폭주시키지 않는다
# ==========================================================
@pytest.fixture
def breadcrumb(monkeypatch):
    import main
    main._MARKET_EMOJI_MEMO.clear()
    monkeypatch.setattr(config.console, "print", lambda *a, **k: None)
    yield main
    main._MARKET_EMOJI_MEMO.clear()


def test_failed_lookup_does_not_refetch_every_screen(breadcrumb, monkeypatch):
    """[회귀] 한 번도 성공하지 못한 지수를 화면마다 다시 조회하던 문제.

    analysis 의 국면 캐시는 **성공했을 때만** 채워진다(실패는 캐시하지 않는 의도적 설계 —
    사용자가 지수 화면에서 재시도할 수 있어야 하기 때문). 브레드크럼처럼 자동으로 자주
    불리는 호출자는 스스로 시간을 묶어야 한다.
    """
    calls = []
    monkeypatch.setattr(analysis, "_fetch_domestic_index_data",
                        lambda mt: calls.append(mt))     # 항상 None → 실패
    for _ in range(30):
        breadcrumb._get_market_state_emoji("KOSPI")
    assert len(calls) <= 1, f"화면마다 지수를 다시 조회한다 ({len(calls)}회)"


def test_memo_expires_so_regime_change_shows_up(breadcrumb, monkeypatch):
    """TTL 이 지나면 다시 본다 — 국면이 바뀌었는데 옛 이모지가 고착되면 안 된다."""
    calls = []
    monkeypatch.setattr(analysis, "_fetch_domestic_index_data",
                        lambda mt: calls.append(mt))
    breadcrumb._get_market_state_emoji("KOSPI")
    assert len(calls) == 1

    # 메모를 만료시킨다
    breadcrumb._MARKET_EMOJI_MEMO["KOSPI"] = (
        time.monotonic() - breadcrumb._MARKET_EMOJI_TTL_SEC - 1, "⚪")
    breadcrumb._get_market_state_emoji("KOSPI")
    assert len(calls) == 2, "TTL 이 지나도 다시 보지 않는다 — 이모지가 고착된다"


def test_emoji_falls_back_to_unknown_on_error(breadcrumb, monkeypatch):
    """국면 판정이 터져도 헤더는 살아 있어야 한다(메뉴가 안 뜨면 안 된다)."""
    def boom(*a, **kw):
        raise RuntimeError("index down")
    monkeypatch.setattr(analysis, "get_market_regime", boom)
    assert breadcrumb._get_market_state_emoji("KOSPI") == "⚪"


def test_preset_emoji_helper_is_gone():
    """프리셋 이모지는 국면 이모지로 대체됐다 — 죽은 코드가 남지 않게 한다."""
    import main
    assert not hasattr(main, "_get_preset_emoji")
