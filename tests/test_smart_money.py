import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime
import config
from modules import analysis
from modules import backtest

@pytest.fixture(autouse=True)
def clear_sm_cache():
    """테스트 간 캐시 간섭을 막기 위한 자동 초기화"""
    analysis.clear_smart_money_cache()
    yield
    analysis.clear_smart_money_cache()

# ==========================================================
# 1. analysis.check_smart_money_turnaround 테스트
# ==========================================================

def test_check_smart_money_overseas():
    """해외 주식은 수급 데이터를 제공하지 않으므로 즉시 False 반환"""
    flag, reason = analysis.check_smart_money_turnaround("AAPL", is_overseas=True)
    assert flag is False
    assert reason == ""

@patch('api.get_investor_trend')
def test_check_smart_money_no_data(mock_api):
    """API 응답이 비어있거나 데이터가 부족한 경우 예외 처리"""
    mock_api.return_value = []
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is False
    
    mock_api.return_value = [{'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'}] # 3일치 미만
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is False

@patch('api.get_investor_trend')
def test_check_smart_money_ssang_ssang(mock_api):
    """당일 쌍끌이 매수 포착 테스트"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '100', 'orgn_ntby_qty': '200'}, # D-day (외인+, 기관+)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'}
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "쌍끌이 매수"

@patch('api.get_investor_trend')
def test_check_smart_money_foreign_turnaround(mock_api):
    """외국인 단독 턴어라운드 (D-day 매수, D-1/D-2 연속 매도) 포착"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '150', 'orgn_ntby_qty': '-50'},  # D-day (매수)
        {'frgn_ntby_qty': '-100', 'orgn_ntby_qty': '-10'}, # D-1 (매도)
        {'frgn_ntby_qty': '-200', 'orgn_ntby_qty': '-10'}, # D-2 (매도)
        {'frgn_ntby_qty': '300', 'orgn_ntby_qty': '-10'}   # D-3
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "외국인 턴어라운드"

@patch('api.get_investor_trend')
def test_check_smart_money_orgn_turnaround_yesterday(mock_api):
    """전일 기준 기관 턴어라운드 포착 (최근 2일 내 검사)"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-50'},  # D-day (매도)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '300'},  # D-1 (매수) -> 턴어라운드 발생일!
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-100'}, # D-2 (매도)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-200'}  # D-3 (매도)
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "기관 턴어라운드"

@patch('api.get_investor_trend')
def test_check_smart_money_cache_hit(mock_api):
    """메모리 TTL 캐시 적용 여부 확인"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '100', 'orgn_ntby_qty': '200'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'}
    ]
    # 첫 번째 호출 (API 사용)
    analysis.check_smart_money_turnaround("005930")
    assert mock_api.call_count == 1
    
    # 두 번째 호출 (캐시 사용으로 API 미호출)
    analysis.check_smart_money_turnaround("005930")
    assert mock_api.call_count == 1

# ==========================================================
# 2. analysis.calculate_score 테스트
# ==========================================================

def test_calculate_score_smart_money():
    """스마트머니 플래그에 따른 0.5점 가산점 정상 부여 확인"""
    test_weights = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
    # 동일한 조건에서 smart_money 여부만 변경
    score_no_sm, _ = analysis.calculate_score(1000, None, None, None, None, None, None, None, False, weights=test_weights, smart_money=False)
    score_sm, details = analysis.calculate_score(1000, None, None, None, None, None, None, None, False, weights=test_weights, smart_money=True)
    
    # 정확히 0.5점 차이 확인
    assert score_sm == round(score_no_sm + 0.5, 2)
    assert any("OBV/SM 개선" in d for d in details)

# ==========================================================
# 3. backtest._append_smart_money_signal 테스트
# ==========================================================

@patch('api.get_investor_trend')
def test_append_smart_money_signal_vectorized(mock_api):
    """백테스팅을 위한 판다스 벡터화 병합 및 조건 처리 확인"""
    # 차트 데이터 프레임 (시간순 정렬)
    df = pd.DataFrame({
        'date': ['20231001', '20231002', '20231003', '20231004', '20231005']
    })
    
    # API 응답 (최신순 역정렬)
    mock_api.return_value = [
        {'stck_bsop_date': '20231005', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'},  # D-Day: 쌍끌이 (True)
        {'stck_bsop_date': '20231004', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '-10'},  # D-1: 외인 매수 (D-2/D-3이 음수이므로 외인 턴어라운드 True)
        {'stck_bsop_date': '20231003', 'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},  # D-2
        {'stck_bsop_date': '20231002', 'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},  # D-3
        {'stck_bsop_date': '20231001', 'frgn_ntby_qty': '10',  'orgn_ntby_qty': '-10'},  # D-4: 쌍끌이 아님 & 과거 데이터 부족으로 턴어라운드 판별 불가 -> False
    ]
    
    with patch('config.console.print'): # 안내 문구 출력 숨김
        res_df = backtest._append_smart_money_signal(df.copy(), "005930", is_overseas=False)
    
    assert 'smart_money' in res_df.columns
    
    # 검증: 20231001 (이전 데이터 없으므로 False)
    assert res_df.loc[res_df['date'] == '20231001', 'smart_money'].iloc[0] == False
    
    # 검증: 20231004 (외인 턴어라운드 성공일)
    assert res_df.loc[res_df['date'] == '20231004', 'smart_money'].iloc[0] == True
    
    # 검증: 20231005 (당일 쌍끌이 + 어제 외인 턴어라운드 성공일 반영됨)
    assert res_df.loc[res_df['date'] == '20231005', 'smart_money'].iloc[0] == True

# ==========================================================
# 3. 과거 수급 소스 (KRX 우선 · KIS 폴백) — 2026-08-24
# ==========================================================
# KIS 수급 TR 에는 기간 파라미터가 없고 최근 30거래일만 온다. 다년 백테스트에서는 창 밖이
# 통째로 '수급 없음'으로 굳어(merge 후 fillna(0)) 스마트머니 축이 사실상 빠져 있었다.
# KRX(pykrx)는 같은 값을 기간으로 주므로 그쪽을 먼저 쓰고, 자격증명이 없으면 종전대로 KIS 로
# 되돌아간다. 아래 테스트는 그 우선순위와 폴백, 그리고 컬럼 대응을 고정한다.

def _bars(dates):
    return pd.DataFrame({"date": list(dates), "close": 70000.0})


def test_investor_frame_prefers_krx():
    """KRX 가 구간을 주면 그걸 쓴다 (KIS 는 부르지도 않는다)."""
    krx = pd.DataFrame({"date": ["20240102", "20240103"],
                        "f_net": [100, -200], "o_net": [300, -400]})
    with patch('modules.krx_daily.get_investor_netbuy', return_value=krx) as mk, \
         patch('api.get_investor_trend') as mkis:
        frame, source = backtest._investor_netbuy_frame(_bars(["20240102", "20240103"]), "005930")

    assert source == "KRX"
    assert list(frame["f_net"]) == [100, -200]
    mk.assert_called_once()
    mkis.assert_not_called()          # 폴백 경로를 건드리지 않는다


def test_investor_frame_falls_back_to_kis_without_krx():
    """KRX 가 None(자격증명 없음 등)이면 기존 KIS 경로로 되돌아간다."""
    kis = [{'stck_bsop_date': '20240102', 'frgn_ntby_qty': '11', 'orgn_ntby_qty': '22'}]
    with patch('modules.krx_daily.get_investor_netbuy', return_value=None), \
         patch('api.get_investor_trend', return_value=kis):
        frame, source = backtest._investor_netbuy_frame(_bars(["20240102"]), "005930")

    assert source == "KIS"
    assert list(frame["f_net"]) == [11] and list(frame["o_net"]) == [22]


def test_investor_frame_none_when_both_sources_empty():
    """둘 다 없으면 None — 호출부가 '수급 없음' 안내를 띄울 수 있어야 한다."""
    with patch('modules.krx_daily.get_investor_netbuy', return_value=None), \
         patch('api.get_investor_trend', return_value=[]):
        frame, source = backtest._investor_netbuy_frame(_bars(["20240102"]), "005930")
    assert frame is None and source is None


def test_krx_source_covers_dates_outside_the_kis_window():
    """핵심 회귀: KIS 창(최근 30일) 밖 과거 구간에서도 시그널이 켜진다.

    종전에는 이 구간이 전부 smart_money=False 였다 — 없는 데이터가 '아니다'로 기록됐다.
    """
    dates = ["20240102", "20240103", "20240104"]
    # 외국인·기관 동반 순매수(c1) → 당일과 다음날 모두 True 가 되는 조건
    krx = pd.DataFrame({"date": dates, "f_net": [500, 600, 700], "o_net": [500, 600, 700]})
    with patch('modules.krx_daily.get_investor_netbuy', return_value=krx), \
         patch('api.get_investor_trend', return_value=[]) as mkis:
        out = backtest._append_smart_money_signal(_bars(dates), "005930", is_overseas=False)

    assert out["smart_money"].all(), "KRX 구간인데 시그널이 꺼져 있다"
    mkis.assert_not_called()


def test_kis_fallback_warns_when_coverage_is_partial(capsys):
    """KIS 폴백이 구간을 못 덮으면 그 사실을 알린다 (조용히 False 로 굳지 않게)."""
    dates = [f"2024010{i}" for i in range(2, 6)] + ["20240108"]
    kis = [{'stck_bsop_date': '20240108', 'frgn_ntby_qty': '1', 'orgn_ntby_qty': '1'}]
    with patch('modules.krx_daily.get_investor_netbuy', return_value=None), \
         patch('api.get_investor_trend', return_value=kis), \
         patch.object(config.console, "print") as mock_print:
        backtest._append_smart_money_signal(_bars(dates), "005930", is_overseas=False)

    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
    assert "KRX_ID" in printed and "수급 데이터가 최근 구간" in printed


# ==========================================================
# 4. 선견(lookahead) — D일 판정이 D일의 확정 수급을 쓴다 (2026-09-01)
# ==========================================================
# 백테스트는 D일 행의 smart_money 를 D일 하루치 순매수 **최종값**으로 만든다. 실매매가
# 그 시점에 보는 것은 '천천히 갱신되는 잠정치'라(api/quotes/price.py 주석) 그날 순매수가
# 얼마로 끝날지 모른다. 장중 스캔 모드는 더 노골적이다 — intraday_bars 가 09:30 봉부터
# 그날 확정 수급을 단다(비가격 컬럼 재사용).
#
# 기존 선견 회귀(tests/test_portfolio_backtest.py)는 미래 **날짜**를 잘라 비교하므로
# 같은 날 안에서 새는 이것을 구조적으로 못 잡는다. 그래서 별도로 못을 박는다.
#
# SMART_MONEY_LAG 는 그 크기를 재기 위한 손잡이다(tools/audit_smart_money_lag.py).
# 기본값 0 = 종전 동작 — 이전 감사 수치와의 연속성을 위해 바꾸지 않는다.

@pytest.fixture(autouse=True)
def _reset_lag():
    backtest.SMART_MONEY_LAG = 0
    yield
    backtest.SMART_MONEY_LAG = 0


def _sm(dates, f, o, **kw):
    krx = pd.DataFrame({"date": list(dates), "f_net": list(f), "o_net": list(o)})
    with patch('modules.krx_daily.get_investor_netbuy', return_value=krx), \
         patch('api.get_investor_trend', return_value=[]):
        return backtest._append_smart_money_signal(_bars(dates), "005930",
                                                   is_overseas=False, **kw)


def test_the_default_still_reads_the_same_days_netbuy():
    """[현행 고정] lag=0 이면 D일 쌍끌이가 **그날** 켜진다 — 이것이 선견의 형태다."""
    dates = ["20240102", "20240103", "20240104"]
    out = _sm(dates, [-1, -1, 900], [-1, -1, 900])
    assert bool(out.loc[out["date"] == "20240104", "smart_money"].iloc[0]) is True


def test_lag_one_only_uses_what_live_already_knows():
    """lag=1 이면 그 쌍끌이는 **다음 날**에야 보인다 (전일까지 확정된 수급만)."""
    dates = ["20240102", "20240103", "20240104", "20240105"]
    out = _sm(dates, [-1, -1, 900, -1], [-1, -1, 900, -1], lag=1)
    got = {d: bool(v) for d, v in zip(out["date"], out["smart_money"])}
    assert got["20240104"] is False, "하루 앞을 보고 있다"
    assert got["20240105"] is True, "전일 확정 수급이 반영되지 않았다"


def test_the_module_default_drives_the_lag_too():
    """감사 도구는 인자가 아니라 모듈 기본값으로 팔을 가른다 — 그 통로를 고정한다."""
    dates = ["20240102", "20240103", "20240104"]
    backtest.SMART_MONEY_LAG = 1
    out = _sm(dates, [-1, -1, 900], [-1, -1, 900])
    assert bool(out.loc[out["date"] == "20240104", "smart_money"].iloc[0]) is False


# ==========================================================
# 5. 감사 재현성 — 이 축은 환경변수 유무로 켜지고 꺼진다 (2026-09-01)
# ==========================================================
# KRX_ID/KRX_PW 가 있으면 전 구간(KRX), 없으면 최근 30거래일만(KIS), 다 실패하면 전 구간
# False. 자격증명이 다른 두 기계의 감사는 서로 다른 전략을 잰 것인데, 결과 어디에도 그
# 상태가 남지 않았다. prepare_universe 가 한 줄로 알린다(warn_if_unmodeled 와 같은 문).

def test_the_source_of_every_stock_is_recorded():
    backtest.reset_smart_money_source()
    krx = pd.DataFrame({"date": ["20240102"], "f_net": [1], "o_net": [1]})
    with patch('modules.krx_daily.get_investor_netbuy', return_value=krx), \
         patch('api.get_investor_trend', return_value=[]):
        backtest._append_smart_money_signal(_bars(["20240102"]), "005930", is_overseas=False)
    with patch('modules.krx_daily.get_investor_netbuy', return_value=None), \
         patch('api.get_investor_trend', return_value=[]), \
         patch.object(config.console, "print"):
        backtest._append_smart_money_signal(_bars(["20240102"]), "000660", is_overseas=False)

    assert backtest.smart_money_source_summary() == {"KRX": 1, "없음": 1}


def test_a_run_without_krx_says_so_loudly():
    """KRX 가 한 종목도 없으면 = 축이 사실상 빠진 채로 도는 중이다. 눈에 띄어야 한다."""
    from modules import portfolio_backtest as pb

    backtest.reset_smart_money_source()
    backtest._SMART_MONEY_SOURCE.update({"005930": "KIS", "000660": None})
    with patch.object(config.console, "print") as mock_print:
        dist = pb.announce_smart_money_source()

    assert dist == {"KIS": 1, "없음": 1}
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
    assert "KRX_ID" in printed


def test_a_run_with_krx_does_not_nag():
    from modules import portfolio_backtest as pb

    backtest.reset_smart_money_source()
    backtest._SMART_MONEY_SOURCE.update({"005930": "KRX"})
    with patch.object(config.console, "print") as mock_print:
        dist = pb.announce_smart_money_source()

    assert dist == {"KRX": 1}
    assert not mock_print.called, "정상 상태에서 콘솔을 어지럽히지 않는다"


# ==========================================================
# [모름 vs 없음] 조회 실패를 1시간짜리 부정 캐시에 굳히지 않는다 (2026-09-05)
# ==========================================================
#  스마트머니 캐시는 TTL 1시간이다. 그 캐시에 '실패'가 들어가면 API 가 곧바로
#  복구돼도 그 종목은 한 시간 동안 재조회조차 되지 않는다(실측: 호출 0회).
#  스마트머니는 STRENGTH 항목(`if obv_trend or smart_money`)과 상태 판정에 쓰이므로,
#  꺼진 채 굳으면 그 종목의 점수가 조용히 낮아진다.

@patch('api.get_investor_trend')
def test_수급_조회_실패는_캐시하지_않는다(mock_api):
    """실패 뒤 복구되면 **다음 호출에서 곧바로** 다시 물어봐야 한다."""
    mock_api.return_value = None                      # 조회 실패 = 모름
    assert analysis.check_smart_money_turnaround("005930") == (False, "")

    mock_api.reset_mock()
    mock_api.return_value = [{'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'}] * 5
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert mock_api.call_count == 1, (
        "실패가 캐시에 굳었다 — 복구된 뒤에도 한 시간 동안 재조회가 없다")
    assert (flag, reason) == (True, "쌍끌이 매수")


@patch('api.get_investor_trend')
def test_진짜_수급_없음은_종전대로_굳힌다(mock_api):
    """ETF·미제공 종목까지 5분마다 다시 묻게 만들면 안 된다(그게 이 캐시의 목적)."""
    mock_api.return_value = []                        # 권위 있는 '없음'
    assert analysis.check_smart_money_turnaround("069500") == (False, "")

    mock_api.reset_mock()
    mock_api.return_value = [{'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'}] * 5
    assert analysis.check_smart_money_turnaround("069500") == (False, "")
    assert mock_api.call_count == 0, "'없음' 캐시가 사라지면 REST/TPS 절감이 무너진다"


def test_api_층이_실패를_None_으로_올린다():
    """소비자가 갈라 볼 수 있으려면 경계에서 모양이 달라야 한다."""
    import api
    from unittest.mock import patch as _patch

    fail = {'rt_cd': '1', 'msg_cd': 'EGW00201', 'msg1': '초당 거래건수를 초과하였습니다.'}
    with _patch('api.call_api', return_value=fail):
        assert api.get_investor_trend("005930") is None

    ok_empty = {'rt_cd': '0', 'output': []}
    with _patch('api.call_api', return_value=ok_empty):
        assert api.get_investor_trend("005931") == [], "권위 있는 '없음'은 빈 리스트다"


def test_실패한_수급은_마이크로캐시에도_안_남는다():
    """5분 캐시에 굳으면 그 사이 재시도가 통째로 사라진다."""
    import api
    from unittest.mock import patch as _patch

    code = "005932"
    fail = {'rt_cd': '1', 'msg1': 'timeout'}
    with _patch('api.call_api', return_value=fail) as m:
        assert api.get_investor_trend(code) is None
        assert m.call_count == 1

    good = {'rt_cd': '0', 'output': [{'stck_bsop_date': '20260904',
                                      'frgn_ntby_qty': '1', 'orgn_ntby_qty': '1'}]}
    with _patch('api.call_api', return_value=good) as m:
        assert api.get_investor_trend(code) == good['output']
        assert m.call_count == 1, "실패가 5분 마이크로캐시에 굳어 재조회가 막혔다"
