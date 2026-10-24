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
"""
import difflib
import json
import os
import subprocess
import sys

import pytest

from tests import cycle_replay

_OUT_ENV = "CYCLE_REPLAY_OUT"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_재생_기록_만들기(tmp_path):
    """(내부용) 아래 픽스처가 새 프로세스에서 부른다 — 하네스를 돌려 기록을 파일로 쓴다."""
    out = os.environ.get(_OUT_ENV)
    if not out:
        pytest.skip("비교 테스트가 새 프로세스로 부르는 내부용 테스트")
    with open(out, "w", encoding="utf-8") as f:
        f.write(cycle_replay.dump(cycle_replay.run(tmp_path)))


@pytest.fixture(scope="module")
def fresh_trace(tmp_path_factory):
    out = tmp_path_factory.mktemp("replay") / "trace.json"
    env = dict(os.environ, **{_OUT_ENV: str(out), "TZ": "Asia/Seoul"})     # 운용 기기와 같은 KST 전제(위 [시간대])
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                          f"{os.path.relpath(__file__, _ROOT)}::test_재생_기록_만들기"],
                         cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0 and out.exists(), (res.stdout[-3000:] + res.stderr[-2000:])
    return json.loads(out.read_text(encoding="utf-8"))


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


def test_재생_기록이_기준과_같다(fresh_trace):
    got = {"env": cycle_replay.env_signature(), "trace": fresh_trace}
    if os.environ.get("CYCLE_REPLAY_UPDATE") == "1":
        os.makedirs(os.path.dirname(cycle_replay.GOLDEN), exist_ok=True)
        with open(cycle_replay.GOLDEN, "w", encoding="utf-8") as f:
            f.write(cycle_replay.dump(got) + "\n")
        pytest.skip("기준 기록을 새로 썼다")
    with open(cycle_replay.GOLDEN, encoding="utf-8") as f:
        want = json.load(f)
    a, b = cycle_replay.dump(want["trace"]), cycle_replay.dump(got["trace"])
    if a != b:
        diff = "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), "기준", "지금", lineterm="", n=2))
        env_note = "" if want["env"] == got["env"] else (
            f"\n\n[판 차이] 기준 기록 환경 {want['env']} · 지금 {got['env']} — 코드 변경 없이 난 차이라면"
            " 의존성 판이 동작을 바꾼 것이다. 차이를 읽고 판단한 뒤 기준 기록을 다시 만든다.")
        pytest.fail("주기 재생 기록이 기준과 다르다:\n" + diff[:8000] + env_note)


def test_같은_조건이면_두_번_돌려도_같다(tmp_path):
    """결정성 — 이게 깨지면 위 비교의 실패가 코드 변경 탓인지 알 수 없다(같은 프로세스에서 연달아 두 번)."""
    a = cycle_replay.dump(cycle_replay.run(tmp_path / "a"))
    b = cycle_replay.dump(cycle_replay.run(tmp_path / "b"))
    assert a == b
