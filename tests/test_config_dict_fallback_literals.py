"""`config.<그룹>.get('KEY', <리터럴>)` 의 리터럴이 **정본 기본값과 같은가**.

[왜 이 파일이 따로 있나 · 2026-09-08]
 자매 파일 test_config_fallback_literals.py 는 `getattr(config, 'KEY', 리터럴)` 만 본다.
 그런데 이 저장소의 튜닝 값은 대부분 **그룹 딕셔너리**에 산다
 (ANALYSIS_THRESHOLDS · SELL_STRATEGY · INDICATOR_PARAMS · MARKET_REGIME_PARAMS · …).
 그 형태는 가드 밖이었고, 전수 조사에서 **32곳이 정본과 어긋나** 있었다. 그중 다수가
 '측정으로 폐기된 옛 값의 마지막 사본'이었다:

   · INDICATOR_PARAMS['OBV_MA_PERIOD']  정본 10 / 폴백 5   (5→10 은 2026-08-17 채택분)
   · SELL_STRATEGY['TS_ACTIVATION_MODE'] 정본 breakeven / 폴백 fixed  (fixed 는 폐기된 모드)
   · MARKET_REGIME_PARAMS['USE_ADAPTIVE_THRESHOLD'] 정본 False / 폴백 True
     (적응형 임계값은 측정 후 OFF 로 확정된 축이다 — 폴백이 그것을 되살린다)
   · ANALYSIS_THRESHOLDS['PYRAMIDING_MAX_COUNT'] 정본 3 / 폴백 1  (실매매 증액 한도)
   · ANALYSIS_THRESHOLDS['TREND_QUALITY_MAX'] 정본 300.0 / 폴백 0 (0 = 상한 해제)
   · RISK_SCALING_PARAMS['WHIPSAW_MIN_SCALE'] 정본 0.85 / 폴백 0.6
     — 같은 줄의 도움말이 "기본 0.85" 라고 적혀 있었다. 한 줄 안에서 서로 모순이었다.

 폴백은 키가 늘 있으니 대개 무동작이다. 위험한 건 실행이 아니라 **선언**이다 —
 읽는 사람은 그 리터럴을 기본값으로 믿고, 키를 옮기거나 이름을 바꾸는 순간 폐기된
 설정이 되살아난다. (관련: [[config-fallback-literals]] · [[config-group-dict-aliasing]])

[별칭을 따라간다] `ss = config.SELL_STRATEGY` 처럼 지역 변수로 받은 뒤 쓰는 자리가 많다.
 별칭을 안 따라가면 이 가드는 절반만 본다 — 실제로 첫 판에서 engine.py 의 네 자리와
 실매매 증액 한도(engine.py 의 at.get)를 놓쳤다. 아래 self-test 가 그 눈이 살아 있는지
 확인한다.
"""
import ast
import os

import pytest

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", ".git", ".venv", "backup", "json", "data", "tests"}

#  일부러 정본과 다른 것들. 사유를 여기 적지 않으면 통과하지 못한다.
ALLOWED = {
    #  0 은 값이 아니라 **'미설정'** 이라는 뜻이다 — 바로 위 docstring 이 그렇게 적고
    #  있고("미설정(0 이하)이면 종전처럼 콜백 배수를 그대로 쓴다"), 코드도 `if m > 0`
    #  으로 갈라 TRAILING_ATR_MULTIPLIER 로 되돌아간다. 정본(3.0)을 폴백에 넣으면
    #  '미설정'을 표현할 방법이 사라진다.
    ("modules/auto_trade/engine.py", "SELL_STRATEGY", "TS_ACTIVATION_ATR_MULTIPLIER"),
    #  TS_ARM_LATCH 는 config 에 없는 **감사 전용 노브**다. tools/audit_atr_cap_and_ts.py
    #  가 반사실 실험에서 주입하고, 그 실험의 '현행'이 False 다. 정본에 넣으면 메뉴에
    #  노출돼 운용 설정처럼 보인다.
    ("modules/portfolio_backtest.py", "SELL_STRATEGY", "TS_ARM_LATCH"),
}


CONFIG_PY = os.path.join(ROOT, "config.py")


def _declared_defaults():
    """config.py 가 **선언한** 그룹 딕셔너리 기본값. 실행 중 config 를 보지 않는다.

    [왜 소스를 파싱하나 · 2026-09-08] 처음에는 살아 있는 config 를 정본으로 삼았다.
     두 가지가 틀렸다.
       · 다른 테스트가 config 를 제자리 수정하면(그럴 권리가 있다) 이 가드가 흔들린다.
         실제로 단독 실행은 통과하고 전체 스위트에서만 실패했다.
       · 더 근본적으로, 실행 중 값에는 사용자의 dynamic_config.json 이 얹혀 있다.
         폴백 리터럴이 맞춰야 할 것은 **선언된 기본값**이지 그 사람의 현재 설정이 아니다.
         (실측: INDICATOR_PARAMS['BOX_PERIOD'] 선언 30 / 실행중 40 — 40 은 사용자 설정이다.
          살아 있는 값에 맞추려다 폴백 세 곳을 사용자 값으로 바꿀 뻔했다.)
    """
    with open(CONFIG_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for n in ast.walk(tree):
        target = value = None
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            target, value = n.target.id, n.value
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            target, value = n.targets[0].id, n.value
        if not target or not target[0].isupper() or not isinstance(value, ast.Dict):
            continue
        group = out.setdefault(target, {})
        for k, v in zip(value.keys, value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            try:
                group[k.value] = ast.literal_eval(v)
            except Exception:
                pass
    return out


DECLARED = _declared_defaults()


def _is_config_dict(name):
    """config.<NAME> 이 딕셔너리인가.

    그룹 딕셔너리는 프록시로 노출돼 vars(config) 에 안 보인다([[config-group-dict-aliasing]]).
    그래서 이름 목록을 만들지 않고 getattr 로 묻는다 — 처음에 vars() 로 짰다가 검사
    대상이 0곳이 되는 것을 self-test 가 잡았다.
    """
    if not name or not name[0].isupper():
        return False
    try:
        return isinstance(getattr(config, name), dict)
    except Exception:
        return False


def _alias_map(scope_node):
    """이 스코프 안에서 `X = config.DICT` 로 만든 별칭 → 딕셔너리 이름."""
    out = {}
    for n in ast.walk(scope_node):
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t, v = n.targets[0], n.value
        if not isinstance(t, ast.Name):
            continue
        if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                and v.value.id == "config" and _is_config_dict(v.attr)):
            out[t.id] = v.attr
    return out


def _scan(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    hits = set()
    for scope in scopes:
        aliases = _alias_map(scope)
        for n in ast.walk(scope):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and len(n.args) == 2):
                continue
            base = n.func.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and base.value.id == "config" and _is_config_dict(base.attr):
                dic = base.attr
            elif isinstance(base, ast.Name) and base.id in aliases:
                dic = aliases[base.id]
            else:
                continue
            key, default = n.args
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not isinstance(default, ast.Constant):
                continue
            hits.add((dic, key.value, default.value, n.lineno))
    return hits


def _repo_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _all_hits():
    out = []
    for path in _repo_files():
        rel = os.path.relpath(path, ROOT)
        try:
            hits = _scan(path)
        except SyntaxError:
            continue
        for dic, key, lit, line in hits:
            out.append((rel, dic, key, lit, line))
    return out


# ---------------------------------------------------------------------------
# 검사기 자신 — 아무것도 안 보고 통과하는 일이 없게
# ---------------------------------------------------------------------------

def test_the_declarations_are_read_from_the_source_not_the_live_module():
    """정본은 config.py 의 선언이다 — 실행 중 값(사용자 설정이 얹힌 것)이 아니다."""
    for name in ("ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS",
                 "MARKET_REGIME_PARAMS", "RISK_SCALING_PARAMS"):
        assert len(DECLARED.get(name, {})) > 3, f"config.py 에서 {name} 선언을 못 읽었다"


def test_the_scan_actually_sees_the_group_dictionaries():
    """프록시로 노출되는 그룹 딕셔너리를 못 보면 이 파일 전체가 무력하다."""
    for name in ("ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS",
                 "MARKET_REGIME_PARAMS", "RISK_SCALING_PARAMS"):
        assert _is_config_dict(name), f"config.{name} 을 딕셔너리로 못 본다"


def test_the_scan_finds_a_meaningful_number_of_sites():
    hits = _all_hits()
    assert len(hits) > 200, f"검사 대상이 {len(hits)}곳뿐이다 — 탐지기가 눈을 감았다"


def test_the_scan_follows_local_aliases():
    """`ss = config.SELL_STRATEGY` 뒤의 ss.get(...) 도 봐야 한다.

    안 따라가면 engine.py 의 청산 축 네 자리를 통째로 놓친다(첫 판에서 실제로 놓쳤다).
    """
    hits = {(dic, key) for _rel, dic, key, _lit, _l in _all_hits()}
    assert ("SELL_STRATEGY", "TS_ACTIVATION_MODE") in hits, \
        "별칭(ss.get)을 통한 참조를 못 본다"


# ---------------------------------------------------------------------------
# 본 검사
# ---------------------------------------------------------------------------

def test_every_dict_fallback_matches_the_canonical_default():
    offenders = []
    for rel, dic, key, lit, line in sorted(_all_hits()):
        if (rel, dic, key) in ALLOWED:
            continue
        src = DECLARED.get(dic)
        if not src:
            continue
        if key not in src:
            offenders.append(f"{rel}:{line}  config.{dic}['{key}'] — 정본에 없는 키 "
                             f"(폴백 {lit!r} 이 영구 기본값이 된다)")
        elif not _same(src[key], lit):
            offenders.append(f"{rel}:{line}  config.{dic}['{key}'] "
                             f"정본={src[key]!r} 폴백={lit!r}")
    assert not offenders, (
        "폴백 리터럴이 정본과 다릅니다. 일부러 다르면 ALLOWED 에 **사유와 함께** 넣으세요:\n"
        + "\n".join(offenders))


@pytest.mark.parametrize("entry", sorted(ALLOWED))
def test_each_allowance_still_points_at_real_code(entry):
    """면제가 낡으면 조용히 구멍이 된다 — 그 자리가 아직 있는지 확인한다."""
    rel, dic, key = entry
    path = os.path.join(ROOT, rel)
    assert os.path.exists(path), f"{rel} 이 사라졌다 — 면제를 지우세요"
    hits = {(d, k) for d, k, _lit, _l in _scan(path)}
    assert (dic, key) in hits, \
        f"{rel} 에 config.{dic}['{key}'] 폴백이 더는 없다 — 면제를 지우세요"
