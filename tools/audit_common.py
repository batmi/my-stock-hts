"""감사 도구 공통 규약 — '무엇이 청산인가'를 한 곳에서 정한다.

[왜 있는가] run_portfolio의 거래 기록에는 청산만 들어 있지 않다. 신규 매수
(reason="매수")와 **증액(reason="피라미딩N차")** 이 같은 리스트에 섞여 있고,
증액 행은 profit=0 · days=0 으로 기록된다. 감사 도구가 청산을 `reason != "매수"`로
거르면 증액이 '수익 0%·보유 0일 청산'으로 표본에 들어온다.

[실측 2026-08-19, 합성 20종목·2년] 표본 37건 → 71건(+91.9%), 상위 10% 청산 평균
104.3% → 78.2%(-26.1%p), 중앙 보유일 51일 → 3일. 수익·MDD·PF는 시뮬레이터가 직접
내므로 무손상이지만, **꼬리 지표·표본 수·보유일·청산 사유 비중**이 흔들린다. 게다가
팔마다 증액 횟수가 다르므로 편향이 팔에 따라 다르게 걸린다 — 계측기가 대안의 한쪽
팔만 깎는 형태이고, 이는 make_scale_fn 오염(2026-08-16)과 같은 실패 유형이다.

[규약] 청산 표본은 **시뮬레이터가 이미 계산해 돌려주는 `r["sells"]`** 를 쓴다.
이 목록은 run_portfolio가 승률·PF 분모로 쓰는 바로 그 집합이라, 감사 지표와 시뮬레이터
지표가 갈라질 수 없다. 사유 어휘를 도구마다 손으로 관리하지 않는 것이 핵심이다
(실제로 SELL_REASONS 사본 19벌에는 "이익보호"가 빠져 있었다).

[2026-08-23] 사본 19벌을 이 모듈로 모았다. 그때까지 그중 17벌은 "교체"(슬롯 회전
청산)가 빠진 채였다 — 회전을 켠 감사에서 그 청산이 표본에서 통째로 사라진다.
회전은 audit_slot_rotation 만 켜는 옵션이라 지금까지 잘못된 수치를 낸 적은 없지만,
사유가 하나 늘 때마다 19곳을 손으로 맞춰야 하는 구조 자체가 결함이었다.
전환은 비회전 표본에서 24개 도구의 지표를 **한 자리도 바꾸지 않는다**(실측 대조).

[지표 자] base_metrics 가 '공통 자'다. 도구마다 metrics() 를 새로 쓰면 같은 이름의
수치가 서로 다른 산식으로 계산돼 도구 간 비교가 성립하지 않는다. 새 도구는 공통
항목을 여기서 받고 고유 항목만 덧붙인다 — tests/test_audit_metrics_consistency.py 가
기존 사본들이 이 자와 어긋나지 않는지 함께 지킨다.
"""

# 시뮬레이터가 청산으로 세는 사유. 표시·분류용으로만 쓰고, 표본을 거르는 데는
#  exits(r)를 쓴다 — 사유를 손으로 나열하는 순간 새 사유가 조용히 빠진다.
#  [SSOT] 어휘를 정하는 것은 **사유를 만들어 내는 쪽**이다. 여기서 다시 적으면
#   시뮬레이터에 사유가 하나 늘 때 감사 표본만 옛 어휘로 남는다.
from modules.portfolio_backtest import EXIT_REASONS as SELL_REASONS  # noqa: E402


def exits(r):
    """청산 표본. run_portfolio 결과에서 신규 매수·증액을 제외한 실현 거래.

    `r["sells"]`가 있으면 그대로 쓴다(시뮬레이터의 승률·PF 분모와 같은 집합).
    없으면(옛 결과 dict) 같은 규칙을 재현한다 — 사유가 청산 어휘에 있거나,
    실현손익이 0이 아닌 기록. 증액은 profit_amt가 정확히 0이라 어느 쪽으로도 걸리지 않는다.
    """
    sells = r.get("sells")
    if sells is not None:
        return sells
    return [t for t in r.get("trades", [])
            if t.get("reason") in SELL_REASONS or t.get("profit_amt", 0) != 0]


def is_exit(reason):
    """단일 사유가 청산인가 — 기록을 한 건씩 훑는 경로(게이트 주입 등)에서 쓴다."""
    return reason in SELL_REASONS


def base_metrics(r, sells=None):
    """감사 지표의 **공통 자**. 도구별 metrics() 는 여기에 고유 항목만 덧붙인다.

    반환 항목
      ret   : 총수익률(%)              — 시뮬레이터 값 그대로
      mdd   : 최대낙폭(%)              — 시뮬레이터 값 그대로 (음수)
      mar   : ret / |mdd|              — 낙폭 대비 수익. mdd=0이면 nan
      pf    : Profit Factor            — 시뮬레이터 값 그대로
      n     : 청산 표본 수
      win   : 승률(%)                  — 청산 표본 기준 (시뮬레이터의 win/loss 카운터가
                                          아니라 exits(r) 분모다. 둘은 같아야 정상이다)
      top10 : 상위 10% 청산 평균 수익률(%) — 추세추종의 꼬리를 보는 항목
      best  : 최대 수익 청산(%)
      big   : +30% 이상 청산 건수
      days  : 보유일 중앙값

    [주의] 절대 수치는 표본·기간·비용 국면에 좌우된다. 도구 간에 비교해도 되는 것은
    **같은 실행 안의 팔끼리**이지, 다른 날 다른 도구가 찍은 숫자끼리가 아니다.
    """
    import numpy as np

    sells = exits(r) if sells is None else sells
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    mdd = r["mdd"]
    return {
        "ret": r["total_return"],
        "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r.get("pf"),
        "n": len(sells),
        "win": sum(1 for p in profits if p > 0) / len(profits) * 100 if profits else 0.0,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def windows(dates, k, whole=False):
    """기간을 k개 하위구간으로 나눈다 — 구간 경계의 **단일 조립 지점**.

    [왜] 이 분할 블록은 감사 도구 49곳에 복제돼 있었다. 경계 규칙 자체는 전부 같지만
    (같은 크기로 끊고 **마지막 구간이 나머지를 흡수**한다), 같은 규칙을 49번 다시 쓰는
    구조에서는 한 곳만 달라져도 도구 간 비교가 조용히 성립하지 않게 된다.

    whole=True 면 맨 앞에 ("전체", 전 기간)을 붙인다. False 면 k>1 일 때 구간만,
    k<=1 이면 ("전체", 전 기간) 하나를 돌려준다(종전 두 변형과 같은 동작).

    [경계 효과] 구간마다 현금에서 다시 시작하므로 구간 수를 바꾸면 절대 수치가 움직인다.
    구간 간·도구 간 비교는 **같은 k**에서만 의미가 있다.
    """
    k = max(1, int(k))
    n = len(dates)
    size = max(1, n // k)
    chunks = [(f"구간{i + 1}", list(dates[i * size:(i + 1) * size if i < k - 1 else n]))
              for i in range(k)]
    if whole:
        return [("전체", list(dates))] + chunks
    return chunks if k > 1 else [("전체", list(dates))]


def seed_notice(n_seeds, flag="--seed", example=None, emit=print):
    """표본 씨드 수가 규약에 못 미치면 경고 한 줄. 판정을 막지는 않는다.

    [왜] audit-seed-robustness: 승패가 31/60 부근이면 **표본 씨드 하나만 바꿔도**
    답이 뒤집힌 적이 있다(2026-08-17, 3슬롯 58%→43%). 그런데 도구 대부분의 기본
    씨드는 1개다 — 한 번 돌린 표를 그대로 결론으로 적기 쉬운 구조다. 기본값을
    3으로 올리면 실행 시간이 그대로 3배가 되므로, 올리는 대신 **잊지 않게** 한다.

    [무엇이 아닌가] 여기서 말하는 씨드는 **종목 표본을 뽑는 난수 씨드**다.
    `--seed-capital`(초기 자본)과는 무관하다.

    반환: 경고를 찍었으면 True.
    """
    n = int(n_seeds)
    if n >= 3:
        return False
    emit(f"[씨드] 표본 씨드 {n}개로 실행 — 경계선 결과는 3개, 채택 직전이면 5개로 "
         f"재확인할 것. 예: {example or f'{flag} 7'} (audit-seed-robustness)")
    return True
