# modules/auto_trade/__init__.py
"""시스템 트레이딩 패키지.

기존 단일 파일 modules/auto_trade.py(6,800줄)를 책임별 서브모듈로 분해:
  - common.py     : 공용 헬퍼 (제한종목/일일자산/ODNO/장시간/OrderStatus/룰 가중치)
  - engine.py     : DefaultStrategy · OrderManager · RiskManager
  - conclusion.py : ConclusionMonitor (체결 감시/확정)
  - trader.py     : AutoTrader (메인 루프)
  - menu.py       : 터미널 메뉴 UI

기존 인터페이스(modules.auto_trade.X) 완전 호환:
아래에서 서브모듈의 공개/비공개 이름을 패키지 네임스페이스로 모두 승격(재수출)하므로,
호출 코드와 테스트의 patch('modules.auto_trade.X') 는 분해 전과 동일하게 동작한다.
(서브모듈 내부의 상호 호출도 _pkg() 접근자로 패키지 네임스페이스를 경유한다)
"""
import sys as _sys

from modules.auto_trade import common as _m_common
from modules.auto_trade import engine as _m_engine
from modules.auto_trade import conclusion as _m_conclusion
from modules.auto_trade import trader as _m_trader
from modules.auto_trade import menu as _m_menu

_self = _sys.modules[__name__]
for _m in (_m_common, _m_engine, _m_conclusion, _m_trader, _m_menu):
    for _n, _v in vars(_m).items():
        if _n.startswith('__') or _n in ('_pkg', '_sys'):
            continue
        if not hasattr(_self, _n):
            setattr(_self, _n, _v)
del _sys, _self, _m, _n, _v
