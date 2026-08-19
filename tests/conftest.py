import json as _json
import sys
import os
import pytest
import pandas as pd
import numpy as np

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api # [추가] 외부 API 차단용
from modules import db_manager # [추가] DB 매니저 임포트
from modules import analysis # [추가] 지수 조회 차단용
from modules.auto_trade import AutoTrader, ConclusionMonitor
from modules.auto_trade import engine as _atr_engine  # [추가] 지수 변동성 배율 전역 격리
from modules.telegram_bot import TelegramCommander

@pytest.fixture(scope="session", autouse=True)
def setup_config():
    """테스트 세션 동안 사용할 설정 초기화 (모의투자 모드 강제)"""
    # 테스트 중 실수로 실전 API가 호출되지 않도록 안전장치
    config.session.initialize(mode="1")
    # [지연 임포트 대응] genai는 운영에서 최초 사용 시 로드되지만, 테스트는
    # modules.theme_analysis.genai.GenerativeModel 을 직접 patch하므로 미리 채워 둔다.
    from modules import theme_analysis
    theme_analysis._ensure_genai()


@pytest.fixture(scope="session", autouse=True)
def block_side_effects_for_whole_session():
    """[격리] 세션 전체에서 '이미지 뷰어 팝업'과 '실제 텔레그램 전송'을 차단한다.

    함수 스코프 fixture(monkeypatch)로는 막을 수 없다. 테스트가 띄운 백그라운드 스레드는
    테스트 종료 후에도 살아남는데, 그 시점엔 monkeypatch가 원복되어 진짜 토큰으로 실제
    메시지가 나간다(운영자 휴대폰에 더미 알림이 도착). 뷰어도 마찬가지로 테스트가 끝난 뒤
    Popen이 살아 있어 ResourceWarning('subprocess is still running')을 남긴다.
    그래서 세션 스코프로 원천 차단한다.

    - 뷰어: chart.open_image_viewer 자체를 무력화한다(차트 파일 생성은 그대로 검증된다).
    - 텔레그램: HTTP 경계에서 막는다. send_telegram_message 함수를 갈아끼우지 않으므로
      '전송이 호출됐는가'를 검증하는 기존 테스트(각자 자기 참조를 patch)는 그대로 동작하고,
      실제 네트워크 전송만 사라진다. getUpdates 폴링도 같은 경로라 함께 막힌다.
    """
    mp = pytest.MonkeyPatch()

    from modules import chart as _chart
    mp.setattr(_chart, "open_image_viewer", lambda *a, **k: True)

    import requests as _requests

    class _FakeTelegramResponse:
        status_code = 200
        text = '{"ok": true, "result": []}'

        @staticmethod
        def json():
            return {"ok": True, "result": []}

    _orig_request = _requests.Session.request

    # [격리] 실 증권사 접속 차단. 테스트가 KIS로 나가면 세 가지가 동시에 나빠진다.
    #  ① 결과가 서버 상태에 좌우돼 플래키해진다(실측: 킬스위치 테스트가 40% 확률로 실패했고,
    #     원인은 _errors_are_not_the_server()의 check_server_health가 진짜로 나간 것이었다).
    #  ② 운영 인스턴스의 유량(20 TPS)을 갉아먹는다.
    #  ③ 토큰 발급은 앱키당 1분 1회다. 테스트 한 번이 실 운용 토큰 발급을 막을 수 있다.
    #  차단은 예외가 아니라 **KIS 오류 응답 형태**로 돌려준다. 예외로 막으면
    #  ThrottledSession.request의 재시도 루프가 백오프와 함께 MAX_RETRIES만큼 돌아
    #  테스트 한 건이 100초까지 늘어난다(실측). rt_cd='1' 응답이면 재시도 없이
    #  호출부가 '조회 실패' 경로를 그대로 타므로 빠르고 결정적이다.
    #  '조용히 가짜 성공'이 되지 않는 것이 핵심이다 — 성공을 흉내 내지 않는다.
    #  실 서버가 필요한 진단은 tools/ 에서 pytest 밖으로 돌린다.
    #
    #  [확대 2026-08-19] 종전에는 KIS·TV·야후만 막았다. 그런데 토스(mode 3·4의 시세·주문
    #  소스)가 열려 있어 test_toss_api.py가 목 없이 실 서버를 호출했고, 실행마다 결과가
    #  달라지며 로그에 '401 invalid-token → 토큰 재발급' 경고가 찍혔다(실측: 전체 스위트
    #  3회 중 1회 실패). 위 ①②③은 토스에도 그대로 적용된다 — 특히 ③은 파이에서 자동매매가
    #  도는 중이면 실 운용 토큰을 무효화할 수 있다(같은 앱키 재발급 = 앞선 토큰 폐기).
    #  DART·네이버·KRX·구글뉴스도 같은 이유로 함께 막는다.
    #  차단 응답은 **호스트별 실패 형태**로 돌려준다. 한 가지 형태로 통일하면 파서가
    #  예외를 내면서 '차단'이 '버그'로 보인다.
    _LIVE_HOSTS = ("koreainvestment.com", "tradingview.com", "finance.yahoo.com")
    _TOSS_HOSTS = ("tossinvest.com",)
    _DART_HOSTS = ("opendart.fss.or.kr", "dart.fss.or.kr")
    _ETC_HOSTS = ("finance.naver.com", "news.google.com", "krx.co.kr")

    class _BlockedResponse:
        """차단 응답의 공통 껍데기 — 본문만 호스트별로 갈아끼운다."""
        status_code = 200
        headers = {}

        def __init__(self, payload):
            self._payload = payload
            self.text = _json.dumps(payload, ensure_ascii=False)
            self.content = self.text.encode("utf-8")

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    _BLOCKED_MSG = "[테스트 격리] 실 서버 접속이 차단되었습니다"

    def _blocked_for(url):
        if any(h in url for h in _TOSS_HOSTS):
            # 토스 envelope: 200 + result 없음 → 호출부가 '조회 실패'로 흐른다.
            #  비 2xx로 막으면 TossApiError가 나면서 재시도·토큰 재발급 경로가 돈다.
            return _BlockedResponse({"result": None,
                                     "error": {"code": "TEST_BLOCKED", "message": _BLOCKED_MSG}})
        if any(h in url for h in _DART_HOSTS):
            # DART status 013 = '조회된 데이터 없음'(정상 케이스) → 경고 로그 없이 빈 결과.
            return _BlockedResponse({"status": "013", "message": _BLOCKED_MSG, "list": []})
        if any(h in url for h in _ETC_HOSTS):
            return _BlockedResponse({})
        return _BlockedResponse({"rt_cd": "1", "msg_cd": "TEST_BLOCKED",
                                 "msg1": _BLOCKED_MSG,
                                 "output": {}, "output1": [], "output2": []})

    _ALL_BLOCKED = _LIVE_HOSTS + _TOSS_HOSTS + _DART_HOSTS + _ETC_HOSTS

    def _guarded_request(self, method, url, *args, **kwargs):
        u = str(url)
        if "api.telegram.org" in u:
            return _FakeTelegramResponse()
        if any(h in u for h in _ALL_BLOCKED):
            return _blocked_for(u)
        return _orig_request(self, method, url, *args, **kwargs)

    mp.setattr(_requests.Session, "request", _guarded_request)

    yield
    mp.undo()


@pytest.fixture(autouse=True)
def isolate_session_state():
    """[격리] config.session은 전역 공유 객체이므로, 개별 테스트가 모드/키를
    바꿔도(예: is_simulation=False) 다음 테스트로 누수되지 않도록 매 테스트 후
    상태를 원복한다.

    누수를 방치하면 setup_config가 강제한 모의투자 모드가 풀려, 시세/지수 조회가
    실전 도메인(:9443)으로 나가 EGW00304(고객식별키 무효) 등이 발생할 수 있다.
    """
    snapshot = dict(config.session.__dict__)
    yield
    config.session.__dict__.clear()
    config.session.__dict__.update(snapshot)


def _mock_index_chart_df(periods=60):
    """KIS 지수 차트(get_domestic_index_chart)의 원시 응답 형태를 흉내 낸 더미 데이터.

    get_domestic_index_data가 이 컬럼들을 rename/숫자변환하므로 원시 컬럼명을 유지한다.
    충분한 길이(>= REGIME_MA_PERIOD)를 제공해 yfinance Fallback(실 네트워크)까지 차단한다.
    """
    dates = pd.date_range(end="2024-01-01", periods=periods).strftime("%Y%m%d")
    base = np.linspace(2400.0, 2450.0, periods)
    return pd.DataFrame({
        'stck_bsop_date': dates,
        'bstp_nmix_prpr': base,
        'bstp_nmix_oprc': base * 0.999,
        'bstp_nmix_hgpr': base * 1.005,
        'bstp_nmix_lwpr': base * 0.995,
        'acml_vol': np.random.randint(1000, 5000, periods),
    })


@pytest.fixture(autouse=True)
def block_external_market_api(request, monkeypatch):
    """[격리] 분석 워커(ThreadPoolExecutor) 등에서 지수 조회가 mock 없이 실행되면
    실제 한투 서버로 네트워크 요청이 나간다. 하위 진입점인 get_domestic_index_chart를
    더미 데이터로 대체해 실 호출과 yfinance Fallback을 모두 차단한다.

    개별 테스트가 직접 patch하면(예: test_strategy) 그 patch가 우선 적용되고
    종료 시 이 기본값으로 복원되므로 충돌하지 않는다.
    단, get_domestic_index_chart 자체의 로직을 검증하는 테스트는
    @pytest.mark.real_index_chart 로 이 mock을 비활성화한다.
    """
    if request.node.get_closest_marker("real_index_chart"):
        return
    monkeypatch.setattr(api, "get_domestic_index_chart",
                        lambda *a, **k: _mock_index_chart_df(), raising=False)
    # [격리] 멀티시세 프리페치가 mock 없는 print_table 테스트에서 실 서버로 나가지 않도록
    # 기본 비활성화한다. (멀티시세 전용 테스트는 개별적으로 다시 켠다)
    monkeypatch.setattr(config, "USE_MULTI_PRICE", False, raising=False)
    monkeypatch.setattr(api, "_MULTI_PRICE_DISABLED", False, raising=False)

_EMPTY_DOMESTIC_BALANCE = {
    'rt_cd': '0', 'msg_cd': 'MOCK', 'msg1': '테스트용 빈 잔고',
    'output1': [],
    'output2': [{
        'dnca_tot_amt': '0', 'prvs_rcdl_excc_amt': '0', 'd2_auto_rdpt_amt': '0',
        'scts_evlu_amt': '0', 'tot_evlu_amt': '0', 'nass_amt': '0',
        'evlu_pfls_smtl_amt': '0', 'asst_icdc_amt': '0',
    }],
}
_EMPTY_OVERSEAS_BALANCE = {
    'rt_cd': '0', 'msg_cd': 'MOCK', 'msg1': '테스트용 빈 잔고',
    'output1': [], 'output2': {},
    'ctx_area_fk200': '', 'ctx_area_nk200': '',
}


_EMPTY_ACCOUNT_INQUIRY = {
    'rt_cd': '0', 'msg_cd': 'MOCK', 'msg1': '테스트용 빈 응답',
    # KIS는 단수 'output'을 dict로, 'output1/2'를 list로 돌려준다.
    #  (fetch_buyable_quantity 등은 output.get(...)을 호출하므로 dict여야 한다)
    'output': {}, 'output1': [], 'output2': [],
    'ctx_area_fk100': '', 'ctx_area_nk100': '',
    'ctx_area_fk200': '', 'ctx_area_nk200': '',
}


@pytest.fixture(autouse=True)
def block_account_inquiry_api(request, monkeypatch):
    """[격리] 계좌 조회 API(``/trading/`` + category='inquiry')를 기본 차단하고
    '빈 계좌' 응답으로 대체한다.

    막지 않으면 잔고·체결내역 조회가 mock 없이 실제 한투 서버로 나간다
    (실측: 잔고 19건 + 체결내역 등). 그러면
      - 네트워크가 끊기거나 토큰이 만료되면 무관한 테스트가 무더기로 흔들리고,
      - 모의계좌의 보유 종목이 바뀌는 것만으로 결과가 달라지며,
      - 계좌번호를 가짜로 바꾸는 테스트는 INVALID_CHECK_ACNO 오류 로그를 뿜는다.
    (직렬 실행에서는 앞선 테스트가 채운 캐시에 가려 잘 드러나지 않고, xdist 병렬에서
     워커가 캐시 없이 시작할 때 표면화된다 — 발견이 늦은 이유다.)

    주문(POST) 경로는 category가 'order'라 여기에 걸리지 않으므로 기존 주문 테스트는
    그대로 동작한다. 조회 API 자체의 파싱·폴백 로직을 검증하는 테스트는
    HTTP 계층(api.session.request)을 직접 mock하므로
    @pytest.mark.real_balance_api 로 이 차단을 비활성화한다.
    """
    if request.node.get_closest_marker("real_balance_api"):
        return
    orig_call_api = api.call_api

    def _guarded_call_api(url_path, *args, **kwargs):
        # call_api(url_path, market, category, action, ...) — 위치·키워드 모두 대응
        category = kwargs.get("category", args[1] if len(args) > 1 else None)
        url = str(url_path)
        if "trading" in url and category == "inquiry":
            if "inquire-balance" in url:
                return dict(_EMPTY_OVERSEAS_BALANCE if "overseas" in url
                            else _EMPTY_DOMESTIC_BALANCE)
            return dict(_EMPTY_ACCOUNT_INQUIRY)
        return orig_call_api(url_path, *args, **kwargs)

    monkeypatch.setattr(api, "call_api", _guarded_call_api)


@pytest.fixture(autouse=True)
def block_buy_restriction_cleanup_thread(monkeypatch):
    """[격리] 수동 매수 제한 정리 데몬 스레드가 테스트에서 뜨지 않게 한다.

    schedule_buy_restriction_cleanup은 주문 성공 시 데몬 스레드를 띄워 최대 10분간
    15초 간격으로 잔고 API를 폴링한다(common.py). 테스트에서 send_order가 성공하면
    이 스레드가 뜨고, **테스트가 끝나 monkeypatch가 원복된 뒤에도 계속 살아남아**
    이후 무관한 테스트가 도는 내내 실제 한투 서버로 잔고 조회를 날린다.
    (실측: 한 번의 전체 실행에서 27건. 오류 로그가 엉뚱한 테스트 이름 밑에 찍혀
     원인 추적이 어려웠다. block_account_inquiry_api만으로는 막히지 않는다 —
     그 fixture는 테스트 종료와 함께 원복되기 때문이다.)
    """
    from modules.auto_trade import common as _at_common
    monkeypatch.setattr(_at_common, "schedule_buy_restriction_cleanup",
                        lambda *a, **k: None)
    monkeypatch.setattr("modules.auto_trade.schedule_buy_restriction_cleanup",
                        lambda *a, **k: None, raising=False)


@pytest.fixture(autouse=True)
def isolate_test_files(tmp_path, monkeypatch):
    """
    모든 테스트 실행 전 자동으로 임시 경로를 할당하여 
    실제 운영 데이터(json, db)가 덮어써지는 것을 방지합니다.
    """
    test_json = tmp_path / "test_stock.json"
    test_token = tmp_path / "test_token_cache.json"
    test_db = tmp_path / "test_trade_history.db"

    # 기본 더미 데이터 초기화
    test_json.write_text('{"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}', encoding='utf-8')

    monkeypatch.setattr(config, "STOCK_DATA_FILE", str(test_json))
    monkeypatch.setattr(config, "TOKEN_CACHE_FILE", str(test_token))
    monkeypatch.setattr(config, "DB_FILE_PATH", str(test_db))
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path)) # 시스템 설정(dynamic_config.json) 덮어쓰기 방지

    # [격리] RESTRICTED_FILE/DAILY_STATE_FILE은 import 시점에 config.JSON_DIR로 고정되므로 위
    # JSON_DIR 패치만으로는 격리되지 않는다. 또한 load/save 함수는 '패키지 재수출 속성'이 아니라
    # modules.auto_trade.common 모듈의 전역을 직접 참조하므로, 반드시 common 쪽을 패치해야 한다.
    # (과거 패키지 속성만 패치해 테스트의 수동매매 감지가 운영 restricted_stocks.json을 오염시켰음)
    monkeypatch.setattr("modules.auto_trade.common.RESTRICTED_FILE", str(tmp_path / "restricted_stocks.json"))
    monkeypatch.setattr("modules.auto_trade.RESTRICTED_FILE", str(tmp_path / "restricted_stocks.json"), raising=False)
    monkeypatch.setattr("modules.auto_trade.common.DAILY_STATE_FILE", str(tmp_path / "daily_asset_state.json"))
    monkeypatch.setattr("modules.auto_trade.DAILY_STATE_FILE", str(tmp_path / "daily_asset_state.json"), raising=False)

    # [추가] 테스트 중 생성되는 파일(차트, 엑셀, 로그) 격리
    test_chart_dir = tmp_path / "chart"
    test_data_dir = tmp_path / "data"
    test_log_dir = tmp_path / "logs"
    test_chart_dir.mkdir(exist_ok=True)
    test_data_dir.mkdir(exist_ok=True)
    test_log_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(config, "CHART_DIR", str(test_chart_dir))
    monkeypatch.setattr(config, "DATA_DIR", str(test_data_dir))
    monkeypatch.setattr(config, "LOG_DIR", str(test_log_dir))
    monkeypatch.setattr(config, "SYSTEM_TRADING_LOG_DIR", str(test_log_dir))

    # [추가] 전역 DB 인스턴스의 경로를 임시 DB로 강제 변경하여 실제 DB 오염 방지
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    monkeypatch.setattr(real_db, "db_path", str(test_db))
    
    if hasattr(real_db, 'local') and hasattr(real_db.local, 'conn'):
        if real_db.local.conn:
            try:
                real_db.local.conn.close()
            except Exception:
                pass
            real_db.local.conn = None
    real_db._init_db()

    # [추가] 테스트 중 실제 텔레그램 메시지 발송 원천 차단
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", False)
    # [추가] send_telegram_message()는 ENABLE_TELEGRAM을 보지 않고 토큰/챗ID 유무만 확인하므로,
    #        운영 .htsrc의 토큰이 환경변수로 설정돼 있으면 테스트가 실제 텔레그램으로 전송하며
    #        네트워크 타임아웃 hang을 유발한다. 토큰을 비워 early-return 시킨다.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "", raising=False)

@pytest.fixture(autouse=True)
def cleanup_global_db_connection():
    """
    각 테스트 실행 후 전역 DBManager가 생성한 '모든 스레드'의 연결을 닫습니다.
    백그라운드 워커 스레드가 만든 thread-local 연결까지 정리하여
    ResourceWarning: unclosed database 를 방지합니다.
    """
    yield

    # 테스트 종료 후 정리 (전체 스레드 연결 일괄 종료)
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    try:
        real_db.close_all_connections()
    except Exception:
        pass

@pytest.fixture(autouse=True)
def reset_all_singletons():
    """
    [전역 설정] 
    모든 테스트 실행 전후로 싱글톤 객체의 상태를 강제 초기화하여 
    테스트 파일 간의 상태 누수(State Leak) 및 간섭을 원천 차단합니다.
    """
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    TelegramCommander._instance = None
    # [격리] 시장 국면 TTL 캐시 초기화 (테스트별 모킹 데이터가 캐시로 새지 않도록)
    analysis._MARKET_REGIME_CACHE.clear()
    # [격리 2026-08-19] tvDatafeed 회로차단. 한 테스트가 '전 재시도 실패'를 만들면 그 신호가
    #  모듈 전역에 남아, 다음 테스트의 재시도 횟수가 4회 → 1회로 조용히 줄어든다
    #  (실제로 test_fred_resilience의 백오프 검증이 그렇게 깨졌다).
    analysis.reset_tvdatafeed_circuit()
    # [격리 2026-08-10] 지수 변동성 배율. trader 루프를 태우는 테스트가 합성 지수
    #  데이터로 이 전역을 갱신하고 되돌리지 않으면, 이후 실행되는 **다른 파일**의
    #  ATR 손절 캡이 조용히 달라진다(effective_atr_stop_cap이 이 값을 본다).
    #  실제로 test_failclosed_chaos 가 0.7296을 남겨 test_settings_guardrails 의
    #  SSOT 검증이 xdist 워커 배치에 따라 간헐 실패했다. 전역이므로 여기서 막는다.
    _atr_engine.set_vol_regime_ratio(1.0)

    yield

    # [격리 2026-08-19] 인스턴스를 None으로 만들어도 **이미 떠 있는 스레드는 안 죽는다** —
    #  그 스레드는 옛 self를 참조로 붙들고 있다. 체결 감시가 매도 체결마다 띄우는
    #  제한 해제 확인 스레드(RestrictionCheck-*, 3초 × 5회)가 대표적이고, 테스트가 끝난
    #  뒤 patch가 원복된 구간에서 깨어나 다음 테스트의 mock을 건드렸다(실측: 전체 스위트
    #  3회 중 2회 간헐 실패, 매번 다른 테스트). 참조를 끊기 전에 종료 신호부터 준다.
    _monitor = ConclusionMonitor._instance
    if _monitor is not None:
        try:
            _monitor.is_running = False
            _monitor.shutdown.set()
        except Exception:
            pass

    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    TelegramCommander._instance = None
    analysis._MARKET_REGIME_CACHE.clear()
    analysis.reset_tvdatafeed_circuit()
    _atr_engine.set_vol_regime_ratio(1.0)

def create_mock_df(trend='up', periods=100, start_price=10000):
    """가상의 주가 데이터프레임 생성 헬퍼 함수"""
    dates = pd.date_range(start="2023-01-01", periods=periods)
    
    if trend == 'up':
        # 우상향: 10000 -> 15000
        close = np.linspace(start_price, start_price * 1.5, periods)
    elif trend == 'down':
        # 우하향: 10000 -> 5000
        close = np.linspace(start_price, start_price * 0.5, periods)
    else:
        # 횡보: 10000원 부근 진동
        close = np.linspace(start_price, start_price, periods)

    # 노이즈 추가
    noise = np.random.normal(0, start_price * 0.005, periods)
    close = close + noise
    
    df = pd.DataFrame({
        'date': dates,
        'close': close,
        'open': close * 0.99,
        'high': close * 1.02,
        'low': close * 0.98,
        'volume': np.random.randint(1000, 10000, periods)
    })
    return df

@pytest.fixture
def sample_uptrend_df():
    """상승장 데이터 (100일)"""
    return create_mock_df(trend='up')

@pytest.fixture
def sample_downtrend_df():
    """하락장 데이터 (100일)"""
    return create_mock_df(trend='down')

@pytest.fixture
def sample_sideways_df():
    """횡보장 데이터 (100일)"""
    return create_mock_df(trend='sideways')