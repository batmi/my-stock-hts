"""오늘의 '시작 자산'(계좌 차단기 분모 · 사이징 기준)이 그럴듯한 값인가.

[왜 이 테스트인가] 이 값의 저장 조건은 `tot_asset > 0` 뿐이었다. 어떤 양수든 그날의
기준이 되고, 한 번 저장되면 load 가 그대로 돌려주므로 하루 종일 고정된다. 기준선이
실제보다 작게 박히면 손실률이 늘 큰 양수로 계산돼 **차단기가 종일 발동하지 않는다** —
아무도 모르는 채로 보호 장치만 사라지는, 가장 나쁜 상태다.

코드는 이 실패 모드를 이미 알고 있었다. engine.check_loss_limit 의 주석이 "증권사 API
통신 오류로 주식 평가액이 0으로 수신되어 예수금만 계산될 때"라고 적어 두고 current_total 을
거른다. 정작 더 위험한 쪽(하루 고정되는 기준선)에는 같은 가드가 없었다.
"""
import pytest

from modules.auto_trade.common import BASELINE_SANITY_RATIO, is_plausible_baseline

KEY = "12345678-01"


def test_normal_day_passes():
    assert is_plausible_baseline(KEY, 10_000_000, last_known=10_200_000)


def test_ordinary_drawdown_still_passes():
    """평범한 손실까지 막으면 정상 운용이 기준선 없이 돌아간다."""
    assert is_plausible_baseline(KEY, 8_500_000, last_known=10_000_000)


def test_quote_outage_shape_is_rejected():
    """주식 평가액이 0으로 와서 예수금만 잡힌 형태 — 이게 실제 실패 모드다."""
    assert not is_plausible_baseline(KEY, 1_500_000, last_known=10_000_000)


def test_boundary_is_inclusive():
    last = 10_000_000
    assert is_plausible_baseline(KEY, int(last * BASELINE_SANITY_RATIO), last_known=last)
    assert not is_plausible_baseline(KEY, int(last * BASELINE_SANITY_RATIO) - 1, last_known=last)


def test_zero_and_negative_never_pass():
    for v in (0, -1, -10_000_000):
        assert not is_plausible_baseline(KEY, v, last_known=10_000_000)


@pytest.mark.parametrize("last", [None, 0])
def test_no_history_passes(last):
    """첫 운용이면 대조할 근거가 없다 — 막으면 아무도 기준선을 못 세운다."""
    assert is_plausible_baseline(KEY, 10_000_000, last_known=last)


def test_growth_is_never_suspicious():
    """입금·수익으로 늘어난 것은 의심 대상이 아니다."""
    assert is_plausible_baseline(KEY, 50_000_000, last_known=10_000_000)


def test_small_but_consistent_account_passes():
    """실제로 작은 계좌(모의·테스트)는 정상이다 — 절대 금액으로 자르지 않는다.

    운영 DB에 27원짜리 계좌가 실재한다(10,027 → 27). 절대 하한을 두면 그런 계좌가
    통째로 막히므로, 판단은 '직전 대비'로만 한다.
    """
    assert is_plausible_baseline(KEY, 27, last_known=27)
    assert not is_plausible_baseline(KEY, 27, last_known=10_027)


# ==========================================================
# 위쪽 이상치 — 거부하지 않고 보이게만 한다 (2026-09-01)
#
# [실측] 가상투자 자산 이력 2026-08-23 행이 10,028,670 → 20,028,670 이었다. 차이가 정확히
# 시드(1,000만)라 자산에 시드가 한 번 더 더해진 것이다. 그 행 하나로 자산 고점이 두 배가
# 되고 드로다운이 -49.98% 로 계산된다(실데이터 재현). get_max_daily_asset 의 고립 이상치
# 제거가 잡아 -1.05% 로 끝났다.
#
# 그래도 **거부하지는 않는다.** 거부하면 정당한 입금 다음 날 기준선이 옛 값으로 굳어
# 차단기가 종일 안 터진다 — 이 함수가 막으려던 바로 그 실패 모드다. 드문 중복 계상을
# 막자고 입금일마다 보호 장치를 끄는 것은 남는 장사가 아니다.
# ==========================================================

def test_a_doubling_still_passes_but_is_logged(caplog):
    """[핵심] 실측 그대로의 값 — 통과시키되 로그에는 남는다."""
    import logging
    with caplog.at_level(logging.WARNING, logger="modules.auto_trade.common"):
        assert is_plausible_baseline(KEY, 20_028_670, last_known=10_028_670)
    assert any("daily_asset_history" in r.message for r in caplog.records), \
        "중복 계상 의심이 조용히 지나갔다"


def test_a_normal_day_logs_nothing(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="modules.auto_trade.common"):
        assert is_plausible_baseline(KEY, 10_100_000, last_known=10_000_000)
    assert not caplog.records, "정상 운용 중에 경고를 쏟으면 아무도 안 읽는다"


# ===========================================================================
# 저장 실패는 조용하면 안 된다 (2026-09-05)
#
# save_daily_initial_asset 은 jsonio.save_json 의 bool 반환을 버렸다. 그러면 그 세션
# 동안은 메모리 값으로 정상 동작해 아무도 모르고, **재기동해야 소실이 드러난다**.
#
# 이 파일은 일일 손실 한도(비상 정지)의 분모이자 드로다운 리스크 스케일링의 기준선이다.
# 잃으면 그날의 낙폭이 조용히 사라지고 차단기가 리셋된다 —
# paper_broker._clear_daily_baseline 의 주석이 같은 사고를 '파일을 지우면'으로 적어 뒀는데,
# 저장 실패는 지운 것과 같은 결과다. 운영기는 램 1GB·SD 카드 라즈베리파이라
# 디스크 가득참·IO 오류가 실재한다.
# ===========================================================================
def test_저장_실패를_성공으로_보고하지_않는다(monkeypatch, tmp_path):
    from core import jsonio
    from modules.auto_trade import common

    monkeypatch.setattr(common, "DAILY_STATE_FILE", str(tmp_path / "daily.json"))
    monkeypatch.setattr(jsonio, "save_json", lambda *a, **k: False)
    assert common.save_daily_initial_asset(KEY, 10_000_000) is False


def test_저장_실패가_로그에_남는다(monkeypatch, tmp_path, caplog):
    from core import jsonio
    from modules.auto_trade import common

    monkeypatch.setattr(common, "DAILY_STATE_FILE", str(tmp_path / "daily.json"))
    monkeypatch.setattr(jsonio, "save_json", lambda *a, **k: False)
    with caplog.at_level("ERROR", logger=common.__name__):
        common.save_daily_initial_asset(KEY, 10_000_000)
    assert "기준선" in caplog.text and "재기동" in caplog.text, caplog.text


def test_정상_저장은_True다(monkeypatch, tmp_path):
    from modules.auto_trade import common

    monkeypatch.setattr(common, "DAILY_STATE_FILE", str(tmp_path / "daily.json"))
    assert common.save_daily_initial_asset(KEY, 10_000_000) is True
    assert common.load_daily_initial_asset(KEY) == 10_000_000


def test_호출부가_실패를_사용자에게_알린다(monkeypatch, tmp_path):
    """'갱신 및 저장'이라고 찍어 놓고 저장이 안 된 상태를 막는다."""
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "modules/auto_trade/trader.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    bare = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value.func).endswith("save_daily_initial_asset")):
            bare.append(n.lineno)
    assert not bare, (
        "기준선 저장 결과를 버리는 호출이 남아 있다 — 실패해도 '저장됨'으로 보인다: "
        + ", ".join(f"trader.py:{ln}" for ln in bare))


# ─────────────────────────────────────────────────────────────────────────────
# ② 기준선을 쓰는 세 경로가 **같은 검사**를 지나는가 (2026-09-06)
#
#  이 함수가 있어도 부르지 않으면 없는 것과 같다. 실제로 네 곳 중 한 곳 — 무중단
#  운용에서 자정에 도는 **날짜 변경 갱신** — 만 `tot_asset > 0` 이었다. 그 값은 그날
#  하루의 손실 한도 분모이자 사이징 기준이 된다.
# ─────────────────────────────────────────────────────────────────────────────
import ast
import inspect


def _baseline_write_sites():
    """save_daily_initial_asset 을 부르는 자리와, 그 자리가 검사를 지나는지."""
    import modules.auto_trade.trader as T

    src = inspect.getsource(T)
    tree = ast.parse(src)
    calls = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "save_daily_initial_asset" in names:
            calls.append((fn.name, names))
    return calls


def test_기준선을_적는_함수는_타당성_검사를_함께_지난다():
    """[가드] 새 저장 경로가 생겨도 검사 없이 지나가지 못하게 못 박는다."""
    sites = _baseline_write_sites()
    assert sites, "save_daily_initial_asset 호출부를 하나도 못 찾았다 — 검사기가 낡았다"
    bad = [name for name, names in sites
           if "is_plausible_baseline" not in names]
    assert not bad, (
        "기준선을 저장하면서 타당성 검사를 지나지 않는 함수가 있다: "
        f"{bad}\n  이 값은 하루 종일 고정된다 — 작게 박히면 차단기가 종일 발동하지 않는다.")


def test_집계_결손은_기준선_갱신을_막는다():
    """비율 검사는 이 시스템에서 도달 불가능하다(노출 상한 40% < 문턱 50%).

    그래서 값이 아니라 **못 읽었다는 사실**로 막아야 한다.
    """
    import modules.auto_trade.trader as T

    src = inspect.getsource(T.AutoTrader._run_loop)
    head = src[:src.index("당일 시작 자산 갱신")]
    tail = head[head.rindex("날짜 변경 감지"):]
    assert "degraded" in tail, \
        "날짜 변경 갱신이 자산 집계의 결손 표식을 보지 않는다"
    assert "is_plausible_baseline" in tail, \
        "날짜 변경 갱신이 타당성 검사를 지나지 않는다"
