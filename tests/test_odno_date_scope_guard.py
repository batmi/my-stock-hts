"""주문번호로 무언가를 찾거나 고치는 자리는 **날짜와 짝지어야** 한다.

[왜 이 파일이 있나 · 2026-09-06]
 증권사 주문번호(odno)는 **당일 채번**이라 매일 0부터 다시 올라간다([[odno-daily-reset]]).
 즉 odno 하나로는 유일한 열쇠가 아니다. 2026-09-04 에 check_trade_exists 가 이 사실을
 인정하고 on_date 를 받았는데, **같은 열쇠를 쓰는 형제들은 그대로였다.** 실측:

   · get_trade_by_odno  → 두 달 전 '매도' 접수 행이 오늘 매수의 '원 주문'으로 잡혔다.
     호출부는 그 행에서 type·profit_amt·score·stop_loss_rate 를 물려주므로, 오늘 낸
     매수가 남의 손익을 달고 원장에 남는다.
   · update_trade       → 그 옛 행을 **덮어썼다**:
       {'time': '2026-07-08 10:00:00', 'type': '매도', 'price': '99999',
        'qty': '3', 'profit_amt': 0, 'order_status': '체결'}
     그 날의 실현손익 -50,000 이 0 으로 사라진다. 되돌릴 수 없고, 바로 이어지는
     매매일지 재적재가 훼손된 과거 행을 웹으로 밀어 올린다.

 읽기의 오판은 다음 주기가 바로잡을 수 있지만 쓰기는 아니다. 사람이 리뷰로 잡기 어려운
 형태이므로(호출은 문법적으로 완벽하다) 구문으로 잡는다.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  날짜를 반드시 함께 받아야 하는 odno 열쇠 함수들.
_SCOPED = {
    "get_trade_by_odno",
    "update_trade",
    "check_trade_exists",
    "get_cancel_record_by_org_odno",
    "get_reserved_order_by_odno",
    "get_original_order_type",
}

#  일부러 날짜를 주지 않는 자리. **사유 없이 넣지 말 것** — 전부 '과거를 뒤지는 것이
#  목적'인 경로여야 한다.
_ALLOWED = {
    ("modules/holdings_backfill.py", "get_trade_by_odno"):
        "과거 보유분을 채워 넣는 경로다. 날짜로 좁히면 애초에 찾을 것이 없다.",
}


def _source_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", "data", "logs", "db",
                                "chart", "json", "backups", "tests")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def _calls_without_date(path):
    """그 파일에서 on_date 없이 부른 열쇠 함수 목록 [(함수명, 줄번호), ...]."""
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = getattr(node.func, "attr", None)
        if fn not in _SCOPED:
            continue
        #  정의 자체(def)는 Call 이 아니므로 여기 오지 않는다. 키워드로만 받는다 —
        #  위치 인자로 넘기면 다른 파라미터에 붙을 수 있어 인정하지 않는다.
        if any(kw.arg == "on_date" for kw in node.keywords):
            continue
        out.append((fn, node.lineno))
    return out


def test_주문번호_조회는_날짜와_짝지어_부른다():
    bad = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        if rel.replace(os.sep, "/") == "modules/db_manager.py":
            continue        # 정의부. 내부 호출은 파라미터를 그대로 넘긴다.
        for fn, line in _calls_without_date(path):
            if (rel.replace(os.sep, "/"), fn) in _ALLOWED:
                continue
            bad.append(f"{rel}:{line} — {fn}() 를 날짜 없이 부른다. "
                       f"주문번호는 당일 채번이라 그 값만으로는 유일하지 않다.")
    assert not bad, "\n  " + "\n  ".join(bad)


def test_예외_목록은_실재하는_자리만_가리킨다():
    """[낡음 자체 점검] 고쳐 놓고 예외만 남으면 그 예외가 다음 사고를 덮는다."""
    stale = []
    for (rel, fn), reason in _ALLOWED.items():
        path = os.path.join(ROOT, rel)
        assert os.path.exists(path), f"{rel} 이 없다 — 예외 목록이 낡았다"
        assert reason.strip(), f"{rel}::{fn} 예외에 사유가 없다"
        if fn not in {f for f, _ in _calls_without_date(path)}:
            stale.append(f"{rel}::{fn} — 이미 날짜를 주고 있다. 예외를 지워라")
    assert not stale, "\n  " + "\n  ".join(stale)


def test_열쇠_함수들이_실제로_날짜를_받는다():
    """목록의 이름이 어긋나면 검사가 조용히 아무것도 안 잡는다."""
    from modules import db_manager
    import inspect
    for fn in _SCOPED:
        f = getattr(db_manager.db, fn, None)
        assert f is not None, f"db_manager 에 {fn} 이 없다 — 목록이 낡았다"
        assert "on_date" in inspect.signature(f).parameters, \
            f"{fn}() 에 on_date 파라미터가 없다"


def test_검사기가_그_모양을_실제로_잡는다(tmp_path):
    """0건을 훑고 초록인 상태를 막는다."""
    sample = tmp_path / "s.py"
    sample.write_text(
        "def f(db, odno):\n"
        "    a = db.get_trade_by_odno(odno)\n"
        "    b = db.update_trade(odno, price=1, on_date='2026-01-01')\n"
        "    return a, b\n", encoding="utf-8")
    hits = _calls_without_date(str(sample))
    assert hits == [("get_trade_by_odno", 2)], hits


def test_검사기가_저장소를_실제로_훑는다():
    files = list(_source_files())
    assert len(files) > 100, f"소스 파일을 {len(files)}개밖에 못 찾았다"
