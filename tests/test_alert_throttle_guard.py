"""스로틀·표식은 **전달을 확인한 뒤에** 찍는다 — 코드 전체를 세는 가드.

[왜 가드가 필요한가] 이 규칙은 2026-09-04 에 alert_delivered 를 만들며 세웠는데,
 그 뒤로도 같은 모양이 여섯 곳에서 더 나왔다(2026-09-07):
   토큰 갱신 지연·복구 · 하트비트 이상 · DB 쓰기 실패 · 서버 장애 진입/복구 ·
   반복 오류 · 방어 모드 발동.
 전부 "보내기 전에 표식을 찍고, 비동기 전송이라 실패는 예외로 오지 않는다"였다.
 사람이 매번 찾는 대신 여기서 센다.

[무엇을 잡는가] **같은 문장 블록** 안에서
   ① send_telegram_message 를 직접 부르고(alert_delivered 를 거치지 않고)
   ② 스로틀성 이름(_alerted_at · _notified · _alert_sent · _last_problem_msg 같은 꼴)에
      대입한다
면 그 자리는 '못 닿아도 침묵'이 된다. 실제 여섯 건이 전부 이 모양이었다 —
같은 if 몸통에서 표식을 찍고 곧바로 보낸다.

[왜 함수 단위가 아니라 블록 단위인가] 처음에는 함수 단위로 셌더니 _run_loop 처럼 400줄에
 알림이 여럿 든 함수에서 **다른 알림의 표식**이 섞여 오탐이 났다. 블록 단위면 '찍고 바로
 보낸다'는 모양만 남는다.

[무엇을 잡지 않는가] 스로틀 없이 그냥 알리는 자리(매 건 알림)는 대상이 아니다 —
 못 보내면 다음 건에서 다시 보내므로 영구 침묵이 아니다. 그래서 ②를 함께 본다.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN = ("modules", "api", "core")

#  '한 번 찍으면 그 창 동안 다시 안 보낸다'는 뜻을 가진 이름들.
THROTTLE_HINTS = ("_alerted_at", "_alert_sent", "_notified", "_last_problem_msg",
                  "_alert_date", "_alert_time", "_fail_alerted", "notified_ts")

#  예외로 남기는 자리와 그 이유. 지울 때는 이유가 사라졌는지 먼저 볼 것.
ALLOW = {
    # (파일, 함수): 이유
    ("modules/telegram_notify.py", "alert_delivered"):
        "이 함수가 전달 확인 그 자체다 — 여기서 send 를 부르는 것이 정의다.",
}


def _raw_send_calls(node):
    """이 노드 아래의 직접 전송 호출 줄번호(alert_delivered 를 거치지 않은 것)."""
    out = []
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        name = (c.func.attr if isinstance(c.func, ast.Attribute)
                else c.func.id if isinstance(c.func, ast.Name) else '')
        if name == 'send_telegram_message':
            out.append(c.lineno)
    return out


def _throttle_marks(node):
    """이 노드 아래의 스로틀성 대입 (줄번호, 이름)."""
    out = []
    for a in ast.walk(node):
        targets = (a.targets if isinstance(a, ast.Assign)
                   else [a.target] if isinstance(a, ast.AnnAssign) else [])
        for t in targets:
            nm = (t.attr if isinstance(t, ast.Attribute)
                  else t.id if isinstance(t, ast.Name) else '')
            if isinstance(nm, str) and any(h in nm for h in THROTTLE_HINTS):
                out.append((a.lineno, nm))
    return out


#  중첩 블록을 가진 문장들. 이것들은 **자기 블록으로 따로** 센다.
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
             ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _blocks(fn):
    """함수 안의 모든 문장 블록(몸통 리스트). 각 블록은 한 번씩만 나온다."""
    out, seen = [], set()
    for node in ast.walk(fn):
        for field in ('body', 'orelse', 'finalbody'):
            b = getattr(node, field, None)
            if isinstance(b, list) and b and isinstance(b[0], ast.stmt) and id(b) not in seen:
                seen.add(id(b))
                out.append(b)
    return out


def _block_offenders(fn):
    """'같은 블록에서 표식을 찍고 곧바로 보낸다'는 자리만 고른다.

    블록의 **직속 단순 문장**만 본다 — 중첩된 if/try 안쪽은 그 블록에서 따로 센다.
    (ast.walk 로 내려가면 400줄짜리 루프의 top-level body 가 함수 전체를 삼켜
     남의 알림 표식이 섞인다. 실제로 그렇게 오탐이 났다.)
    """
    hits = []
    for block in _blocks(fn):
        sends, marks = [], []
        for stmt in block:
            if isinstance(stmt, _COMPOUND):
                continue                        # 자기 블록에서 센다
            sends += _raw_send_calls(stmt)
            marks += _throttle_marks(stmt)
        if sends and marks:
            hits.append((min(sends), sorted({m for _, m in marks})))
    return hits


def _offenders():
    bad = []
    for d in SCAN:
        for path in sorted((ROOT / d).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:                     # pragma: no cover
                continue
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                if (rel, fn.name) in ALLOW:
                    continue
                for ln, marks in _block_offenders(fn):
                    bad.append((rel, fn.name, ln, marks))
    return bad


def test_스로틀을_찍는_자리는_전달을_확인한다():
    bad = _offenders()
    assert not bad, (
        "표식을 찍으면서 전달을 확인하지 않는 자리 — 못 닿으면 그 창 동안 침묵한다.\n"
        "alert_delivered(...) 가 True 일 때만 표식을 찍도록 고칠 것:\n"
        + "\n".join(f"  {p}:{ln}  {f}()  표식={m}" for p, f, ln, m in bad))


def test_탐지기_자체가_동작한다(tmp_path):
    """가드가 아무것도 못 찾으면 조용히 통과한다 — 그 상태를 먼저 막는다."""
    import textwrap

    src = textwrap.dedent('''
        def f(self):
            api.send_telegram_message("x")
            self._foo_alerted_at = 1
    ''')
    tree = ast.parse(src)
    fn = tree.body[0]
    assert _raw_send_calls(fn), "직접 전송 호출을 못 찾는다"
    assert _throttle_marks(fn) == [(4, '_foo_alerted_at')], "스로틀 표식을 못 찾는다"
    assert _block_offenders(fn), "같은 블록에서 찍고 보내는 모양을 못 잡는다"


def test_alert_delivered_는_대상이_아니다():
    """전달을 확인하는 호출은 이 규칙을 이미 지킨 것이다."""
    import textwrap

    src = textwrap.dedent('''
        def f(self):
            if alert_delivered("x"):
                self._foo_alerted_at = 1
    ''')
    fn = ast.parse(src).body[0]
    assert _raw_send_calls(fn) == [], "alert_delivered 를 직접 전송으로 셌다"
    assert _block_offenders(fn) == []


def test_스로틀이_없으면_대상이_아니다():
    """매 건 알림은 못 보내도 다음 건에서 다시 보낸다 — 영구 침묵이 아니다."""
    import textwrap

    src = textwrap.dedent('''
        def f(self):
            api.send_telegram_message("체결 알림")
            self.last_price = 100
    ''')
    fn = ast.parse(src).body[0]
    assert _raw_send_calls(fn) and _throttle_marks(fn) == []
    assert _block_offenders(fn) == []


def test_다른_알림의_표식은_섞이지_않는다():
    """함수 단위로 세면 400줄짜리 루프에서 남의 표식이 섞여 오탐이 난다(실제로 났다)."""
    import textwrap

    src = textwrap.dedent('''
        def f(self):
            if a:
                if alert_delivered("경보"):
                    self._foo_alerted_at = 1
            if b:
                api.send_telegram_message("다른 알림")
    ''')
    fn = ast.parse(src).body[0]
    assert _raw_send_calls(fn) and _throttle_marks(fn), "표본이 두 조건을 다 담아야 한다"
    assert _block_offenders(fn) == [], "블록이 다른데 같은 자리로 셌다"
