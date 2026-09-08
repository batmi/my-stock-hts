"""감사 유니버스가 **한 시점의 세계**인지 지킨다.

[무엇이 문제였나 · 2026-09-08] 스냅샷은 목록마다 따로 찍힌다. `KRX-DESC` 가 rule_pool 에
쓰이기 시작하면서 그날 처음 만들어졌고(2026-09-08), `KRX` 는 2026-08-23 · `KRX-DELISTING`
은 2026-08-24 자였다 — 유니버스 하나가 **2주 벌어진 두 목록을 조인해** 만들어지고 있었다.

목록 고정([[audit-universe-reproducibility]])의 요점은 '같은 날의 같은 세계를 본다'이므로
날짜가 갈리면 그 요점이 깨진다. 특히 KRX(시총·풀 정렬)와 KRX-DESC(업종·배제 규칙)는
Code 로 조인되므로, 한쪽에만 있는 종목이 생기면 방어주·지주 배제가 조용히 빗나간다
([[defensive-sector-exclusion]] · [[discover-menu-rules-audited]]).

메타에 '받은 시각'만 있고 **데이터 날짜**가 없던 것도 함께 고쳤다 — 받은 시각은 그 파일이
어느 날의 목록인지 말해 주지 않는다(실측: 08-24 09:45 에 받은 KRX 는 08-23 자 파일이었다).
"""
import datetime as dt

import pytest

from tools import audit_universe as au


@pytest.fixture(autouse=True)
def clean():
    au._SNAPSHOT_DATES.clear()
    yield
    au._SNAPSHOT_DATES.clear()


def _say(capsys):
    return capsys.readouterr().out


def test_a_single_date_says_nothing(capsys):
    au._SNAPSHOT_DATES.update({"KRX": "2026-08-23", "KRX-DESC": "2026-08-23"})
    au._announce_snapshot_skew()
    assert "갈립니다" not in _say(capsys)


def test_a_day_of_slack_is_tolerated(capsys):
    """상장목록은 하루 사이 거의 안 변한다 — 며칠 차이까지 조르면 경고가 무뎌진다."""
    au._SNAPSHOT_DATES.update({"KRX": "2026-08-23", "KRX-DESC": "2026-08-23",
                               "KRX-DELISTING": "2026-08-24"})
    au._announce_snapshot_skew()
    assert "갈립니다" not in _say(capsys)


def test_the_joined_pair_must_match_exactly(capsys):
    """KRX 와 KRX-DESC 는 Code 로 조인된다 — 하루만 어긋나도 배제 규칙이 빗나간다."""
    au._SNAPSHOT_DATES.update({"KRX": "2026-08-23", "KRX-DESC": "2026-08-24"})
    au._announce_snapshot_skew()
    out = _say(capsys)
    assert "갈립니다" in out
    assert "배제 규칙" in out, "무엇이 틀어지는지 말하지 않으면 경고가 아니다"


def test_a_wide_spread_is_announced_even_when_the_pair_agrees(capsys):
    """폐지 목록만 2주 뒤처져도 생존 편향 표본이 다른 세계다."""
    au._SNAPSHOT_DATES.update({"KRX": "2026-09-08", "KRX-DESC": "2026-09-08",
                               "KRX-DELISTING": "2026-08-24"})
    au._announce_snapshot_skew()
    out = _say(capsys)
    assert "갈립니다" in out and "일 벌어" in out


def test_unknown_dates_do_not_trigger_a_false_alarm(capsys):
    """옛 스냅샷은 데이터 날짜가 없다('?'). 모른다를 '어긋났다'로 접으면 안 된다."""
    au._SNAPSHOT_DATES.update({"KRX": "?", "KRX-DESC": "2026-08-23"})
    au._announce_snapshot_skew()
    assert "갈립니다" not in _say(capsys)


def test_the_shipped_snapshots_are_actually_aligned():
    """지금 저장소에 있는 스냅샷이 실제로 한 시점인지 본다(회귀 방지)."""
    import json
    import os
    dates = {}
    for kind in au._LISTING_KINDS:
        _f, meta = au._listing_paths(kind)
        if not os.path.exists(meta):
            pytest.skip(f"{kind} 메타 없음 — 스냅샷 미생성 환경")
        dates[kind] = json.load(open(meta, encoding="utf-8")).get("data_date")
    assert dates["KRX"] == dates["KRX-DESC"], \
        f"조인되는 두 목록이 다른 날이다: {dates}"
    days = sorted(dt.datetime.strptime(d, "%Y-%m-%d").date()
                  for d in dates.values() if d)
    assert (days[-1] - days[0]).days <= au._SNAPSHOT_SKEW_TOLERANCE_DAYS, dates


def test_refresh_covers_every_listing():
    """갱신이 한 목록이라도 빠뜨리면 그것만 다른 날짜로 남는다 — 이번 결함의 원인."""
    import inspect
    src = inspect.getsource(au.refresh_listings)
    assert "_LISTING_KINDS" in src, "갱신 대상 목록이 하드코딩되어 있다"
    assert set(au._LISTING_KINDS) == {"KRX", "KRX-DESC", "KRX-DELISTING"}
