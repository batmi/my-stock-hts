"""시장 세션 표기(표 제목 옆 라벨) 테스트.

같은 표라도 08:30의 현재가는 NXT 프리마켓 체결가, 10:00은 KRX 정규장가, 22:00은 이미
마감된 KRX 확정 종가다. 값만으로는 구분이 안 되므로 표 제목에 세션을 붙여 오독을 막는다.

세션 경계는 기존 판정과 어긋나면 안 된다.
  - 국내: api._nxt_quote_phase() (프리 08:00~09:00 / 정규 09:00~15:30 / 애프터 15:30~20:00)
  - 미국: modules/trading.py 주문 세션 자동판별 (ET 04:00/09:30/16:00/20:00)
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import api


class _FrozenDatetime(datetime):
    """api 모듈의 datetime.now()만 고정한다(다른 동작은 datetime 그대로)."""
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _freeze_kr(dt, holiday=False, simulation=False):
    _FrozenDatetime._now = dt
    return (patch.object(api, 'datetime', _FrozenDatetime),
            patch.object(api, 'is_holiday_today', lambda: holiday),
            patch.object(api.config.session, 'is_simulation', simulation))


# ==========================================================
# 국내 — domestic_session_phase
# ==========================================================

@pytest.mark.parametrize("hh, mm, expect", [
    (7, 59, 'closed'),      # 개장 전 야간
    (8, 0, 'nxt_pre'),      # NXT 프리마켓 시작
    (8, 59, 'nxt_pre'),
    (9, 0, 'krx'),          # KRX 정규장 시작
    (15, 29, 'krx'),
    (15, 30, 'nxt_after'),  # KRX 마감 → NXT 애프터마켓
    (20, 0, 'nxt_after'),   # 애프터마켓 종료 시각(포함) — _nxt_quote_phase와 동일
    (20, 1, 'closed'),
    (23, 30, 'closed'),
])
def test_domestic_session_phase_boundaries(hh, mm, expect):
    a, b, c = _freeze_kr(datetime(2026, 7, 28, hh, mm))
    with a, b, c:
        assert api.domestic_session_phase() == expect


def test_domestic_session_phase_holiday_overrides_clock():
    """휴장일은 정규장 시간대여도 'holiday'."""
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 10, 0), holiday=True)
    with a, b, c:
        assert api.domestic_session_phase() == 'holiday'


def test_domestic_phase_matches_nxt_quote_phase():
    """표기 경계와 시세 처리 경계(_nxt_quote_phase)가 어긋나지 않는다."""
    mapping = {'nxt_pre': 'active', 'nxt_after': 'active',
               'krx': 'skip', 'closed': 'offhours', 'holiday': 'offhours'}
    for hh in range(24):
        for mm in (0, 29, 30, 59):
            a, b, c = _freeze_kr(datetime(2026, 7, 28, hh, mm))
            with a, b, c:
                assert mapping[api.domestic_session_phase()] == api._nxt_quote_phase(), \
                    f"{hh:02d}:{mm:02d} 경계 불일치"


# ==========================================================
# 국내 — 라벨
# ==========================================================

def test_domestic_label_regular_is_krx():
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 10, 0))
    with a, b, c:
        text, style = api.market_session_label(False)
    assert text == "KRX 정규장"
    assert style == "green"


@pytest.mark.parametrize("hh, mm, expect", [
    (8, 30, "NXT 프리마켓 · KRX 개장 전"),
    (16, 0, "NXT 애프터마켓 · KRX 마감"),
])
def test_domestic_label_nxt_windows(hh, mm, expect):
    a, b, c = _freeze_kr(datetime(2026, 7, 28, hh, mm))
    with a, b, c:
        text, style = api.market_session_label(False)
    assert text == expect
    assert style == "yellow"


def test_domestic_label_simulation_marks_nxt_unsupported():
    """모의투자(VTS)는 NXT 미지원 — 그 시간대에도 화면값은 KRX 종가다."""
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 8, 30), simulation=True)
    with a, b, c:
        text, style = api.market_session_label(False)
    assert "모의투자" in text and "KRX 종가" in text
    assert style == "dim"


@pytest.mark.parametrize("krx_fixed, expect_basis", [
    (True, "KRX 종가"),      # USE_KRX_CLOSE_AFTER_HOURS=True
    (False, "NXT 최종가"),   # 끄면 마지막 실거래가(전날 NXT 종가)를 그대로 노출
])
def test_domestic_label_closed_shows_price_basis(krx_fixed, expect_basis):
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 22, 0))
    with a, b, c, patch.object(api, 'display_price_krx_fixed', lambda _=False: krx_fixed):
        text, style = api.market_session_label(False)
    assert text == f"장 마감 · {expect_basis}"
    assert style == "dim"


# ==========================================================
# 국내 ETF — NXT 비거래
# ==========================================================

@pytest.mark.parametrize("hh, mm, session", [
    (8, 30, "NXT 프리마켓"),
    (16, 0, "NXT 애프터마켓"),
])
def test_domestic_etf_label_marks_nxt_untraded(hh, mm, session):
    """NXT는 ETF/ETN을 취급하지 않는다 — 세션은 열려도 값은 KRX 종가에서 멈춘다.
    세션 이름만 띄우면 '지금 거래 중'으로 오독되므로 미거래를 명시하고 dim 처리한다."""
    a, b, c = _freeze_kr(datetime(2026, 7, 28, hh, mm))
    with a, b, c:
        text, style = api.market_session_label(False, is_domestic_etf=True)
    assert text == f"{session} · ETF 미거래(KRX 종가)"
    assert style == "dim"


def test_domestic_etf_label_regular_session_unchanged():
    """정규장에는 ETF도 KRX에서 정상 거래된다 — 주식 표와 같은 라벨."""
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 10, 0))
    with a, b, c:
        assert api.market_session_label(False, is_domestic_etf=True) == ("KRX 정규장", "green")


def test_domestic_etf_label_closed_always_krx_basis():
    """ETF는 NXT 체결 자체가 없어, USE_KRX_CLOSE_AFTER_HOURS를 꺼도 기준은 KRX 종가다."""
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 22, 0))
    with a, b, c, patch.object(api, 'display_price_krx_fixed', lambda _=False: False):
        text, _style = api.market_session_label(False, is_domestic_etf=True)
    assert text == "장 마감 · KRX 종가"


def test_us_label_ignores_domestic_etf_flag():
    """미국 표에는 국내 ETF 플래그가 영향을 주지 않는다."""
    et = datetime(2026, 7, 22, 10, 30)
    with patch.object(api, 'now_us_eastern', lambda: et):
        assert api.market_session_label(True, is_domestic_etf=True)[0] == "정규장 · ET 10:30"


def test_domestic_label_holiday():
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 11, 0), holiday=True)
    with a, b, c, patch.object(api, 'display_price_krx_fixed', lambda _=False: True):
        text, _style = api.market_session_label(False)
    assert text.startswith("휴장")


# ==========================================================
# 세션 전환 알림 — 자정 날짜 변경 노이즈 차단
# ==========================================================

def test_session_phase_key_folds_closed_and_holiday():
    """마감·휴장은 둘 다 '거래 없음' — 같은 키여야 한다.

    휴장 판정이 달력일 기준이라 자정에 'holiday'↔'closed'가 뒤집힌다. 접지 않으면
    시장에서 아무 일도 없는 자정마다 '시장 상태 변경' 텔레그램이 나간다
    (실측 2026-08-03 00:00 "장 마감 · KRX 종가").
    """
    from modules.auto_trade.trader import session_phase_key
    assert session_phase_key('closed') == session_phase_key('holiday')
    # 거래 있는 단계는 서로/idle과 구분된 채로 남아야 한다
    live = {session_phase_key(p) for p in ('nxt_pre', 'krx', 'nxt_after')}
    assert live == {'nxt_pre', 'krx', 'nxt_after'}
    assert session_phase_key('closed') not in live


def test_session_phase_key_midnight_rollover_is_silent():
    """일 23:59 → 월 00:00: 실제 phase는 바뀌지만 알림 키는 그대로다."""
    from modules.auto_trade.trader import session_phase_key
    a, b, c = _freeze_kr(datetime(2026, 8, 2, 23, 59), holiday=True)   # 일요일 심야
    with a, b, c:
        before = session_phase_key(api.domestic_session_phase())
    a, b, c = _freeze_kr(datetime(2026, 8, 3, 0, 0), holiday=False)    # 월요일 자정
    with a, b, c:
        after = session_phase_key(api.domestic_session_phase())
    assert before == after == 'idle'


def test_session_phase_key_market_close_still_notifies():
    """20:00 애프터마켓 → 20:01 마감은 진짜 전환 — 알림이 살아 있어야 한다."""
    from modules.auto_trade.trader import session_phase_key
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 20, 0))
    with a, b, c:
        before = session_phase_key(api.domestic_session_phase())
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 20, 1))
    with a, b, c:
        after = session_phase_key(api.domestic_session_phase())
    assert before == 'nxt_after' and after == 'idle'


# ==========================================================
# 미국 — us_session_phase / 라벨
# ==========================================================

@pytest.mark.parametrize("hh, mm, expect", [
    (3, 59, 'day'),       # 야간 ATS(주간거래) 종료 직전
    (4, 0, 'pre'),
    (9, 29, 'pre'),
    (9, 30, 'regular'),
    (15, 59, 'regular'),
    (16, 0, 'after'),
    (19, 59, 'after'),
    (20, 0, 'day'),       # 데이마켓 시작
    (22, 0, 'day'),
])
def test_us_session_phase_boundaries(hh, mm, expect):
    et = datetime(2026, 7, 22, hh, mm)   # 수요일(거래일)
    with patch.object(api, 'now_us_eastern', lambda: et), \
         patch.object(api, 'us_day_market_session', lambda: "20260723"):
        assert api.us_session_phase() == expect


def test_us_session_phase_weekend_is_closed():
    """토요일 정규장 시간대·주말 야간 모두 닫힘."""
    with patch.object(api, 'now_us_eastern', lambda: datetime(2026, 7, 25, 11, 0)):
        assert api.us_session_phase() == 'closed'
    # 금요일 밤은 토요일 세션 귀속 → us_day_market_session()이 None
    with patch.object(api, 'now_us_eastern', lambda: datetime(2026, 7, 24, 21, 0)):
        assert api.us_session_phase() == 'closed'


@pytest.mark.parametrize("hh, mm, head, style", [
    (8, 0, "프리마켓", "yellow"),
    (10, 30, "정규장", "green"),
    (17, 0, "애프터마켓", "yellow"),
    (22, 0, "데이마켓", "yellow"),
])
def test_us_label_includes_et_clock(hh, mm, head, style):
    """미국 라벨은 현지시각(ET)을 함께 보여준다 — 시스템 시간은 KST라 세션 감이 안 온다."""
    et = datetime(2026, 7, 22, hh, mm)
    with patch.object(api, 'now_us_eastern', lambda: et), \
         patch.object(api, 'us_day_market_session', lambda: "20260723"):
        text, st = api.market_session_label(True)
    assert text == f"{head} · ET {hh:02d}:{mm:02d}"
    assert st == style


# ==========================================================
# 표 제목 태그
# ==========================================================

def test_session_tag_wraps_label_in_markup():
    a, b, c = _freeze_kr(datetime(2026, 7, 28, 10, 0))
    with a, b, c:
        tag = api.market_session_tag(False)
    assert tag == "  [dim]│[/dim] [green]KRX 정규장[/green]"


def test_session_tag_never_raises():
    """표기는 부가정보 — 판정이 실패해도 빈 문자열로 화면을 막지 않는다."""
    with patch.object(api, 'market_session_label', side_effect=RuntimeError("boom")):
        assert api.market_session_tag(False) == ""


# ==========================================================
# print_table — ETF 플래그 전달
# ==========================================================

@pytest.mark.parametrize("title, is_overseas, is_etf, expect_kr_etf", [
    ("국내 ETF 기술적 분석", False, None, True),    # 제목으로 판정
    ("국내 ETF 분석 정보", False, None, True),
    ("국내 주식 기술적 분석", False, None, False),
    ("미국 ETF 기술적 분석", True, None, False),    # 미국 ETF는 NXT와 무관
    ("국내 주식 분석 정보", False, True, True),     # 제목이 '주식'이어도 명시 인자가 이긴다
    ("국내 ETF 기술적 분석", False, False, False),
])
def test_print_table_passes_etf_flag_to_session_tag(title, is_overseas, is_etf, expect_kr_etf):
    from modules import analysis

    seen = {}

    def _tag(ovs, kr_etf=False):
        seen['args'] = (ovs, kr_etf)
        return ""

    with patch.object(analysis.api, 'market_session_tag', _tag):
        analysis.print_table(title, [], is_overseas=is_overseas,
                             market_regime_adj={}, is_etf=is_etf)

    assert seen['args'] == (is_overseas, expect_kr_etf)
