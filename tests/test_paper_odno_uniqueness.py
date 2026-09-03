"""가상 주문번호는 절대 겹치면 안 된다.

[배경] 종전 형식은 `P{초단위시각}{종목코드 끝 2자리}` 였다. 매도 워커는 4스레드 병렬이라
급락으로 손절이 한꺼번에 나가면 같은 초에 두 주문이 생기고, 코드 끝 2자리까지 같으면
(005930·000930 처럼 44종목 유니버스에서 드물지 않다) 주문번호가 충돌한다.

충돌의 대가가 크다 — paper_broker.get_fill_by_odno 도 db.get_trade_by_odno 도 주문번호
하나로만 찾으므로, conclusion._apply_paper_fill 이 **다른 종목의 체결(수량·단가·손익)** 을
이 종목에 반영한다. 관찰 모드는 배관을 검증하려고 도입한 것이라, 배관이 틀린 체결을
기록하면 관찰 자체가 무의미해진다.
"""
import concurrent.futures
import re

from modules import paper_broker


def test_same_code_same_second_never_collides():
    """같은 종목을 연속 발주해도 겹치지 않는다."""
    got = [paper_broker._new_odno("005930") for _ in range(500)]
    assert len(set(got)) == len(got), "같은 초 안에서 주문번호가 겹쳤다"


def test_codes_sharing_the_last_two_digits_never_collide():
    """끝 2자리가 같은 서로 다른 종목이 같은 순간에 나가도 겹치지 않는다."""
    got = [paper_broker._new_odno(c)
           for _ in range(200) for c in ("005930", "000930", "123930")]
    assert len(set(got)) == len(got), "끝 2자리가 같은 종목끼리 주문번호가 겹쳤다"


def test_parallel_sell_workers_do_not_collide():
    """4스레드 동시 손절(실제 발생 조건)에서도 유일하다."""
    codes = ["005930", "000930", "207940", "035420"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        got = [f.result() for f in
               [ex.submit(paper_broker._new_odno, codes[i % 4]) for i in range(400)]]
    assert len(set(got)) == len(got), "병렬 발주에서 주문번호가 겹쳤다"


def test_shape_stays_recognisable():
    """형식은 그대로 P로 시작하고, 로그에서 종목을 짚을 수 있게 끝 2자리를 남긴다."""
    odno = paper_broker._new_odno("005930")
    assert odno.startswith("P")
    assert re.fullmatch(r"P\d{18}30\d{3}", odno), odno
    # PRE_* 임시 ID와 구분돼야 한다(conclusion 이 접두사로 가른다)
    assert not odno.startswith("PRE_")
