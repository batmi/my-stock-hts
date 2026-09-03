"""나머지 영역(journal_sync·telegram_bot·api) 감사에서 세운 불변식.

2026-09-03. 이 세 영역은 대체로 단단했다 — 토큰 갱신은 쿨다운·복구 알림까지 갖췄고,
매매일지 outbox 는 통신 실패를 세지 않고 명시 거절만 dead-letter 로 뺀다. 텔레그램은
chat_id fail-closed 이고 직접 발주 명령이 없다. 여기 남기는 것은 그중 **갈라질 수 있던
두 자리**다.
"""
import ast
import os
import re

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py_files(*bases):
    for base in bases:
        target = os.path.join(ROOT, base)
        if os.path.isfile(target):
            yield target
            continue
        for dirpath, _, files in os.walk(target):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def test_indicator_overlay_always_passes_the_gate():
    """지표에 실시간가를 얹는 모든 자리가 api.chart_overlay_price 를 지나야 한다.

    [왜] 이 게이트가 'KRX 정규장에만 봉을 반영한다'를 단독으로 정한다. 정규장 밖 현재가는
    NXT 체결가라, 그대로 얹으면 지표가 흔들리고 **종목 분석 화면과 자동매매의 점수가
    갈린다**(krx-nxt-data-boundary). 2026-09-03 실측: 12곳 중 2곳이 게이트를 안 지나고
    내부 조건(chart_overlay_enabled)을 손으로 복제하고 있었다. 값은 같았지만, 게이트에
    조건이 하나 늘면 그 두 곳만 따라오지 않는다.
    """
    offenders = []
    for path in _py_files("modules", "api"):
        src = open(path, encoding='utf-8').read()
        for i, line in enumerate(src.split("\n"), 1):
            if "apply_realtime_price(" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#") or "def " in stripped:
                continue
            # 넘기는 가격 인자가 게이트를 지났는가 (같은 줄이거나 직전에 만든 변수).
            #  주석은 걷어낸다 — 설명문에 함수 이름이 있다고 게이트를 지난 것이 아니다
            #  (처음에 이 검사가 자기 주석에 속아 초록이었다).
            ctx_lines = [l for l in src.split("\n")[max(0, i - 4):i]
                         if not l.strip().startswith("#")]
            if "chart_overlay_price" in "\n".join(ctx_lines):
                continue
            offenders.append(f"{os.path.relpath(path, ROOT)}:{i}  {stripped[:80]}")

    assert not offenders, (
        "실시간가를 게이트 없이 지표에 반영하는 자리가 있다 — "
        "api.chart_overlay_price() 를 지날 것:\n  " + "\n  ".join(offenders))


def test_protection_disabling_commands_say_what_stops():
    """보호를 끄는 텔레그램 명령은 무엇이 함께 멈추는지 응답에 밝혀야 한다.

    /stop 은 "보유 N종목의 손절·트레일링 감시도 함께 멈춥니다"라고 밝힌다. /addrestrict 는
    같은 크기의 결정(그 종목의 매도 판정이 통째로 빠진다)인데 "차단되었습니다"로만 끝나고
    있었다 — 매수를 막는 것으로만 읽힌다.
    """
    src = open(os.path.join(ROOT, "modules", "telegram_bot.py"), encoding='utf-8').read()
    tree = ast.parse(src)
    lines = src.split("\n")

    for name in ("_cmd_stop", "_cmd_addrestrict"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert fn is not None, f"{name} 이 사라졌다"
        body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        assert "손절" in body and "트레일링" in body, (
            f"{name}: 무엇이 함께 멈추는지 사용자에게 밝히지 않는다")


def test_telegram_has_no_direct_order_command():
    """텔레그램에서 직접 발주하는 경로가 생기지 않았는지 지킨다.

    발신자 검증은 chat_id fail-closed 하나뿐이다. 메신저 하나가 뚫리면 곧 주문이 되는
    구조는 만들지 않는다 — 지금은 제한 등록·시작/정지까지가 한계다.
    """
    src = open(os.path.join(ROOT, "modules", "telegram_bot.py"), encoding='utf-8').read()
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    for banned in ("api.place_order", "send_order(", "revise_cancel_order("):
        assert banned not in code, f"텔레그램에서 발주 경로가 생겼다: {banned}"


def test_telegram_rejects_when_chat_id_is_unset():
    """TELEGRAM_CHAT_ID 가 없으면 모든 수신 명령을 무시한다(fail-closed).

    2026-08-10 에 `if config.TELEGRAM_CHAT_ID and ...` 였던 것을 뒤집은 자리다.
    환경변수가 비면 조건이 통째로 거짓이 되어 **누구의 명령이든 통과**했다.
    """
    src = open(os.path.join(ROOT, "modules", "telegram_bot.py"), encoding='utf-8').read()
    assert "if not config.TELEGRAM_CHAT_ID or chat_id != str(config.TELEGRAM_CHAT_ID)" in src, (
        "발신자 검증이 fail-closed 형태가 아니다")
