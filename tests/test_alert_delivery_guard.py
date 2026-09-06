"""안전장치가 꺼졌다는 경보는 **전달을 확인하고** 스로틀을 찍는다 (AST 가드).

[이 가드가 잡는 것] `api.send_telegram_message` 는 기본이 **비동기**라 전송이 실패해도
예외를 던지지 않는다. 그래서 아래 관용구는 아무것도 지키지 못한다:

    try:
        api.send_telegram_message(msg)
    except Exception:
        pass

전송이 실패해도 조용히 통과하고, 호출부는 '보냈다'로 굳는다. 대부분의 알림은 그래도
괜찮다(다음 주기에 또 온다). 문제는 **한 번만 나가는 경보**다 — 스로틀이 걸려 있거나
상태가 한 번만 바뀌는 알림은 그 한 번을 놓치면 영영 오지 않는다:

  · 미체결 취소 실패(engine) — "이 종목은 손절 판정에서 제외됩니다. 직접 취소하세요"
  · 시작 자산 이상(trader)   — "계좌 차단기가 동작하지 않습니다" (하루 1회)
  · 예약 주문 결과 불명(reserved_order_monitor) — "HTS 에서 확인하세요" (다시 발동 안 함)

이런 자리는 `telegram_notify.alert_delivered` 를 써서 **전달 여부를 돌려받아야** 한다.

[예외를 두는 법] 매 주기 다시 나가는 알림이라면 폴백이 옳다. 아래 _ALLOWED 에 사유와
함께 적는다 — 비워 두는 것이 기본이다.
"""
import ast
import os

# "파일::함수" — 전달 확인이 필요 없는 자리. 사유를 반드시 함께 적는다.
#  줄 번호로 적지 않는다 — 한 줄만 옮겨도 예외가 엉뚱한 곳을 가리킨다.
#  기준은 하나다: **사람의 조치를 요구하는가, 그리고 한 번만 나가는가.**
#  둘 다 아니면 폴백이 옳다 — 못 닿아도 다음 기회가 있고, 놓쳐도 잃는 것이 없다.
_ALLOWED: dict[str, str] = {
    'modules/auto_trade/engine.py::manage_unfilled_orders':
        "미체결 시간 초과 취소 통지 — 조치를 요구하지 않는 사후 통지이고, 취소 자체는 "
        "DB(insert_trade)와 로그에 남는다. 같은 상황이 또 오면 또 나간다.",
    'modules/auto_trade/trader.py::_reconcile_offline_transfer':
        "정지 중 입출금 자동 반영 통지 — 본문이 '조치할 것은 없습니다'라고 적는다. "
        "반영은 이미 DB 에 끝났고 리스크 기준은 파생 보정된다.",
    'modules/auto_trade/trader.py::_monitor_account_status':
        "외부 예수금 입출금 자동 감지 통지 — 위와 같은 이유. 기준선을 옮기지 않고 "
        "알리기만 하는 자리다.",
}

# 이 파일들의 경보는 '한 번만' 나가거나 안전장치 상태를 알린다.
_CRITICAL_FILES = (
    'modules/auto_trade/engine.py',
    'modules/auto_trade/trader.py',
    'modules/reserved_order_monitor.py',
    'modules/market_halt.py',
)


class _SwallowedAlerts(ast.NodeVisitor):
    """`try: ... send_telegram_message(...) ... except: pass` 를 함수 단위로 찾는다."""

    def __init__(self):
        self.hits = []          # (함수명, 줄번호)
        self._fn = []

    def visit_FunctionDef(self, node):
        self._fn.append(node.name)
        self.generic_visit(node)
        self._fn.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Try(self, node):
        swallowed = any(all(isinstance(st, ast.Pass) for st in h.body)
                        for h in node.handlers)
        if swallowed:
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else None)
                if name == 'send_telegram_message':
                    self.hits.append((self._fn[-1] if self._fn else '<module>', n.lineno))
        self.generic_visit(node)


def test_안전장치_경보는_전송_실패를_삼키지_않는다():
    offenders = []
    for rel in _CRITICAL_FILES:
        if not os.path.exists(rel):
            continue
        with open(rel, encoding='utf-8') as fh:
            src = fh.read()
        tree = ast.parse(src, rel)
        lines = src.splitlines()
        v = _SwallowedAlerts()
        v.visit(tree)
        for fname, lineno in sorted(set(v.hits), key=lambda x: x[1]):
            if f"{rel}::{fname}" in _ALLOWED:
                continue
            offenders.append(f"{rel}::{fname} (줄 {lineno})\n"
                             f"      {lines[lineno - 1].strip()[:110]}")

    assert not offenders, (
        "경보 전송을 `except: pass` 로 삼키는 자리가 있다.\n"
        "api.send_telegram_message 는 기본이 비동기라 실패해도 예외가 오지 않는다 — "
        "이 try 는 아무것도 지키지 않는다.\n"
        "한 번만 나가는 경보라면 telegram_notify.alert_delivered 로 전달을 확인하고, "
        "정말 폴백이 옳다면 _ALLOWED 에 사유와 함께 적어라.\n"
        + "\n".join(offenders))


def test_예외_목록은_실재하는_자리만_가리킨다():
    """코드가 고쳐졌는데 예외만 남으면, 그 예외가 다음 결함을 덮는다."""
    live = set()
    for rel in _CRITICAL_FILES:
        if not os.path.exists(rel):
            continue
        with open(rel, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), rel)
        v = _SwallowedAlerts()
        v.visit(tree)
        live |= {f"{rel}::{fn}" for fn, _ in v.hits}

    stale = sorted(set(_ALLOWED) - live)
    assert not stale, f"이미 사라진 자리를 가리키는 예외가 남아 있다: {stale}"


def test_가드가_실제로_탐지할_수_있다():
    """탐지기가 고장 나면 이 가드는 늘 초록이다 — 스스로를 시험한다."""
    src = (
        "def f():\n"
        "    try:\n"
        "        api.send_telegram_message('x')\n"
        "    except Exception:\n"
        "        pass\n"
    )
    v = _SwallowedAlerts()
    v.visit(ast.parse(src))
    assert v.hits == [('f', 3)]

    ok = (
        "def f():\n"
        "    try:\n"
        "        api.send_telegram_message('x')\n"
        "    except Exception as e:\n"
        "        logger.error(e)\n"
    )
    v2 = _SwallowedAlerts()
    v2.visit(ast.parse(ok))
    assert v2.hits == []
