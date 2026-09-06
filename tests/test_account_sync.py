import pytest
from unittest.mock import patch, MagicMock
from modules import account
import api
import config

@patch('api.get_today_history')
@patch('modules.account.db_manager.db')
def test_sync_today_trades(mock_db, mock_get_history):
    """금일 체결 내역 동기화 테스트"""
    # Setup mock API response
    mock_get_history.return_value = {
        'rt_cd': '0',
        'output1': [
            {
                'odno': '1001', 'avg_prvs': '70000', 'tot_ccld_qty': '10', 
                'pdno': '005930', 'prdt_name': 'Samsung', 
                'sll_buy_dvsn_cd': '02', # Buy
                'ord_dt': '20230101', 'ord_tmd': '120000'
            }
        ]
    }
    
    # Setup mock DB
    mock_db.check_trade_exists.return_value = False # New trade
    mock_db.get_trade_by_odno.return_value = None
    
    # Config setup
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    # 자동매매 계좌가 갈리면 같은 응답을 두 계좌에서 두 번 읽는다(단일계좌로 고정).
    config.session.auto_cano = "12345678"
    config.session.auto_acnt_prdt_cd = "01"

    count = account.sync_today_trades()
    
    assert count == 1
    mock_db.insert_trade.assert_called_once()

    # [수정] 체결 확인 로직 변경으로 원본 주문의 가격을 업데이트하지 않음.
    # mock_db.update_trade.assert_called_once_with('1001', price=70000.0)

def test_동기화_실패는_조용히_넘어가지_않는다(capsys):
    """`except Exception: pass` 로 계좌 하나가 통째로 사라지던 자리.

    이 블록은 당일 체결을 trades 에 적재하는 전체 경로를 감싼다 — 조회 실패도,
    insert_trade 실패도 여기서 사라졌다. 그러면 그 계좌의 오늘 체결이 DB 에 없는 채로
    화면은 '동기화 완료'라고 말하고, 평단·진입일·손절 기준이 붙을 자리를 잃는다
    (체결 기록이 그 모든 것의 근거다). [[db-failure-visibility]]
    """
    from unittest.mock import patch

    import config
    from modules import account

    with patch("api.get_today_history", side_effect=RuntimeError("조회 폭발")), \
         patch("api.get_overseas_today_history", return_value=[]):
        count = account.sync_today_trades()

    out = capsys.readouterr().out
    assert count == 0
    assert "동기화에 실패한 계좌" in out, f"실패가 화면에 밝혀지지 않았다:\n{out}"
    assert "'없음'이 아닙니다" in out


def _hist_item(odno, code, name, avg="70000"):
    return {'odno': odno, 'avg_prvs': avg, 'tot_ccld_qty': '10',
            'pdno': code, 'prdt_name': name, 'sll_buy_dvsn_cd': '02',
            'ord_dt': '20230101', 'ord_tmd': '120000'}


@patch('api.get_overseas_today_history', return_value={'rt_cd': '1'})
@patch('api.get_today_history')
@patch('modules.account.db_manager.db')
def test_체결_한_건이_깨져도_나머지는_적재된다(mock_db, mock_get_history, _ovrs, capsys):
    """종전에는 바깥 except 가 **계좌 단위**라, 한 건이 깨지면 그 뒤 종목의 체결이
    통째로 사라졌다. 체결 기록은 손절선·진입일·실현손익이 붙는 근거다."""
    mock_get_history.return_value = {'rt_cd': '0', 'output1': [
        _hist_item('2001', '005930', 'Samsung'),
        _hist_item('2002', '000660', 'Hynix'),
    ]}
    mock_db.check_trade_exists.return_value = False
    #  앞 건에서만 던진다 — 원인이 무엇이든 그 건에서 멈춰야 한다.
    mock_db.get_trade_by_odno.side_effect = (
        lambda odno, **k: (_ for _ in ()).throw(RuntimeError("이 건에서 터진다"))
        if odno == '2001' else None)

    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = "12345678"
    config.session.auto_acnt_prdt_cd = "01"

    count = account.sync_today_trades()

    assert count == 1, "앞 건의 오류로 뒤 종목의 체결까지 사라졌다"
    saved = [c.args[5] for c in mock_db.insert_trade.call_args_list]
    assert saved == ['2002'], saved
    #  건너뛴 사실은 반드시 드러나야 한다 — '없음'과 '못 읽음'은 다르다.
    assert "동기화에 실패한 계좌" in capsys.readouterr().out
