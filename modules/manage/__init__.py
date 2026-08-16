# modules/manage/__init__.py
"""관심 종목 관리 패키지.

기존 단일 파일들을 관심종목·펀더멘털 단위로 묶어 분해:
  - watchlist.py       : [7] 관심 종목 관리 (기존 modules/manage.py)
  - discover.py        : [7-4] 관심 종목 탐색 (규칙 기반 후보 발굴)
  - events.py          : 배당·실적 캘린더 (DART + yfinance)
  - econ_events.py     : 주요 경제 이벤트 일정 (FRED + 연준 캘린더)
  - disclosure.py      : [6-6] 공시 모니터링·실적 추적 + 공시 텔레그램 알림
  - insider.py         : [6-7] 수급·물량 신호 (자기주식 결정·메자닌 오버행)
  - financials.py      : [6-8] 재무 스냅샷 (DART 주요계정)

기존 인터페이스(modules.manage.X) 완전 호환:
watchlist의 이름들을 패키지 네임스페이스로 승격(재수출)하므로, 호출 코드와
테스트의 patch('modules.manage.X') 는 분해 전과 동일하게 동작한다.
"""
import sys as _sys

from modules.manage import discover, events, disclosure  # noqa: F401 (하위 메뉴 모듈)
from modules.manage import watchlist as _m_watchlist

_self = _sys.modules[__name__]
for _n, _v in vars(_m_watchlist).items():
    if _n.startswith('__') or _n in ('_pkg', '_sys'):
        continue
    if not hasattr(_self, _n):
        setattr(_self, _n, _v)
del _sys, _self, _n, _v
