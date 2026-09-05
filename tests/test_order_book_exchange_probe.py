"""해외 호가 조회의 거래소 탐색 루프가 예외로 끊기지 않는다.

KIS 해외 호가(HHDFS76200200)는 EXCD(거래소)를 요구하는데 우리는 종목의 거래소를 모를 수
있다. 그래서 캐시된 거래소 → NASD/NAS/NYSE/NYS/AMEX/AMS 순으로 **차례로 물어보는 탐색
루프**가 있다. 틀린 거래소로 물으면 KIS 는 오류가 아니라 `rt_cd='0'` 에 **빈 output** 을
준다 — 그것이 '이 거래소가 아니다'라는 신호다.

그런데 유효성 검사가

    float(out.get('pask1', 0)) > 0

였다. dict.get 의 기본값은 키가 **없을 때만** 쓰인다. 키가 있고 값이 '' 이면 float('') 이
ValueError 를 내고, 그 예외가 루프를 통째로 끊었다 — **정작 루프가 존재하는 이유인 바로 그
상황에서** 나머지 거래소를 못 보게 된다. 첫 후보가 빗나가면 그 종목의 호가는 영영 못 받는다
(수급 게이트 ask_bid_ratio · 수동 호가창 화면).

관련: [[unknown-vs-empty]]
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config


def test_빈_문자열_호가는_예외가_아니라_다음_거래소로_넘어간다(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(config.session, "exchange_cache", {}, raising=False)
    monkeypatch.setattr(config.session, "update_cache_and_save",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(api, "_get_micro_cache", lambda *a, **k: None)
    monkeypatch.setattr(api, "_set_micro_cache", lambda *a, **k: None)

    asked = []

    def _fake_call(url_path, market, category, action, params=None, **kw):
        excd = params["EXCD"]
        asked.append(excd)
        if excd == "NYSE":
            return {'rt_cd': '0', 'output1': {'pask1': '190.5', 'pbid1': '190.4'}}
        # 틀린 거래소: 성공 코드 + 빈 문자열 (KIS 실제 동작)
        return {'rt_cd': '0', 'output1': {'pask1': '', 'pbid1': ''}}

    with patch.object(api, "call_api", side_effect=_fake_call):
        res = api.get_order_book("IBM", is_overseas=True)

    assert res.get('rt_cd') == '0', "빈 응답 하나에 탐색이 끊겼다"
    assert res['output1']['pask1'] == '190.5'
    assert "NYSE" in asked, f"NYSE 까지 가지 못했다: {asked}"


def test_모든_거래소가_비면_실패로_답한다(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(config.session, "exchange_cache", {}, raising=False)
    monkeypatch.setattr(api, "_get_micro_cache", lambda *a, **k: None)
    monkeypatch.setattr(api, "_set_micro_cache", lambda *a, **k: None)

    with patch.object(api, "call_api",
                      return_value={'rt_cd': '0', 'output1': {'pask1': '', 'pbid1': ''}}):
        res = api.get_order_book("ZZZZ", is_overseas=True)
    assert res.get('rt_cd') == '9999'


@pytest.mark.parametrize("value,expected", [
    ("", 0.0), (None, 0.0), ("  ", 0.0), ("N/A", 0.0),
    ("1,234.5", 1234.5), ("7", 7.0), (float("nan"), 0.0),
])
def test_safe_float가_읽을_수_없는_값을_기본값으로_돌린다(value, expected):
    assert api.safe_float(value) == expected


def test_safe_float의_기본값은_바꿀_수_있다():
    assert api.safe_float("", default=None) is None
