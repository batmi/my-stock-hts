"""DB 쓰기가 **실패를 조용히 삼키지 못하게** 한다.

[왜 · 2026-09-06 감사] 같은 모양의 결함이 이 파일 안에서만 여섯 번 나왔다.

    for attempt in range(5):
        try:  ... conn.commit(); break
        except sqlite3.OperationalError: ... break
        except Exception: break
    return 0 / [] / (아무것도 안 돌려줌)

호출부는 그 0·[]·None 을 **'할 일이 없었다'**로 읽는다. 실제로 무슨 일이 벌어졌는지:

  · save_daily_asset        → 출금 기록이 사라져 90일간 가짜 드로다운이 한도를 묶는다
  · delete_trailing_stop    → 새 포지션이 이전 포지션의 고점을 물려받아 즉시 청산된다
  · insert_half_tp          → 재기동 후 이미 반쪽 판 포지션을 또 판다
  · cancel_reserved_buy_orders  → 남은 예약 매수가 발동해 같은 종목에 두 번째 진입
  · cancel_reserved_sell_orders → 보유가 없는데 매도 예약이 남아 오발동
  · cancel_other_reserved_orders → OCO 형제가 살아남아 **이중 매도**

전부 '되돌릴 수 없는 쪽'이다. 그래서 규칙은 하나다:
**쓰기 함수의 예외 처리는 결과를 돌려주거나(return), 올리거나(raise), 최소한 남긴다(log).**
아무 말 없이 `break`/`pass` 하지 않는다.
"""
import ast
import pathlib

import pytest


SRC = pathlib.Path(__file__).resolve().parent.parent / "modules" / "db_manager.py"

#  "함수명" — 조용히 넘겨도 되는 자리. 사유를 반드시 함께 적는다.
_ALLOWED: dict[str, str] = {
    "run_vacuum":
        "종료 시 최적화 — 실패해도 데이터가 달라지지 않고, atexit 에서 도는 자리라 "
        "알릴 대상도 없다. 다음 종료에 다시 시도한다.",
}

_WRITE_SQL = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _sql_head(node):
    """execute() 에 넘긴 SQL 의 앞부분. 못 읽으면 None.

    [넓힘 2026-09-06] 종전에는 ast.Constant 만 봤다. 그래서 **f-string 으로 조립한
     SQL 은 검사에서 통째로 빠졌다** — update_trade 가 정확히 그 모양이라
     (`f"UPDATE trades SET {...} WHERE {where}"`), 그 함수의 침묵한 핸들러가 이 가드를
     그냥 통과했다. 실제로 그 침묵이 물었다: 내부 호출 하나가 TypeError 를 내며 커밋
     전에 끊겼는데 화면에도 로그에도 아무 흔적이 없었다.
    """
    if not node.args:
        return None
    a = node.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.JoinedStr):
        for part in a.values:          # f-string 은 조각의 나열이다
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return part.value
            return None                # 첫 조각부터 값이면 무엇으로 시작할지 모른다
    return None


def _writes(fn):
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute"):
            head = _sql_head(n)
            if head and head.strip().upper().startswith(_WRITE_SQL):
                return True
    return False


#  '말했다'로 인정하는 호출. **화면 출력은 기록이 아니다** — 운영기는 헤드리스라
#  보는 사람이 없고, config.SCREEN_DEBUG_LEVEL 이 OFF 면 그마저도 안 찍힌다.
_SPEAKS = ("logger", "_note_write_failure", "_swallowed", "send_telegram_message",
           "alert_delivered", "warning", "error", "exception", "critical")


def _speaks(node):
    """이 문장이 흔적을 남기는가(로그·경보). 화면 출력은 세지 않는다."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "attr", None) or getattr(f, "id", None) or ""
            owner = getattr(getattr(f, "value", None), "id", "") or ""
            if name in _SPEAKS or owner in _SPEAKS:
                return True
    return False


def _is_silent(handler):
    """이 핸들러가 아무 말 없이 넘어가는가.

    재시도로 넘어가는 `continue` 는 침묵이 아니다 — 아직 포기하지 않았다는 뜻이다.
    결과를 돌려주거나(return) 올리면(raise) 호출부가 안다. 그 셋 다 아니면 **흔적을
    남겨야** 한다 — 화면에만 찍는 것은 남긴 것이 아니다(위 _SPEAKS 주석).
    """
    for stmt in handler.body:
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue)):
            return False
        if _speaks(stmt):
            return False
    return True


def _offenders():
    tree = ast.parse(SRC.read_text(encoding="utf-8"), str(SRC))
    src_lines = SRC.read_text(encoding="utf-8").splitlines()
    out = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if not _writes(fn) or fn.name in _ALLOWED:
            continue
        for h in ast.walk(fn):
            if isinstance(h, ast.ExceptHandler) and _is_silent(h):
                out.append(f"{fn.name} (줄 {h.lineno}): "
                           f"{src_lines[h.lineno - 1].strip()[:80]}")
    return out


def test_db_writes_do_not_swallow_failures_silently():
    offenders = _offenders()
    assert not offenders, (
        "DB 쓰기가 실패를 조용히 삼키는 자리가 있다.\n"
        "호출부는 그 결과를 '할 일이 없었다'로 읽고, 그 오해는 전부 되돌릴 수 없는\n"
        "쪽으로 간다(가짜 드로다운·즉시 청산·중복 진입·이중 매도).\n"
        "  고치는 법: 결과를 돌려주거나(return False/None), 올리거나(raise),\n"
        "            최소한 logger 로 남긴다.\n\n"
        + "\n".join(f"  · {o}" for o in offenders))


def test_the_allow_list_points_at_places_that_still_exist():
    """코드가 고쳐졌는데 예외만 남으면, 그 예외가 다음 결함을 덮는다."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"), str(SRC))
    live = {fn.name for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and _writes(fn)}
    stale = sorted(set(_ALLOWED) - live)
    assert not stale, f"이미 사라진 자리를 가리키는 예외가 남아 있다: {stale}"


def test_every_allowed_entry_has_a_reason():
    for name, why in _ALLOWED.items():
        assert why and len(why) > 20, f"{name}: 사유가 비어 있다"


def test_the_detector_actually_detects():
    """가드가 고장 나면 늘 초록이다 — 탐지기 자체를 시험한다."""
    bad = ast.parse(
        "def f(self):\n"
        "    try:\n"
        "        self._get_conn().cursor().execute('DELETE FROM t')\n"
        "    except Exception:\n"
        "        pass\n")
    fn = bad.body[0]
    assert _writes(fn), "쓰기 함수를 알아보지 못한다"
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert _is_silent(handlers[0]), "명백한 침묵을 놓친다"


def test_the_detector_accepts_a_logged_handler():
    ok = ast.parse(
        "def f(self):\n"
        "    try:\n"
        "        self._get_conn().cursor().execute('DELETE FROM t')\n"
        "    except Exception as e:\n"
        "        logger.error(e)\n"
        "        return False\n")
    handlers = [h for h in ast.walk(ok.body[0]) if isinstance(h, ast.ExceptHandler)]
    assert not _is_silent(handlers[0]), "정상적인 처리를 위반으로 잡는다"


def test_the_detector_treats_a_retry_as_not_silent():
    """잠금 재시도(continue)는 포기가 아니다."""
    retry = ast.parse(
        "def f(self):\n"
        "    for i in range(5):\n"
        "        try:\n"
        "            self._get_conn().cursor().execute('UPDATE t SET a=1')\n"
        "        except Exception:\n"
        "            continue\n")
    handlers = [h for h in ast.walk(retry.body[0]) if isinstance(h, ast.ExceptHandler)]
    assert not _is_silent(handlers[0])


def test_f_string_으로_조립한_SQL도_검사_대상이다(tmp_path):
    """[넓힘 자체 점검 · 2026-09-06] 이 구멍으로 update_trade 가 통째로 빠져 있었다.

    쓰기 SQL 을 f-string 으로 만드는 것은 흔한 일이고(SET 절을 조건부로 붙인다),
    그 함수들이야말로 갱신 대상이 많아 실패의 대가가 크다.
    """
    import ast as _ast
    src = ("def f(cursor, updates, where):\n"
           "    try:\n"
           "        cursor.execute(f\"UPDATE t SET {updates} WHERE {where}\", [])\n"
           "    except Exception as e:\n"
           "        break_it = 1\n")
    fn = _ast.parse(src).body[0]
    assert _writes(fn), "f-string 으로 조립한 UPDATE 를 쓰기로 보지 않는다"


def test_화면_출력만으로는_말했다고_보지_않는다():
    """운영기는 헤드리스라 보는 사람이 없고, SCREEN_DEBUG_LEVEL 이 OFF 면 그마저 없다.

    실제로 이 구멍이 물었다 — update_trade 안의 TypeError 로 UPDATE 가 커밋 전에
    끊겼는데 화면에도 로그에도 아무 흔적이 없었다.
    """
    import ast as _ast
    screen_only = _ast.parse(
        "try:\n"
        "    pass\n"
        "except Exception as e:\n"
        "    config.console.print(f'error {e}')\n").body[0].handlers[0]
    assert _is_silent(screen_only), "화면에만 찍는 핸들러를 '말했다'로 본다"

    logged = _ast.parse(
        "try:\n"
        "    pass\n"
        "except Exception as e:\n"
        "    logger.error(f'error {e}')\n"
        "    config.console.print('x')\n").body[0].handlers[0]
    assert not _is_silent(logged), "로그를 남기는데도 침묵으로 본다"
