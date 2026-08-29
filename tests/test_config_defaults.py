"""설정 기본값의 단일 소스 보장.

종전에는 기본값이 세 곳(GlobalSettings 클래스 · reset_all_settings 의 하드코딩 딕셔너리
· json/dynamic_config.json)에 있었고, 한쪽만 고쳐지는 사고가 실제로 두 번 났다
(2026-08-05 DD_SCALE 구 값 부활, 2026-08-09 동적 ATR 캡 8키 누락).
하드코딩은 제거했고, 여기서 그 제거가 성립하는 전제를 고정한다.
"""
import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

GROUPS = ["ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS",
          "SCORING_WEIGHTS", "MARKET_REGIME_PARAMS", "RISK_SCALING_PARAMS"]

# 표시 전용 키 — 운용자가 자유롭게 바꿔도 매매·백테스트·감사가 달라지지 않는다.
#  [왜 예외인가 · 2026-08-29] 이 검사가 겨냥하는 위험은 '다이얼을 채택했는데 운용 파일의
#   옛 값이 조용히 되돌린다'이다. 그건 **매매 판정에 들어가는 값**에만 성립한다.
#   화면에만 쓰이는 값까지 붙잡으면, 운용자의 정당한 튜닝 때문에 이 검사가 상시 빨간불이
#   되고 정작 매매 다이얼이 어긋났을 때 신호가 묻힌다(경보가 무뎌진다).
#  [등재 기준] 그 키를 읽는 코드가 화면 출력 경로에만 있을 것. 새로 넣을 때는 호출처를
#   전부 확인하고 아래에 근거를 함께 적는다. 로직에 편입되면 즉시 뺀다.
DISPLAY_ONLY = {
    # detect_recent_box 의 유일한 호출처가 modules/chart.py 의 차트 그리기다.
    #  스코어링·진입·청산·백테스트 어디에도 들어가지 않는다(2026-08-29 호출처 전수 확인).
    ("INDICATOR_PARAMS", "BOX_PERIOD"),
    ("INDICATOR_PARAMS", "BOX_VALUE_AREA_PCT"),
}


def test_mutable_defaults_are_isolated_per_instance():
    """하드코딩을 지울 수 있었던 근거 — 새 인스턴스는 이전 인스턴스의 제자리 수정을 물려받지 않는다.

    이 성질이 깨지면(pydantic 동작 변경 등) reset_all_settings 가 오염된 값을
    '기본값'이라며 되살리게 되므로, 여기서 먼저 실패해야 한다.
    """
    a = config.GlobalSettings()
    a.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 99.0
    a.SELL_STRATEGY["TIME_STOP_DAYS"] = 999

    fresh = config.GlobalSettings()
    assert fresh.ANALYSIS_THRESHOLDS["BUY_SCORE"] != 99.0
    assert fresh.SELL_STRATEGY["TIME_STOP_DAYS"] != 999


def test_reset_all_settings_restores_class_defaults(tmp_path, monkeypatch):
    """'전체 초기화'는 클래스 기본값과 정확히 같아야 한다(어떤 키도 빠지지 않는다)."""
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path))
    config.set_config_profile(None)
    try:
        config.settings.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 1.0
        config.settings.SYSTEM_MAX_HOLDINGS = 99

        config.reset_all_settings()

        defaults = config.GlobalSettings()
        for group in GROUPS:
            assert getattr(config.settings, group) == getattr(defaults, group), group
        assert config.settings.SYSTEM_MAX_HOLDINGS == defaults.SYSTEM_MAX_HOLDINGS
    finally:
        config.set_config_profile(None)


def test_no_hardcoded_default_dicts_in_reset():
    """초기화 경로가 기본값을 다시 타이핑하지 않는지 — 세 번째 사본이 되살아나는 것을 막는다."""
    src = open(config.__file__, encoding="utf-8").read()
    body = src[src.index("def reset_all_settings"):]
    body = body[:body.index("\n# ")]
    for group in GROUPS:
        assert f"settings.{group} = {{" not in body, (
            f"{group} 기본값이 reset_all_settings 에 다시 하드코딩됐다 — "
            f"GlobalSettings 클래스 기본값만 진실이어야 한다")


def test_saved_config_has_no_unknown_keys():
    """운영 설정 파일이 클래스에 없는 키를 들고 있지 않은지(리네임·삭제 후 잔재 탐지)."""
    path = os.path.join(os.path.dirname(config.__file__), "json", "dynamic_config.json")
    if not os.path.exists(path):
        pytest.skip("운영 설정 파일 없음")
    data = json.load(open(path, encoding="utf-8"))
    defaults = getattr(config.GlobalSettings(), "model_dump", config.GlobalSettings().dict)()

    unknown_top = [k for k in data if k not in defaults]
    assert not unknown_top, f"클래스에 없는 최상위 키: {unknown_top}"

    for group in GROUPS:
        if group not in data:
            continue
        unknown = [k for k in data[group] if k not in defaults[group]]
        assert not unknown, f"{group} 에 클래스에 없는 키: {unknown}"


def test_strategy_dials_agree_between_class_and_saved_config():
    """전략 다이얼이 **클래스 기본값과 운영 설정 파일에서 같은 값**인가.

    [왜] 다이얼을 채택할 때는 두 곳을 함께 고쳐야 한다. 한쪽만 고치면 조용히 갈라지는데,
     방향에 따라 증상이 다르다.
       · config.py 만 고침 → 기동 시 load_dynamic_config() 가 **옛 값으로 되돌린다.**
         채택했다고 믿는 값으로 매매가 돌지 않는다.
       · dynamic 만 고침   → 코드를 읽는 사람과 실제 동작이 어긋난다.
     감사 도구는 config 를 import 한 뒤 값을 읽는데, config 는 import 시점에 dynamic 을
     덮어쓰므로 **감사는 dynamic 을 잰다.** 즉 config.py 리터럴만 고친 채택은 백테스트에도
     반영되지 않는다 — 잰 것과 도는 것이 같아 보이지만 둘 다 옛 값인 상태다.

    최상위 키(ENABLE_TELEGRAM·TELEGRAM_INSTANCE_NAME 등)는 머신마다 다른 운영 설정이므로
    검사하지 않는다. 전략 딕셔너리 그룹만 본다.

    표시 전용 키(DISPLAY_ONLY)도 빼둔다 — 매매가 달라지지 않는 값까지 붙잡으면 운용자의
    정당한 튜닝으로 상시 실패해, 정작 매매 다이얼이 어긋났을 때 그 신호가 묻힌다.
    """
    path = os.path.join(os.path.dirname(config.__file__), "json", "dynamic_config.json")
    if not os.path.exists(path):
        pytest.skip("운영 설정 파일 없음")
    saved = json.load(open(path, encoding="utf-8"))
    defaults = getattr(config.GlobalSettings(), "model_dump", config.GlobalSettings().dict)()

    drift = []
    for group in GROUPS:
        for key, value in (saved.get(group) or {}).items():
            if (group, key) in DISPLAY_ONLY:
                continue
            base = defaults[group].get(key)
            if base != value:
                drift.append(f"{group}.{key}: config.py={base!r} vs dynamic_config={value!r}")
    assert not drift, (
        "전략 다이얼이 두 곳에서 다르다 — 채택 시 양쪽을 함께 고쳐야 한다:\n  "
        + "\n  ".join(drift))


def test_display_only_keys_really_are_display_only():
    """화이트리스트가 낡지 않게 한다 — 표시 전용 키가 매매 경로로 새면 잡는다.

    이 목록의 존재 이유는 '매매가 달라지지 않는다'이다. 그 전제가 깨지면 예외도 깨져야
    하는데, 사람이 알아서 빼주기를 기대할 수는 없다. 매매 판정 계층에서 그 이름이
    보이면 여기서 실패시킨다.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    # 매매 판정이 사는 곳 — 여기서 키 이름이 보이면 더 이상 표시 전용이 아니다.
    trading_paths = [
        root / "modules" / "auto_trade",
        root / "modules" / "backtest.py",
        root / "modules" / "trading.py",
    ]
    sources = []
    for p in trading_paths:
        if p.is_dir():
            sources += [f for f in p.rglob("*.py")]
        elif p.exists():
            sources.append(p)

    offenders = []
    for _, key in DISPLAY_ONLY:
        for f in sources:
            if key in f.read_text(encoding="utf-8"):
                offenders.append(f"{key} → {f.relative_to(root)}")
    assert not offenders, (
        "표시 전용으로 등재된 키가 매매 경로에서 쓰인다 — DISPLAY_ONLY 에서 빼고 "
        "양쪽 기본값을 동기화해야 한다:\n  " + "\n  ".join(offenders))
