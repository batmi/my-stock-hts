"""KIS·토스·외부 시세 API 계층 (구 api.py 7,596줄의 분해 결과).

[왜 나눴나]
한 파일에 휴장일 달력·세션 판정·yfinance·차트 디스크 캐시·TradingView·KIS REST·토스·
주문·잔고가 전부 섞여 있었다. 어디를 고쳐도 파일 전체를 다시 읽어야 했고, 테스트는
필요 없는 계층까지 통째로 끌고 왔다. modules/auto_trade 를 패키지로 나눈 것과 같은 방식이다.

[구조]
    instruments      NXT 취급 여부·국내 ETF/ETN 판별
    market_calendar  휴장일(국내·미국·거래소 MIC)과 해외 시각
    sessions         세션 판정(정규·프리·애프터·데이마켓)과 화면 표기
    yf_quotes        yfinance·TradingView 시세 + 마이크로 캐시
    chart_cache      차트 메모리·디스크 캐시, 관심종목 예열
    http             TPS 게이트·재시도·커넥션 풀(ThrottledSession)
    auth             토큰 발급·갱신과 공용 호출 진입점(call_api)
    charts           일봉·주봉·분봉 조회
    indices          지수·K200 선물
    quotes/nxt       NXT 시세·멀티 시세 배치
    quotes/price     현재가·호가·수급·해외 상세
    toss             토스증권 계층 + 국내 일봉 폴백
    account          잔고·체결내역·미체결
    orders           주문 접수·정정·취소·예수금

[호출 규약 — 중요]
바깥에서는 예전 그대로 `import api` 하고 `api.get_current_price(...)` 로 쓴다. 이 파일이
서브모듈의 이름을 전부 패키지 네임스페이스로 다시 올려 두기 때문이다.

서브모듈끼리 부를 때는 상대 모듈을 직접 import 하지 말고 `_api().이름()` 을 쓴다. 분해
전에는 모두 한 모듈이라 테스트의 patch.object(api, 'X') 가 모든 호출부에 걸렸는데,
서브모듈이 상대를 직접 import 하면 그 patch 가 닿지 않기 때문이다
(modules/auto_trade 의 _pkg() 와 같은 규약).

[테스트에서 상태·모듈 별칭을 건드릴 때]
함수·클래스는 예전처럼 patch.object(api, ...) 로 잡힌다(호출부가 _api() 를 지나므로).
그러나 **모듈 레벨 변수와 import 별칭**은 그 값을 실제로 들고 있는 서브모듈을 지정해야 한다:
    api.instruments._NXT_MASTER_LOADED = False        (O)
    api._NXT_MASTER_LOADED = False                    (X — 패키지 쪽 사본만 바뀐다)
    patch('api.toss.datetime')                        (O)
    patch('api.datetime')                             (X — 서브모듈은 자기 datetime 을 본다)
"""
import logging
import sys as _sys
import types as _types

import yfinance as yf   # noqa: F401  (아래 경고 억제가 yfinance import '뒤'에 와야 한다)

import config
# yfinance는 import 시 'default::DeprecationWarning:^yfinance' 필터를 등록해 자기 경고를 강제 노출한다.
# warnings 필터는 나중 등록분이 앞서므로, yfinance import '뒤'에 다시 걸어야 억제가 유효하다.
config.silence_yfinance_numpy_warning()

# [추가] yfinance 자체 ERROR 로그('Failed download' 등)는 락 직렬화로 빈도가 급감하며,
#  남는 일시적 실패는 우리 쪽에서 빈 DataFrame 감지 후 재시도/폴백으로 처리하므로
#  라이브러리 로그 레벨을 CRITICAL로 올려 노이즈를 억제한다.
try:
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
except Exception:
    pass

logger = logging.getLogger("api")

# 서브모듈 이름(패키지 네임스페이스로 끌어올릴 순서 = import 순서)
_LAYER_NAMES = ('instruments', 'market_calendar', 'sessions', 'yf_quotes', 'chart_cache',
                'http', 'auth', 'charts', 'indices', 'quotes.nxt', 'quotes.price',
                'toss', 'account', 'orders')


#  이름 색인. 서브모듈을 다 읽은 뒤 아래에서 채운다 — 여기서 미리 비워 두는 이유는
#  __getattr__ 가 **import 도중에도** 불리기 때문이다(auth 의 _token_session 생성이
#  http 의 GatedRetry 를 쓴다). 그 시점에는 색인이 비어 있고 sys.modules 경로로 찾는다.
_NAME_INDEX = {}


def __getattr__(name):
    """패키지에 없는 이름은 서브모듈에서 찾는다 — 이 패키지의 유일한 이름 해석 경로다.

    **import 문들보다 먼저** 정의해 둔다. 서브모듈이 import 시점에 다른 계층의 이름을
    부르는 경우가 있는데(위 _NAME_INDEX 주석 참조), 그때는 아직 색인이 없으므로
    sys.modules 를 직접 훑는 두 번째 경로로만 찾을 수 있다.
    """
    layers = _NAME_INDEX.get(name)
    if layers:
        return getattr(layers[0], name)
    import sys
    for _n in _LAYER_NAMES:
        _m = sys.modules.get(f"{__name__}.{_n}")
        if _m is not None and hasattr(_m, name):
            return getattr(_m, name)
    raise AttributeError(f"module 'api' has no attribute '{name}'")


from . import instruments          # noqa: E402
from . import market_calendar      # noqa: E402
from . import sessions             # noqa: E402
from . import yf_quotes            # noqa: E402
from . import chart_cache          # noqa: E402
from . import http                 # noqa: E402
from . import auth                 # noqa: E402
from . import charts               # noqa: E402
from . import indices              # noqa: E402
from .quotes import nxt as quotes_nxt        # noqa: E402
from .quotes import price as quotes_price    # noqa: E402
from . import toss                 # noqa: E402
from . import account              # noqa: E402
from . import orders               # noqa: E402

# [리팩토링] 텔레그램 발신 계층은 modules/telegram_notify.py 로 분리되었다.
# 기존 호출부(api.send_telegram_message 등) 호환을 위한 재수출(re-export).
from modules.telegram_notify import (_get_telegram_footer, send_telegram_message,   # noqa: E402
                                     send_telegram_photo)

# [리팩토링] OpenDART 연동 계층은 modules/dart_api.py 로 분리되었다. (재수출)
from modules.dart_api import (DART_BASE_URL, call_dart, get_dart_corp_map,           # noqa: E402
                              get_dart_dividend, get_dart_acc_month, get_dart_disclosures,
                              get_dart_insider_trades, get_dart_major_holdings,
                              get_dart_financials, get_dart_paid_increase_detail,
                              get_dart_bond_issue_detail, get_dart_document_text,
                              get_dart_earnings_brief,
                              get_dart_treasury_decisions, get_dart_free_increase_detail,
                              get_dart_capital_reduction_detail, get_dart_financial_index,
                              get_dart_dividend_decision, get_dart_shares_outstanding,
                              DART_INDEX_CLASSES)

# ---------------------------------------------------------------------------
# 이름 해석과 patch 전파
# ---------------------------------------------------------------------------
# [원칙] 값의 진실은 언제나 **서브모듈**에 있다. 패키지는 이름을 찾아 주는 창구일 뿐,
#  사본을 들고 있지 않는다. 사본을 만들면 서브모듈이 global 로 다시 묶은 값
#  (캐시 플래그·비활성 타임스탬프 같은 것)과 곧바로 어긋난다.
#
# [읽기] api.X 는 아래 __getattr__ 이 서브모듈에서 찾아 준다. 이름→모듈 색인을 한 번
#  만들어 두므로 조회 비용은 딕셔너리 한 번이다.
#
# [쓰기] api.X = ... (테스트의 patch 가 하는 일) 는 그 이름을 가진 **모든 서브모듈에
#  그대로 전파**한다. 분해 전에는 전부 한 모듈이라 patch 하나가 모든 호출부에 걸렸는데,
#  전파가 없으면 같은 파일 안에서 부르는 쪽만 patch 를 놓치기 때문이다. 전파 덕분에
#      patch.object(api, 'call_api')      → api.auth.call_api 까지 바뀐다
#      patch.object(api, 'datetime', ...)  → 시각을 쓰는 모든 계층이 함께 얼어붙는다
#  즉 이 분해는 바깥에서 보기에 아무것도 바뀌지 않는다.
_LAYERS = (instruments, market_calendar, sessions, yf_quotes, chart_cache, http, auth,
           charts, indices, quotes_nxt, quotes_price, toss, account, orders)

for _layer in _LAYERS:
    for _name in vars(_layer):
        if _name.startswith('__') or _name == '_api':
            continue
        _NAME_INDEX.setdefault(_name, []).append(_layer)
del _layer, _name


def __dir__():
    return sorted(set(globals()) | set(_NAME_INDEX))


class _ApiPackage(_types.ModuleType):
    """api.X = ... 를 그 이름을 가진 서브모듈 전부에 전파하는 모듈 타입."""

    def __setattr__(self, name, value):
        layers = _NAME_INDEX.get(name) if not name.startswith('__') else None
        if layers:
            for layer in layers:
                setattr(layer, name, value)
            #  패키지 쪽에는 사본을 남기지 않는다 — 남기면 이후 읽기가 서브모듈이 아니라
            #  그 사본을 보게 되어, 서브모듈이 값을 다시 묶었을 때 조용히 어긋난다.
            self.__dict__.pop(name, None)
            return
        self.__dict__[name] = value

    def __delattr__(self, name):
        #  [주의 · 테스트] 삭제도 전파된다. `mock.patch('api.X', create=True)` 는 종료 시
        #   restore 가 아니라 **delattr** 을 부르므로, X 가 실제로 어느 서브모듈에 있으면
        #   그 세션 내내 사라진다(뒤 테스트가 AttributeError 로 깨진다 — 실측).
        #   이미 있는 이름에는 create=True 를 쓰지 말 것.
        layers = _NAME_INDEX.get(name)
        if layers:
            for layer in layers:
                try:
                    delattr(layer, name)
                except AttributeError:
                    pass
            self.__dict__.pop(name, None)
            return
        del self.__dict__[name]


_sys.modules[__name__].__class__ = _ApiPackage
