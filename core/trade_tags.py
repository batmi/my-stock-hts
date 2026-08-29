"""매매 기록 사유 태그 — 잔고 화면과 텔레그램이 공유하는 단일 소스.

[왜 분리했나 · 2026-08-29] 같은 if-elif 사다리가 modules/account.py(메뉴 9 거래내역)와
modules/telegram_bot.py(/history)에 글자 그대로 복제돼 있었다. 2026-08-26 피라미딩 태그
누락(39e922e)도 두 곳을 똑같이 고쳐야 했고, 어휘가 하나 늘 때마다 한쪽이 빠질 자리가
남는다. 청산 사유 어휘를 시뮬레이터 한 곳으로 모은 것과 같은 처방이다.

사유 문자열은 사람이 읽는 자유 텍스트라 부분 문자열로 가른다. 순서가 의미를 가지므로
(먼저 걸리는 규칙이 이긴다) 표를 그대로 쓰고, 규칙을 더할 때는 위치를 함께 정한다.
"""

# 매수 사유 → 태그. 각 항목은 (태그, 판정 함수).
#  [순서] 피라미딩이 가장 먼저다 — 증액은 신규 진입과 사유의 결이 다른데, 분기가 없으면
#   태그 자리가 비고 그 빈자리를 접수(미체결) 상태 태그가 채웠다(실측 2026-08-26).
_BUY_RULES = (
    ("추가매수", lambda r, u: "피라미딩" in r or "PYRAMID" in u),
    ("돌파매수", lambda r, u: "슈퍼모멘텀" in r or "BREAKOUT" in r),
    ("눌림목",   lambda r, u: "역매수" in r or "역추세" in r or "TRAILING_BUY" in r),
    ("추세매수", lambda r, u: "조건 만족" in r or "SCORE" in r),
    ("수동매수", lambda r, u: "수동" in r),
)

# 매도 사유 → 태그. ATR손절이 손절보다 먼저여야 세부 사유가 뭉개지지 않는다.
_SELL_RULES = (
    ("반익절",       lambda r, u: "반익절" in r),
    ("과열매도",     lambda r, u: "과열" in r),
    ("익절",         lambda r, u: "익절" in r),
    ("ATR손절",      lambda r, u: "ATR" in r and "손절" in r),
    ("손절",         lambda r, u: "손절" in r),
    ("트레일링스탑", lambda r, u: "트레일링" in r),
    ("시간청산",     lambda r, u: "시간" in r and "청산" in r),
    ("추세이탈",     lambda r, u: "추세" in r or "점수" in r or "매도진입" in r),
    ("수동매도",     lambda r, u: "수동" in r),
)


def _match(reason, rules):
    if not reason:
        return ""
    upper = reason.upper()
    for tag, hit in rules:
        if hit(reason, upper):
            return tag
    return ""


def classify_buy_reason(reason):
    """매수 사유 문자열 → 태그("추가매수" 등). 해당 없으면 빈 문자열."""
    return _match(reason, _BUY_RULES)


def classify_sell_reason(reason):
    """매도 사유 문자열 → 태그("트레일링스탑" 등). 해당 없으면 빈 문자열."""
    return _match(reason, _SELL_RULES)


def apply_buy_tag(reason):
    """매수 사유에 분류 태그를 붙여 돌려준다. 이미 붙어 있으면 그대로.

    사유가 `[강매수] ...` 처럼 스냅샷 상태 태그로 시작하면 **그 뒤에** 끼워 넣는다.
    맨 앞에 붙이면 상태와 분류의 우선순위가 화면마다 뒤바뀌어 읽힌다.
    """
    tag = classify_buy_reason(reason)
    if not tag or f"[{tag}]" in reason:
        return reason
    if reason.startswith("["):
        close_idx = reason.find("]")
        if close_idx != -1:
            return f"{reason[:close_idx + 1]} [{tag}]{reason[close_idx + 1:]}"
        return reason
    return f"[{tag}] {reason}"


def apply_sell_tag(reason):
    """매도 사유에 분류 태그를 붙여 돌려준다.

    매수와 달리 이미 `[` 로 시작하면 건드리지 않는다 — 매도 사유의 선행 태그는 상태가
    아니라 사유 그 자체인 경우가 많아, 덧붙이면 같은 말이 두 번 나온다.
    """
    if not reason or reason.startswith("["):
        return reason
    tag = classify_sell_reason(reason)
    return f"[{tag}] {reason}" if tag else reason
