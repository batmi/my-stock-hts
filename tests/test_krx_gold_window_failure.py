"""KRX 금현물 일봉: **구간 조회 실패는 '그 구간이 없다'가 아니다**.

[왜 이 파일이 있나 · 2026-09-07]
KRX 는 한 번에 2년까지만 준다. 그래서 긴 기간은 창을 나눠 최신순으로 여러 번 받는다.
전송 계층(_post)은 실패를 None, 빈 응답을 [] 로 **이미 갈라서** 돌려준다. 그런데
소비자가 `raw or []` 와 `if not got: break` 로 둘을 같은 자리에 넣고 있었다.

결과: 뒤쪽(과거) 창이 한 번 실패하면 요청 기간의 일부만 담긴 시계열이 **아무 표시 없이**
돌아가고, 그것이 캐시 TTL(기본 6시간) 동안 정상 결과로 굳는다.
실측: 3년(창 2개)을 요청하고 과거 창만 실패시키니 28행(1개월)이 돌아왔고, 호출부가
짧다는 것을 알 방법이 없었다(attrs 에도 표시가 없다).

이 시계열은 화면 표시로 끝나지 않는다 — 9-5 백테스트에 KRXGOLD 로 들어간다
([[krx-gold-index-source]]). 기간이 조용히 잘린 채 검증되는 것은 krx_daily.get_daily 가
캐시본의 조회 기간을 따지는 이유와 같다.

'상장 이전이라 없다'(응답은 정상, 0행)는 여전히 정상 종료다 — 그건 답이기 때문이다.
"""
from unittest.mock import patch

import pytest

from modules import krx_data


def _rows(n, month="01"):
    return [{"TRD_DD": f"2025/{month}/{d:02d}", "TDD_OPNPRC": "100", "TDD_HGPRC": "110",
             "TDD_LWPRC": "90", "TDD_CLSPRC": "105", "ACC_TRDVOL": "10"}
            for d in range(1, n + 1)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    krx_data.clear_cache()
    monkeypatch.setattr(krx_data, "is_available", lambda: True)
    yield
    krx_data.clear_cache()


def _run(responses, days=1100, use_cache=True):
    """창 순서대로 responses 를 돌려준다. (실측: 1100일 = 창 2개)"""
    seq = list(responses)
    calls = []

    def fake_post(bld, **params):
        calls.append(params.get("strtDd"))
        return seq.pop(0) if seq else None

    with patch.object(krx_data, "_post", fake_post):
        return krx_data.get_gold_daily(days=days, use_cache=use_cache), calls


def test_창이_둘_이상인_요청이다():
    """이 파일의 전제 — 창이 하나면 검사할 것이 없다."""
    assert len(krx_data._range_windows(1100)) >= 2


def test_과거_창_실패를_조용히_짧은_시계열로_돌려주지_않는다(caplog):
    with caplog.at_level("WARNING", logger="modules.krx_data"):
        df, _ = _run([_rows(28), None])

    assert df is not None and len(df) == 28
    assert any("짧다" in r.message for r in caplog.records), \
        "요청보다 짧은 시계열을 돌려주면서 아무 말도 하지 않았다"


def test_잘린_시계열은_캐시에_굳지_않는다():
    """굳으면 TTL(기본 6시간) 동안 모두가 짧은 답을 받는다."""
    df1, _ = _run([_rows(28), None])
    assert len(df1) == 28

    # 두 번째 호출은 캐시가 아니라 다시 조회해야 한다 — 이번엔 두 창 다 성공시킨다.
    df2, calls = _run([_rows(28), _rows(20, month="02")])
    assert calls, "캐시에 굳어 다시 조회하지 않았다"
    assert len(df2) == 48


def test_상장_이전이라_빈_응답인_것은_정상이다():
    """[] 는 답이다 — 받은 만큼 쓰고, 경고하지 않고, 캐시한다."""
    df, _ = _run([_rows(28), []])
    assert len(df) == 28

    df2, calls = _run([_rows(99)])          # 캐시가 살아 있으면 조회하지 않는다
    assert calls == [], "정상 결과인데 캐시하지 않았다"
    assert len(df2) == 28


def test_첫_창부터_실패하면_값을_돌려주지_않는다():
    df, _ = _run([None])
    assert df is None
