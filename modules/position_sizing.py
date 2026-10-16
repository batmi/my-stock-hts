"""신규 진입 1건의 배분액 — 실매매(RiskManager.allocate_budget)·백테스트(allocate_amount) 공통.

[왜 · 2026-10-16] 3층 min 결합이 두 벌이었다(백테스트 독스트링이 "동일한 3층 min 결합"이라고
 선언하고 tests/test_sizing_parity.py 가 그 선언을 대조했다). 결합식은 이제 여기 하나다.
 계좌 상태(입출금 보정 기준선·리스크 스케일·로그)는 호출부가 읽어 인자로 넘긴다 —
 이 함수는 API·DB·알림을 건드리지 않는다.

[3층 — 각자 '상한'을 내고 가장 작은 값을 쓴다]
  1) 기초 비중: 기준 자산 × invest_ratio × risk_scale            (집중 방지 + 국면 축소)
  2) 리스크:   기준 자산 × RISK_PER_TRADE(×risk_scale) / (손절폭 × 갭버퍼) (손절 시 손실액 상한)
  3) 변동성:   기초 × clamp(TARGET_VOL / 연환산 변동성)             (변동성 균질화, 확대 ≤ 기초)
 2)와 3)은 '얼마를 살까'에 대한 서로 다른 답이지 누적 벌점이 아니라 곱하지 않는다
 (근거·실측은 engine.RiskManager.allocate_budget 독스트링).

[남은 차이 — scale_risk_layer] 실매매는 risk_scale 을 2)에도 곱하고, 백테스트는 1)에만 곱한다
 (호출부가 비중에 미리 곱해 넘긴다). 도달 가능한 영역(손절폭 = ATR × 배수)에서는 결과가 같다
 — 실데이터 615건 매수 불일치 0건(2026-08-18, test_sizing_parity 독스트링). 없애는 것은
 판단 변경이라 리팩터링에 섞지 않았다.

[진입 측 적용 순서 — 2026-10-16 코드 기준 지도] 순서를 바꾸면 두 경로를 함께 고칠 것.
  실매매(trader, 주기 단위)
    0. 주기 전제: 장 열림 · 개장 첫 30분 보류 · 방어 모드(일일 손실) · 당일 거래내역을 못
       읽으면 그 주기 '매수만' 미룬다(재진입 게이트 입력이므로). 매도 검사는 별도로 먼저 돈다.
    1. 후보별(_analyze_candidate_worker): 트레이딩 제한 → 진행 중 주문 → 보유 종목 제외 →
       시장 지수 필터 → 일봉·체결강도 조회 → 상관 필터 → (RS 필터, 기본 OFF) →
       analyze_buy(채점 → 상태 → 체결강도·호가잔량비 게이트) → 추세품질 상한 → 재진입 차단
       (당일 손절가 위 되사기 금지·재진입 허들)
    2. 순위: 점수 1순위 · 추세품질 동점가름(trader.candidate_priority_key)
    3. 집행(_execute_buy_orders, 순위대로): 실시간가 확인·오염 일봉 금지 → 예수금 하한 →
       슬롯 상한 → 손절률 결정(손절 없으면 진입 금지) → **배분액(position_budget)** →
       마지막 슬롯 처리 → 호가·슬리피지 → 1주 미만 처리 → 포트폴리오 히트 캡 축소/보류 →
       매수가능수량 → 주문(결과 불명이면 OrderOutcomeUnclear — 히트 선점분을 반납하지 않는다)
    4. 피라미딩: 보유 분석 경로(_try_pyramid_buy) — engine.pyramid_gate_ok(공통) + 시장 게이트
       + 신규 매수와 같은 가드(손절·미체결·히트, [[seed-spend-guard-parity]])
  백테스트(portfolio_backtest.run_portfolio, 하루 단위)
    1) 매도(decide_sell → exit_rules) → 히트 예산·방어 모드 → 2) 피라미딩(같은 pyramid_gate_ok)
    → 3) 신규: 되사기 금지 → 상태·점수 → 시장 필터 차단일 → 추세품질 상한 → entry_gate(상관 등
    보유 구성 의존 조건) → 순위(실매매와 같은 동점가름) → 히트 예산 → **배분액(allocate_amount →
    position_budget)** → 1주 미만(oversize_limit) → 현금
  백테스트가 재현하지 못하는 것:
    · 켜져 있으면 portfolio_backtest.unmodeled_entry_features() 가 경고하는 것 — 체결강도·
      호가잔량비 게이트, 개장 직후 진입 보류
    · 경고 없이 남는 환경 차이 — 실시간가 조회 실패, 매수가능수량 API, 주문 결과 불명
    결과 차이를 볼 때 '공통 규칙 불일치'가 아니라 '환경 차이'로 분류한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizingParams:
    risk_per_trade: float = 4.0          # SYSTEM_RISK_PER_TRADE (%)
    gap_buffer: float = 1.2              # RISK_SCALING_PARAMS['GAP_RISK_BUFFER'] (≥1.0 로 보정된 값)
    vol_targeting: bool = True           # USE_VOLATILITY_TARGETING
    target_vol: float = 0.25
    scale_min: float = 0.4
    scale_max: float = 2.0


@dataclass(frozen=True, slots=True)
class SizingResult:
    final: int            # 최종 배분액(가용 현금 상한 적용)
    base: int             # 1) 기초 비중 상한
    risk_cap: int         # 2) 리스크 상한(미적용이면 0)
    vol_cap: int          # 3) 변동성 상한(미적용이면 0)
    vol_scale: float      # 3)의 배수(미적용이면 1.0)
    vol_input_bad: bool   # 변동성 층을 켰는데 ATR·가격이 없어 빠졌다(상시 구속층 누락)


def params_from_config(cfg):
    """config 모듈에서 SizingParams 를 읽는다(폴백 리터럴은 두 호출부의 종전 값 그대로)."""
    params = getattr(cfg, "RISK_SCALING_PARAMS", {}) or {}
    try:
        gap = max(1.0, float(params.get("GAP_RISK_BUFFER", 1.2)))
    except (TypeError, ValueError):
        gap = 1.2
    return SizingParams(
        risk_per_trade=getattr(cfg, "SYSTEM_RISK_PER_TRADE", 4.0),
        gap_buffer=gap,
        vol_targeting=getattr(cfg, "USE_VOLATILITY_TARGETING", True),
        target_vol=getattr(cfg, "TARGET_VOLATILITY", 0.25),
        scale_min=getattr(cfg, "VOLATILITY_SCALING_MIN", 0.4),
        scale_max=getattr(cfg, "VOLATILITY_SCALING_MAX", 2.0),
    )


def position_budget(equity, avail_cash, invest_ratio, stop_loss_rate, atr, price, params,
                    risk_scale=1.0, scale_risk_layer=True):
    """배분액 계산(순수 함수). equity 는 호출부가 고른 '기준 자산'이다."""
    base = int(equity * (invest_ratio * risk_scale))
    target = base

    risk_cap = 0
    if params.risk_per_trade > 0 and stop_loss_rate and abs(stop_loss_rate) > 0:
        rpt = params.risk_per_trade * risk_scale if scale_risk_layer else params.risk_per_trade
        max_loss = equity * (rpt / 100.0)
        risk_cap = int(max_loss / ((abs(stop_loss_rate) / 100.0) * params.gap_buffer))
        target = min(target, risk_cap)

    vol_cap, vol_scale = 0, 1.0
    vol_input_bad = params.vol_targeting and not (atr and price and price > 0)
    if params.vol_targeting and atr and price and price > 0:
        annual_vol = (atr / price) * math.sqrt(252)
        if annual_vol > 0:
            vol_scale = params.target_vol / annual_vol
            vol_scale = max(params.scale_min, min(params.scale_max, vol_scale))
            vol_cap = min(int(base * vol_scale), base)
            target = min(target, vol_cap)

    return SizingResult(final=min(target, avail_cash), base=base, risk_cap=risk_cap, vol_cap=vol_cap,
                        vol_scale=vol_scale, vol_input_bad=bool(vol_input_bad))
