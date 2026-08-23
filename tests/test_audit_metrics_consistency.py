"""감사 도구의 지표가 **하나의 자**로 계산되는가.

[왜] tools/ 의 감사 스크립트는 81개이고, 그중 24개가 각자 `metrics()` 를 따로 정의한다.
 2026-08-23 시점에 그 24벌은 **서로 완전히 같은 것이 하나도 없었다**(AST 해시 전수 비교).
 대부분은 고유 항목이 덧붙은 차이지만, 공통 항목(수익·MDD·MAR·PF·승률·꼬리)까지 각자
 다시 계산한다. 같은 이름의 수치가 도구마다 다른 산식으로 나오면 도구 간 비교가 성립하지
 않고, 그 비교 위에 파라미터 채택 결정이 서 있다.

 이 저장소는 이미 계측기 층에서 두 번 결함을 겪었다 — make_scale_fn 오염(6개 도구),
 청산 표본 오염(3개 도구). 둘 다 '도구마다 자기 계산을 갖고 있어서' 생긴 일이다.

[이 테스트가 지키는 것]
 ① 모든 감사 도구의 metrics() 는 공통 항목에서 audit_common.base_metrics 와 같은 값을
    내야 한다. 고유 항목은 자유다.
 ② 청산 표본은 audit_common.exits 로만 고른다 — 사유 어휘를 손으로 나열한 사본이
    다시 생기면 잡는다. (실제로 사본 19벌 중 17벌에 "교체"가 빠져 있었다.)

[합성 결과 dict 의 규약] 시뮬레이터가 실제로 돌려주는 모양을 흉내내되, **내부적으로
 모순이 없어야** 한다. r["win"]/r["loss"] 는 시뮬레이터가 sells 에서 직접 세는 값이므로
 (portfolio_backtest.run_portfolio), 여기서도 표본에서 파생시킨다. 임의로 넣으면 승률
 정의가 다르다는 거짓 불일치가 난다.
"""
import ast
import glob
import importlib
import math
import os

import pytest

from tools.audit_common import base_metrics, exits, windows

# metrics() 의 이름만 다른 동의어. 값은 같아야 한다.
ALIAS = {"wr": "win"}

# 시그니처가 다른 도구 — 포트폴리오 결과 dict 하나를 받는 형태가 아니다.
#  (audit_axis_combination.metrics 는 지수 시계열·노출도를 함께 받는 별개 계산이다.)
SKIP = {"audit_axis_combination.py"}


def _synth_result(with_rotation=False):
    """run_portfolio 결과를 흉내낸 self-consistent 한 dict."""
    reasons = ["손절", "ATR손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호"]
    trades = []
    for i, rs in enumerate(reasons * 3):
        p = (i % 7 - 3) * 7.5
        trades.append({"code": f"C{i:03d}", "date": f"2024{(i % 12) + 1:02d}15",
                       "reason": rs, "profit": p, "profit_amt": int(p * 1000),
                       "days": 5 + i % 30, "mfe": abs(p) + 8.0,
                       "armed": i % 2 == 0, "bep": i % 3 == 0,
                       "qty": 10, "price": 1000.0, "amount": 10000.0})
    if with_rotation:
        for j in range(3):
            trades.append({"code": f"R{j:03d}", "date": f"20240{j + 4}20", "reason": "교체",
                           "profit": 18.0 + j, "profit_amt": (18 + j) * 1000,
                           "days": 40 + j, "mfe": 26.0 + j, "armed": True, "bep": False,
                           "qty": 10, "price": 1000.0, "amount": 10000.0})
    # 증액은 청산이 아니다 — profit_amt 가 정확히 0이라 어느 규칙으로도 걸리지 않아야 한다.
    trades.append({"code": "P001", "date": "20240310", "reason": "증액", "profit": 0.0,
                   "profit_amt": 0, "days": 0, "mfe": 0.0, "armed": False, "bep": False,
                   "qty": 5, "price": 1000.0, "amount": 5000.0})

    sells = [t for t in trades if t["reason"] != "증액"]
    return {
        "total_return": 41.7, "mdd": -18.3, "pf": 1.62,
        "trades": trades, "sells": sells,
        # 시뮬레이터와 같은 방식으로 센다 (run_portfolio 의 win/loss 정의)
        "win": sum(1 for t in sells if t["profit_amt"] > 0),
        "loss": sum(1 for t in sells if t["profit_amt"] <= 0),
        "avg_slots": 3.1, "rotations": 3 if with_rotation else 0,
        "intraday_exits": 4, "intraday_mismatch": 0, "win_rate": 52.4,
        "final_capital": 14170000, "cagr": 12.3, "exposure": 0.71,
        "buys": 21, "pyramids": 1, "equity": [1.0, 1.2, 0.95, 1.417],
        "blocked": 0, "skipped": 0, "days": 730, "slots_used": 3.1,
    }


def _tools_with_metrics():
    out = []
    for path in sorted(glob.glob("tools/audit_*.py")):
        name = os.path.basename(path)
        if name in SKIP:
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        if any(isinstance(n, ast.FunctionDef) and n.name == "metrics" for n in tree.body):
            out.append(name)
    return out


TOOLS = _tools_with_metrics()


def _close(a, b):
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) < 1e-9
    return a == b


def test_tool_discovery_is_not_empty():
    """도구 탐색이 무너지면 아래 테스트가 통째로 공짜 통과한다."""
    assert len(TOOLS) >= 20, f"metrics() 를 가진 감사 도구가 {len(TOOLS)}개뿐 — 탐색이 깨졌다"


@pytest.mark.parametrize("tool", TOOLS)
def test_metrics_agrees_with_common_ruler(tool):
    """도구별 metrics() 의 공통 항목이 base_metrics 와 같은 값을 내야 한다."""
    module = importlib.import_module("tools." + tool[:-3])
    fn = getattr(module, "metrics", None)
    assert fn is not None, f"{tool}: metrics() 를 찾지 못했다"

    r = _synth_result()
    base = base_metrics(r)
    got = fn(r)

    bad = [f"{k}={v!r} (공통 자: {base[ALIAS.get(k, k)]!r})"
           for k, v in got.items()
           if ALIAS.get(k, k) in base and not _close(v, base[ALIAS.get(k, k)])]
    assert not bad, (
        f"{tool} 의 metrics() 가 공통 자와 다른 값을 낸다 — 도구 간 비교가 성립하지 않는다:\n  "
        + "\n  ".join(bad))


@pytest.mark.parametrize("tool", TOOLS)
def test_metrics_counts_rotation_exits(tool):
    """회전(교체) 청산이 표본에 들어가는가.

    사유 어휘를 손으로 나열한 사본은 새 사유가 늘 때 조용히 놓친다. 실제로 사본 17벌이
    "교체"를 빠뜨리고 있었고, 회전을 켠 감사에서 그 청산이 표본에서 사라졌다.
    """
    module = importlib.import_module("tools." + tool[:-3])
    fn = module.metrics
    plain, rotated = fn(_synth_result(False)), fn(_synth_result(True))
    if "n" not in plain:
        pytest.skip(f"{tool}: 표본 수(n)를 내지 않는다")
    assert plain["n"] != rotated["n"], (
        f"{tool} 이 '교체' 청산을 표본에서 놓친다 — 사유를 손으로 나열하지 말고 "
        f"audit_common.exits(r) 를 쓸 것")


def test_no_tool_redefines_the_exit_vocabulary():
    """감사 도구가 청산 사유 어휘를 자기 파일에서 다시 정의하면 안 된다."""
    offenders = []
    for path in sorted(glob.glob("tools/audit_*.py")):
        if path.endswith("audit_common.py"):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:
            targets = getattr(node, "targets", [])
            if any(isinstance(t, ast.Name) and t.id == "SELL_REASONS" for t in targets):
                offenders.append(os.path.basename(path))
    assert not offenders, (
        "청산 사유 어휘는 audit_common.SELL_REASONS 하나뿐이어야 한다. 사본: "
        + ", ".join(offenders))


def test_exits_excludes_pyramid_rows():
    """증액(피라미딩) 행은 청산 표본이 아니다 — 꼬리 지표를 무너뜨린다."""
    r = _synth_result()
    assert all(t["reason"] != "증액" for t in exits(r))
    assert base_metrics(r)["n"] == len(r["sells"])


# ---------------------------------------------------------------------------
# 구간 분할
# ---------------------------------------------------------------------------
def _legacy_split_a(dates, k):
    """종전 변형 A — k>1 이면 구간만, k<=1 이면 전체 하나."""
    k = max(1, k)
    size = len(dates) // k
    return ([(f"구간{i + 1}", list(dates[i * size:(i + 1) * size if i < k - 1 else len(dates)]))
             for i in range(k)] if k > 1 else [("전체", list(dates))])


def _legacy_split_b(dates, k):
    """종전 변형 B — 항상 전체를 앞에 붙인다."""
    k = max(1, k)
    size = max(1, len(dates) // k)
    return [("전체", list(dates))] + [
        (f"구간{i + 1}", list(dates[i * size:(i + 1) * size if i < k - 1 else len(dates)]))
        for i in range(k)]


@pytest.mark.parametrize("k", range(1, 9))
def test_windows_reproduces_legacy_splits(k):
    """공용 windows() 가 종전 두 변형과 **경계까지 같아야** 한다.

    이 등가성이 깨지면 구간 수치가 옛 기록과 비교 불가능해진다 — 이 저장소의 감사
    결론 상당수가 구간별 승패로 판정돼 있으므로, 조용히 바뀌면 안 된다.
    """
    for n in (37, 120, 253, 399):
        dates = list(range(n))
        assert windows(dates, k) == _legacy_split_a(dates, k), f"A형 불일치 n={n} k={k}"
        assert windows(dates, k, whole=True) == _legacy_split_b(dates, k), f"B형 불일치 n={n} k={k}"


def test_windows_last_chunk_absorbs_remainder():
    """마지막 구간이 나머지를 흡수한다 — 표본이 잘려나가면 안 된다."""
    dates = list(range(253))
    covered = [d for _label, chunk in windows(dates, 4) for d in chunk]
    assert covered == dates
