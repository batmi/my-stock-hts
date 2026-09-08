"""국면을 '못 읽은 것'과 '횡보'는 다른 사실이다 — 그 둘이 같은 값이던 자리를 고정한다.

[무엇이 문제였나 · 2026-09-08]
 get_market_regime_detail 은 지수 조회가 실패해도, 데이터가 모자라도, 시장이 실제로 횡보여도
 **완전히 같은 딕셔너리**를 돌려줬다. 그 값을 읽는 것은 진입 리스크 스케일링인데, 횡보는
 '축소할 이유 없음'이라 배수가 x1.00 으로 돌아간다. 즉 지수가 끊긴 그 순간에 신규 진입
 한도가 최대로 열렸고, 로그는 "리스크 한도 정상 복원"이라고 말했다 — 회복이 아니라 실명이다.

 시장 필터(USE_MARKET_FILTER)를 켠 계정은 같은 장애에서 매수 자체가 보류되므로 가려져 있었다.
 필터를 끈 운용(라즈베리파이 가상투자)에서는 이 배수가 **유일하게 남은 시장 인식 브레이크**다.
"""
import pandas as pd
import pytest

import config
from modules import analysis


# ---------------------------------------------------------------------------
# 1. 판정 불가는 판정 불가라고 말해야 한다
# ---------------------------------------------------------------------------

def _clear():
    analysis._MARKET_REGIME_CACHE.clear()
    analysis._REGIME_UNKNOWN_WARNED.clear()


def test_a_short_series_says_it_could_not_judge():
    df = pd.DataFrame({'date': [f"2026090{i}" for i in range(1, 6)],
                       'close': [100.0, 101, 102, 103, 104]})
    info = analysis.classify_regime_from_df(df)
    assert info['unknown'] is True, info


def test_a_real_judgement_says_it_judged():
    #  느린 EMA(41) 를 채우고도 남는 우상향 시계열 — 실제로 판정이 성립한다.
    closes = [100.0 * (1.01 ** i) for i in range(200)]
    df = pd.DataFrame({'date': range(200), 'close': closes})
    info = analysis.classify_regime_from_df(df)
    assert info['unknown'] is False, info
    assert info['regime'] in analysis.REGIME_DISPLAY


def test_a_failed_index_lookup_is_not_reported_as_sideways(monkeypatch):
    """조회 실패와 실제 횡보가 같은 값이면 아래 스케일링이 둘을 구분할 방법이 없다."""
    def _boom(*a, **k):
        raise RuntimeError("지수 조회 실패")

    _clear()
    monkeypatch.setattr(analysis, 'get_domestic_index_data', _boom)
    info = analysis.get_market_regime_detail("KOSPI")
    assert info['unknown'] is True, info


def test_the_silence_is_broken_when_the_regime_cannot_be_judged(monkeypatch, caplog):
    """가드가 조용하면 없는 것과 같다 — 판정 불가는 거래일당 한 번 로그에 남는다."""
    _clear()
    monkeypatch.setattr(analysis, 'get_domestic_index_data', lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        analysis.get_market_regime_detail("KOSPI")
        analysis.get_market_regime_detail("KOSPI")   # 같은 거래일 — 두 번 찍히면 안 된다
    hits = [r for r in caplog.records if "MARKET_REGIME" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in hits]
    assert "판정하지 못했습니다" in hits[0].getMessage()


# ---------------------------------------------------------------------------
# 2. 모르면 완화하지 않는다
# ---------------------------------------------------------------------------

def _trader(monkeypatch):
    from modules.auto_trade.trader import AutoTrader
    t = object.__new__(AutoTrader)      # Class.__new__ 는 싱글톤을 돌려준다
    t.risk_scale = 1.0
    t.risk_scale_by_market = {}
    t.risk_scale_regime_by_market = {}
    t.risk_scale_reason = ""
    t.risk_scale_reason_by_market = {}
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.logs = []
    t.log = lambda m, *a, **k: t.logs.append(m)
    monkeypatch.setattr(AutoTrader, '_get_account_drawdown_pct', lambda self, p=None: 0.0)
    return t


def _regime(monkeypatch, **kw):
    base = {'regime': "Sideways", 'score_adj': 0.0, 'moved_pct': 0.0,
            'whipsaw_ratio': None, 'segments': 0, 'unknown': True}
    base.update(kw)
    monkeypatch.setattr(analysis, 'get_market_regime_detail', lambda m: base)


BAD = dict(regime="PendDown", whipsaw_ratio=0.85, segments=6, unknown=False)


def test_a_blind_cycle_does_not_reopen_the_entry_limit(monkeypatch):
    """실측 재현: x0.51 → (지수 조회 실패) → 종전 x1.00. 눈을 감은 순간 한도가 최대로 열렸다."""
    t = _trader(monkeypatch)
    _regime(monkeypatch, **BAD)
    t._update_risk_scale()
    measured = t.risk_scale
    assert measured < 1.0, "전제가 성립하지 않는다 — 악조건에서 배수가 줄어야 한다"

    _regime(monkeypatch)                      # 판정 불가
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(measured), (
        f"국면을 못 읽자 한도가 {measured:.2f} → {t.risk_scale:.2f} 로 열렸다")
    assert t.risk_scale_by_market["KOSPI"] == pytest.approx(measured)
    assert "판정 불가" in t.risk_scale_reason


def test_the_limit_is_released_only_when_the_market_is_actually_seen(monkeypatch):
    """유지가 고착이 되면 안 된다 — 실제로 강세로 판정되면 배수는 풀린다."""
    t = _trader(monkeypatch)
    _regime(monkeypatch, **BAD)
    t._update_risk_scale()
    _regime(monkeypatch)
    t._update_risk_scale()
    _regime(monkeypatch, regime="Bull", whipsaw_ratio=0.05, segments=6, unknown=False)
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(1.0), t.risk_scale_reason


def test_a_cold_start_while_blind_stays_at_the_old_behaviour(monkeypatch):
    """직전 측정치가 아예 없으면 유지할 값도 없다 — 종전대로 x1.00 이고, 그 사실을 숨기지 않는다."""
    t = _trader(monkeypatch)
    _regime(monkeypatch)
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(1.0)


def test_the_held_scale_does_not_multiply_the_drawdown_twice(monkeypatch):
    """저장본에는 드로다운이 이미 곱해져 있다. 그것을 국면 배수로 재사용하면 두 번 곱해진다."""
    from modules.auto_trade.trader import AutoTrader
    t = _trader(monkeypatch)
    monkeypatch.setattr(AutoTrader, '_get_account_drawdown_pct', lambda self, p=None: 12.0)
    _regime(monkeypatch, **BAD)
    t._update_risk_scale()
    with_dd = t.risk_scale
    assert with_dd < 0.51, "전제가 성립하지 않는다 — 드로다운 배수가 걸려야 한다"

    _regime(monkeypatch)
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(with_dd), (
        f"판정 불가 주기에 드로다운이 다시 곱해졌다 ({with_dd:.4f} → {t.risk_scale:.4f})")


def test_the_recovery_message_is_not_printed_while_blind(monkeypatch):
    """'리스크 한도 정상 복원'은 회복됐을 때만 나와야 한다 — 실명 중에 나오면 거짓말이다."""
    t = _trader(monkeypatch)
    _regime(monkeypatch, **BAD)
    t._update_risk_scale()
    t.logs.clear()
    _regime(monkeypatch)
    t._update_risk_scale()
    assert not [m for m in t.logs if "정상 복원" in m], t.logs


# ---------------------------------------------------------------------------
# 3. 화면도 같은 사실을 말해야 한다
# ---------------------------------------------------------------------------

def test_the_unknown_marker_finally_reaches_the_screen():
    """⚪ 는 '조회 실패'를 위해 만들어졌는데 종전에는 한 번도 도달하지 않았다."""
    emoji, label = analysis.describe_regime(
        {'regime': "Sideways", 'segments': 0, 'whipsaw_ratio': None, 'unknown': True})
    assert emoji == analysis.REGIME_EMOJI_UNKNOWN
    assert label == "판정 불가"
    #  ⚪ 는 하단 버튼 매칭 목록에 이미 들어 있다 — 화면에 나와도 버튼이 깨지지 않는다.
    assert analysis.REGIME_EMOJI_UNKNOWN in analysis.all_regime_emojis()


def test_a_real_sideways_market_is_still_called_sideways():
    """'판정 보류'는 실제 횡보에만 쓰인다 — 둘을 뒤집으면 반대 방향의 거짓말이다."""
    emoji, label = analysis.describe_regime(
        {'regime': "Sideways", 'segments': 6, 'whipsaw_ratio': 0.3, 'unknown': False})
    assert emoji == analysis.REGIME_EMOJI["Sideways"]
    assert label == analysis.REGIME_DISPLAY["Sideways"][0]


def test_no_screen_keeps_its_own_copy_of_the_regime_label():
    """국면 라벨을 직접 조립하는 화면이 남아 있으면 그 화면만 옛 글자를 계속 쓴다."""
    offenders = []
    checked = 0
    for path in ("modules/auto_trade/trader.py", "modules/telegram_bot.py"):
        src = open(path, encoding='utf-8').read()
        #  [자기검사] 이 화면이 국면을 아예 안 그리게 되면 위 순회는 조용히 0곳을 보고
        #   테스트는 통과한다 — 그 침묵을 막는다.
        assert "describe_regime" in src, f"{path}: 국면 표시가 사라졌다면 이 가드를 다시 짜라"
        checked += 1
        for i, line in enumerate(src.splitlines(), 1):
            code = line.split("  #")[0]
            if line.strip().startswith("#"):
                continue
            if "regime_emoji(" in code and "def regime_emoji" not in code:
                offenders.append(f"{path}:{i}")
    assert not offenders, (
        "국면 이모지를 describe_regime 을 거치지 않고 직접 만든다 — "
        f"판정 불가가 다시 '판정 보류'로 접힌다: {offenders}")
    assert checked == 2
