"""빠른 핵심 검사 — 커밋 전·CI 에서 매번 돌리는 작은 묶음(전체 회귀 검사와 구분한다).

[무엇을 담나] 판단·주문·기록의 핵심 불변식과, 재발한 적 있는 결함을 막는 가드만 넣는다.
 시각·장 상태에 기대지 않고 네트워크 없이 도는 것만 고른다. 전체 스위트는 CI 가 매일 장 밖·장중
 두 시각에 따로 돌린다(.github/workflows/checks.yml — 시각 의존이 되살아났는지 보는 대조).
   · 채점: 구조화 결과·만점·가중치 범위·경계 원장
   · 판단 공통화: 청산 체인·청산 패리티·사이징 패리티·52주 창 패리티·단일 종목 엔진·화면 판정·동적 손절 캡 입력
   · 설정: 전역 임시 변경 금지·주기 스냅샷·폴백 리터럴·그룹 딕셔너리 대입·개별 룰 저장 왕복
   · 주문 상태: 결과 불명·중복 체결·재시작 미체결·부분체결 정책·DB 결과 불명·가상투자 가로채기·시험 모델
   · 구조: pyflakes 기준선·README 트리·import 부작용·계층 패키지 규약·AutoTrader 분리 연결부·주기 재생 기록·최저 Python 판(3.12) 호환·pandas 판 의미·실행 기록

[실행]
  python tools/core_checks.py            # 핵심 묶음
  python tools/core_checks.py -x -q      # pytest 인자를 그대로 넘긴다
 전체 회귀 검사는 `python -m pytest` 다(요청 시·릴리스 전 — [[testing-scope-preference]]).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_TESTS = (
    # 채점
    "tests/test_score_breakdown.py",
    "tests/test_scoring_max_invariant.py",
    "tests/test_scoring_weight_bounds.py",
    "tests/test_scoring_weights_normalize.py",
    "tests/test_score_boundary_ledger.py",
    # 판단 공통화
    "tests/test_exit_rules.py",
    "tests/test_exit_parity.py",
    "tests/test_sizing_parity.py",
    "tests/test_w52_window_parity.py",
    "tests/test_single_engine_parity.py",
    "tests/test_screen_judgment_parity.py",
    "tests/test_live_vol_regime.py",
    # 설정
    "tests/test_config_no_global_override.py",
    "tests/test_config_cycle_snapshot.py",
    "tests/test_config_fallback_literals.py",
    "tests/test_config_dict_fallback_literals.py",
    "tests/test_config_group_aliasing.py",
    "tests/test_rule_persistence.py",
    # 주문 상태
    "tests/test_order_outcome_unclear.py",
    "tests/test_order_outcome_unknown.py",
    "tests/test_duplicate_fill_guard.py",
    "tests/test_pending_order_restart.py",
    "tests/test_partial_fill_policy.py",
    "tests/test_db_queue_timeout_unknown.py",
    "tests/test_paper_interception_contract.py",
    "tests/test_paper_fault_model.py",
    "tests/test_performance_report.py",
    # 구조
    "tests/test_pyflakes_baseline.py",
    "tests/test_readme_tree_sync.py",
    "tests/test_import_side_effects.py",
    "tests/test_layer_packages.py",
    "tests/test_trader_split_parity.py",
    "tests/test_cycle_replay.py",
    "tests/test_python_min_compat.py",
    "tests/test_pandas_version_semantics.py",
    "tests/test_run_manifest.py",
    # 감사 도구 규약(씨드 안내·공통 구간 분할)과 경계 실험 도구
    "tests/test_audit_seed_notice.py",
    "tests/test_audit_common_conventions.py",
    "tests/test_audit_synergy_boundary.py",
)


def main(argv=None):
    import pytest
    os.chdir(ROOT)
    missing = [t for t in CORE_TESTS if not os.path.exists(os.path.join(ROOT, t))]
    if missing:
        print(f"[핵심 검사] 목록에 있는데 파일이 없다: {missing}")
        return 2
    return pytest.main(list(CORE_TESTS) + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
