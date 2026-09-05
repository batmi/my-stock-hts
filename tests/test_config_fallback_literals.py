"""`getattr(config, 'KEY', <리터럴>)` 의 리터럴이 **정본 기본값과 같은가**.

[왜 이 파일이 있나 · 2026-09-05]
이 저장소가 같은 형태로 세 번 물렸다.
 · `TRAILING_ATR_MULTIPLIER` — 백테스트 폴백 3.0 / 실매매 3.5 (2026-08-23 수정)
 · 시간청산 기한 폴백 리터럴 10 / 정본 15 (telegram_bot, 2026-09-04 수정)
 · TPS 밴드 폴백 [0.85, 0.98] — **2026-08-09 에 '실측 무릎보다 밴드 전체가 위라
   컨트롤러가 4일 내내 하한에 눌려 있었다'는 이유로 폐기된 값**이 api/http.py 에
   그대로 남아 있었다. 그 값들의 마지막 사본이 폴백 리터럴이었다.

폴백은 키가 늘 있으니 대개 무동작이다 — 그래서 조용히 낡는다. 위험한 건 실행이 아니라
**선언**이다: 읽는 사람은 그 리터럴을 기본값으로 믿고, 키를 옮기거나 이름을 바꾸는 순간
폐기된 설정이 되살아난다. 값이 갈라질 자리를 없애는 편이 값을 맞추는 것보다 낫지만,
폴백 자체는 방어로 남겨야 하므로 **같은지를 테스트가 지킨다.**

관련: [[config-mode-profiles]] · [[residual-dials-closed]]
"""
import ast
import os

import pytest

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  일부러 다른 것들. 사유를 여기 적지 않으면 통과하지 못한다.
ALLOWED = {
    #  백테스트는 실매매 전용 게이트를 **인자로 받을 때만** 켠다. 안 넘기면 꺼진 채로
    #  도는 것이 종전 동작이고, 그 사실은 _warn_missing_live_hooks 가 따로 알린다
    #  ([[reimplementation-parity-gaps]] ③).
    ("modules/portfolio_backtest.py", "SYSTEM_DAILY_LOSS_LIMIT"),
    ("modules/portfolio_backtest.py", "SYSTEM_ENTRY_OPEN_DELAY_USE"),
    ("modules/portfolio_backtest.py", "SYSTEM_ENTRY_OPEN_DELAY_MINUTES"),
    ("modules/portfolio_backtest.py", "MAX_POSITION_OVERSHOOT"),
    #  비용 0 은 '비용 모름'이 아니라 '슬리피지 미적용'이라는 뜻으로 쓰인다
    #  (관찰모드·백테스트가 각자 배수를 넘긴다).
    ("core/trading_cost.py", "SLIPPAGE_RATE"),
}

#  값이 아니라 환경·경로인 키. 폴백이 정본과 다른 것이 정상이다
#  (정본은 절대경로·환경변수 주입값이고 폴백은 상대경로·빈 문자열이다).
ENV_KEYS = {
    "DATA_DIR", "JSON_DIR", "LOG_DIR", "DB_FILE_PATH", "PAPER_DB_FILE_PATH",
    "SYSTEM_TRADING_LOG_DIR", "CHART_DIR",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DART_API_KEY",
    "TV_USERNAME", "TV_PASSWORD", "JOURNAL_API_URL", "JOURNAL_API_KEY",
    "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL",
    "KRX_ID", "KRX_PW", "ACTIVE_PRESET", "session",
}

_SENTINEL = object()


def _declared_defaults():
    """정본 기본값: GlobalSettings 필드 기본값 + config.py 모듈 최상단 리터럴 대입."""
    out = {}
    inst = config.GlobalSettings()
    for name in type(inst).model_fields:
        out[name] = getattr(inst, name)
    tree = ast.parse(open(os.path.join(ROOT, "config.py"), encoding="utf-8").read())
    for node in tree.body:                       # 최상단만 — 함수 안의 지역 대입은 아니다
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        value = getattr(node, "value", None)
        if value is None:
            continue
        try:
            v = ast.literal_eval(value)
        except Exception:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                out.setdefault(t.id, v)
    return out


def _fallback_sites():
    """(상대경로, 줄번호, 키, 폴백값) 목록. tests/ 와 tools/ 는 제외."""
    sites = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", "data", "logs", "db",
                                "tests", "tools", "chart", "json", "backups")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ROOT)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:                  # pragma: no cover
                continue
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and len(n.args) == 3):
                    continue
                f = n.func
                if not (isinstance(f, ast.Name) and f.id == "getattr"):
                    continue
                obj = n.args[0]
                name = getattr(obj, "id", None) or (
                    obj.attr if isinstance(obj, ast.Attribute) else None)
                if name not in ("config", "settings"):
                    continue
                try:
                    key = ast.literal_eval(n.args[1])
                    dflt = ast.literal_eval(n.args[2])
                except Exception:
                    continue
                if isinstance(key, str):
                    sites.append((rel, n.lineno, key, dflt))
    return sites


def test_폴백_리터럴이_정본_기본값과_같다():
    """[핵심] 폴백이 낡으면 그 리터럴이 폐기된 설정의 마지막 사본이 된다."""
    defaults = _declared_defaults()
    bad = []
    for rel, lineno, key, dflt in _fallback_sites():
        if key in ENV_KEYS or (rel, key) in ALLOWED or key not in defaults:
            continue
        real = defaults[key]
        if isinstance(real, (dict, list, set)):
            continue
        if dflt != real:
            bad.append(f"{rel}:{lineno}  {key}: 폴백 {dflt!r} ≠ 정본 {real!r}")
    assert not bad, (
        "폴백 리터럴이 정본 기본값과 다릅니다. 값을 맞추거나, 일부러 다르면 "
        "ALLOWED 에 사유와 함께 등록하세요:\n  " + "\n  ".join(bad))


def test_스캐너가_실제로_무언가를_보고_있다():
    """0건을 훑고 초록인 상태를 막는다."""
    sites = _fallback_sites()
    assert len(sites) > 50, f"폴백 사용처를 {len(sites)}건밖에 못 찾았다 — 스캐너가 고장났다"
    assert any(k == "TARGET_VOLATILITY" for _r, _l, k, _d in sites)


@pytest.mark.parametrize("path, key", sorted(ALLOWED))
def test_예외_목록은_실제로_존재하는_자리다(path, key):
    """고쳐서 사라진 예외가 목록에 남아 있으면 다음 사람이 잘못된 지도를 읽는다."""
    assert any(r == path and k == key for r, _l, k, _d in _fallback_sites()), \
        f"ALLOWED 에 {path}/{key} 가 있는데 그런 폴백이 없다 — 목록에서 지우세요"
