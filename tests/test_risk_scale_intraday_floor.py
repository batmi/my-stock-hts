"""리스크 배수는 같은 날 안에서 조이기만 한다 — 푸는 것은 다음 날 첫 주기(trader._hold_intraday_floor).

[무엇이 있었나 · 2026-10-24 파이 가상투자 08-21~10-23 재독]
 배수를 주기(약 1분)마다 새로 재면서, 드로다운이 5%·10% 문턱 근처에 있거나 지수가 '하락 미확정'
 경계에 있는 날 1~2분 간격으로 오르내렸다(배수 변경 로그 하루 최대 21줄 · 09-08).
 09-14 에는 히트 캡(x0.49 → 4.9%)이 45분간 막고 있던 삼성E&A 진입이 배수가 x0.90 으로 풀린
 6분 사이에 16주로 들어갔다(09-29 시간청산 -5.3%). 이 배수를 검증한 감사는 일봉 종가로 하루
 한 값을 썼다 — 분 단위로 풀렸다 조였다 하는 것은 검증된 동작이 아니다.

[고정하는 것]
 · 장중에 나빠지면 그 주기에 바로 따른다(급락일 대응은 그대로).
 · 장중에 좋아져도 그날은 유지하고, 사유에 그렇다고 적는다. 안내 로그는 하루 1회.
 · 다음 날 첫 주기에는 그때 계산값으로 다시 시작한다.
 · 장중 재기동에도 풀리지 않는다(일일 상태 파일). 어제 기록은 쓰지 않는다.
 · 설정을 바꾸면 그 자리에서 새로 잰다.
 · 어떤 순서로 값이 와도 적용값은 그 주기 계산값 이하이고, 그날 계산값의 최솟값과 같다.
"""
import random

import pytest

import config
from modules import analysis
from modules.auto_trade import common as at_common
from modules.auto_trade.trader import AutoTrader

PARAMS = {
    "USE_REGIME_RISK_SCALING": True, "PENDING_DOWN_RISK_SCALE": 0.6, "BEAR_RISK_SCALE": 1.0,
    "USE_WHIPSAW_RISK_SCALING": True, "WHIPSAW_LO": 0.40, "WHIPSAW_HI": 0.75, "WHIPSAW_MIN_SCALE": 0.85,
    "USE_DRAWDOWN_RISK_SCALING": True, "DD_LEVEL_1": 5.0, "DD_SCALE_1": 0.9,
    "DD_LEVEL_2": 10.0, "DD_SCALE_2": 0.8, "DD_LOOKBACK_DAYS": 90,
}
CALM = {'regime': "Bull", 'score_adj': 0.0, 'moved_pct': 0.0, 'whipsaw_ratio': 0.10,
        'segments': 6, 'unknown': False}
PEND = dict(CALM, regime="PendDown", whipsaw_ratio=0.62)


class World:
    """주기마다 바꿔 끼우는 시장·계좌 상태."""
    def __init__(self):
        self.day = "2026-09-14"
        self.dd = 7.4
        self.regime = {"KOSPI": CALM, "KOSDAQ": CALM}


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(config, "RISK_SCALING_PARAMS", dict(PARAMS))
    monkeypatch.setattr(analysis, "get_market_regime_detail", lambda m: w.regime[m])
    monkeypatch.setattr(AutoTrader, "_get_account_drawdown_pct", lambda self, p=None: w.dd)
    monkeypatch.setattr(AutoTrader, "_risk_floor_day", staticmethod(lambda: w.day))
    monkeypatch.setattr("modules.auto_trade.trader.trade_account_key", lambda: "50000000-29")
    monkeypatch.setattr("modules.auto_trade.alert_delivered", lambda *a, **k: True)   # 드로다운 10% 경보
    return w


def _trader():
    """싱글톤을 거치지 않은 맨 인스턴스 — '재기동'은 새로 하나 만드는 것이다."""
    t = object.__new__(AutoTrader)
    t.risk_scale = 1.0
    t.risk_scale_by_market = {}
    t.risk_scale_regime_by_market = {}
    t.risk_scale_reason = ""
    t.risk_scale_reason_by_market = {}
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.logs = []
    t.log = lambda m, *a, **k: t.logs.append(m)
    return t


def _scale_lines(t):
    return [m for m in t.logs if m.startswith("[리스크 스케일링] 신규 진입") or "정상 복원" in m]


# ─────────────────────────── 09-14 재현 ───────────────────────────

def test_the_6_minute_window_that_let_a_blocked_entry_in_stays_shut(world):
    """실측 재현: x0.49 → (KOSPI 하락 미확정 해소) x0.90 → x0.49. 가운데 6분에 히트 캡이 9.0% 로 열렸다."""
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()
    tight = t.risk_scale
    assert tight == pytest.approx(0.6 * 0.9 * 0.9, abs=0.01), "전제 — 하락 미확정·휩소율·드로다운이 겹친 배수"

    world.regime["KOSPI"] = CALM                 # 11:52 — 지수가 경계 위로
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(tight), "장중에 풀렸다 — 그 사이로 막혀 있던 진입이 들어간다"
    assert t.risk_scale_by_market["KOSPI"] == pytest.approx(tight)
    assert t.risk_scale_reason.startswith("[장중 유지]"), t.risk_scale_reason
    assert "하락 미확정" in t.risk_scale_reason_by_market["KOSPI"]

    world.regime["KOSPI"] = PEND                 # 11:58 — 다시 경계 아래
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(tight)
    assert len(_scale_lines(t)) == 1, _scale_lines(t)


def test_the_heat_cap_follows_the_held_scale(world):
    """히트 캡·종목 사이징이 읽는 값도 유지된 배수다 — 표시만 유지하고 게이트는 풀리면 소용없다."""
    from modules.auto_trade.engine import RiskManager
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()
    world.regime["KOSPI"] = CALM
    t._update_risk_scale()
    rm = object.__new__(RiskManager)
    rm.trader = t
    assert rm.current_risk_scale("KOSPI") == pytest.approx(t.risk_scale_by_market["KOSPI"])
    assert rm.current_risk_scale() < 0.9 * 0.999


# ─────────────────────────── 조이는 쪽은 즉시 ───────────────────────────

def test_a_worse_reading_applies_in_the_same_cycle(world):
    t = _trader()
    world.dd = 3.0
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(1.0)
    world.regime["KOSPI"] = PEND
    world.dd = 11.0
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(0.6 * 0.9 * 0.8, abs=0.01)
    assert not t.risk_scale_reason.startswith("[장중 유지]")


def test_the_drawdown_threshold_no_longer_flaps(world):
    """드로다운 5% 문턱을 오가는 날 — 종전에는 오갈 때마다 한 줄씩, 배수도 따라 흔들렸다."""
    t = _trader()
    seen = []
    for dd in (5.1, 4.9, 5.2, 4.8, 5.05, 4.95):
        world.dd = dd
        t._update_risk_scale()
        seen.append(t.risk_scale)
    assert seen == [pytest.approx(0.9)] * len(seen)
    assert len(_scale_lines(t)) == 1


def test_the_hold_is_announced_once_a_day(world):
    t = _trader()
    world.dd = 5.5
    t._update_risk_scale()
    for dd in (4.0, 5.5, 4.0, 4.0):
        world.dd = dd
        t._update_risk_scale()
    notes = [m for m in t.logs if "장중에 풀렸지만 오늘은 유지" in m]
    assert len(notes) == 1, notes
    assert "KOSPI 계산 x1.00 → 유지 x0.90" in notes[0]


# ─────────────────────────── 푸는 것은 다음 날 ───────────────────────────

def test_the_next_day_starts_from_its_own_reading(world):
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()
    world.regime["KOSPI"] = CALM
    world.dd = 2.0
    t._update_risk_scale()
    assert t.risk_scale < 1.0

    world.day = "2026-09-15"
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(1.0), t.risk_scale_reason
    assert t.risk_scale_reason == ""
    assert any("정상 복원" in m for m in t.logs)


def test_a_new_day_can_start_tighter_than_yesterday_closed(world):
    t = _trader()
    world.dd = 2.0
    t._update_risk_scale()
    world.day = "2026-09-15"
    world.dd = 11.0
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(0.8)


# ─────────────────────────── 재기동 ───────────────────────────

def test_a_restart_during_the_day_keeps_the_floor(world):
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()
    tight = t.risk_scale

    world.regime["KOSPI"] = CALM
    t2 = _trader()                               # 같은 날 재기동 — 메모리는 비었다
    t2._update_risk_scale()
    assert t2.risk_scale == pytest.approx(tight), "재기동으로 장중 최저가 풀렸다"
    assert t2.risk_scale_by_market["KOSPI"] == pytest.approx(tight)
    assert t2.risk_scale_reason.startswith("[장중 유지]")


def test_yesterdays_floor_is_not_carried_into_today(world):
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()

    world.regime["KOSPI"] = CALM
    world.dd = 2.0
    world.day = "2026-09-15"
    t2 = _trader()
    t2._update_risk_scale()
    assert t2.risk_scale == pytest.approx(1.0)


def test_the_floor_does_not_clobber_the_daily_baseline(world, monkeypatch):
    """같은 파일에 시작 자산·기준 원금이 있다 — 한쪽 저장이 다른 쪽을 지우면 차단기 기준이 사라진다."""
    monkeypatch.setattr(at_common, "datetime", _FixedDay(world.day))
    key = "50000000-29"
    assert at_common.save_daily_initial_asset(key, 9_000_000, principal=9_500_000)
    t = _trader()
    world.dd = 6.0
    t._update_risk_scale()
    assert at_common.load_daily_initial_asset(key) == 9_000_000
    assert at_common.load_daily_principal(key) == 9_500_000

    assert at_common.save_daily_initial_asset(key, 9_100_000)
    floor = at_common.load_daily_risk_floor(key, world.day)
    assert floor and floor["scale"] == pytest.approx(0.9)


def test_a_failed_save_warns_once_and_still_holds_in_session(world, monkeypatch, caplog):
    monkeypatch.setattr("modules.auto_trade.trader.save_daily_risk_floor", lambda *a, **k: False)
    t = _trader()
    world.regime["KOSPI"] = PEND
    with caplog.at_level("WARNING"):
        t._update_risk_scale()
        world.regime["KOSPI"] = dict(PEND, whipsaw_ratio=0.75)
        t._update_risk_scale()
    warns = [r for r in caplog.records if "최저 배수를 파일에 남기지 못했습니다" in r.getMessage()]
    assert len(warns) == 1
    tight = t.risk_scale
    world.regime["KOSPI"] = CALM
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(tight)


def test_an_unreadable_file_falls_back_to_the_plain_reading(world, monkeypatch):
    """재기동 뒤 파일을 못 읽으면 이번 주기 계산값이다 — 종전 동작이고, 그보다 느슨해지지는 않는다."""
    t = _trader()
    world.regime["KOSPI"] = PEND
    t._update_risk_scale()
    monkeypatch.setattr("modules.auto_trade.trader.load_daily_risk_floor", lambda *a, **k: None)
    world.regime["KOSPI"] = CALM
    t2 = _trader()
    t2._update_risk_scale()
    assert t2.risk_scale == pytest.approx(0.9)


# ─────────────────────────── 설정 변경 ───────────────────────────

def test_changing_the_settings_takes_effect_at_once(world, monkeypatch):
    t = _trader()
    world.dd = 6.0
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(0.9)
    monkeypatch.setattr(config, "RISK_SCALING_PARAMS", dict(PARAMS, USE_DRAWDOWN_RISK_SCALING=False))
    t._update_risk_scale()
    assert t.risk_scale == pytest.approx(1.0), "축을 껐는데 그날 내내 묶였다"


# ─────────────────────────── 성질 ───────────────────────────

def test_the_applied_scale_is_the_running_minimum_and_never_looser(world):
    """어떤 순서로 값이 와도 — 적용값 ≤ 그 주기 계산값, 적용값 = 그날 계산값의 누적 최솟값."""
    rng = random.Random(20261024)
    regimes = [CALM, PEND, dict(PEND, regime="Sideways"), dict(CALM, whipsaw_ratio=0.70)]
    for trial in range(30):
        world.day = f"2026-10-{trial % 28 + 1:02d}"
        t = _trader()
        probe = _trader()                                  # 래칫 없이 같은 값을 잰다
        probe._hold_intraday_floor = lambda p, s, r, ms, mr: (s, r, ms, mr)
        mins = {"ACC": 1.0, "KOSPI": 1.0, "KOSDAQ": 1.0}
        for _ in range(25):
            world.dd = rng.choice((0.0, 4.9, 5.1, 9.9, 10.1, 12.0))
            world.regime = {"KOSPI": rng.choice(regimes), "KOSDAQ": rng.choice(regimes)}
            t._update_risk_scale()
            probe._update_risk_scale()
            mins = {"ACC": min(mins["ACC"], probe.risk_scale),
                    **{m: min(mins[m], probe.risk_scale_by_market[m]) for m in ("KOSPI", "KOSDAQ")}}
            assert t.risk_scale <= probe.risk_scale + 1e-9
            assert t.risk_scale == pytest.approx(mins["ACC"])
            for m in ("KOSPI", "KOSDAQ"):
                assert t.risk_scale_by_market[m] <= probe.risk_scale_by_market[m] + 1e-9
                assert t.risk_scale_by_market[m] == pytest.approx(mins[m])
            assert t.risk_scale == pytest.approx(min(t.risk_scale_by_market.values()))


class _FixedDay:
    """common.datetime 대역 — 파일의 날짜를 테스트의 '하루'에 맞춘다."""
    def __init__(self, day):
        import datetime as _dt
        self._now = _dt.datetime.strptime(day + " 10:00", "%Y-%m-%d %H:%M")

    def now(self, tz=None):
        return self._now
