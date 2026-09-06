"""깨진 한 줄이 보유 현황 메시지 전체를 지우지 않는가 (감사 2026-09-06, 배치 63).

format_holdings_block 은 텔레그램 /status·장 시작 알림·장 종료 알림이 함께 쓰는
표기 SSOT 다. 그런데 각 항목을 `int(item['evlu_amt'])` 처럼 하드 서브스크립트로 읽고
루프를 감싸는 try 가 없었다.

KIS 는 값이 없을 때 키를 **주고 빈 문자열**을 담는다(dict.get 의 기본값은 키가 없을
때만 쓰인다). int('') 는 ValueError 다 — 종목 하나가 그 상태면 **메시지가 통째로
사라진다.** 운영자는 보유 현황을 아예 못 받는다.

같은 교훈이 한 층 아래에도 있었다(account.fetch_domestic_balance — 2026-09-06 배치 50).
읽을 수 없는 줄은 그 줄만 빼고, 뺐다는 사실을 밝힌다.
"""
import pytest

from modules.auto_trade import common


def _h(code, name, qty=10, prpr=70000, avg=60000, eval_amt=700000, pl=100000, rate=16.67):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': str(qty), 'prpr': str(prpr),
            'pchs_avg_pric': str(avg), 'evlu_amt': str(eval_amt),
            'evlu_pfls_amt': str(pl), 'evlu_pfls_rt': str(rate)}


def test_정상_보유는_모두_표시된다():
    msg = common.format_holdings_block([_h('005930', '삼성전자'), _h('000660', 'SK하이닉스')])
    assert '삼성전자' in msg and 'SK하이닉스' in msg
    assert '빠졌습니다' not in msg


@pytest.mark.parametrize("field", ['evlu_amt', 'evlu_pfls_amt', 'evlu_pfls_rt', 'prpr', 'hldg_qty'])
def test_한_종목의_빈_필드가_전체_메시지를_지우지_않는다(field):
    """KIS 는 값이 없을 때 빈 문자열을 준다 — int('') 는 ValueError."""
    broken = _h('005930', '삼성전자')
    broken[field] = ''
    good = _h('000660', 'SK하이닉스')

    msg = common.format_holdings_block([broken, good])

    assert 'SK하이닉스' in msg, f"'{field}' 가 빈 한 종목 때문에 메시지 전체가 사라졌다"
    assert '삼성전자' not in msg.split('빠졌습니다')[0], "읽을 수 없는 종목이 본문에 남았다"


def test_뺀_사실을_밝힌다():
    """조용히 줄이면 '판 줄 알았는데 아직 들고 있는' 오해가 난다."""
    broken = _h('005930', '삼성전자')
    broken['evlu_amt'] = ''
    msg = common.format_holdings_block([broken, _h('000660', 'SK하이닉스')])

    assert '빠졌습니다' in msg and '삼성전자' in msg


def test_키_자체가_없어도_같은_규칙이다():
    broken = _h('005930', '삼성전자')
    del broken['evlu_pfls_rt']
    msg = common.format_holdings_block([broken, _h('000660', 'SK하이닉스')])
    assert 'SK하이닉스' in msg and '빠졌습니다' in msg
