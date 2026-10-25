"""주기 재생 하네스(tests/cycle_replay.py)로 AutoTrader 를 돌린 기록이 기준 기록과 같아야 한다.

[왜 · 2026-10-24] AutoTrader 분리(C1)의 동등성 기준이다. 기준 기록(tests/fixtures/cycle_replay_golden.json)은
분리 전 코드로 만들었다. 바깥에서 보이는 결과 — 주문·체결·거래 기록·알림·로그·화면·상태 — 가 한 글자라도
다르면 실패하고 차이를 보여 준다.

[새 프로세스에서 돈다] 시세 연결 상태·알림 발신 이력 같은 프로세스 전역 상태는 앞 테스트가 남긴 값이
화면에 그대로 찍힌다. 같은 워커에 어떤 테스트가 먼저 돌았는지에 따라 기록이 달라지지 않게, 비교용 기록은
새 프로세스에서 만든다.

[시간대] 새 프로세스는 TZ=Asia/Seoul 로 띄운다. 국내 세션·휴장 판정은 naive datetime.now() 를 KST 로 믿고,
 로컬 시간대가 KST 가 아니면 자동매매 시간 게이트를 닫는다(api/sessions). 운용 기기와 같은 전제를 여기서
 세워 두지 않으면, 시간대가 다른 기기에서는 재생이 '장 마감 대기'로 빠져 매매 경로를 하나도 지나지 않는다(실측).

[판이 달라도 비교한다] 2026-10-24 실측 — Python 3.12·3.13·3.14 × pandas 2.3.3·3.1.0 × numpy 2.2·2.4·2.5
 조합에서 기록이 한 글자도 같았다. 그래서 CI 의 모든 판에서 비교한다(핵심 검사). 기준 기록에는 만든 환경을
 함께 남겨, 판이 다른 곳에서 실패하면 그 사실을 실패 문구에 붙인다 — 코드 변경 없이 그 차이가 났다면
 의존성 판이 동작(숫자·표 모양)을 바꾼 것이니, 차이를 읽고 판단한 뒤 기준 기록을 다시 만든다.

[의도한 변경일 때] 로그 문구를 고치는 등 동작을 일부러 바꿨다면 기준 기록을 다시 만든다:
    CYCLE_REPLAY_UPDATE=1 python -m pytest tests/test_cycle_replay.py -o addopts=
 그리고 커밋 메시지에 기록이 왜 바뀌었는지 적는다. 구조만 바꾸는 커밋(C1 분리)에서는 바뀌면 안 된다.

[시나리오] cycle_replay.SCENARIOS 마다 기준 기록이 따로 있다 — base(매매 경로)·market_stress(시장·리스크 경로,
 2026-10-24)·account_sync(계좌 동기화 경로, 2026-10-25)·sell_paths(매도 경로, 2026-10-25)·buy_paths(매수 경로,
 2026-10-25). 시나리오마다 '실제로 그 경로를 지났는가'를 먼저 보고 나서 기록을 비교한다.
"""
import difflib
import json
import os
import subprocess
import sys

import pytest

from tests import cycle_replay

_OUT_ENV = "CYCLE_REPLAY_OUT"
_SCENARIO_ENV = "CYCLE_REPLAY_SCENARIO"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_재생_기록_만들기(tmp_path):
    """(내부용) 아래 픽스처가 새 프로세스에서 부른다 — 하네스를 돌려 기록을 파일로 쓴다."""
    out = os.environ.get(_OUT_ENV)
    if not out:
        pytest.skip("비교 테스트가 새 프로세스로 부르는 내부용 테스트")
    with open(out, "w", encoding="utf-8") as f:
        f.write(cycle_replay.dump(cycle_replay.run(tmp_path, scenario=os.environ.get(_SCENARIO_ENV, "base"))))


def _fresh(tmp_path_factory, scenario):
    out = tmp_path_factory.mktemp(f"replay_{scenario}") / "trace.json"
    env = dict(os.environ, **{_OUT_ENV: str(out), _SCENARIO_ENV: scenario,
                              "TZ": "Asia/Seoul"})     # 운용 기기와 같은 KST 전제(위 [시간대])
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                          f"{os.path.relpath(__file__, _ROOT)}::test_재생_기록_만들기"],
                         cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0 and out.exists(), (res.stdout[-3000:] + res.stderr[-2000:])
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fresh_trace(tmp_path_factory):
    return _fresh(tmp_path_factory, "base")


@pytest.fixture(scope="module")
def stress_trace(tmp_path_factory):
    return _fresh(tmp_path_factory, "market_stress")


@pytest.fixture(scope="module")
def account_trace(tmp_path_factory):
    return _fresh(tmp_path_factory, "account_sync")


@pytest.fixture(scope="module")
def sell_trace(tmp_path_factory):
    return _fresh(tmp_path_factory, "sell_paths")


@pytest.fixture(scope="module")
def buy_trace(tmp_path_factory):
    return _fresh(tmp_path_factory, "buy_paths")


_TRACE_FIXTURE = {"base": "fresh_trace", "market_stress": "stress_trace", "account_sync": "account_trace",
                  "sell_paths": "sell_trace", "buy_paths": "buy_trace"}


def test_재생은_매매_경로를_실제로_지난다(fresh_trace):
    """하네스가 빈 주기를 돌고 있으면 비교가 공허하게 통과한다 — 각 경로를 실제로 지났는지 먼저 본다."""
    logs = "\n".join(fresh_trace["logs"])
    for marker in ("피라미딩 실행", "ATR손절", "시간청산(", "추세이탈(", "트레일링스탑 (", "보유 슬롯 가득 참",
                   "매수 실행:"):
        assert marker in logs, f"재생이 '{marker}' 경로를 지나지 않았다"
    fills = [f["type"] for f in fresh_trace["fills"]]
    assert fills.count("매수") >= 7 and fills.count("매도") >= 4, fills
    assert fresh_trace["stop"]["telegram"], "정지 알림이 없다"
    for name, lines in fresh_trace["screens"].items():
        assert lines, f"{name}: 화면이 비어 있다"
        assert not any(x.startswith("<") and "Error" in x for x in lines), f"{name}: 화면 출력 중 예외 {lines[:3]}"


def test_압박_재생은_시장_리스크_경로를_실제로_지난다(stress_trace):
    """market_stress 가 노리는 경로 — 하나라도 빠지면 그 경로의 분리는 기준 없이 옮겨진다."""
    logs = "\n".join(stress_trace["logs"])
    tg = "\n".join(stress_trace["telegram"])
    alerts = "\n".join(a["message"] for a in stress_trace["alerts"])
    for marker, where in (("[시장 감지] KOSDAQ", tg), ("[시장 회복] KOSDAQ", tg),     # 필터 차단·해제 알림(근거 줄 포함)
                          ("└ 지수 ", tg),
                          ("[시장 필터링] 하락장 매수 보류", logs),                  # 차단된 시장의 후보를 건너뜀
                          ("[판단 보류] KOSPI", alerts), ("KOSPI 지수 데이터 부족/조회 실패", logs),
                          ("[시장 필터링] 지수 판단 불가(KOSPI, KOSDAQ)", logs),
                          ("[계좌 드로다운", alerts), ("[리스크 스케일링] 계좌 드로다운", logs),
                          ("하락 미확정 x0.6 휩소율", logs),                     # 국면·휩소율 배수
                          ("축소 사유가 장중에 풀렸지만 오늘은 유지합니다", logs),    # 장중 래칫
                          ("리스크스케일 x", logs),                              # 그 배수로 사이징한 매수
                          ("KOSDAQ 지수 조회 실패: 재생", logs), ("[판단 보류] KOSDAQ", alerts),   # 조회 중 예외
                          ("지수 장기 이력을 받지 못해 ATR 손절 캡을 고정값", logs)):            # 동적 손절 캡 고정
        assert marker in where, f"압박 재생이 '{marker}' 경로를 지나지 않았다"
    assert any(a["urgent"] for a in stress_trace["alerts"] if "[계좌 드로다운" in a["message"])
    state = stress_trace["state"]
    assert state["risk_scale"] < 1.0 and state["risk_floor"]["scale"] == pytest.approx(state["risk_scale"])
    assert state["risk_scale_reason_by_market"]["KOSPI"].startswith("[장중 유지]")
    assert state["market_status_notified"].get("KOSPI") is True        # 판단 보류 알림 래치
    for name, lines in stress_trace["screens"].items():
        assert lines, f"{name}: 화면이 비어 있다"
        assert not any(x.startswith("<") and "Error" in x for x in lines), f"{name}: 화면 출력 중 예외 {lines[:3]}"


def test_계좌_재생은_계좌_동기화_경로를_실제로_지난다(account_trace):
    """account_sync 가 노린 경로 — 하나라도 빠지면 그 경로의 분리는 기준 없이 옮겨진다."""
    logs = "\n".join(account_trace["logs"])
    tg = "\n".join(account_trace["telegram"])
    alerts = "\n".join(a["message"] for a in account_trace["alerts"])
    for marker, where in (("[기동 동기화] 외부 체결 2건을 기록했습니다. 제한 등록: 재생외부(100090)", logs),
                          ("⛔ 아래 종목은 운용자가 직접 매수한 것으로 보고", alerts),      # 외부 매수 제한 안내
                          ("조회 구간보다 과거에 진입해 일부만 복원된 종목", alerts),
                          ("정지 중 발생한 예수금 출금 자동 반영", logs),                   # 오프라인 입출금 대조
                          ("[정지 중 출금 자동 반영]", tg),
                          ("[방어 모드] 일일 손실 한도 초과", alerts),                     # 계좌 차단기
                          ("[방어 모드] 신규 매수 중단", logs), ("매수 스킵: 방어 모드", logs),
                          ("[방어 모드 해제] 입출금을 기준 자산에 반영했습니다", tg),       # 입출금 뒤 재평가
                          ("외부 예수금 출금 자동 감지", logs), ("[예수금 출금 자동 감지]", tg),   # 3주기 연속 확인
                          ("[시작 자산 보류] 자산 집계에서 결손이 있습니다(해외잔고)", tg),   # 기동 자산 결손
                          ("초기 자산: 보류 — 결손 없는 자산 조회에서 잡습니다", tg),        # 시작 알림
                          ("자산 집계 일부 실패(결손: 해외잔고) — 이번 주기 총자산을 대안 계산(", logs),  # 0주기
                          ("[시스템 보정] 초기 자산 정보 갱신 및 저장", logs)):              # 1주기(온전)
        assert marker in where, f"계좌 재생이 '{marker}' 경로를 지나지 않았다"
    #  0주기의 보류는 기동이 이미 알린 사실이다 — 같은 날 두 번 보내지 않는다.
    assert "[시작 자산 보류] 자산 집계 일부 실패" not in alerts + tg
    assert "초기 자산: 0원" not in tg and "이전 자산" not in logs
    state = account_trace["state"]
    assert state["buy_halted"] is False and state["buy_halt_kind"] is None, "방어 모드가 스스로 풀리지 않았다"
    assert state["net_transfer_today"] <= cycle_replay.ACCOUNT_INTRADAY[2] + 1000
    rows = {r[0]: r for r in account_trace["asset_history"]}
    assert len(rows) == 2, rows
    first, today = sorted(rows)
    assert rows[first][2] <= -cycle_replay.ACCOUNT_OFFLINE_WITHDRAWAL + 1000, "오프라인 출금이 대조점 행에 없다"
    assert rows[today][2] == state["net_transfer_today"], "오늘 순입출금이 자산 이력에 없다"
    for name, lines in account_trace["screens"].items():
        assert lines, f"{name}: 화면이 비어 있다"
        assert not any(x.startswith("<") and "Error" in x for x in lines), f"{name}: 화면 출력 중 예외 {lines[:3]}"


def test_매도_재생은_매도_경로를_실제로_지난다(sell_trace):
    """sell_paths 가 노린 경로 — 하나라도 빠지면 그 경로의 분리는 기준 없이 옮겨진다."""
    logs = "\n".join(sell_trace["logs"])
    tg = "\n".join(sell_trace["telegram"])
    alerts = "\n".join(a["message"] for a in sell_trace["alerts"])
    for marker, where in (("[분석스킵] 재생제한: 트레이딩 제한", logs),                          # 미관리 포지션
                          ("[매도스킵] KODEX 재생(100120): ETF 제외 설정", logs),
                          ("[TrailingStop] KODEX 재생(100120) 앵커 복원", logs),               # 판정 밖 앵커 복원
                          ("종목: 재생제한(100110)", alerts), ("종목: KODEX 재생(100120)", alerts),
                          ("[손절 보호] 재생대기(100130) 수익률", logs), ("🛑 [손절 보호] 재생대기", tg),  # 미체결 매수 취소
                          ("[고아주문 해제] 100130", logs),
                          ("매도 실행: 재생대기 - ATR손절", logs),                                # 다음 주기 청산
                          ("[예약취소] 전량 매도로 인해 대기 중이던 재생대기 매도 예약 주문 1건", logs),
                          ("매도 실행: 재생반익 - 반익절", logs), ("[개별 룰 적용]", logs),          # 반익절
                          ("[권리 조정] 재생분할(100150)", logs), ("트레일링 최고가 0.5000배 보정", alerts),
                          ("예약 매수 취소: PRICE", alerts),
                          ("매도 결과 불명: 재생불명", logs), ("[매도 결과 불명] 재생불명", tg),       # 응답 유실
                          ("매도 수량 조정: 재생불명 20주 -> 15주", logs),
                          ("매도 중단: 재생수량 주문 가능 수량 부족 (미체결 존재 가능성 · 3회 연속)", logs),
                          ("[손절선 이탈 — 매도 실패]", alerts),
                          ("[분석스킵] 재생시세(100180): 현재가 조회 실패(판정 불가)", logs),          # 시세 실패
                          ("[분석실패] 재생오류(100190)", logs), ("매도 판정 오류 (분석 실패)", alerts),
                          ("매도 수량 조회 실패: 재생오류", logs),
                          ("💤 [거래 종료]", logs), ("[관찰 모드] 마감 자산 스냅샷 기록", logs),         # 마감 뒤
                          ("[장마감] 청산 신호 점검", logs),
                          ("[장마감] 매도 신호 감지 (주문 미전송): 재생수량", logs),
                          ("[장마감 후 매도 신호]", alerts)):
        assert marker in where, f"매도 재생이 '{marker}' 경로를 지나지 않았다"
    state = sell_trace["state"]
    assert state["half_tp_cache"] == ["100140"], state["half_tp_cache"]
    assert state["pending_orders"] == [] and state["stuck_pending_streak"] == {}, "취소한 매수가 대기로 남았다"
    assert state["after_hours_scan_date"] == state["paper_closing_snapshot_date"] == "20261021"
    assert [r[3] for r in sell_trace["reserved"]] == ["CANCELED", "CANCELED"], sell_trace["reserved"]
    assert ["100150", 9694.5] in sell_trace["anchors"], "분할 뒤 앵커가 환산되지 않았다"
    held = {p["code"]: p["qty"] for p in sell_trace["positions"]}
    assert held.get("100150") == 20 and "100130" not in held and "100160" not in held and "100190" not in held, held
    #  손절 보호가 낸 취소는 원장에 취소 이력이 있어야 체결 감시가 '외부 취소'로 읽지 않는다(2026-10-25).
    assert [(t["type"], t["qty"]) for t in sell_trace["trades"]
            if t["code"] == "100130" and "손절 보호" in str(t["reason"])] == [("매수취소(자동)", "5")]
    #  관심목록 밖 보유를 추가 매수·매도해도 가상 원장의 이름이 코드로 덮이지 않는다(2026-10-25).
    assert {p["code"]: p["name"] for p in sell_trace["positions"]}.get("100140") == "재생반익"
    assert "100140(100140)" not in logs and "100130(100130)" not in logs
    for name, lines in sell_trace["screens"].items():
        assert lines, f"{name}: 화면이 비어 있다"
        assert not any(x.startswith("<") and "Error" in x for x in lines), f"{name}: 화면 출력 중 예외 {lines[:3]}"


def test_매수_재생은_매수_경로를_실제로_지난다(buy_trace):
    """buy_paths 가 노린 경로 — 하나라도 빠지면 그 경로의 분리는 기준 없이 옮겨진다."""
    logs = "\n".join(buy_trace["logs"])
    tg = "\n".join(buy_trace["telegram"])
    for marker, where in (("매수 스킵: 미체결 주문 현황 미확인", logs),                          # 0주기: 복구 실패
                          ("피라미딩 보류: 재생증액A - 미체결 주문 현황 미확인", logs),
                          ("피라미딩 보류: 재생증액A - 증액 횟수를 확인할 수 없습니다", logs),     # 증액 가드
                          ("피라미딩 보류: 재생증액B - KOSDAQ 지수 판단 불가", logs),
                          ("피라미딩 보류: 재생증액A - 예수금 부족", logs),
                          ("피라미딩 실행: 재생증액B", logs),
                          ("피라미딩 중단: 재생증액A - 주문 접수 여부를 확인하지 못했습니다", logs),
                          ("[장마감] 피라미딩 신호 감지 (주문 미전송): 재생증액A - 피라미딩 2차", logs),
                          ("[분석스킵] 재생고정손절(101030): 당일 손절가 재진입 불가", logs),     # 재진입 방어
                          ("[분석스킵] 재생ATR손절(101040): 당일 재진입 불가 (체결강도 135.0% <= 기존매수 150.0%)", logs),
                          ("[당일 재진입(기존 150.0% 경신)]", logs),
                          ("[상관관계 보류] (보유·미체결 재생증액A 상관계수: 1.00", logs),        # 후보 게이트
                          ("[추세품질 상한] (추세품질", logs), ("[매수 스킵] 트레이딩 제한 종목 (1개): 재생제한", logs),
                          ("[매도비:0.60<1.0]", logs), ("[체결:60.0%<100.0%]", logs),
                          ("[분석스킵] 재생차트(101110): 차트 데이터 없음", logs),
                          ("[분석실패] 재생오류(101120): 매수 판정 중 오류", logs),
                          ("[시장 필터링] 지수 판단 불가(KOSDAQ) 매수 보류 (1종목): 재생코스닥", logs),
                          ("[매수 보류] 재생무손절(101150) 손절 기준 없음", logs),                # 주문 가드
                          ("매수 보류: 재생시세(101130) - 실시간 현재가 조회 실패", logs),
                          ("[리스크 조정] ATR 손절률(", logs), ("매수 보류: 재생고가(101140) - 1주", logs),
                          ("[예약취소] 신규 매수로 인해 대기 중이던 재생신규 매수 예약 주문 1건", logs),
                          ("매수 중단: 최대 보유 종목 수(4개) 도달", logs),
                          ("(보유 3 + 미체결 1)", logs),                                         # 미체결이 슬롯을 차지
                          ("[매수스킵] 매수 조건 충족 1종목 — 주문 미전송 (예수금 부족 — 500원", logs),
                          ("매수 보류: 재생고점삭제 - 이전 포지션의 트레일링 고점을 지우지 못했습니다", logs),
                          ("[반익절] 재생ATR손절 이전 기록을 지우지 못했습니다", logs),
                          ("매수 중단: 재생유실 - 주문 접수 여부를 확인하지 못했습니다", logs),
                          ("[매수 결과 불명] 재생유실", tg)):
        assert marker in where, f"매수 재생이 '{marker}' 경로를 지나지 않았다"
    #  응답이 유실된 증액도 횟수는 올린 채 둔다 — 다음 주기가 상한을 넘겨 한 번 더 증액하지 않게.
    counts = {r[0]: r[2] for r in buy_trace["anchors"]}
    assert counts.get("101010") == 1 and counts.get("101020") == 1, buy_trace["anchors"]
    assert [r[3] for r in buy_trace["reserved"]] == ["CANCELED"], buy_trace["reserved"]
    ledger = {r[0]: r for r in buy_trace["signal_ledger"]}
    #  (code, cycles, passed, vol, abr, hold, corr, rs, tq, reentry, other, slot, cash, market, …)
    assert ledger["101070"][6] >= 1 and ledger["101080"][8] >= 1 and ledger["101100"][4] == 1
    assert ledger["101030"][9] == 1 and ledger["101040"][9] == 2 and ledger["101190"][13] == 1
    assert ledger["101160"][11] == 1 and ledger["101160"][12] == 1, ledger["101160"]
    held = {p["code"] for p in buy_trace["positions"]}
    assert held == {"101010", "101020", "101040"}, held        # 재생유실은 접수되지 않았다
    state = buy_trace["state"]
    assert state["pending_restore_ok"] is True and state["pending_orders"] == []
    for name, lines in buy_trace["screens"].items():
        assert lines, f"{name}: 화면이 비어 있다"
        assert not any(x.startswith("<") and "Error" in x for x in lines), f"{name}: 화면 출력 중 예외 {lines[:3]}"


@pytest.mark.parametrize("scenario", sorted(cycle_replay.SCENARIOS))
def test_재생_기록이_기준과_같다(scenario, request):
    trace = request.getfixturevalue(_TRACE_FIXTURE[scenario])
    golden = cycle_replay.SCENARIOS[scenario]
    got = {"env": cycle_replay.env_signature(), "trace": trace}
    if os.environ.get("CYCLE_REPLAY_UPDATE") == "1":
        os.makedirs(os.path.dirname(golden), exist_ok=True)
        with open(golden, "w", encoding="utf-8") as f:
            f.write(cycle_replay.dump(got) + "\n")
        pytest.skip("기준 기록을 새로 썼다")
    with open(golden, encoding="utf-8") as f:
        want = json.load(f)
    a, b = cycle_replay.dump(want["trace"]), cycle_replay.dump(got["trace"])
    if a != b:
        diff = "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), "기준", "지금", lineterm="", n=2))
        env_note = "" if want["env"] == got["env"] else (
            f"\n\n[판 차이] 기준 기록 환경 {want['env']} · 지금 {got['env']} — 코드 변경 없이 난 차이라면"
            " 의존성 판이 동작을 바꾼 것이다. 차이를 읽고 판단한 뒤 기준 기록을 다시 만든다.")
        pytest.fail(f"주기 재생 기록({scenario})이 기준과 다르다:\n" + diff[:8000] + env_note)


@pytest.mark.parametrize("scenario", sorted(cycle_replay.SCENARIOS))
def test_같은_조건이면_두_번_돌려도_같다(tmp_path, scenario):
    """결정성 — 이게 깨지면 위 비교의 실패가 코드 변경 탓인지 알 수 없다(같은 프로세스에서 연달아 두 번)."""
    a = cycle_replay.dump(cycle_replay.run(tmp_path / "a", scenario=scenario))
    b = cycle_replay.dump(cycle_replay.run(tmp_path / "b", scenario=scenario))
    assert a == b
