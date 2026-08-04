import time

import pytest
from datetime import datetime as _real_datetime
from unittest.mock import patch, MagicMock
import api
from modules import auto_trade


class FixedDatetime(_real_datetime):
    """now()만 고정하고 나머지(strptime 등)는 실제 datetime 동작을 유지하는 테스트용 대역.

    MagicMock으로 datetime 모듈 객체를 통째로 갈아끼우면 strftime 포맷과 무관하게
    같은 값이 나오고 strptime도 깨지므로, 서브클래스로 now()만 고정한다.
    """
    _FIXED = _real_datetime(2026, 1, 2, 16, 0, 0)  # NXT 운영 시간(16:00)

    @classmethod
    def now(cls, tz=None):
        return cls._FIXED

# --- NXT Master Tests ---
@patch('api.requests.get')
def test_load_nxt_master_success(mock_get):
    """NXT 마스터 파일 정상 로드 및 파싱 테스트"""
    # 캐시 및 플래그 초기화
    api._NXT_MASTER_LOADED = False
    api._NXT_TRADEABLE_CACHE.clear()
    
    mock_res = MagicMock()
    mock_res.status_code = 200
    # 정상적인 NXT 마스터 파일 응답 (파이프 구분자)
    mock_res.text = "005930|삼성전자|... \n000660|SK하이닉스|...\n"
    mock_get.return_value = mock_res
    
    api.load_nxt_master()
    
    assert api._NXT_MASTER_LOADED is True
    assert "005930" in api._NXT_TRADEABLE_CACHE
    assert "000660" in api._NXT_TRADEABLE_CACHE

@patch('api.requests.get')
def test_load_nxt_master_fail(mock_get):
    """NXT 마스터 파일 로드 실패 시 Fallback 동작 테스트

    [변경] 종전에는 실패해도 로드 완료로 못 박아 프로세스 수명 내내 재시도하지 않았다.
     그 상태에서는 NXT 미지원 종목의 주문(매도 포함)이 계속 거부될 수 있으므로,
     실패는 '완료'가 아니라 '쿨다운 후 재시도'로 바뀌었다.
    """
    api._NXT_MASTER_LOADED = False
    api._NXT_MASTER_RETRY_AT = 0.0
    api._NXT_TRADEABLE_CACHE.clear()

    # API 통신 에러 발생 시뮬레이션
    mock_get.side_effect = Exception("API Connection Error")

    api.load_nxt_master()

    # 실패는 완료가 아니다 — 다음 기회에 다시 받아야 한다
    assert api._NXT_MASTER_LOADED is False
    # 캐시는 비어있어야 함
    assert len(api._NXT_TRADEABLE_CACHE) == 0
    # 다만 매 주문마다 5초씩 붙잡히면 안 되므로 쿨다운이 걸려야 한다
    assert api._NXT_MASTER_RETRY_AT > time.time()

def test_is_nxt_tradeable():
    """NXT 거래 대상 종목 확인 로직 테스트"""
    api._NXT_MASTER_LOADED = True
    api._NXT_TRADEABLE_CACHE = {"005930"}
    api._NXT_REJECTED_CACHE.clear()

    # 1. 마스터 파일 캐시에 종목이 존재할 때
    assert api.is_nxt_tradeable("005930") is True
    # 2. 마스터 파일 캐시에 종목이 없을 때
    assert api.is_nxt_tradeable("000660") is False

    # 3. 마스터 파일 로드 실패로 캐시가 완전히 비어있는 경우 Fallback (모두 통과)
    api._NXT_TRADEABLE_CACHE.clear()
    assert api.is_nxt_tradeable("000660") is True


@patch('api.requests.get')
def test_load_nxt_master_retries_after_the_cooldown(mock_get):
    """[핵심] 쿨다운이 지나면 다시 받아야 한다 — 영구 포기면 종전과 같아진다.

    라즈베리파이는 패키지 적용으로 수시로 재시작되고, 기동 시 5초 타임아웃 한 번에
    실패하면 그 세션 내내 거래소 코드가 낙관 배정으로 고정됐다.
    """
    api._NXT_MASTER_LOADED = False
    api._NXT_MASTER_RETRY_AT = 0.0
    api._NXT_TRADEABLE_CACHE.clear()
    mock_get.side_effect = Exception("boom")

    api.load_nxt_master()
    assert mock_get.call_count == 1
    api.load_nxt_master()          # 쿨다운 중이라 API를 다시 때리지 않는다
    assert mock_get.call_count == 1, "쿨다운을 무시하고 매번 5초씩 붙잡는다"

    # 쿨다운이 지나면 재시도하고, 이번엔 성공한다
    api._NXT_MASTER_RETRY_AT = time.time() - 1
    ok = MagicMock(status_code=200, text="005930|삼성전자|...\n")
    mock_get.side_effect = None
    mock_get.return_value = ok
    api.load_nxt_master()
    assert api._NXT_MASTER_LOADED is True, "쿨다운 후 재시도가 일어나지 않았다"
    assert "005930" in api._NXT_TRADEABLE_CACHE


@patch('api.requests.get')
def test_empty_master_response_is_not_success(mock_get):
    """HTTP 200이어도 종목이 하나도 없으면 성공이 아니다(스펙 변경·빈 응답)."""
    api._NXT_MASTER_LOADED = False
    api._NXT_MASTER_RETRY_AT = 0.0
    api._NXT_TRADEABLE_CACHE.clear()
    mock_get.return_value = MagicMock(status_code=200, text="")

    api.load_nxt_master()

    assert api._NXT_MASTER_LOADED is False, "빈 응답을 로드 성공으로 확정했다"


@patch('api.requests.get')
def test_load_failure_is_logged_where_the_operator_can_see_it(mock_get, caplog):
    """[가시화] 종전에는 debug 레벨이라 기본 설정(INFO)에서 파일에 남지 않았다."""
    api._NXT_MASTER_LOADED = False
    api._NXT_MASTER_RETRY_AT = 0.0
    api._NXT_TRADEABLE_CACHE.clear()
    mock_get.side_effect = Exception("API Connection Error")

    with caplog.at_level("WARNING", logger=api.logger.name):
        api.load_nxt_master()

    assert any("NXT 마스터" in r.message for r in caplog.records), \
        "로드 실패가 WARNING 이상으로 남지 않는다 — 운영자가 알 방법이 없다"


# ─────────── 거래소 코드 오배정 복구 (실계좌 전용 경로) ───────────
#
# 이 분기는 `if not config.session.is_simulation:` 안에 있어 모의·가상투자에서는
# 한 번도 실행되지 않는다. 실계좌 첫날에 처음 도는 코드라 테스트로만 검증할 수 있다.

REJECT = {'rt_cd': '1', 'msg_cd': 'APBK3026', 'msg1': '종목정보를 확인할 수 없습니다', 'output': {}}
ACCEPT = {'rt_cd': '0', 'msg_cd': '0000', 'msg1': '주문 접수 완료', 'output': {'ODNO': 'X1'}}
NO_CASH = {'rt_cd': '1', 'msg_cd': 'APBK1234', 'msg1': '주문가능금액이 부족합니다', 'output': {}}


@pytest.fixture
def real_account():
    """실계좌 세션(모의 아님) — SOR 분기가 실제로 도는 조건."""
    api._NXT_REJECTED_CACHE.clear()
    with patch.object(api.config.session, 'is_simulation', False), \
         patch.object(api.config.session, 'is_toss', False), \
         patch.object(api, '_paper_active', return_value=False), \
         patch.object(api, '_prepare_account_params', return_value=("12345678", "01")), \
         patch.object(api, 'is_nxt_tradeable', return_value=True):
        yield
    api._NXT_REJECTED_CACHE.clear()


def _order(action="sell"):
    return api.place_order("domestic", action, "069500", 10, 30000, "00")


def test_sell_rejected_by_sor_is_retried_on_krx(real_account):
    """[핵심] 매도가 거래소 코드 때문에 거부되면 KRX로 재시도해야 한다.

    매수 거부는 기회 손실로 끝나지만 매도 거부는 보유 포지션의 청산이 막힌 것이다.
    """
    with patch.object(api, 'call_api', side_effect=[REJECT, ACCEPT]) as call:
        res = _order("sell")

    assert call.call_count == 2, "거부로 끝났다 — 청산이 막힌다"
    assert res['rt_cd'] == '0'
    assert call.call_args_list[0].kwargs['data']['EXCG_ID_DVSN_CD'] == "SOR"
    assert call.call_args_list[1].kwargs['data']['EXCG_ID_DVSN_CD'] == "KRX"


def test_the_rejected_code_is_remembered(real_account):
    """다음 주문(특히 손절)이 왕복 없이 곧바로 KRX로 나가야 한다."""
    with patch.object(api, 'call_api', side_effect=[REJECT, ACCEPT]):
        _order("sell")

    assert "069500" in api._NXT_REJECTED_CACHE, "거부 이력을 기록하지 않아 매번 왕복한다"


def test_a_rejection_history_outranks_the_master():
    """[배선] 기록만 하고 판정이 안 보면 소용이 없다 — 증권사 응답이 마스터를 이긴다.

    (real_account 픽스처는 is_nxt_tradeable을 mock 하므로 여기서는 쓰지 않는다.)
    """
    api._NXT_REJECTED_CACHE.clear()
    with patch.object(api, '_NXT_MASTER_LOADED', True), \
         patch.object(api, '_NXT_TRADEABLE_CACHE', {"069500"}):
        assert api.is_nxt_tradeable("069500") is True     # 전제: 마스터는 가능하다고 본다
        api._NXT_REJECTED_CACHE.add("069500")
        assert api.is_nxt_tradeable("069500") is False, "실제 거부 이력을 무시한다"
    api._NXT_REJECTED_CACHE.clear()


def test_other_failures_are_never_retried(real_account):
    """[안전] 다른 실패는 재시도하지 않는다 — 접수됐을 수 있어 이중 주문이 된다."""
    with patch.object(api, 'call_api', side_effect=[NO_CASH, ACCEPT]) as call:
        res = _order("buy")

    assert call.call_count == 1, "무관한 실패에 재시도했다 — 이중 주문 위험"
    assert res['msg_cd'] == 'APBK1234'


def test_success_does_not_trigger_a_second_order(real_account):
    """대조군 — 정상 접수는 한 번만 나가야 한다."""
    with patch.object(api, 'call_api', side_effect=[ACCEPT]) as call:
        res = _order("buy")
    assert call.call_count == 1
    assert res['output']['ODNO'] == 'X1'


def test_retry_happens_only_once(real_account):
    """KRX 재시도까지 실패하면 거기서 멈춘다 — 무한 재주문 방지."""
    with patch.object(api, 'call_api', side_effect=[REJECT, REJECT]) as call:
        res = _order("sell")
    assert call.call_count == 2
    assert res['rt_cd'] == '1'


def test_krx_routed_orders_skip_the_fallback(real_account):
    """이미 KRX로 나가는 주문은 재시도 대상이 아니다(거래소 문제가 아니므로)."""
    with patch.object(api, 'is_nxt_tradeable', return_value=False), \
         patch.object(api, 'call_api', side_effect=[REJECT, ACCEPT]) as call:
        res = _order("sell")
    assert call.call_count == 1
    assert call.call_args_list[0].kwargs['data']['EXCG_ID_DVSN_CD'] == "KRX"
    assert res['rt_cd'] == '1'


# [Fix] patch 대상은 modules.auto_trade가 아니라 실제 코드가 사는 modules.auto_trade.trader다.
#  (패키지 분해 후에도 옛 경로를 patch하고 있어 mock이 걸리지 않았고, 그 결과 이 테스트는
#   실행 시각이 실제로 NXT 시간대(15:30~20:00, 08:00~08:50)일 때만 통과하는 시간의존 테스트였다.)
@patch('modules.auto_trade.trader.datetime', FixedDatetime)
@patch('modules.auto_trade.api.is_nxt_tradeable', return_value=False)
def test_nxt_market_skip_logic(mock_nxt_tradeable):
    """NXT 장 시간대에 거래 불가 종목(ETF 등) 분석 스킵 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    # 시장 필터(fail-closed)가 NXT 스킵보다 먼저 걸리지 않도록 정상 지수 상태를 세팅한다
    trader.buy_halted = False
    trader.market_index_status = {
        "KOSPI": {"is_healthy": True, "unknown": False, "current": 2500.0},
        "KOSDAQ": {"is_healthy": True, "unknown": False, "current": 800.0},
    }

    # 1. 매수 후보 분석 워커 스킵 테스트
    item = {'code': '000660', 'name': 'SK하이닉스', 'group': 'stocks_kr'}
    res = trader._analyze_candidate_worker(
        item=item,
        holding_codes=set(),
        rules_map={},
        restricted_stocks={},
        market_regime_adj={},
        safe_delay=0,
        reentry_hurdles={},
        holdings_dfs={},
        holding_groups_map={}
    )
    
    # NXT 거래 불가 종목이므로 스킵 로그 객체 반환 확인
    assert res is not None
    assert res.get('type') == 'log_only'
    assert 'NXT스킵' in res.get('log')
    
    # 2. 매도 후보 분석 로직 스킵 테스트
    holdings = [{'pdno': '000660', 'prdt_name': 'SK하이닉스', 'ord_psbl_qty': '10', 'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '55000'}]
    with patch.object(trader, 'set_stock_state') as mock_set_state:
        trader._check_sell_conditions(holdings)
        # NXT 불가 종목으로 스킵되며, 상태가 None으로 세팅되었는지 확인
        mock_set_state.assert_called_with('000660', None)