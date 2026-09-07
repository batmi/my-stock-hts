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
#
#  [2026-09-07] 실매매가 내는 사유 **넷**이 이 표에 없었다 — 수익보존·이익보호·본전청산·
#   하락반전. engine.analyze_sell 이 실제로 내보내는 문자열인데(engine.py 1503·1506·1508·
#   1568) 전부 빈 태그가 됐다. 이 표의 존재 이유가 '어휘가 늘 때 한쪽이 빠지지 않게'인데
#   정작 표 자신이 빠져 있었다.
#   빈 태그는 그냥 비어 있는 것으로 끝나지 않는다 — 소비자(account.py 매매내역)가
#   `if not reason_display.startswith("[")` 로 그 자리를 **상태 태그**(기간만료·발동실패·
#   예약취소)로 채운다. 2026-08-26 피라미딩 태그 누락 때 접수 상태 태그가 그 자리를
#   차지했던 것과 같은 모양이다(이 파일 맨 위 주석).
#   백테스트 전용 어휘(교체·데이터종료)도 함께 넣는다. 화면에는 안 오지만, 정본 목록
#   (portfolio_backtest.EXIT_REASONS) 전체가 태그를 받는지 검사하려면 예외 목록이
#   없어야 한다 — 예외 목록은 낡는다.
_SELL_RULES = (
    ("반익절",       lambda r, u: "반익절" in r),
    ("하락반전",     lambda r, u: "하락반전" in r),
    ("과열매도",     lambda r, u: "과열" in r),
    ("익절",         lambda r, u: "익절" in r),
    #  '이익보호'·'수익보존'은 같은 장치(PROFIT_LOCK)의 두 표기다. 둘 다 '익절'을
    #   부분 문자열로 갖지 않으므로 익절 규칙이 가리지 않는다.
    ("이익보호",     lambda r, u: "이익보호" in r or "수익보존" in r),
    ("본전청산",     lambda r, u: "본전" in r),
    ("ATR손절",      lambda r, u: "ATR" in r and "손절" in r),
    ("손절",         lambda r, u: "손절" in r),
    ("트레일링스탑", lambda r, u: "트레일링" in r),
    #  '시간청산'은 '시간'과 '청산'을 함께 본다 — '본전청산'이 위에서 이미 걸린다.
    ("시간청산",     lambda r, u: "시간" in r and "청산" in r),
    ("추세이탈",     lambda r, u: "추세" in r or "점수" in r or "매도진입" in r),
    ("교체",         lambda r, u: "교체" in r),
    ("데이터종료",   lambda r, u: "데이터종료" in r),
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
