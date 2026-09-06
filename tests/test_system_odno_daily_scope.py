"""시스템 주문번호 집합도 날짜와 짝지어야 한다.

[왜] `_SYSTEM_ODNOS` 는 날짜 없는 문자열 집합이었고 **한 번도 비워지지 않았다**.
 증권사 주문번호는 당일 채번이라 매일 순번이 0부터 다시 올라간다([[odno-daily-reset]]) —
 어제의 시스템 주문번호가 오늘은 **다른 사람의 다른 주문**을 가리킨다. 운용은 무중단
 (파이 상시 가동)이라 이 집합에는 며칠치가 쌓인 채 남는다.

 결과: 운용자가 HTS 로 직접 산 주문이 '시스템 주문'으로 읽혀 **수동매매 제한 등록이
 건너뛰어진다**(conclusion 의 외부 매수 판정). 그러면 자동매매가 같은 종목을 다시 산다 —
 제한 등록이 막으려던 바로 그 상황이다. 순번은 작은 값부터 채워지므로 초반 번호는
 사실상 매일 재사용된다(드문 우연이 아니다).
"""
from datetime import datetime, timedelta

from modules.auto_trade import common

TODAY = datetime.now().strftime('%Y-%m-%d')
YDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


def _clear():
    with common._SYSTEM_ODNOS_LOCK:
        common._SYSTEM_ODNOS.clear()


def test_어제_시스템_주문번호가_오늘_외부주문을_가리키지_않는다():
    _clear()
    common.register_system_odno("0000123", on_date=YDAY)
    assert common.is_system_odno("0000123", on_date=YDAY) is True
    assert common.is_system_odno("0000123", on_date=TODAY) is False, \
        "어제 번호가 오늘 외부 주문을 시스템 주문으로 둔갑시킨다 — 수동매매 제한이 건너뛰어진다"


def test_같은_날_등록한_번호는_그대로_인식된다():
    _clear()
    common.register_system_odno("0000123")
    assert common.is_system_odno("0000123") is True


def test_날짜가_바뀌면_지난_날짜는_버린다():
    """무중단 운용에서 며칠치가 무한히 쌓이지 않게 한다."""
    _clear()
    common.register_system_odno("0000123", on_date=YDAY)
    common.register_system_odno("0000999", on_date=TODAY)   # 오늘 등록 = 가지치기 발동
    assert common._SYSTEM_ODNOS == {(TODAY, "0000999")}


def test_is_system_trade_의_odno_폴백도_날짜를_본다():
    _clear()
    common.register_system_odno("0000123", on_date=YDAY)
    #  넘겨준 날짜가 **실제로 쓰여야** 한다 — 기본값(오늘)로 떨어지면 아래가 거짓이 된다.
    #  (오늘 날짜로 먼저 물으면 가지치기가 어제 항목을 지우므로 이 검사가 먼저다.)
    assert common.is_system_trade("매수(수동)", "0000123", on_date=YDAY) is True
    # '(AUTO)' 표기가 없는 외부 매수. 번호만 어제 것과 같다 — 오늘 판정에서는 남이다.
    assert common.is_system_trade("매수(수동)", "0000123", on_date=TODAY) is False
    # 표기가 있으면 날짜와 무관하게 시스템 주문이다(재기동 백필 경로가 이것에 기댄다).
    assert common.is_system_trade("매수(AUTO)", "0000123", on_date=TODAY) is True


def test_빈_주문번호는_시스템_주문이_아니다():
    _clear()
    assert common.is_system_odno("") is False
    assert common.is_system_odno(None) is False
    common.register_system_odno(None)
    assert common._SYSTEM_ODNOS == set()


def test_외부매수_판정이_날짜를_넘겨_부른다():
    """호출부가 날짜를 안 주면 위 방어가 그 자리에서만 무의미해진다."""
    import ast
    import inspect
    from modules.auto_trade import conclusion

    src = inspect.getsource(conclusion)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "is_system_odno"]
    assert calls, "is_system_odno 호출을 찾지 못했다(이름이 바뀌었는가)"
    for c in calls:
        assert any(kw.arg == "on_date" for kw in c.keywords), \
            "is_system_odno 를 날짜 없이 부르는 자리가 있다 — 어제 번호가 오늘 판정을 흔든다"
