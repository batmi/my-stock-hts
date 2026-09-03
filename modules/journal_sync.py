"""매매일지 웹서버 연동 — Universal Trading History API v2 클라이언트.

Outbox 패턴
-----------
체결 처리 경로에서는 **절대 네트워크를 타지 않는다.**
`db_manager.insert_trade()` 가 거래 기록과 같은 트랜잭션으로 `journal_outbox` 에
적재하고, 이 모듈의 백그라운드 워커가 배치로 전송한다.

  - 체결 확인 루프가 네트워크 지연(수 초)에 묶이지 않는다
  - 라즈베리파이가 단절·재부팅돼도 큐가 DB에 남아 자동으로 복구된다
  - 서버가 brokerExecutionId 로 멱등 처리하므로 재전송이 언제나 안전하다
    (다만 **갱신되지는 않는다** — 이미 있는 건은 duplicate 로 건너뛴다.
     뒤늦은 정정은 PATCH 로 보낸다. `_send_corrections` 참고)

2단 방어
--------
1단(큐)이 막지 못하는 구멍이 하나 있다. `enqueue()` 는 `is_enabled()` 뒤에 있어서
**연동이 꺼져 있던 동안의 체결은 큐에 들어가지도 않는다.** 메뉴 토글 OFF, 환경변수
누락으로 재시작된 구간이 여기 해당하고, 이건 재시도로는 영영 복구되지 않는다.

  1단 큐   : 서버가 죽어 있는 동안의 체결 — 큐에 남았다가 복구 시 자동 전송
  2단 백필 : 연동이 꺼져 있던 동안의 체결 — `backfill_once()` 가 로컬 `trades` 를
             서버의 마지막 동기화 지점과 대조해 큐에 없는 건을 주워 담는다

전송을 포기하는 기준
--------------------
서버가 **이 건을 명시적으로 거절한** 횟수(`reject_count`)만 세어 `_MAX_REJECTS` 에
닿으면 dead-letter 로 뺀다. 통신 실패는 세지 않는다 — 세면 웹서버가 오래 죽어 있을 때
멀쩡한 대기열이 통째로 폐기된다. 반대로 아예 빼지 않으면 서버가 영구 거절하는 행
하나가 배치 앞자리를 잡고 뒤의 정상 건까지 막는다.

멱등키
------
`{env}:{계좌}:{체결일}:{주문번호}:{상태}` 형태로 만든다. 증권사 주문번호(odno)는
영업일마다 재사용되므로 주문번호만 쓰면 다른 날의 다른 체결이 중복으로 오인되어
서버에서 조용히 버려진다. 계좌·일자를 반드시 포함해야 한다.

매매 분류(tradeClass)
---------------------
HTS 는 자기 계좌에서 일어난 체결을 **전부** 보고한다 — 토스 앱이나 증권사 HTS 에서
사람이 직접 낸 주문까지 포함해서다. 예전에는 그 전부를 '시스템'으로 못 박아 보내
자동매매 성과와 수동 매매가 한 덩어리가 됐다. 지금은 `isSystem` 으로 구분한다:
AutoTrader 가 낸 주문만 True, 예약·수동·외부는 False, 출처 불명은 필드를 싣지 않아
서버가 같은 종목의 직전 분류를 물려받게 한다. (`_is_system` 참고)

HTS 를 여러 대 돌릴 때
----------------------
서버의 하트비트·재동기화 명령 스코프는 API 키가 아니라 **사용자**다(키는 인증 직후
username 으로 바뀐다). 그래서 키를 따로 발급해도 인스턴스는 갈라지지 않는다.
`botId` 가 그 구분자다. 겹치면
  - 상태가 한 칸에 덮여 쓰여 실전봇이 죽어도 가상봇 Ping 이 '정상'으로 유지되고
  - 웹에서 누른 재동기화를 엉뚱한 봇이 채가서 '완료'로 뜬다(조용한 실패)

**모드는 `--mode` CLI 인자라 환경변수로 구분되지 않는다.** 그래서 botId 는
`JOURNAL_BOT_ID`(없으면 `JOURNAL_SOURCE`)에 런타임 모드·계좌를 붙여 만든다
(`_bot_id`). 같은 기기에서 ~/.htsrc 하나로 세 모드를 돌려도 충돌하지 않는다.

`JOURNAL_SOURCE` 는 **설치(기기)마다** 달라야 한다 — 같으면 백필 기준점(last-sync)이
남의 체결까지 포함해 앞당겨져, 뒤처진 인스턴스의 누락 구간이 스캔에서 통째로 빠진다.

설정 (~/.htsrc 에 export 후 재시작)
-----------------------------------
  export JOURNAL_API_URL="https://your-host"     # 필수 (미설정 시 연동 전체 비활성)
  export JOURNAL_API_KEY="skm_..."               # 필수 (웹 설정에서 발급)
  export JOURNAL_SOURCE="my-stock-hts"           # 선택 (last-sync 스코프 기준)
  export JOURNAL_BOT_ID=""                       # 선택 (기본 JOURNAL_SOURCE)
  export JOURNAL_BOT_LABEL=""                    # 선택 (웹 표시명, 기본 자동생성)
"""

import json
import logging
import os
import sys
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 전송 대상 주문 상태. '접수'는 아직 체결이 아니고, 취소는 체결 기록이 아니므로 보내지 않는다.
#  '체결(추정)'은 잔고 대조로 추정한 건이라 confidence=ESTIMATED 로 표시해 전송한다.
_SYNCABLE_STATUS = {
    '체결': 'CONFIRMED',
    '체결(추정)': 'ESTIMATED',
}

_FLUSH_INTERVAL_SEC = 30      # 대기열 전송 주기
_BATCH_SIZE = 100             # 한 번에 보낼 최대 건수 (라즈베리파이 메모리 여유 고려)
_HTTP_TIMEOUT = 8             # 초 — 짧게 잡아 워커가 오래 물리지 않게 한다
# 서버가 '이 건'을 명시적으로 거절한 횟수가 이 값에 닿으면 대기열에서 뺀다(dead-letter).
#  통신 실패(서버 다운·타임아웃)는 여기에 세지 않는다 — 세면 웹서버가 반나절만 죽어 있어도
#  대기열 전체가 폐기된다. 세는 것은 서버가 응답으로 거절 사유를 준 경우뿐이라,
#  여기까지 온 행은 재시도해도 결과가 달라지지 않는 페이로드다.
_MAX_REJECTS = 5

# 전송 완료 행 보존 기간. 라즈베리파이 SD카드에 payload JSON 이 무한 누적되는 걸 막는다.
# 백필 스캔 범위(_BACKFILL_LOOKBACK_DAYS)보다 반드시 길어야 한다 — 짧으면 이미 보낸 건이
# 큐에서 사라진 뒤 백필이 다시 주워 담아 매번 재전송한다.
_RETENTION_DAYS = 90
_PURGE_INTERVAL_SEC = 24 * 3600

# 백필(누락 회수) — 큐에 아예 들어가지 못한 체결을 로컬 trades 에서 찾아 회수한다.
#  연동 토글이 꺼져 있었거나 환경변수가 빠진 채 돌던 구간의 체결이 여기에 해당한다.
#  그 구간엔 enqueue() 가 통째로 건너뛰므로 큐 재시도로는 영원히 복구되지 않는다.
_BACKFILL_INTERVAL_SEC = 6 * 3600
_BACKFILL_STARTUP_DELAY_SEC = 60   # 기동 직후는 로그인·유니버스 적재가 끝나길 기다린다
_BACKFILL_LOOKBACK_DAYS = 30       # 서버에 기록이 하나도 없을 때 거슬러 올라갈 기본 범위
_BACKFILL_OVERLAP_MIN = 10         # 마지막 동기화 지점 앞뒤 경계에서 빠지는 건이 없도록
_BACKFILL_MAX_ROWS = 500           # 한 번에 스캔할 로컬 행 상한 (라파 메모리 보호)

# 봇 상태 Ping 주기. 웹 대시보드는 3회 연속 누락(+여유)되면 '통신단절'로 표시하므로,
# 이 값을 늘리면 장애 감지가 그만큼 늦어진다. 서버 상수와 짝을 이룬다.
_PING_INTERVAL_SEC = 10
_PING_TIMEOUT = 4             # 초 — 10초마다 도는 하트비트가 통신 지연에 오래 물리면 안 된다
_TICK_INTERVAL_SEC = _PING_INTERVAL_SEC   # 워커 순회 주기(Ping 주기에 맞춘다)
# 10초 간격이라 실패 로그를 매번 남기면 로그가 이것만으로 찬다.
# 첫 실패(= 감지 시점)와 이후 5분마다 한 번씩만 WARNING 으로 남긴다.
_PING_WARN_EVERY = 30
_SHUTDOWN_PING_TIMEOUT = 3    # 초 — 종료 통지가 프로그램 종료를 붙잡지 않도록 짧게


# ══════════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════════

def _cfg(name, default=''):
    return getattr(config, name, default) or default


def _egress_blocked():
    """테스트 세션에서는 웹서버로 나가는 모든 요청을 원천 차단한다. (차단이면 사유 문자열)

    [2026-09-03 사고] 부분체결 회계 테스트가 만든 가짜 삼성전자 매도 2건이 실제 웹저널에
     기록됐다. 경로는 이랬다 — 테스트가 `db_manager.db`(운영 DB 핸들)로 insert_trade 를
     부르자 같은 트랜잭션에서 journal_outbox 에 `isSimulated=false` 로 적재됐고, 같은
     맥북에서 돌던 **실전(mode 2) 인스턴스의 워커**가 몇 초 뒤 그것을 집어 전송했다.
     테스트 프로세스 안에 워커가 없어도 새어 나간다는 뜻이다.

    그래서 방어를 세 겹으로 둔다. ① 테스트는 운영 DB 를 만지지 못한다(conftest 가 경로를
    갈아끼운다) ② 테스트 세션의 HTTP 는 호스트 단위로 막힌다(conftest) ③ 그 둘을 다 뚫어도
    이 함수가 마지막으로 막는다. 실매매가 아닌 기록이 웹저널에 한 건이라도 섞이면
    승률·손익 통계가 곧바로 틀어지므로, 의심스러우면 보내지 않는 쪽으로 닫는다.

    검증 목적으로 진짜 전송이 필요한 도구(tools/journal_sync_e2e.py)는 pytest 밖에서
    돌므로 여기 걸리지 않는다.
    """
    if os.environ.get('PYTEST_CURRENT_TEST') or 'pytest' in sys.modules:
        return "테스트 세션(pytest)에서는 매매일지 웹서버 전송이 차단됩니다"
    return ''


def _has_credentials():
    return bool(_cfg('JOURNAL_API_URL') and _cfg('JOURNAL_API_KEY'))


def is_enabled():
    """메뉴 0 토글이 켜져 있고, URL·API 키가 모두 설정돼야 연동이 동작한다.

    설정(JOURNAL_SYNC_USE)과 자격증명(환경변수)을 분리한 이유:
      - 자격증명은 소스·설정파일에 남기면 안 되므로 환경변수로만 받는다
      - 사용 여부는 재시작 없이 껐다 켤 수 있어야 하므로 dynamic_config 에 둔다

    가상투자(mode 1)도 같은 스위치를 쓴다. 설정은 모드별 프로필로 갈리므로
    (dynamic_config.paper.json), 가상에서 켜고 끈 것이 실전으로 새지 않는다.

    **가상투자는 웹저널 계정 자체를 따로 쓴다** — 실전과 다른 기기에서 돌고
    `JOURNAL_API_KEY` 가 그 기기의 `~/.htsrc` 에 따로 있다. 그래서 두 기록이
    서버에서 섞일 일이 없다. 전송되는 건에 `isSimulated=true` 를 싣는 것은 그
    분리와 별개로, 계정 안에서 한 번 더 표시해 두기 위한 것이다.
    """
    if not getattr(config.settings, 'JOURNAL_SYNC_USE', False):
        return False
    return _has_credentials()


def _base_url():
    return _cfg('JOURNAL_API_URL').rstrip('/')


def _source():
    return _cfg('JOURNAL_SOURCE', 'my-stock-hts')


def _is_paper():
    """가상투자(mode 1) 세션인가. 실거래와 섞이면 안 되는 모든 분기의 기준."""
    try:
        return bool(getattr(config.session, 'is_paper', False))
    except Exception:      # noqa: BLE001
        return False


def _bot_env():
    """운용 환경 토큰 (paper/toss/real). 실패하면 빈 문자열.

    가상투자를 별도 토큰으로 가르지 않으면 botId 가 실전과 같은 `...:real:` 이 되어
    웹 목록에서 가상봇이 실전봇 칸을 덮어쓴다.
    """
    try:
        if _is_paper():
            return 'paper'
        if getattr(config.session, 'is_toss', False):
            return 'toss'
        return 'real'
    except Exception:      # noqa: BLE001
        return ''


def _bot_id():
    """봇 인스턴스 식별자. HTS 를 여러 대 돌릴 때 서로를 구분하는 유일한 값이다.

    서버의 하트비트·명령 스코프는 API 키가 아니라 **사용자**라(키는 인증 직후
    username 으로 바뀐다), 키를 따로 발급해도 인스턴스는 갈라지지 않는다.

    **환경변수만으로는 부족하다.** 투자 모드는 `--mode` CLI 인자로 정해지므로
    (main.py), 같은 기기에서 `~/.htsrc` 하나를 공유한 채 가상·실전·토스를 함께
    돌리면 JOURNAL_BOT_ID·JOURNAL_SOURCE 가 셋 다 같은 값이 된다. 그러면 세
    인스턴스가 서버의 같은 칸을 10초마다 덮어써서, 웹 목록에 자기 자리가 없는
    봇이 생긴다(실측 2026-08-03: 3대를 돌렸는데 2대만 보임).

    그래서 환경변수는 '설치 식별자'로만 쓰고, 여기에 런타임 정체성(모드·계좌)을
    덧붙여 **구성상 충돌이 불가능하게** 만든다. 모드·계좌는 재시작해도 변하지
    않으므로 식별자도 안정적이다(매번 바뀌면 유령 봇이 쌓인다).
    """
    base = _cfg('JOURNAL_BOT_ID') or _source()
    try:
        account = (getattr(config.session, 'cano', '') or '').replace('-', '').strip()
    except Exception:      # noqa: BLE001
        account = ''
    suffix = ':'.join(part for part in (_bot_env(), account) if part)
    if not suffix:
        return base[:64]
    # 길이 상한은 base 쪽에서 깎는다 — 뒤를 자르면 구분자가 날아가 충돌이 되살아난다.
    return f'{base[:64 - len(suffix) - 1]}:{suffix}'


def _bot_label():
    """웹 화면에 뜰 표시명. 미설정 시 운용 계좌·환경으로 만든다.

    botId 는 기계용 식별자라 화면에 그대로 띄우면 어느 계좌인지 알 수 없다.
    목록에서 '어느 봇이 죽었나'를 판단하려면 사람이 읽는 이름이 필요하다.

    한투 실전은 거래 계좌와 시스템 트레이딩 계좌가 다를 수 있어 둘 다 적는다 —
    거래 계좌만 띄우면 정작 자동매매가 도는 계좌가 화면에서 사라진다.
    """
    label = _cfg('JOURNAL_BOT_LABEL')
    if label:
        return label[:60]
    try:
        env = {'toss': '토스', 'paper': '가상', 'real': '실전'}.get(_bot_env(), '')
        account = _account_text(config.session.cano, config.session.acnt_prdt_cd)
        auto = _account_text(getattr(config.session, 'auto_cano', ''),
                             getattr(config.session, 'auto_acnt_prdt_cd', ''))
        name = f'{env} {account}'.strip()
        if auto and auto != account:
            name = f'{name}·자동 {auto}'
        return (name or _bot_id())[:60]
    except Exception:      # noqa: BLE001 - 표시명은 부가정보라 실패해도 Ping 을 막지 않는다
        return _bot_id()[:60]


def _account_text(cano, product_code):
    """계좌 표기 `12345678-01`. 상품코드는 별도 필드라 붙이지 않으면 화면에서 빠진다.

    (토스는 상품코드 개념이 없어 cano 에 전체 번호가 들어 있다 — 그대로 둔다.)
    """
    cano = (cano or '').strip()
    product_code = (product_code or '').strip()
    if not cano:
        return ''
    return f'{cano}-{product_code}' if product_code else cano


# ══════════════════════════════════════════════════════════════════════
# 페이로드 변환
# ══════════════════════════════════════════════════════════════════════

def _is_overseas(code):
    """종목코드 형태로 해외 여부를 판단한다 (국내는 6자리 숫자)."""
    code = (code or '').strip()
    return not (len(code) == 6 and code.isdigit())


def _exchange_for(code, overseas):
    """거래소 코드. 서버가 이 값으로 **현지 거래일**을 계산하므로 해외는 특히 중요하다.

    미국 애프터마켓 체결(한국시간 새벽)을 거래소 정보 없이 보내면 서버가 KST 날짜로
    귀속시켜 거래일이 하루 밀린다. 매매 유니버스(stock.json)에 등록된 종목은
    거래소가 함께 들어 있으므로 그대로 쓴다.
    """
    if not overseas:
        return 'KRX'
    try:
        stock_data = getattr(config.session, 'stock_data', None) or {}
        for key in ('stocks_us', 'etfs_us'):
            for item in stock_data.get(key, []):
                if (item.get('code') or '').upper() == code.upper():
                    return item.get('exchange') or ''
    except Exception:
        pass
    # 유니버스 밖(수동·외부 주문)이면 추측하지 않고 비워 둔다. 서버는 요청에 실린
    # 오프셋 기준 날짜로 폴백하므로 최소한 잘못된 거래소명이 기록되진 않는다.
    return ''


def _order_origin(type_str):
    """`매수(AUTO)` 같은 로컬 타입 문자열에서 주문 출처를 뽑아낸다."""
    t = type_str or ''
    if '(AUTO)' in t:
        return 'AUTO'
    if '(예약)' in t:
        return 'RESERVED'
    if '(외부)' in t:
        return 'EXTERNAL'
    if '(수동)' in t:
        return 'MANUAL'
    return ''


def _is_system(type_str):
    """이 체결이 **시스템 트레이딩이 낸 주문**인가. 모르면 None.

    HTS 는 자기 계좌에서 일어난 체결을 전부 보고한다 — 토스 앱이나 증권사 HTS 에서
    사람이 직접 낸 주문까지 포함해서다. 그것들을 자동매매와 한 덩어리로 묶으면
    '시스템이 얼마나 벌었나'라는 질문에 답할 수 없게 된다.

      AUTO     — AutoTrader 가 전략 판단으로 낸 주문. 이것만 시스템이다.
      RESERVED — 예약주문 발동. 실행은 무인이지만 조건을 건 주체가 사람이다.
      MANUAL   — 우리 HTS 메뉴에서 사람이 낸 주문.
      EXTERNAL — 앱(MTS)/증권사 HTS 등 외부에서 낸 주문을 잔고 대조로 감지한 것.
      (꼬리표 없음) — 출처를 알 수 없다. **단정하지 않고 None 을 돌려준다.**
        서버는 이 경우 같은 종목의 직전 분류를 물려받는다. 여기서 False 로 눕히면
        '사람이 냈다'고 확정한 셈이 되어 그 폴백이 동작하지 않는다.
    """
    origin = _order_origin(type_str)
    if not origin:
        return None
    return origin == 'AUTO'


def _side(type_str):
    t = type_str or ''
    if '매수' in t or 'buy' in t.lower():
        return 'BUY'
    if '매도' in t or 'sell' in t.lower():
        return 'SELL'
    return ''


def _executed_at(time_str, overseas):
    """로컬 체결시각(KST 문자열)을 오프셋 포함 RFC3339 로 바꾼다.

    로컬 DB의 `time` 은 한국 시간이므로 항상 +09:00 을 붙인다. 서버는 거래소
    코드를 함께 보고 현지 거래일을 계산하므로, 해외 체결도 이대로 보내면 된다.
    """
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            dt = datetime.strptime(str(time_str), fmt).replace(tzinfo=KST)
            return dt.strftime('%Y-%m-%dT%H:%M:%S%z')
        except (TypeError, ValueError):
            continue
    return datetime.now(KST).strftime('%Y-%m-%dT%H:%M:%S%z')


def _exec_id(trade):
    """멱등키 — {env}:{계좌}:{체결일}:{주문번호}:{상태}

    주문번호(odno)는 영업일마다 재사용되므로 계좌·일자 없이는 다른 날의 다른
    체결이 중복으로 오인되어 서버에서 조용히 버려진다.
    '체결(추정)'과 '체결'을 상태까지 키에 넣어 구분하면, 추정 기록이 먼저 올라간 뒤
    확정 기록이 별건으로 들어오는 대신 각각 남으므로 나중에 정정할 수 있다.
    """
    # 가상 체결은 계좌번호가 'PAPER' 라 실거래와 겹치지 않지만, 키만 봐도
    # 어느 쪽인지 알 수 있어야 서버에서 잘못 들어간 건을 골라낼 수 있다.
    env = 'SIM' if trade.get('is_sim') else ('PAPER' if _is_paper() else 'REAL')
    account = (trade.get('account') or '').replace('-', '')
    day = str(trade.get('time') or '')[:10].replace('-', '')
    odno = (trade.get('odno') or '').strip() or 'NOODNO'
    status = 'E' if trade.get('order_status') == '체결(추정)' else 'F'
    return f'{env}:{account}:{day}:{odno}:{status}'


def _format_pnl(trade, currency):
    """매도 실현손익을 사람이 읽는 한 줄로. 값이 없으면 빈 문자열."""
    def _num(key):
        try:
            return float(trade.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    amount, rate = _num('profit_amt'), _num('profit_rate')
    if not amount and not rate:
        return ''
    if currency == 'KRW':
        amount_text = f'{amount:+,.0f}원'
    else:
        amount_text = f'{amount:+,.2f} {currency}'
    return f'손익: {amount_text} ({rate:+.2f}%)'


# 진입 사유의 대괄호 묶음(`[점수:9.5, ...]`)을 줄 단위로 끊기 위한 경계.
#  한 줄로 붙여 보내면 카드에서 통째로 흘러 눈으로 훑기 어렵다.
_MEMO_SEGMENT = re.compile(r'\s+(?=\[)')

# 서버 상한은 5000자. 넘기면 요청이 거절되고 결국 dead-letter 로 빠지므로 여유를
# 두고 자른다 — 메모가 길어서 체결 기록을 통째로 잃는 건 말이 안 된다.
_MEMO_MAX_CHARS = 4900


def _memo_lines(trade, side, currency):
    """메모에 넣을 줄 목록. 표시(HTML) 이전의 순수 텍스트."""
    entry = (trade.get('_entry_reason') or '').strip()
    fill = (trade.get('reason') or '').strip()

    lines = []
    if entry:
        # `조건 만족(슈퍼모멘텀) [점수:9.5] [ATR:1,129]` → 세 줄.
        # 대괄호 묶음은 하나의 진입 사유를 나눠 적은 것이므로 '·' 를 붙이지 않는다.
        lines.extend(seg for seg in _MEMO_SEGMENT.split(entry) if seg)

    def _section(text):
        """항목 구분자. 앞에 아무것도 없으면 점만 덩그러니 남으므로 붙이지 않는다."""
        return f'· {text}' if lines else text

    # 외부·수동 주문은 접수와 체결 사유가 같을 수 있다 — 같은 문장을 두 번 적지 않는다.
    if fill and fill != entry:
        lines.append(_section(fill))
    if side == 'SELL':
        pnl = _format_pnl(trade, currency)
        if pnl:
            lines.append(_section(pnl))
    return lines


def _html_escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _compose_memo(trade, side, currency):
    """웹 일지에 남길 메모를 만든다.

    **왜 샀는지·왜 팔았는지는 `접수` 행에만 있다.** `체결` 행의 reason 은 언제나
    "체결 확인 (...)" 확인 문구뿐이라, 그것만 보내면 정작 판단 근거가 통째로
    빠진다. 호출 전에 `enqueue()` 가 원 주문의 사유를 `_entry_reason` 으로 붙여
    준다(없으면 확인 문구만 남는다).

    매도는 실현손익을 함께 적는다 — 구조화 필드(realizedPnl)로도 보내지만
    웹 카드 본문에는 그 값이 나오지 않아 일지만 봐서는 결과를 알 수 없다.

    출력은 `<p>` 문단들이다. 서버의 memo 는 웹 카드에 **HTML 그대로** 그려지므로
    개행문자로는 줄이 나뉘지 않는다. 카드 본문이 Quill 의 `.ql-editor` 안이라
    문단 여백이 0 이고, 그래서 `<p>` 가 `<br>` 보다 안전하다(편집기로 열었을 때도
    Quill 의 기본 표현과 같아 서식이 흐트러지지 않는다).
    """
    lines = _memo_lines(trade, side, currency)

    html, used = [], 0
    for line in lines:
        block = f'<p>{_html_escape(line)}</p>'
        # 상한을 넘기면 그 줄부터 버린다. 중간에서 자르면 태그가 깨져 카드 전체가
        # 망가지므로, 줄 단위로만 끊는다.
        if used + len(block) > _MEMO_MAX_CHARS:
            break
        html.append(block)
        used += len(block)
    return ''.join(html)


def build_payload(trade):
    """로컬 trades 행(dict)을 API TradeRecordInput 으로 변환한다."""
    code = (trade.get('code') or '').strip()
    overseas = _is_overseas(code)
    status = trade.get('order_status')
    confidence = _SYNCABLE_STATUS.get(status, 'CONFIRMED')

    try:
        qty = float(trade.get('qty') or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        price = float(trade.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0

    payload = {
        'symbol': code,
        'name': trade.get('name') or '',
        'side': _side(trade.get('type')),
        'price': price,
        'volume': qty,
        'executedAt': _executed_at(trade.get('time'), overseas),
        'brokerExecutionId': _exec_id(trade),
        # 가상투자 DB 는 파일이 분리돼 있어 is_sim 을 세우지 않는다(항상 0).
        # 세션 플래그가 유일한 기준 — 여기를 놓치면 가상 체결이 실거래로 기록된다.
        'isSimulated': bool(trade.get('is_sim')) or _is_paper(),
        'status': 'FILLED',
        'confidence': confidence,
        'source': _source(),
        'currency': 'USD' if overseas else 'KRW',
        'exchange': _exchange_for(code, overseas),
        'assetType': 'STOCK',
        'subAccount': (trade.get('account') or '').replace('-', ''),
        'orderId': trade.get('odno') or '',
    }
    payload['memo'] = _compose_memo(trade, payload['side'], payload['currency'])

    origin = _order_origin(trade.get('type'))
    if origin:
        payload['orderOrigin'] = origin

    # ⭐️ 분류(tradeClass)를 '시스템'으로 못 박아 보내던 것을 걷어냈다. 외부 앱·HTS 에서
    #    사람이 낸 주문까지 전부 시스템 트레이딩 성과로 뭉쳐졌기 때문이다.
    #    시스템이 낸 주문일 때만 분류를 확정해 보내고, 아니거나 모르면 **필드를 아예
    #    싣지 않는다** — 서버가 같은 종목의 직전 분류(장기투자 등)를 물려받는다.
    system = _is_system(trade.get('type'))
    if system is not None:
        payload['isSystem'] = system
        if system:
            payload['tradeClass'] = '시스템'

    if trade.get('org_odno'):
        payload['originalOrderId'] = trade['org_odno']

    # 매도 실현손익 — 봇이 이미 계산해 둔 값을 넘겨야 서버 통계가 정확해진다.
    if payload['side'] == 'SELL':
        try:
            if trade.get('profit_amt'):
                payload['realizedPnl'] = float(trade['profit_amt'])
        except (TypeError, ValueError):
            pass
        try:
            if trade.get('profit_rate'):
                payload['realizedPnlRate'] = float(trade['profit_rate'])
        except (TypeError, ValueError):
            pass

    for src, dst in (('strategy_score', 'strategyScore'), ('stop_loss_rate', 'stopLossRate')):
        try:
            value = float(trade.get(src) or 0)
            if value:
                payload[dst] = value
        except (TypeError, ValueError):
            pass

    return payload


# ══════════════════════════════════════════════════════════════════════
# 큐 적재 (db_manager 의 트랜잭션 안에서 호출)
# ══════════════════════════════════════════════════════════════════════

# 원 주문(진입/청산 근거)이 실려 있는 상태들. '취소'는 근거가 아니라 결과다.
_ENTRY_STATUS = ('접수', '정정')


def _lookup_entry_reason(cursor, trade):
    """이 체결을 낳은 원 주문의 사유를 찾는다. 호출자의 커서를 그대로 쓴다.

    같은 주문번호(odno)라도 **영업일마다 재사용**되므로 날짜로 반드시 좁혀야 한다.
    좁히지 않으면 다른 날 같은 번호였던 주문의 근거가 엉뚱하게 따라붙는다.

    정정 주문은 '정정' 행의 사유가 "사용자 정정" 같은 확인 문구뿐이라 근거가 되지
    못한다. 그 경우 원주문번호(org_odno)로 한 단계만 거슬러 올라가 진짜 근거를 찾는다.
    """
    odno = (trade.get('odno') or '').strip()
    day = str(trade.get('time') or '')[:10]
    account = trade.get('account') or ''
    if not odno or not day:
        return ''

    placeholders = ','.join('?' * len(_ENTRY_STATUS))

    def _fetch(order_no):
        cursor.execute(
            f"SELECT reason FROM trades "
            f"WHERE odno = ? AND substr(time, 1, 10) = ? AND account = ? "
            f"  AND order_status IN ({placeholders}) AND reason IS NOT NULL AND reason != '' "
            f"ORDER BY id DESC LIMIT 1",
            (order_no, day, account) + _ENTRY_STATUS)
        row = cursor.fetchone()
        return (row[0] or '').strip() if row else ''

    reason = _fetch(odno)
    if not reason and trade.get('org_odno'):
        reason = _fetch(str(trade['org_odno']).strip())
    return reason


def enqueue(cursor, trade, quiet=False, backlog=False, resend=False):
    """전송 대기열에 적재한다. 호출자의 트랜잭션·커서를 그대로 쓴다.

    전송 대상이 아니면 조용히 무시한다. **여기서 예외를 올리면 거래 기록 저장이
    함께 롤백되므로**, 판단이 애매하면 적재하지 않는 쪽을 택한다.

    quiet=True 는 백필처럼 수백 건을 한 번에 훑는 경로용이다. 건별 로그를 남기면
    로그가 그것만으로 차므로 호출부가 요약 한 줄만 남긴다.

    backlog=True 는 뒤늦게 밀어 넣는 건(재동기화)이라는 표시다. 전송 순서에서
    뒤로 밀려, 대량 적재 뒤에 난 실시간 체결이 그 뒤에 줄 서지 않게 한다.

    resend=True 는 이미 큐에 있는 건도 **페이로드를 다시 만들어 덮고** 전송 대기로
    되돌린다. 큐에는 적재 시점에 만든 JSON 이 통째로 들어 있어서, 그대로 다시 보내면
    그동안 고친 표현·필드가 반영되지 않는다. dead-letter 행은 건드리지 않는다.

    반환값이 True 면 INSERT 문을 실제로 실행했다는 뜻이다(중복이라 무시됐을 수도
    있다). 신규 적재 여부까지 알아야 하면 호출 직후 `cursor.rowcount` 를 본다.
    """
    if not is_enabled():
        return False
    if trade.get('order_status') not in _SYNCABLE_STATUS:
        return False
    # 폐기된 KIS 모의투자 시절 기록은 보내지 않는다. 모드는 사라졌지만 실계좌 DB에
    #  남은 is_sim=1 레코드가 백필로 딸려 나갈 수 있다. 가상투자(mode 1) 체결은
    #  DB 파일이 따로라 is_sim=0 이므로 여기 걸리지 않는다 — 관문은 is_enabled 쪽이다.
    if trade.get('is_sim'):
        return False
    if not _side(trade.get('type')):
        return False  # 매수/매도로 해석되지 않는 기록(확인요망 등)은 보내지 않는다

    if not trade.get('_entry_reason'):
        try:
            # 진입/청산 근거는 원 주문('접수') 행에만 있다 — 없으면 메모가
            # "체결 확인" 한 줄로 끝나 판단 근거가 통째로 사라진다.
            trade = dict(trade, _entry_reason=_lookup_entry_reason(cursor, trade))
        except Exception as e:
            # 근거가 없어도 체결 기록 자체는 반드시 나가야 한다. 여기서 예외를
            # 올리면 호출자의 트랜잭션이 통째로 롤백되어 거래 기록까지 날아간다.
            logger.debug(f"[Journal] 원 주문 사유 조회 실패(무시): {e}")

    payload = build_payload(trade)
    if not payload['symbol'] or payload['volume'] <= 0:
        return False

    row = (payload['brokerExecutionId'], json.dumps(payload, ensure_ascii=False),
           datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'), 1 if backlog else 0)
    if resend:
        # remote_id 는 **지우지 않는다.** 서버는 같은 brokerExecutionId 를 duplicate 로
        #  건너뛰기만 하고 덮어쓰지 않으므로(stock-memo: trading_api/entries._insert_trade),
        #  이미 들어간 기록의 정정분을 다시 POST 하면 조용히 무시된다. 서버 id 를 들고
        #  있어야 PATCH 로 고칠 수 있다. needs_patch 가 그 갈림길이다 —
        #  이미 보낸 건이면 PATCH, 아직 안 보낸 건이면 종전대로 POST.
        cursor.execute(
            "INSERT INTO journal_outbox (exec_id, payload, created_at, is_backlog) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(exec_id) DO UPDATE SET "
            "  payload = excluded.payload, is_backlog = excluded.is_backlog, "
            "  synced_at = NULL, attempts = 0, "
            "  needs_patch = CASE WHEN journal_outbox.remote_id IS NOT NULL THEN 1 ELSE 0 END, "
            "  last_attempt_at = NULL, last_error = NULL "
            "WHERE dead_at IS NULL", row)
    else:
        cursor.execute(
            "INSERT OR IGNORE INTO journal_outbox "
            "(exec_id, payload, created_at, is_backlog) VALUES (?, ?, ?, ?)", row)

    if quiet:
        return True
    if cursor.rowcount:
        logger.info(
            f"[Journal] 대기열 적재: {payload['side']} {payload['name']}({payload['symbol']}) "
            f"{payload['volume']:g}주 @{payload['price']:,g} "
            f"[{payload['brokerExecutionId']}]")
    else:
        # 같은 체결이 다시 기록된 경우(재확인·재시작 등). 중복 전송이 아니라 정상 동작이다.
        logger.info(f"[Journal] 대기열 중복 스킵: {payload['brokerExecutionId']}")
    return True


def enqueue_standalone(trade):
    """자체 커넥션으로 적재 (백필·수동 재전송 등 트랜잭션 밖에서 쓰는 경로)."""
    from modules import db_manager
    with db_manager.db.lock:
        conn = db_manager.db._get_conn()
        try:
            queued = enqueue(conn.cursor(), trade)
            if queued:
                conn.commit()
            return queued
        except Exception as e:
            logger.warning(f"[Journal] 큐 적재 실패: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════
# HTTP 클라이언트
# ══════════════════════════════════════════════════════════════════════

class _TokenCache:
    """Access Token 캐시. 만료 전 재사용하고 401 을 받으면 즉시 폐기한다."""

    def __init__(self):
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get(self, force=False):
        with self._lock:
            if not force and self._token and time.time() < self._expires_at:
                return self._token

            blocked = _egress_blocked()
            if blocked:
                logger.warning(f"[Journal] 토큰 발급 차단 — {blocked}")
                return None

            import requests
            try:
                res = requests.post(
                    f'{_base_url()}/api/v1/auth/token',
                    headers={'X-API-KEY': _cfg('JOURNAL_API_KEY')},
                    timeout=_HTTP_TIMEOUT)
            except Exception as e:
                logger.warning(f"[Journal] 토큰 발급 요청 실패: {e}")
                return None

            if res.status_code != 200:
                logger.warning(f"[Journal] 토큰 발급 거부 ({res.status_code}): {res.text[:200]}")
                return None

            data = res.json()
            self._token = data.get('access_token')
            # 만료 5분 전에 미리 갱신해 경계에서 401 을 맞지 않게 한다.
            self._expires_at = time.time() + max(int(data.get('expires_in', 86400)) - 300, 60)
            logger.info(f"[Journal] 접속 토큰 발급 완료 "
                        f"(유효 {int(data.get('expires_in', 86400)) // 3600}시간, "
                        f"권한: {' '.join(data.get('scopes') or []) or '미표기'})")
            return self._token


_tokens = _TokenCache()


def _request(method, path, *, json_body=None, params=None, retry_on_401=True,
             timeout=None, quiet=False):
    """인증 헤더를 붙여 요청한다. (response 또는 None)

    quiet=True 는 호출부가 실패 로그를 직접 관리할 때 쓴다(하트비트 등).
    """
    blocked = _egress_blocked()
    if blocked:
        logger.warning(f"[Journal] {method} {path} 차단 — {blocked}")
        return None

    import requests

    token = _tokens.get()
    if not token:
        return None

    try:
        res = requests.request(
            method, f'{_base_url()}{path}',
            headers={'Authorization': f'Bearer {token}'},
            json=json_body, params=params, timeout=timeout or _HTTP_TIMEOUT)
    except Exception as e:
        if not quiet:
            logger.warning(f"[Journal] {method} {path} 요청 실패: {e}")
        return None

    if res.status_code == 401 and retry_on_401:
        # 토큰 만료·키 폐기 — 한 번만 새로 받아 재시도한다.
        _tokens.invalidate()
        return _request(method, path, json_body=json_body, params=params,
                        retry_on_401=False, timeout=timeout, quiet=quiet)
    return res


# ══════════════════════════════════════════════════════════════════════
# 워커
# ══════════════════════════════════════════════════════════════════════

def _backoff_seconds(attempts):
    """지수 백오프 (상한 1시간). 서버가 죽어 있을 때 헛된 재시도를 줄인다."""
    return min(60 * (2 ** min(attempts, 6)), 3600)


# 백오프 판정을 SQL 로 내린 표현식. `_backoff_seconds` 와 같은 식이어야 한다.
#  (SQLite: `<<` 는 시프트, 인자 2개짜리 min() 은 스칼라 최솟값)
#  파이썬에서 걸러내면 '아직 대기 중인 행'도 스캔 한도를 차지해, 적체가 한도를 넘는
#  순간 뒤쪽 행이 조회조차 되지 않는다(전송 가능한데 영영 안 나감).
_BACKOFF_SQL = "min(60 * (1 << min(attempts, 6)), 3600)"


def _fetch_pending(limit=_BATCH_SIZE):
    """지금 보낼 수 있는 대기 행. 백오프 중이거나 dead-letter 된 행은 제외된다."""
    from modules import db_manager
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        # last_attempt_at 과 now_str 은 둘 다 KST 무오프셋 문자열이라 strftime('%s')
        # 이 양쪽을 똑같이 UTC 로 간주한다 — 차이값은 그대로 정확하다.
        # CAST 는 생략하면 안 된다. strftime 은 TEXT 를 돌려주는데 SQLite 는 숫자를
        # 언제나 텍스트보다 작다고 보므로, 캐스팅 없이 비교하면 백오프가 통째로
        # 무력화되어 죽은 서버에 매 순회 재요청을 때린다.
        # is_backlog 를 먼저 정렬한다. 재동기화로 1년치를 밀어 넣으면 그 뒤에 난
        # 실시간 체결이 backlog 전체 뒤에 줄을 서서 몇 분씩 밀린다. 유지보수
        # 작업이 실시간 경로 앞에 서면 안 된다.
        cursor.execute(
            "SELECT id, exec_id, payload, attempts, last_attempt_at FROM journal_outbox "
            "WHERE synced_at IS NULL AND dead_at IS NULL "
            "  AND COALESCE(needs_patch, 0) = 0 "
            "  AND (last_attempt_at IS NULL "
            f"      OR CAST(strftime('%s', last_attempt_at) AS INTEGER) + {_BACKOFF_SQL} "
            "          <= CAST(strftime('%s', ?) AS INTEGER)) "
            "ORDER BY COALESCE(is_backlog, 0), id LIMIT ?", (now_str, limit))
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"[Journal] 대기열 조회 실패: {e}")
        return []


def _mark_result(results_by_id, now_str):
    """전송 결과를 대기열에 반영한다. {outbox_id: (synced, remote_id, error, rejected)}

    `rejected` 는 **서버가 이 건을 명시적으로 거절했는지**다. 통신 실패와 구분해야
    한다 — 서버가 거절한 건만 세어 `_MAX_REJECTS` 에 닿으면 dead-letter 로 빼낸다.
    그대로 두면 서버가 영구 거절하는 행 하나가 매 배치 앞자리를 차지해 **뒤에 쌓인
    정상 건까지 영영 나가지 못한다**(head-of-line blocking). 반대로 통신 실패까지
    세면 웹서버가 오래 죽어 있을 때 멀쩡한 대기열이 통째로 폐기된다.
    """
    from modules import db_manager
    if not results_by_id:
        return
    buried = []
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            for outbox_id, mark in results_by_id.items():
                synced, remote_id, error, rejected = mark
                if synced:
                    cursor.execute(
                        "UPDATE journal_outbox SET synced_at = ?, remote_id = ?, "
                        "last_attempt_at = ?, last_error = NULL WHERE id = ?",
                        (now_str, remote_id, now_str, outbox_id))
                    continue

                cursor.execute(
                    "UPDATE journal_outbox SET attempts = attempts + 1, "
                    "reject_count = COALESCE(reject_count, 0) + ?, "
                    "last_attempt_at = ?, last_error = ? WHERE id = ?",
                    (1 if rejected else 0, now_str, (error or '')[:500], outbox_id))
                if not rejected:
                    continue
                cursor.execute(
                    "UPDATE journal_outbox SET dead_at = ? "
                    "WHERE id = ? AND dead_at IS NULL AND COALESCE(reject_count, 0) >= ?",
                    (now_str, outbox_id, _MAX_REJECTS))
                if cursor.rowcount:
                    buried.append((outbox_id, error))
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 대기열 갱신 실패: {e}")

    for outbox_id, error in buried:
        # 운용자가 알아채야 하는 상황이다 — 이 체결은 자동으로는 더 이상 나가지 않는다.
        logger.warning(
            f"[Journal] 전송 포기(dead-letter): outbox#{outbox_id} "
            f"— 서버가 {_MAX_REJECTS}회 거절, 마지막 사유: {error or '미상'}")


def _match_results(outbox_ids, payloads, results):
    """응답 항목을 대기열 행에 짝지어 [(outbox_id, item|None)] 을 만든다.

    계약상 `results` 는 요청과 같은 순서·같은 길이지만, 그 가정 하나가 어긋나면
    **엉뚱한 행이 전송 완료로 표시되어 체결이 영구 유실된다.** 응답에 이미 들어 있는
    `brokerExecutionId` 로 맞춰 보고, 그게 없을 때만 `index` → 위치 순으로 물러선다.
    """
    by_exec = {}
    for item in results:
        if isinstance(item, dict) and item.get('brokerExecutionId'):
            by_exec[item['brokerExecutionId']] = item

    matched = []
    for i, oid in enumerate(outbox_ids):
        exec_id = (payloads[i] or {}).get('brokerExecutionId')
        item = by_exec.get(exec_id) if exec_id else None
        if item is None:
            # 멱등키로 못 찾으면 서버가 준 index 를, 그것도 없으면 위치를 쓴다.
            candidate = results[i] if i < len(results) else None
            if isinstance(candidate, dict):
                idx = candidate.get('index')
                # index 가 자기 위치와 다르면 순서가 어긋난 응답이다 — 짝짓지 않는다.
                item = candidate if idx in (None, i) else None
        matched.append((oid, item))
    return matched


def _send_batch(payloads, outbox_ids, exec_ids):
    """한 묶음을 전송하고 결과를 대기열에 반영한다. (성공, 실패) 반환.

    서버가 묶음 **전체**를 거절(4xx)하면 절반으로 쪼개 다시 보낸다. 1건까지 좁히면
    진범이 특정되므로 그 행만 거절로 세고 나머지는 정상 전송된다. 쪼개지 않으면
    페이로드 한 건 때문에 묶음 전체가, 나아가 대기열 전체가 멈춘다.
    """
    res = _request('POST', '/api/v1/trades/batch',
                   json_body={'source': _source(), 'trades': payloads})
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

    # ── 서버에 닿지 못했거나 서버가 넘어진 경우: 거절이 아니므로 세지 않고 그대로 재시도
    if res is None:
        _mark_result({oid: (False, None, '서버 응답 없음', False) for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code == 429:
        retry_after = res.headers.get('Retry-After', '?')
        logger.info(f"[Journal] 레이트 리밋 — {retry_after}초 후 재시도")
        _mark_result({oid: (False, None, f'429 (Retry-After={retry_after})', False)
                      for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code >= 500:
        message = f'{res.status_code}: {res.text[:200]}'
        logger.warning(f"[Journal] 서버 오류 — {message}")
        _mark_result({oid: (False, None, message, False) for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    # ── 묶음 전체 거절(400/413/422 등): 반씩 쪼개 진범을 좁힌다
    if res.status_code not in (200, 201):
        message = f'{res.status_code}: {res.text[:200]}'
        if len(payloads) > 1:
            logger.warning(f"[Journal] 배치 거절 — {message} / {len(payloads)}건을 분할 재시도")
            half = len(payloads) // 2
            ok_a, fail_a = _send_batch(payloads[:half], outbox_ids[:half], exec_ids[:half])
            ok_b, fail_b = _send_batch(payloads[half:], outbox_ids[half:], exec_ids[half:])
            return ok_a + ok_b, fail_a + fail_b
        logger.warning(f"[Journal] 단건 거절 — {message} [{exec_ids[0]}]")
        _mark_result({outbox_ids[0]: (False, None, message, True)}, now_str)
        return 0, 1

    try:
        results = (res.json() or {}).get('results') or []
    except Exception:
        results = []

    marks = {}
    ok = fail = 0
    for oid, item in _match_results(outbox_ids, payloads, results):
        if item is None:
            # 성공 여부를 알 수 없다. 재전송해도 서버가 멱등 처리하므로 다시 보내는
            # 편이 안전하다. 서버 잘못이라 단정할 수 없으니 거절로는 세지 않는다.
            marks[oid] = (False, None, '응답에 결과 항목 없음', False)
            fail += 1
        elif item.get('status') in ('created', 'duplicate'):
            marks[oid] = (True, item.get('id'), None, False)   # 성공 행에서 거절 플래그는 무의미
            ok += 1
            if item.get('warnings'):
                logger.info(f"[Journal] 서버 경고({item.get('brokerExecutionId')}): "
                            f"{'; '.join(item['warnings'])}")
        else:
            # 서버가 사유를 붙여 거절한 건 — 재시도해도 결과가 같다. 거절로 센다.
            marks[oid] = (False, None, f"{item.get('errorCode')}: {item.get('error')}", True)
            fail += 1

    _mark_result(marks, now_str)
    return ok, fail


# ══════════════════════════════════════════════════════════════════════
# 기초잔고 — 연동 이전부터 들고 있던 종목의 씨앗
# ══════════════════════════════════════════════════════════════════════

def _opening_marker(account):
    """이 계좌의 기초잔고를 이미 보냈는가."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        row = conn.execute("SELECT sent_at FROM journal_opening WHERE account = ?",
                           (account,)).fetchone()
        return bool(row and row['sent_at'])
    except Exception as e:
        # 표시를 못 읽으면 **보낸 것으로 친다**. 두 번 보내면 없는 매수가 생겨
        # 보유 수량이 부풀지만, 안 보내면 경고 한 줄로 끝난다.
        logger.warning(f"[Journal] 기초잔고 표시 조회 실패 — 전송을 건너뜁니다: {e}")
        return True


def _mark_opening_sent(account, as_of, count):
    from modules import db_manager
    try:
        with db_manager.db.lock:
            conn = db_manager.db._get_conn()
            conn.execute(
                "INSERT INTO journal_opening (account, as_of, sent_at, count) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(account) DO UPDATE SET "
                "  as_of = excluded.as_of, sent_at = excluded.sent_at, count = excluded.count",
                (account, as_of, datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'), count))
            conn.commit()
    except Exception as e:
        logger.warning(f"[Journal] 기초잔고 표시 기록 실패 — 다음 기동에 또 보낼 수 있습니다: {e}")


def _codes_with_local_buys():
    """로컬에 매수 체결 기록이 있는 종목 코드 집합.

    이 종목들은 기초잔고에서 뺀다. 기록이 있으면 그 매수가 정상 경로나 백필로
    서버에 닿으므로, 합성 기초잔고를 더하면 **없는 매수를 하나 더 만드는 셈**이다.
    반대로 기록이 아예 없는 종목은 서버가 그 매수를 영영 알 수 없다 —
    기초잔고가 메우려는 구멍이 정확히 그것이다.

    (기록은 있는데 백필 범위를 벗어나 끝내 안 올라가는 종목은 씨앗을 못 받는다.
     덜 심는 쪽이 두 번 심는 쪽보다 낫다 — 잘못된 매수는 되돌리기 어렵다.)
    """
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT code FROM trades WHERE order_status IN "
            f"({','.join('?' * len(_SYNCABLE_STATUS))}) AND type LIKE '%매수%'",
            tuple(_SYNCABLE_STATUS)).fetchall()
        codes = {(r['code'] or '').strip().upper() for r in rows}
        rows = conn.execute(
            "SELECT DISTINCT code FROM trades WHERE order_status IN "
            f"({','.join('?' * len(_SYNCABLE_STATUS))}) AND type LIKE '%buy%'",
            tuple(_SYNCABLE_STATUS)).fetchall()
        return codes | {(r['code'] or '').strip().upper() for r in rows}
    except Exception as e:
        # 못 읽으면 전량 제외 — 무엇이 이미 기록됐는지 모르는 채로 씨를 뿌리면 안 된다.
        logger.warning(f"[Journal] 기존 매수 기록 조회 실패 — 기초잔고를 건너뜁니다: {e}")
        return None


def _current_positions():
    """현재 보유 종목. 조회 자체가 실패하면 None (빈 계좌와 구분해야 한다)."""
    import api

    positions = []
    try:
        res = api.get_domestic_balance() or {}
    except Exception as e:
        logger.warning(f"[Journal] 기초잔고 — 국내 잔고 조회 실패: {e}")
        return None
    if str(res.get('rt_cd', '1')) != '0':
        logger.warning(f"[Journal] 기초잔고 — 국내 잔고 조회 실패: {res.get('msg1')}")
        return None

    for item in (res.get('output1') or []):
        try:
            qty = float(item.get('hldg_qty') or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        positions.append({
            'symbol': (item.get('pdno') or '').strip(),
            'name': item.get('prdt_name') or '',
            'volume': qty,
            'avgPrice': float(item.get('pchs_avg_pric') or 0),
            'currency': 'KRW',
            'exchange': 'KRX',
        })

    # 해외는 조회 실패해도 국내분은 보낸다 — 해외 계좌가 없는 설치가 대부분이다.
    try:
        res = api.get_overseas_balance() or {}
        if str(res.get('rt_cd', '1')) == '0':
            for item in (res.get('output1') or []):
                qty = float(item.get('ovrs_cblc_qty') or 0)
                if qty <= 0:
                    continue
                code = (item.get('ovrs_pdno') or '').strip()
                positions.append({
                    'symbol': code,
                    'name': item.get('ovrs_item_name') or '',
                    'volume': qty,
                    'avgPrice': float(item.get('pchs_avg_pric') or 0),
                    'currency': 'USD',
                    'exchange': _exchange_for(code, True),
                })
    except Exception as e:
        logger.warning(f"[Journal] 기초잔고 — 해외 잔고 조회 실패(국내분만 보냅니다): {e}")

    return [p for p in positions if p['symbol'] and p['avgPrice'] > 0]


def opening_once():
    """연동 시작 시점의 보유 잔고를 서버에 매수 기록으로 심는다. (전송 건수)

    **왜 필요한가**: 연동 이전부터 들고 있던 종목은 서버에 매수 기록이 없다. 그러면
    그 종목의 첫 매도가 `needsReview`('매수 기록 없음')로 찍히고 보유 수량 집계가
    음수로 내려간다 — 서버의 매도 무결성 검사가 그 상태를 계속 경고한다.

    **계좌당 한 번만** 보낸다. 서버 멱등키에 날짜가 박히므로(`OPENING:{env}:{날짜}:{종목}`)
    다른 날 또 보내면 같은 종목의 기초잔고가 하나 더 생긴다 — 없는 매수를 만드는 것이라
    첫 문제보다 나쁘다. 그래서 로컬에 보낸 사실을 남기고, 그것을 못 읽으면 보내지 않는다.
    """
    if not is_enabled():
        return 0

    try:
        account = _account_text(config.session.cano, config.session.acnt_prdt_cd)
    except Exception:      # noqa: BLE001
        account = ''
    if not account:
        return 0
    if _opening_marker(account):
        return 0

    known = _codes_with_local_buys()
    if known is None:
        return 0

    positions = _current_positions()
    if positions is None:
        return 0        # 조회 실패 — 표시를 남기지 않아 다음 기동에 다시 시도한다

    as_of = datetime.now(KST).strftime('%Y-%m-%d')
    seeds = [p for p in positions if p['symbol'].upper() not in known]
    if not seeds:
        # 심을 것이 없다는 것도 결론이다. 표시를 남겨 매 기동마다 잔고를 묻지 않는다.
        _mark_opening_sent(account, as_of, 0)
        logger.info(f"[Journal] 기초잔고 — 심을 종목 없음 (보유 {len(positions)}건은 "
                    f"모두 로컬 매수 기록이 있습니다)")
        return 0

    body = {
        'asOf': as_of,
        'isSimulated': _is_paper(),
        'source': _source(),
        'positions': [dict(p, subAccount=account.replace('-', '')) for p in seeds],
    }
    res = _request('POST', '/api/v1/positions/opening', json_body=body)
    if res is None or res.status_code not in (200, 201):
        reason = f'{res.status_code}: {res.text[:200]}' if res is not None else '응답 없음'
        logger.warning(f"[Journal] 기초잔고 전송 실패 ({reason}) — 다음 기동에 다시 시도합니다")
        return 0

    try:
        result = res.json() or {}
    except Exception:
        result = {}
    inserted = int(result.get('inserted') or 0)
    _mark_opening_sent(account, as_of, inserted)
    logger.info(f"[Journal] 기초잔고 등록 — {inserted}건 "
                f"(대상 {len(seeds)}건 / 보유 {len(positions)}건, 기준일 {as_of})")
    return inserted


# ══════════════════════════════════════════════════════════════════════
# 정정분 — 이미 서버에 들어간 기록을 고친다
# ══════════════════════════════════════════════════════════════════════

# PATCH 로 고칠 수 있는 필드(서버 trading_api/routes._PATCHABLE 과 짝을 이룬다).
# 여기 없는 필드가 바뀌면 서버는 옛 값을 그대로 들고 있게 되므로, 서버가 목록을
# 넓히면 여기도 함께 넓혀야 한다.
_PATCHABLE_FIELDS = (
    'price', 'volume', 'status', 'confidence', 'realizedPnl', 'realizedPnlRate',
    'fee', 'tax', 'strategyScore', 'stopLossRate', 'memo', 'name',
)


def _fetch_corrections(limit=_BATCH_SIZE):
    """PATCH 로 보내야 하는 정정 행. 백오프·dead-letter 는 대기열과 같은 규칙."""
    from modules import db_manager
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, exec_id, payload, remote_id FROM journal_outbox "
            "WHERE synced_at IS NULL AND dead_at IS NULL "
            "  AND COALESCE(needs_patch, 0) = 1 AND remote_id IS NOT NULL "
            "  AND (last_attempt_at IS NULL "
            f"      OR CAST(strftime('%s', last_attempt_at) AS INTEGER) + {_BACKOFF_SQL} "
            "          <= CAST(strftime('%s', ?) AS INTEGER)) "
            "ORDER BY id LIMIT ?", (now_str, limit))
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"[Journal] 정정 대기열 조회 실패: {e}")
        return []


def _clear_patch_flag(outbox_id):
    """PATCH 를 포기하고 신규 등록(POST) 경로로 되돌린다.

    서버에 그 기록이 없을 때(404) 쓴다 — 운용자가 웹에서 지웠거나, API 로 손댈 수
    없는 기록이 된 경우다. 정정분을 삼키는 것보다 다시 올리는 편이 낫다:
    로컬 체결 기록이 원본이고, 재동기화(resync)가 하려는 일도 정확히 그것이다.
    """
    from modules import db_manager
    try:
        with db_manager.db.lock:
            conn = db_manager.db._get_conn()
            conn.execute("UPDATE journal_outbox SET needs_patch = 0, remote_id = NULL "
                         "WHERE id = ?", (outbox_id,))
            conn.commit()
    except Exception as e:
        logger.warning(f"[Journal] 정정 플래그 해제 실패(outbox#{outbox_id}): {e}")


def _send_corrections():
    """정정 행을 하나씩 PATCH 한다. (성공, 실패) 반환.

    **왜 배치가 아니라 건별인가**: 서버의 배치 엔드포인트는 같은 brokerExecutionId 를
    `duplicate` 로 건너뛰기만 하고 값을 덮지 않는다. 그래서 정정분을 배치로 다시
    보내면 클라이언트는 '전송 완료'로 도장을 찍는데 서버 값은 옛것 그대로 남는다 —
    로컬과 웹 매매일지의 손익이 조용히 갈린다. 갱신 경로는 PATCH 하나뿐이다.
    """
    rows = _fetch_corrections()
    if not rows:
        return 0, 0

    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    marks = {}
    ok = fail = 0
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except Exception:
            marks[row['id']] = (False, None, '페이로드 파싱 불가', True)
            fail += 1
            continue

        body = {k: payload[k] for k in _PATCHABLE_FIELDS if k in payload}
        if not body:
            # 고칠 것이 없으면 이미 서버 값과 같다 — 다시 보낼 이유가 없다.
            marks[row['id']] = (True, row['remote_id'], None, False)
            ok += 1
            continue

        res = _request('PATCH', f"/api/v1/trades/{row['remote_id']}", json_body=body)
        if res is None:
            marks[row['id']] = (False, None, '서버 응답 없음', False)
            fail += 1
        elif res.status_code == 200:
            marks[row['id']] = (True, row['remote_id'], None, False)
            ok += 1
            logger.info(f"[Journal] 정정 반영: {payload.get('name')}({payload.get('symbol')}) "
                        f"[{row['exec_id']}]")
        elif res.status_code == 404:
            # 서버에 그 기록이 없다 — PATCH 로는 영영 안 된다. 신규 등록으로 되돌린다.
            logger.info(f"[Journal] 정정 대상이 서버에 없음 — 신규 등록으로 전환 "
                        f"[{row['exec_id']}]")
            _clear_patch_flag(row['id'])
        elif res.status_code == 429 or res.status_code >= 500:
            marks[row['id']] = (False, None, f'{res.status_code}', False)
            fail += 1
        else:
            marks[row['id']] = (False, None, f'{res.status_code}: {res.text[:200]}', True)
            fail += 1

    _mark_result(marks, now_str)
    return ok, fail


def flush_once():
    """대기열을 한 번 비운다. (전송 성공 건수, 실패 건수) 반환."""
    if not is_enabled():
        return 0, 0

    # 정정분을 먼저 보낸다. 뒤로 미루면 사람이 보는 화면에 옛 손익이 더 오래 남는다.
    ok_patch, fail_patch = _send_corrections()

    rows = _fetch_pending()
    if not rows:
        return ok_patch, fail_patch

    payloads, outbox_ids, exec_ids = [], [], []
    for row in rows:
        try:
            payloads.append(json.loads(row['payload']))
            outbox_ids.append(row['id'])
            exec_ids.append(row['exec_id'])
        except Exception:
            # 손상된 페이로드는 재시도해도 소용없다 — 거절로 세어 결국 dead-letter 시킨다.
            _mark_result({row['id']: (False, None, '페이로드 파싱 불가', True)},
                         datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))

    if not payloads:
        return ok_patch, fail_patch

    ok, fail = _send_batch(payloads, outbox_ids, exec_ids)
    ok += ok_patch
    fail += fail_patch
    if ok or fail:
        logger.info(f"[Journal] 전송 완료 {ok}건 / 실패 {fail}건 "
                    f"(대기 잔량 {pending_count()}건)")
    return ok, fail


_ping_fail_streak = 0

# 다음 Ping 에 실어 보낼 명령 처리 결과. 서버는 이걸 받아야 명령을 완료 처리한다.
_pending_ack = None
# 이미 실행한 명령 id. 서버는 ack 를 받을 때까지 같은 명령을 계속 내려보내므로,
# 이 값이 없으면 10초마다 재동기화가 반복된다. 재시작하면 잊는데, 그때는 한 번 더
# 실행될 뿐 결과가 달라지지 않으므로(멱등) 디스크에 남기지 않는다.
_handled_command_id = None


def ping(status='running', message=None, timeout=None, force=False):
    """봇 상태 Ping. 웹 대시보드의 가동 표시등을 켜고, 서버 지시를 받아 처리한다.

    status: running(가동) / stopped(정상 종료) / error(오류)
    force:  메뉴 토글(JOURNAL_SYNC_USE)을 무시하고 자격증명만으로 보낸다.
            연동을 '끄는' 순간의 종료 통지가 여기에 해당한다 — 토글이 이미
            False 로 바뀐 뒤에 stop() 이 불리므로, 검사하면 통지가 통째로
            누락되어 웹 표시등이 계속 '정상 가동중'으로 남는다.
    """
    global _ping_fail_streak, _pending_ack, _handled_command_id

    if not (_has_credentials() if force else is_enabled()):
        return False
    body = {
        'status': status,
        'isSimulated': _is_paper(),
        # botId 가 없으면 서버가 사용자당 한 칸에 상태를 겹쳐 쓴다 — HTS 를 여러 대
        # 돌릴 때 실전봇의 죽음이 가상봇 Ping 에 가려지고, 재동기화 명령도 아무
        # 봇이나 채간다.
        'botId': _bot_id(),
        'label': _bot_label(),
    }
    if message:
        body['message'] = message[:500]
    if _pending_ack:
        body['commandAck'] = _pending_ack

    res = _request('POST', '/api/v1/bot/status', json_body=body,
                   timeout=timeout or _PING_TIMEOUT, quiet=True)
    ok = bool(res is not None and res.status_code == 200)

    if ok:
        # 서버가 200 을 줬으면 ack 도 전달됐다 — 다음 Ping 에 또 실어 보내지 않는다.
        _pending_ack = None
        # 끊겼다가 살아난 것은 운용자가 알아야 하므로 회복만 INFO 로 남긴다.
        if _ping_fail_streak:
            logger.info(f"[Journal] 봇 상태 Ping 회복 (연속 실패 {_ping_fail_streak}회 후)")
        _ping_fail_streak = 0
        logger.debug(f"[Journal] 봇 상태 Ping 전송 (status={status})")

        try:
            payload = res.json() or {}
        except Exception:
            payload = {}
        command_id = payload.get('commandId')
        if command_id is not None and command_id != _handled_command_id:
            _handled_command_id = command_id
            ack = _handle_command(payload)
            if ack:
                # 다음 순회에서 보낸다. 여기서 곧바로 다시 Ping 하면 하트비트
                # 주기가 흐트러지고, 실패 시 재시도 경로가 두 벌이 된다.
                _pending_ack = ack
                trigger()   # 재동기화분을 다음 주기까지 묵혀 둘 이유가 없다
    else:
        _ping_fail_streak += 1
        reason = res.status_code if res is not None else '응답 없음'
        # 10초 간격이라 매번 남기면 로그가 이것만으로 찬다 — 첫 실패와 이후 5분마다만.
        if _ping_fail_streak == 1 or _ping_fail_streak % _PING_WARN_EVERY == 0:
            logger.warning(f"[Journal] 봇 상태 Ping 실패 ({reason}) "
                           f"— 연속 {_ping_fail_streak}회")
    return ok


def _notify_shutdown(status='stopped', message=None):
    """종료 사실을 웹서버에 한 번 알린다.

    종료 경로에서 호출되므로 무슨 일이 있어도 예외를 올리지 않고, 응답이 늦어도
    프로그램 종료를 붙잡지 않도록 타임아웃을 짧게 둔다.
    """
    try:
        ok = ping(status, message=message, timeout=_SHUTDOWN_PING_TIMEOUT, force=True)
        if ok:
            logger.info(f"[Journal] 종료 상태 통지 완료 (status={status})")
        else:
            # 못 보내도 서버는 Ping 누락으로 곧 '통신단절'을 표시하므로 치명적이지 않다.
            logger.warning("[Journal] 종료 상태 통지 실패 — 웹 표시등은 Ping 누락으로 전환됩니다.")
        return ok
    except Exception as e:
        logger.debug(f"[Journal] 종료 상태 통지 중 오류(무시): {e}")
        return False


def notify_shutdown(status='stopped', message=None):
    """프로그램 종료 시 호출 — 웹 대시보드 표시등을 즉시 '정지됨'으로 바꾼다.

    이 통지가 없으면 Ping 이 3회 누락될 때까지 '정상 가동중'으로 남는다.
    """
    if not _has_credentials():
        return False
    return _notify_shutdown(status, message)


def pending_count():
    """미전송 건수 (메뉴/상태 표시용). dead-letter 된 행은 제외한다."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM journal_outbox "
                       "WHERE synced_at IS NULL AND dead_at IS NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def dead_count():
    """전송을 포기한 건수. 0이 아니면 운용자가 원인을 봐야 한다."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM journal_outbox WHERE dead_at IS NOT NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════
# 대기열 정리 (retention)
# ══════════════════════════════════════════════════════════════════════

def purge_synced(days=_RETENTION_DAYS):
    """전송이 끝난 지 `days` 지난 행을 지운다. 삭제 건수 반환.

    라즈베리파이 SD카드에 payload JSON 이 체결마다 무한 누적되는 걸 막는다.
    미전송·dead-letter 행은 건드리지 않는다 — 전자는 아직 보내야 하고, 후자는
    운용자가 원인을 봐야 한다.

    VACUUM 은 하지 않는다. 라파에서 수십 MB DB 를 VACUUM 하면 그동안 쓰기가
    통째로 막히고, 어차피 SQLite 가 빈 페이지를 재사용하므로 실익이 없다.
    """
    from modules import db_manager
    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM journal_outbox "
                "WHERE synced_at IS NOT NULL AND synced_at < ?", (cutoff,))
            removed = cursor.rowcount or 0
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 대기열 정리 실패: {e}")
            return 0
    if removed:
        logger.info(f"[Journal] 전송 완료 {removed}건 정리 ({days}일 경과분)")
    return removed


# ══════════════════════════════════════════════════════════════════════
# 백필 — 큐에 들어가지 못한 체결 회수
# ══════════════════════════════════════════════════════════════════════

def _fetch_last_sync():
    """서버가 마지막으로 저장한 체결 지점을 조회한다. (dict 또는 None)

    `source` 를 반드시 실어야 한다. 빼면 웹 UI 에서 손으로 입력한 기록까지 섞여,
    미래 날짜 기록 하나만 있어도 백필 구간이 통째로 건너뛰어진다.
    계좌로는 좁히지 않는다 — 계좌가 여럿일 때 한쪽만 앞서 있으면 뒤처진 계좌의
    누락 구간이 스캔에서 빠진다.
    """
    res = _request('GET', '/api/v1/trades/last-sync',
                   params={'source': _source(),
                           'isSimulated': 'true' if _is_paper() else 'false'})
    if res is None:
        return None
    if res.status_code == 403:
        # trades:read 없이 발급된 키. 전송은 되지만 누락 회수는 못 한다.
        logger.warning("[Journal] 백필 불가 — API 키에 trades:read 권한이 없습니다. "
                       "웹 설정에서 키를 다시 발급하세요.")
        return None
    if res.status_code != 200:
        logger.warning(f"[Journal] 마지막 동기화 지점 조회 실패 "
                       f"({res.status_code}): {res.text[:200]}")
        return None
    try:
        return res.json() or {}
    except Exception:
        return None


def _backfill_since(last_sync):
    """스캔 시작 시각(로컬 KST 문자열)을 정한다.

    서버가 주는 `lastExecutedAt` 은 UTC 인데 로컬 `trades.time` 은 KST 다.
    변환하지 않고 비교하면 9시간이 통째로 어긋난다.
    """
    default = datetime.now(KST) - timedelta(days=_BACKFILL_LOOKBACK_DAYS)
    raw = (last_sync or {}).get('lastExecutedAt')
    if not raw:
        return default.strftime('%Y-%m-%d %H:%M:%S')
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return default.strftime('%Y-%m-%d %H:%M:%S')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # 경계에서 한 건도 빠지지 않도록 조금 앞에서부터 다시 훑는다. 중복은
    # exec_id UNIQUE 와 서버 멱등 처리가 걸러 주므로 겹치는 쪽이 안전하다.
    since = parsed.astimezone(KST) - timedelta(minutes=_BACKFILL_OVERLAP_MIN)
    return max(since, default).strftime('%Y-%m-%d %H:%M:%S')


_FILL_COLUMNS = ("time, type, code, name, qty, price, odno, org_odno, account, "
                 "is_sim, profit_amt, profit_rate, reason, strategy_score, "
                 "order_status, stop_loss_rate")


def _local_fills_between(cursor, since_str, until_str, limit):
    """[since, until] 구간의 로컬 체결. 오래된 것부터. 호출자의 커서를 쓴다."""
    statuses = tuple(_SYNCABLE_STATUS)
    placeholders = ','.join('?' * len(statuses))
    cursor.execute(
        f"SELECT {_FILL_COLUMNS} FROM trades "
        f"WHERE order_status IN ({placeholders}) AND time >= ? AND time <= ? "
        f"ORDER BY time LIMIT ?", statuses + (since_str, until_str, limit))
    return [dict(row) for row in cursor.fetchall()]


def _local_fills_since(since_str, limit=_BACKFILL_MAX_ROWS):
    """`since_str` 이후의 로컬 체결 기록. 오래된 것부터."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        return _local_fills_between(conn.cursor(), since_str, '9999-12-31 23:59:59', limit)
    except Exception as e:
        logger.warning(f"[Journal] 로컬 체결 조회 실패: {e}")
        return []


def backfill_once():
    """큐에 없는 로컬 체결을 찾아 대기열에 넣는다. (회수 건수, 스캔 건수) 반환.

    **이 함수가 막는 것**: 연동 토글이 꺼져 있었거나 환경변수가 빠진 채 돌던 구간의
    체결이다. 그 구간엔 `enqueue()` 가 통째로 건너뛰므로 큐에 아무것도 남지 않아,
    나중에 연동을 켜도 재시도 로직으로는 영원히 복구되지 않는다. 서버가 죽어 있던
    구간은 큐에 남아 있으므로 여기서 할 일이 없다.

    적재만 하고 전송은 하지 않는다 — 기존 워커가 다음 주기에 알아서 보낸다.
    """
    if not is_enabled():
        return 0, 0

    since = _backfill_since(_fetch_last_sync())
    rows = _local_fills_since(since)
    if not rows:
        return 0, 0

    from modules import db_manager
    queued = 0
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            for trade in rows:
                # quiet=True: 수백 건을 훑으므로 건별 로그는 남기지 않는다.
                #  enqueue 가 True 를 돌려준 직후의 rowcount 는 방금 실행한
                #  INSERT OR IGNORE 의 결과라, 1이면 큐에 없던 신규 건이다.
                if enqueue(cursor, trade, quiet=True) and cursor.rowcount:
                    queued += 1
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 백필 적재 실패: {e}")
            return 0, len(rows)

    if queued:
        # 정상 경로가 놓친 체결이 있었다는 뜻이다 — 조용히 넘기면 안 된다.
        logger.warning(f"[Journal] 백필 — 대기열에 없던 체결 {queued}건 회수 "
                       f"(스캔 {len(rows)}건, {since} 이후)")
    else:
        logger.debug(f"[Journal] 백필 — 누락 없음 (스캔 {len(rows)}건, {since} 이후)")
    return queued, len(rows)


# ══════════════════════════════════════════════════════════════════════
# 재동기화 — 웹에서 지운 기록을 다시 보낸다
# ══════════════════════════════════════════════════════════════════════

def _parse_command_date(value, default=None, end_of_day=False):
    """`YYYY-MM-DD` 또는 RFC3339 를 로컬(KST) 비교용 문자열로 바꾼다."""
    if not value:
        return default
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return default
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(KST)
    if end_of_day and len(text) <= 10:
        # 종료일만 준 경우(2026-08-01)는 그날 하루를 통째로 포함해야 한다.
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def resync_once(date_from=None, date_to=None):
    """지정 기간의 로컬 체결을 전송 대기열에 되돌린다. (대상 건수, 스캔 건수)

    **백필과 다른 점**: 백필은 '큐에 없는' 건만 줍지만, 재동기화는 **이미 보낸
    건까지 다시 보낸다.** 서버에서 운용자가 지운 기록은 로컬 outbox 에 전송 완료로
    남아 있어 백필로는 절대 회수되지 않기 때문이다.

    중복 걱정은 하지 않아도 된다 — 서버가 `brokerExecutionId` 로 멱등 처리해
    이미 있는 기록은 `duplicate` 로 건너뛴다. 그래서 기간을 넉넉히 잡는 편이 낫다.

    dead-letter 행은 건드리지 않는다. 서버가 반복 거절한 데는 이유가 있고,
    운용자가 원한 것은 '지운 기록 복구'이지 '거절된 기록 재시도'가 아니다.
    """
    if not is_enabled():
        return 0, 0

    from modules import db_manager
    since = _parse_command_date(
        date_from,
        (datetime.now(KST) - timedelta(days=_BACKFILL_LOOKBACK_DAYS)
         ).strftime('%Y-%m-%d %H:%M:%S'))
    until = _parse_command_date(date_to, '9999-12-31 23:59:59', end_of_day=True)

    queued = scanned = 0
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            # 라파 메모리를 지키려 한 번에 다 읽지 않고 끊어서 훑는다. 범위를
            # 소진할 때까지 계속한다 — 1년을 눌렀는데 중간에 잘리면 그게 더 나쁘다.
            cutoff = since
            while True:
                rows = _local_fills_between(cursor, cutoff, until, _BACKFILL_MAX_ROWS)
                if not rows:
                    break
                for trade in rows:
                    scanned += 1
                    # resend=True: 큐에 없으면 새로 넣고, 있으면 페이로드를 다시
                    #  만들어 덮은 뒤 전송 대기로 되돌린다. 저장된 JSON 을 그대로
                    #  다시 보내면 그동안 고친 표현이 반영되지 않는다.
                    if enqueue(cursor, trade, quiet=True, backlog=True, resend=True):
                        queued += cursor.rowcount   # dead-letter 행은 0 이 나온다
                last_time = str(rows[-1].get('time') or '')
                if len(rows) < _BACKFILL_MAX_ROWS or last_time <= cutoff:
                    break                     # 다 훑었거나 더 진전이 없다
                cutoff = last_time
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 재동기화 적재 실패: {e}")
            return 0, scanned

    logger.info(f"[Journal] 재동기화 — {queued}건 재전송 대기 "
                f"(스캔 {scanned}건, {since} ~ {date_to or '현재'})")
    return queued, scanned


def _handle_command(body):
    """Ping 응답에 실려 온 서버 지시를 처리한다. ack 로 보낼 dict 또는 None.

    **구현한 명령만 실행하고 나머지는 무시한다.** 특히 `pause`/`resume` 은 매매
    자체를 멈추는 지시라, 웹서버가 침해되거나 버그를 내면 포지션을 든 채로 봇이
    멈춘다. 재동기화(이미 내 것인 데이터를 다시 보내는 일)와 같은 취급을 하면 안 된다.
    """
    if not isinstance(body, dict):
        return None
    command = body.get('command')
    if not command or command == 'none':
        return None

    command_id = body.get('commandId')
    if command != 'resync':
        logger.warning(f"[Journal] 지원하지 않는 서버 지시 무시: {command} "
                       f"(id={command_id})")
        return None
    if command_id is None:
        logger.warning("[Journal] commandId 없는 재동기화 지시 무시 "
                       "— 중복 실행을 막을 수 없습니다.")
        return None

    params = body.get('commandParams') or {}
    date_from = params.get('from') if isinstance(params, dict) else None
    date_to = params.get('to') if isinstance(params, dict) else None

    logger.info(f"[Journal] 서버 재동기화 지시 수신 (id={command_id}, "
                f"{date_from or '기본범위'} ~ {date_to or '현재'})")
    try:
        queued, scanned = resync_once(date_from, date_to)
    except Exception as e:
        logger.error(f"[Journal] 재동기화 실패: {e}")
        return {'id': command_id, 'result': 'failed', 'count': 0, 'message': str(e)[:500]}

    return {
        'id': command_id,
        'result': 'queued' if queued else 'skipped',
        'count': queued,
        'message': f'로컬 체결 {scanned}건 확인, {queued}건 재전송 대기열 적재',
    }


class JournalSyncWorker:
    """대기열을 주기적으로 비우고 봇 상태를 보고하는 단일 백그라운드 스레드."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.is_running = False
        self.thread = None
        self._wake = threading.Event()
        self._last_ping = 0.0
        self._last_flush = 0.0
        self._force_flush = False
        # 첫 백필은 기동 직후가 아니라 조금 뒤에 돈다 — 로그인·유니버스(stock.json)
        # 적재가 끝나야 해외 종목의 거래소를 제대로 붙일 수 있다.
        self._next_backfill = 0.0
        self._next_purge = 0.0

    def start(self):
        if self.is_running:
            return
        if not is_enabled():
            # 왜 안 도는지가 로그만 봐도 판별되어야 한다 — 설정(메뉴)과 자격증명(환경변수)을 구분해 남긴다.
            if not getattr(config.settings, 'JOURNAL_SYNC_USE', False):
                logger.info("[Journal] 매매일지 연동 비활성 (메뉴 0 → 5-3 스위치 OFF)")
            else:
                missing = [n for n in ('JOURNAL_API_URL', 'JOURNAL_API_KEY') if not _cfg(n)]
                logger.info(f"[Journal] 매매일지 연동 비활성 (환경변수 미설정: {', '.join(missing)})")
            return
        self.is_running = True
        self._next_backfill = time.time() + _BACKFILL_STARTUP_DELAY_SEC
        self._next_purge = time.time() + _BACKFILL_STARTUP_DELAY_SEC
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="JournalSync")
        self.thread.start()
        buried = dead_count()
        # botId 를 반드시 남긴다 — 웹 목록에 봇이 안 보일 때 '식별자가 겹쳤나'를
        # 확인할 유일한 단서다. 겹치면 서로의 상태를 덮어써 한쪽이 통째로 사라진다.
        logger.info(f"[Journal] 매매일지 연동 시작 — {_base_url()} "
                    f"(botId={_bot_id()}, source={_source()}, "
                    f"대기 잔량 {pending_count()}건"
                    f"{f', 전송포기 {buried}건' if buried else ''})")

    def stop(self, notify='stopped'):
        """워커를 멈춘다.

        notify 에 상태를 주면 종료 사실을 웹서버에 즉시 알린다. 이 신호가 없으면
        웹 대시보드는 Ping 이 3회 누락될 때까지 '정상 가동중'으로 남아 있게 된다.
        """
        if not self.is_running:
            return
        self.is_running = False
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=3)

        if notify:
            # 워커 스레드가 멈춘 뒤에 보낸다 — 루프의 running Ping 이 이 값을 덮어쓰지 않도록.
            _notify_shutdown(notify)
        logger.info(f"[Journal] 매매일지 연동 중지 (미전송 {pending_count()}건은 큐에 보존)")

    def trigger(self):
        """즉시 1회 순회를 깨운다 (체결 직후 지연 없이 반영하고 싶을 때)."""
        self._force_flush = True
        self._wake.set()

    def _run_loop(self):
        # 순회는 Ping 주기(10초)에 맞춰 돌되, 대기열 전송은 종전대로 30초마다만 한다.
        # (하트비트를 빠르게 하려고 전송까지 10초마다 돌리면 서버 부하만 3배가 된다)
        while self.is_running:
            try:
                now = time.time()
                if self._force_flush or (now - self._last_flush) >= _FLUSH_INTERVAL_SEC:
                    self._force_flush = False
                    flush_once()
                    self._last_flush = time.time()

                if time.time() - self._last_ping >= _PING_INTERVAL_SEC:
                    # 실패해도 _last_ping 을 갱신한다. 갱신하지 않으면 서버가 죽어 있는 동안
                    # 매 순회마다 재시도해 타임아웃으로 루프가 계속 물린다.
                    self._last_ping = time.time()
                    ping('running')

                # 백필·정리는 저빈도라 flush 뒤에 붙여 둔다. 실패해도 다음 주기에
                # 다시 시도하면 되므로 여기서 예외를 따로 잡지 않는다(루프가 삼킨다).
                if time.time() >= self._next_backfill:
                    self._next_backfill = time.time() + _BACKFILL_INTERVAL_SEC
                    # 기초잔고가 백필보다 먼저다. 연동 이전 보유분의 씨앗이 없으면
                    #  그 종목의 첫 매도가 '매수 기록 없음'으로 찍힌다.
                    opening_once()
                    queued, _ = backfill_once()
                    if queued:
                        self._force_flush = True   # 회수분은 다음 순회까지 기다리지 않는다

                if time.time() >= self._next_purge:
                    self._next_purge = time.time() + _PURGE_INTERVAL_SEC
                    purge_synced()
            except Exception as e:
                # 워커가 죽으면 큐가 영영 쌓이기만 한다 — 어떤 예외도 루프를 끊지 못하게 한다.
                logger.error(f"[Journal] 동기화 루프 오류(계속 진행): {e}")

            self._wake.wait(timeout=_TICK_INTERVAL_SEC)
            self._wake.clear()


def start():
    """앱 기동 시 호출 — 연동이 꺼져 있으면 아무 일도 하지 않는다."""
    JournalSyncWorker().start()


def stop(notify='stopped'):
    """메뉴에서 연동을 끌 때 / 프로그램 종료 시 호출 — 워커 스레드를 정리한다.

    대기열에 쌓인 미전송 건은 지우지 않는다. 다시 켜면 그대로 이어서 전송된다.
    종료 사실은 웹서버에 알려 대시보드 표시등을 즉시 '정지됨'으로 바꾼다.
    """
    JournalSyncWorker().stop(notify=notify)


def trigger():
    """체결 직후 즉시 전송을 깨운다."""
    if is_enabled():
        JournalSyncWorker().trigger()
