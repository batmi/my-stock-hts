"""감사 유니버스가 **날짜를 넘어 재현되는가**.

[왜] 2026-08-24 TQ 국면 축을 재검증하다 기록이 재현되지 않았다. 다이얼 세 개
 (TQ 상한·시간청산·동점가름)를 전부 되돌려도 안 맞았고, 남은 원인이 유니버스였다.

 `extend_targets(mode="random")` 은 시총 내림차순 리스트를 고정 씨드로 셔플해 앞에서
 잘랐다. 고정 씨드 셔플은 '인덱스 → 자리'의 고정 치환이라, 리스트에서 **한 종목의
 위치만 밀려도 그 뒤 전부의 자리가 바뀐다.** 정작 데이터는 거의 안 움직이는데
 (시총 ±2% 지터에 상위 500 구성원은 3개=0.6%만 교체) 뽑히는 60종목은 41.5개가
 갈렸다. 흔들린 것은 유니버스가 아니라 **계측기**였다.

 게다가 `_listing` 은 호출마다 원격을 받아 스냅샷을 덮어썼다 — 캐시는 실패 시 폴백일
 뿐이었다. 그래서 같은 씨드로 같은 도구를 다른 날 돌리면 다른 표본을 재고 있었다.

[이 테스트가 지키는 것]
 ① 뽑기는 **구성원 집합**의 함수다 — 순위가 뒤바뀌어도 같은 종목이 나온다.
 ② 구성원이 조금 바뀌면 뽑기도 조금만 바뀐다(위치 의존이면 통째로 갈린다).
 ③ 스냅샷이 있으면 원격을 두드리지 않는다(고정이 기본, 갱신은 명시적으로).
"""
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import audit_universe as AU  # noqa: E402


def _listing_df(n=600, order="marcap"):
    """합성 종목 목록. `order` 로 시총 순위만 뒤섞는다(구성원은 같다)."""
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    caps = [float(n - i) for i in range(n)]           # 내림차순
    if order == "scrambled":
        # 상위 500 안에서만 순위를 뒤집는다 — 구성원은 그대로, 순서만 바뀐다.
        caps = caps[:500][::-1] + caps[500:]
    return pd.DataFrame({"Code": codes, "Name": [f"종목{c}" for c in codes],
                         "Market": ["KOSPI"] * n, "Marcap": caps})


def test_뽑기는_순위가_아니라_구성원의_함수다(monkeypatch):
    """상위 pool 안에서 시총 순위를 뒤집어도 **같은 60종목**이 나와야 한다."""
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: _listing_df(order="marcap"))
    a = {c for c, _ in AU.extend_targets(set(), 60, mode="random")}

    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: _listing_df(order="scrambled"))
    b = {c for c, _ in AU.extend_targets(set(), 60, mode="random")}

    assert a == b, (
        f"순위만 바뀌었는데 {len(a - b)}종목이 갈렸다 — 뽑기가 리스트 위치에 의존한다. "
        "셔플+슬라이스가 아니라 종목별 해시로 뽑아야 한다."
    )


def test_구성원이_조금_바뀌면_뽑기도_조금만_바뀐다(monkeypatch):
    """상위 pool 구성원 3개가 교체될 때 뽑히는 60종목의 변화도 그 정도여야 한다."""
    base_df = _listing_df()
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: base_df)
    a = {c for c, _ in AU.extend_targets(set(), 60, mode="random")}

    # 498~500위와 501~503위를 맞바꾼다 — 구성원 3개 교체.
    swapped = base_df.copy()
    caps = list(swapped["Marcap"])
    for i in (497, 498, 499):
        caps[i], caps[i + 3] = caps[i + 3], caps[i]
    swapped["Marcap"] = caps
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: swapped)
    b = {c for c, _ in AU.extend_targets(set(), 60, mode="random")}

    changed = len(a - b)
    assert changed <= 3, (
        f"구성원은 3개만 바뀌었는데 뽑기는 {changed}종목이 갈렸다 — 위치 의존이 남아 있다"
    )


def test_같은_입력에는_같은_뽑기(monkeypatch):
    df = _listing_df()
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: df)
    assert AU.extend_targets(set(), 60, mode="random") == \
        AU.extend_targets(set(), 60, mode="random")


def test_씨드가_다르면_다른_뽑기(monkeypatch):
    """대조군 — 위 테스트들이 '항상 같은 것만 준다'로 통과하지 않게 한다."""
    df = _listing_df()
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: df)
    a = {c for c, _ in AU.extend_targets(set(), 60, mode="random", seed=1)}
    b = {c for c, _ in AU.extend_targets(set(), 60, mode="random", seed=2)}
    assert len(a - b) > 20, "씨드를 바꿔도 거의 같은 종목이 나온다 — 무작위가 아니다"


def test_스냅샷이_있으면_원격을_두드리지_않는다(tmp_path, monkeypatch, capsys):
    """고정이 기본이다. 원격이 죽어 있어도(=예외) 스냅샷으로 조용히 돈다."""
    csv = tmp_path / "KRX.csv"
    _listing_df(n=10).to_csv(csv, index=False, encoding="utf-8")
    (tmp_path / "KRX.meta.json").write_text(
        json.dumps({"fetched_at": "2026-08-24 09:45"}), encoding="utf-8")
    monkeypatch.setattr(AU, "_listing_paths",
                        lambda kind: (str(csv), str(tmp_path / "KRX.meta.json")))
    monkeypatch.delenv("AUDIT_LISTING_REFRESH", raising=False)
    AU._LISTING_ANNOUNCED.clear()

    def _boom(*a, **k):
        raise AssertionError("스냅샷이 있는데 원격을 받았다 — 고정이 깨졌다")
    monkeypatch.setitem(sys.modules, "FinanceDataReader",
                        type("M", (), {"StockListing": staticmethod(_boom)}))

    df = AU._listing("KRX")
    assert len(df) == 10
    assert "2026-08-24" in capsys.readouterr().out, "어느 스냅샷을 쟀는지 찍지 않는다"


def test_갱신은_명시적으로만(tmp_path, monkeypatch):
    """AUDIT_LISTING_REFRESH 를 켰을 때만 원격을 받는다."""
    csv = tmp_path / "KRX.csv"
    _listing_df(n=10).to_csv(csv, index=False, encoding="utf-8")
    monkeypatch.setattr(AU, "_listing_paths",
                        lambda kind: (str(csv), str(tmp_path / "KRX.meta.json")))
    fetched = []

    def _fetch(kind):
        fetched.append(kind)
        return _listing_df(n=20)
    monkeypatch.setitem(sys.modules, "FinanceDataReader",
                        type("M", (), {"StockListing": staticmethod(_fetch)}))

    monkeypatch.setenv("AUDIT_LISTING_REFRESH", "1")
    AU._LISTING_ANNOUNCED.clear()
    assert len(AU._listing("KRX")) == 20
    assert fetched == ["KRX"]
    meta = json.loads((tmp_path / "KRX.meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == 20 and meta["fetched_at"], "갱신 시각을 남기지 않는다"


def test_신형우선주는_풀에_들어오지_않는다(monkeypatch):
    """우선주 표기는 두 가지다 — 끝자리 숫자(구형)와 **끝자리 알파벳**(신형).

    끝자리 5·7·9만 보던 필터는 신형을 통째로 놓쳤다. 2026-08-24 스냅샷 기준 상위 500
    풀 안에 미래에셋증권2우B·한화3우B·CJ4우(전환) 세 종목이 남아 있었다. 표본이 작아
    당장은 안 뽑혔더라도, `--pool` 을 키우면 그만큼 더 샌다.
    """
    df = pd.DataFrame({
        "Code": ["005930", "005935", "00680K", "00088K", "0120G0", "000660"],
        "Name": ["삼성전자", "삼성전자우", "미래에셋증권2우B", "한화3우B",
                 "삼양바이오팜", "SK하이닉스"],
        "Market": ["KOSPI"] * 6,
        "Marcap": [6e14, 5e14, 4e14, 3e14, 2e14, 1e14],
    })
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: df)

    picked = {c for c, _ in AU.extend_targets(set(), 10)}

    assert "00680K" not in picked and "00088K" not in picked, \
        "신형우선주(끝자리 알파벳)가 감사 유니버스에 들어왔다"
    assert "005935" not in picked, "구형 우선주 배제가 깨졌다"
    assert "0120G0" in picked, (
        "가운데에 문자가 오는 코드는 최근 상장 **보통주**의 신규 채번이다"
        "(0120G0 삼양바이오팜) — '문자 포함'으로 자르면 멀쩡한 종목이 날아간다")
    assert {"005930", "000660"} <= picked


# ==========================================================
# 시점별 시가총액 (mode='pit') — 2026-08-24
# ==========================================================
# `mode='marcap'`/`'random'` 은 **오늘의** 시총으로 과거 유니버스를 고른다. 10년 백테스트라면
# "그때는 고를 수 없었던 종목"을 그때 심는 셈이다 — 실측으로 현행 marcap 상위 10에
# LG에너지솔루션(2022년 상장)이 들어 있었다. `mode='pit'` 은 그 시점 시총으로 고른다.
# 뽑기 방식(해시)은 random 과 같고 **시총을 언제 재느냐만 다르다** — 그래야 둘을 나란히
# 돌렸을 때 차이가 look-ahead 의 크기가 된다.

def test_pit_은_그_시점_시총으로_고른다(monkeypatch):
    """오늘 1위지만 그 시점엔 없던 종목은 뽑히지 않아야 한다."""
    today = pd.DataFrame({
        "Code": ["373220", "005930", "015760"],       # LG엔솔(2022 상장) · 삼성전자 · 한국전력
        "Name": ["LG에너지솔루션", "삼성전자", "한국전력"],
        "Market": ["KOSPI"] * 3,
        "Marcap": [9e14, 8e14, 1e14],
    })
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: today)
    # 그 시점(2016)에는 LG엔솔이 없고 한국전력이 2위였다
    monkeypatch.setattr(AU, "_pit_marcap", lambda date: {"005930": 8e14, "015760": 3e14})
    monkeypatch.setattr(AU, "_name_map", lambda: {"005930": "삼성전자", "015760": "한국전력"})

    now = {c for c, _ in AU.extend_targets(set(), 3, mode="marcap")}
    pit = {c for c, _ in AU.extend_targets(set(), 3, mode="pit", pit_date="20160826")}

    assert "373220" in now, "현행 모드는 오늘 시총을 쓴다(대조군)"
    assert "373220" not in pit, \
        "그 시점에 상장도 안 된 종목이 PIT 유니버스에 들어왔다 — look-ahead 가 남아 있다"
    assert pit == {"005930", "015760"}


def test_pit_도_우선주_스팩을_배제한다(monkeypatch):
    """배제 규칙은 현재 목록과 PIT 목록이 **같아야** 한다 (같은 헬퍼를 쓴다)."""
    monkeypatch.setattr(AU, "_pit_marcap", lambda date: {
        "005930": 9e14, "005935": 8e14, "00680K": 7e14, "123456": 6e14, "654321": 5e14})
    monkeypatch.setattr(AU, "_name_map", lambda: {
        "005930": "삼성전자", "005935": "삼성전자우", "00680K": "미래에셋증권2우B",
        "123456": "케이비제20호스팩", "654321": "OO리츠"})

    picked = {c for c, _ in AU.extend_targets(set(), 10, mode="pit", pit_date="20160826")}
    assert picked == {"005930"}, f"배제가 새고 있다: {picked}"


def test_pit_은_뽑기_방식이_random_과_같다(monkeypatch):
    """같은 구성원·같은 씨드면 두 모드가 같은 종목을 뽑아야 한다.

    그래야 random↔pit 비교에서 **시총을 언제 쟀는가**만 남는다. 뽑기까지 다르면
    차이가 look-ahead 때문인지 뽑기 때문인지 못 가른다.
    """
    codes = [f"{i:06d}" for i in range(1, 601)]
    caps = {c: float(600 - i) for i, c in enumerate(codes)}
    df = pd.DataFrame({"Code": codes, "Name": [f"종목{c}" for c in codes],
                       "Market": ["KOSPI"] * 600, "Marcap": [caps[c] for c in codes]})
    monkeypatch.setattr(AU, "_listing", lambda kind, refresh=None: df)
    monkeypatch.setattr(AU, "_pit_marcap", lambda date: caps)
    monkeypatch.setattr(AU, "_name_map", lambda: {c: f"종목{c}" for c in codes})

    a = {c for c, _ in AU.extend_targets(set(), 60, mode="random")}
    b = {c for c, _ in AU.extend_targets(set(), 60, mode="pit", pit_date="20160826")}
    assert a == b


def test_pit_은_시총을_못_받으면_조용히_비지_않는다(monkeypatch, capsys):
    """자격증명이 없으면 빈 결과를 주되 **이유를 말해야** 한다.

    조용히 0종목을 돌려주면 '확장 없이 잰 수치'가 확장한 것처럼 기록된다.
    """
    monkeypatch.setattr(AU, "_pit_marcap", lambda date: None)
    out = AU.extend_targets(set(), 10, mode="pit", pit_date="20160826")
    assert out == []
    assert "KRX_ID" in capsys.readouterr().out


def test_pit_은_기준일이_없으면_거부한다():
    with pytest.raises(ValueError):
        AU.extend_targets(set(), 10, mode="pit")
