"""구조화된 채점 결과 — analysis.score_breakdown 의 반환 형식.

[왜] calculate_score 는 (총점, 설명 문자열 목록)만 돌려줬다. 감사 도구들은 그 문자열의
 접두사("EMA: ", "[상한] EMA 군집" …)와 괄호 속 숫자("(+0.50)")를 정규식으로 다시 뽑아
 항목·배점을 복원했다. 그러면 화면 문구 하나만 고쳐도 실험 계산이 조용히 틀어진다
 (미분류 문구는 '기타'로 떨어지거나 0점으로 읽힌다). 이 모듈은 항목마다 안정적인 ID 와
 수치를 들고 다니게 해, 문구는 화면용으로만 남긴다.

[불변식] (tests/test_score_breakdown.py 가 지킨다)
  · total == calculate_score(...)[0]  — 같은 입력이면 기존 총점과 비트 단위로 같다.
  · details == calculate_score(...)[1] — 화면 설명도 같은 순서·같은 문구다.
  · sum(item.contribution) == raw_total (감점 포함, 0 하한 적용 전).
  · EMA 군집 상한은 별도 항목(trend.ma_cluster_cap)의 음수 기여로 표현된다 — 개별 EMA
    항목의 contribution 은 상한 전 점수이고, 상한이 깎은 양은 그 한 항목에만 실린다.
    그래서 '단순 합'과 '실제 반영값'의 차이가 결과 안에 그대로 보인다.

[상태값]
  met       조건 충족, 점수 반영
  unmet     입력은 있었고 조건 미충족
  unknown   필수 입력이 없거나(None) NaN — '조건 미충족'과 구분한다. 점수는 0 이다
            (기존 동작 그대로; 확인 불가 항목을 빼고 10점으로 재환산하지 않는다).
  held      조건은 충족했지만 정합 게이트가 보류시켰다(가격 모멘텀의 1·3개월 게이트)
  shadowed  조건은 충족했지만 같은 슬롯의 앞 항목이 이미 점수를 받았다
            (예: VOL 급증과 VOL 추세는 한 슬롯 0.5점을 나눠 쓴다)

[fail-open 기록] 가격 모멘텀의 단기 정합 게이트는 1·3개월 수익률을 모르면(None/NaN)
 게이트를 적용하지 않는다(기존 호출자 호환, test_trend_persistence_scoring 에 명시).
 그 경우 item.unverified 에 확인하지 못한 입력 이름을 남긴다 — 같은 7.5점이라도
 확인한 정보의 범위가 다르다는 것이 결과에서 드러나게 한다. 행동(점수)은 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass, field

# 채점 규칙의 판(版). 항목 조건·배점·슬롯 구조를 바꾸면 올린다(설정값 변경은 아래
#  params_fingerprint 가 따로 잡는다). 실험 기록·신호 원장에 함께 남겨, 어느 규칙으로
#  매긴 점수인지 사후에 가를 수 있게 한다.
SCORING_RULES_VERSION = "2026-10-16.1"

MET = "met"
UNMET = "unmet"
UNKNOWN = "unknown"
HELD = "held"
SHADOWED = "shadowed"

# 팩터 ID — config.SCORING_WEIGHTS 의 키와 같다. 감점은 팩터 가중치를 빌려 쓰지만
#  (MACD 데드크로스는 TREND 배수 등) 분류상 PENALTY 로 묶는다.
TREND, MOMENTUM, STRENGTH, SYNERGY, PENALTY = "TREND", "MOMENTUM", "STRENGTH", "SYNERGY", "PENALTY"

# 항목 명세: id → (팩터, 설계 배점(가중치 1.0 기준, 감점은 음수), 가중치 축, 슬롯, 필수 입력)
#  · 가중치 축: 배점에 곱해지는 r_* 의 출처 팩터(감점도 여기서 축을 가져간다).
#  · 슬롯: 같은 슬롯 항목은 서로 대체 관계라 동시에 점수를 받지 않는다(만점 계산 기준).
#          'ma_cluster' 는 슬롯이 아니라 상한 2.0 이 걸리는 묶음이다(cluster 로 따로 표시).
#  · 필수 입력: 하나라도 None/NaN 이면 그 항목은 unknown.
#  이 표는 채점 코드가 읽는 SSOT 가 아니라 **설명용 카탈로그**다 — 채점 코드는 analysis 에
#  있고, test_score_breakdown 이 두 쪽이 같은 ID 집합·배점을 쓰는지 대조한다.
ITEM_SPECS = {
    # ── TREND (4.0) : MA 군집(상한 2.0) + 지속 0.5 + MACD 0선 0.5 + MACD 교차/확산 0.5 + SAR 0.5
    "trend.ema20_above":        (TREND, 0.5, TREND, None, ("price", "ema20")),
    "trend.ema_aligned":        (TREND, 1.0, TREND, None, ("ema20", "ema60", "ema120")),
    "trend.ema5_over_20":       (TREND, 0.5, TREND, None, ("ema_5", "ema20")),
    "trend.ma60_breakout_early": (TREND, 0.5, TREND, "ma_break", ("price", "ema20", "ema60")),
    "trend.short_surge":        (TREND, 0.5, TREND, "ma_break", ("price", "ema_5", "ema20", "ema60")),
    "trend.ema120_support":     (TREND, 0.5, TREND, None, ("price", "ema120")),
    "trend.ma_cluster_cap":     (TREND, 0.0, TREND, None, ()),
    "trend.persist":            (TREND, 0.5, TREND, None, ("trend_persist",)),
    "trend.macd_above_zero":    (TREND, 0.5, TREND, None, ("macd",)),
    "trend.macd_new_gc":        (TREND, 0.5, TREND, "macd_cross", ("macd_hist", "prev_macd_hist")),
    "trend.macd_expanding":     (TREND, 0.5, TREND, "macd_cross", ("macd", "macd_hist", "prev_macd_hist")),
    "trend.sar":                (TREND, 0.5, TREND, None, ("price", "sar")),
    # ── MOMENTUM (2.5) : RSI 1.0 + CCI 0.5 + DMI 0.5 + 가격 모멘텀 0.5
    "momentum.rsi_strong":      (MOMENTUM, 0.5, MOMENTUM, "rsi_base", ("rsi",)),
    "momentum.rsi_extension":   (MOMENTUM, 0.5, MOMENTUM, None, ("rsi",)),
    "momentum.rsi_pullback":    (MOMENTUM, 0.5, MOMENTUM, "rsi_base", ("rsi", "price", "ema60")),
    "momentum.cci_up":          (MOMENTUM, 0.5, MOMENTUM, "cci", ("cci",)),
    "momentum.cci_escape":      (MOMENTUM, 0.5, MOMENTUM, "cci", ("cci", "prev_cci", "price", "ema60")),
    "momentum.dmi":             (MOMENTUM, 0.5, MOMENTUM, None, ("plus_di", "minus_di")),
    "momentum.price_momentum":  (MOMENTUM, 0.5, MOMENTUM, None, ("mom_ret", "w52_pos")),
    # ── STRENGTH (1.5) : ADX 0.5 + 거래량 0.5 + 수급 0.5
    "strength.adx":             (STRENGTH, 0.5, STRENGTH, None, ("adx",)),
    "strength.vol_spike":       (STRENGTH, 0.5, STRENGTH, "volume", ()),
    "strength.vol_trend":       (STRENGTH, 0.5, STRENGTH, "volume", ()),
    "strength.flow":            (STRENGTH, 0.5, STRENGTH, None, ()),
    # ── SYNERGY (2.0) : 두 항목이 'MACD 확산 유지' 조건을 공유한다(경계에서 2점이 함께 움직인다)
    "synergy.trend_start":      (SYNERGY, 1.0, SYNERGY, None, ("price", "ema60", "macd_hist", "prev_macd_hist", "adx")),
    "synergy.momentum_thrust":  (SYNERGY, 1.0, SYNERGY, None, ("macd_hist", "prev_macd_hist", "rsi")),
    # ── 감점
    "penalty.macd_dead_cross":  (PENALTY, -0.5, TREND, None, ("macd", "macd_signal")),
    "penalty.macd_accel_down":  (PENALTY, -0.5, MOMENTUM, None, ("macd_hist", "prev_macd_hist")),
    "penalty.minus_di":         (PENALTY, -0.5, STRENGTH, None, ("plus_di", "minus_di")),
}

# 보고서용 짧은 라벨(감사 도구 표의 행 이름). 종전 audit_score_factors.LABELS 의 이름을
#  그대로 옮겼다 — 과거 출력과 행 이름이 같아야 날짜를 넘어 표를 맞대 볼 수 있다.
#  가격 모멘텀은 보류(held) 상태를 따로 센다(HELD_LABELS).
ITEM_LABELS = {
    "trend.ema20_above": "추세 EMA20위", "trend.ema_aligned": "추세 정배열",
    "trend.ema5_over_20": "추세 EMA5>20", "trend.ma60_breakout_early": "추세 60돌파[초기]",
    "trend.short_surge": "추세 단기급등", "trend.ema120_support": "추세 EMA120위",
    "trend.ma_cluster_cap": "추세 MA상한적용", "trend.persist": "추세 지속이력",
    "trend.macd_above_zero": "추세 MACD0선위", "trend.macd_new_gc": "추세 MACD신규GC",
    "trend.macd_expanding": "추세 MACD확산", "trend.sar": "추세 SAR",
    "momentum.rsi_strong": "모멘텀 RSI강세", "momentum.rsi_extension": "모멘텀 RSI확장",
    "momentum.rsi_pullback": "모멘텀 RSI눌림", "momentum.cci_up": "모멘텀 CCI상승",
    "momentum.cci_escape": "모멘텀 CCI탈출", "momentum.dmi": "모멘텀 DMI",
    "momentum.price_momentum": "모멘텀 가격",
    "strength.adx": "강도 ADX", "strength.vol_spike": "강도 VOL급증",
    "strength.vol_trend": "강도 VOL추세", "strength.flow": "강도 수급",
    "synergy.trend_start": "시너지 추세시작", "synergy.momentum_thrust": "시너지 모멘텀폭발",
    "penalty.macd_dead_cross": "감점 MACD_DC", "penalty.macd_accel_down": "감점 MACD가속",
    "penalty.minus_di": "감점 -DI우위",
}
HELD_LABELS = {"momentum.price_momentum": "모멘텀 가격보류"}

# MA 군집(상한 2.0 이 걸리는 묶음) 소속 항목
MA_CLUSTER = ("trend.ema20_above", "trend.ema_aligned", "trend.ema5_over_20",
              "trend.ma60_breakout_early", "trend.short_surge", "trend.ema120_support")
MA_CLUSTER_CAP_BASE = 2.0

# 결합 조건 — 여러 항목이 공유하는 하위 조건. 경계 하나가 두 항목을 함께 뒤집는 곳을 명시한다.
#  synergy.* 둘 다 'macd_expanding'(macd_hist > 0 and macd_hist >= prev_macd_hist * 0.8)을 쓴다.
SHARED_CONDITIONS = {
    "macd_expanding": ("synergy.trend_start", "synergy.momentum_thrust"),
    "synergy_structure_ok": ("synergy.trend_start", "synergy.momentum_thrust"),
    "in_uptrend_structure": ("momentum.rsi_pullback", "momentum.cci_escape"),
}
MACD_EXPAND_RATIO = 0.8

# 입력별 필요 이력(거래일). df 로부터 내부 산출할 때의 최소 길이이며, 사전계산 값을 인자로
#  넘기는 백테스트도 같은 워밍업을 거친 값이어야 한다. 이보다 짧으면 그 입력은 None 으로
#  남고 해당 항목은 unknown 이 된다(=점수 0, 기존 동작).
#  키는 config.INDICATOR_PARAMS 의 룩백 키이거나 고정 길이다.
INPUT_HISTORY = {
    "ema120": ("EMA 120", 120),
    "trend_persist": ("TREND_PERSIST_LOOKBACK", 120),
    "mom_ret": ("MOMENTUM_LOOKBACK", 126 + 1),
    "mom_ret_1m": ("MOMENTUM_LOOKBACK_1M", 21 + 1),
    "mom_ret_3m": ("MOMENTUM_LOOKBACK_3M", 63 + 1),
    "w52_pos": ("52주(365 달력일, 실패 시 250봉)", 250),
    "vol_ma": ("VOLUME_MA_PERIOD", 20),
    "prev_cci": ("CCI_WINDOW + 1", 21),
}
# 매수 판단의 '선택' 입력 — 없으면 해당 게이트를 적용하지 않는다(fail-open).
#  그 외 입력은 없으면 해당 항목이 unknown(0점)이 된다.
OPTIONAL_GATE_INPUTS = ("mom_ret_1m", "mom_ret_3m")


def is_missing(v):
    """None 또는 NaN 이면 True (bool·int 는 결측이 아니다)."""
    if v is None:
        return True
    try:
        return bool(v != v)  # NaN 만 자기 자신과 다르다(float·numpy 공통)
    except Exception:
        return False


@dataclass(slots=True)
class ScoreItem:
    item_id: str
    factor: str
    status: str
    base: float               # 설계 배점(가중치 1.0 기준, 감점은 음수)
    weighted: float           # 가중치 반영 배점 = 충족 시 받는 점수(감점은 음수)
    contribution: float       # 총점에 실제로 더해진 값(상한 조정은 cap 항목에 따로 실린다)
    detail: str | None = None  # 화면 설명 문구(점수가 없거나 보류 문구가 없으면 None)
    inputs: dict = field(default_factory=dict)
    unverified: tuple = ()     # fail-open 으로 통과한 확인 불가 입력 이름


def _build_items(raw_items, inputs, axes):
    """채점 중 쌓인 (id, 충족, 기여, 문구, 상태, 미확인) 튜플을 ScoreItem 으로 푼다."""
    out = []
    for item_id, cond, contribution, detail, status, unverified in raw_items:
        factor, base, axis, _slot, required = ITEM_SPECS[item_id]
        weighted = round(abs(base) * axes[axis], 2)
        if base < 0:
            weighted = -weighted
        if status is None:
            if cond:
                status = MET
            elif any(is_missing(inputs[k]) for k in required):
                status = UNKNOWN
            else:
                status = UNMET
        if contribution is None:
            contribution = weighted if status == MET else 0.0
        out.append(ScoreItem(item_id, factor, status, base, weighted, contribution, detail,
                             {k: inputs[k] for k in required}, tuple(unverified)))
    return tuple(out)


@dataclass(slots=True)
class ScoreResult:
    total: float               # 최종 점수 — calculate_score 의 첫 반환값과 동일
    raw_total: float           # 0 하한 적용 전 합계(감점 포함)
    details: list              # calculate_score 의 두 번째 반환값과 동일
    penalty: float
    ma_cluster: dict           # {'raw': 상한 전 합, 'cap': 상한, 'applied': 상한 적용 여부}
    weights: dict              # 실제 적용된 팩터 가중치
    params: dict               # 채점에 쓰인 임계값 스냅샷
    derived_inputs: tuple      # df 로부터 함수 내부에서 산출한 입력 이름(호출자가 안 넘긴 것)
    diagnostics: dict          # 경계 계측용 보조값(예: MACD 확산 여유)
    raw_items: list = field(default_factory=list, repr=False)
    inputs: dict = field(default_factory=dict, repr=False)   # 채점에 실제로 쓰인 입력값
    axes: dict = field(default_factory=dict, repr=False)     # 팩터별 가중치 배수 r_*
    rules_version: str = SCORING_RULES_VERSION
    _items: tuple | None = field(default=None, repr=False)

    @property
    def items(self):
        """항목별 결과(ScoreItem 튜플) — 처음 읽을 때 만든다(채점 경로의 비용 절감)."""
        if self._items is None:
            self._items = _build_items(self.raw_items, self.inputs, self.axes)
        return self._items

    @property
    def missing_inputs(self):
        """채점 입력 중 None/NaN 이었던 이름."""
        return tuple(k for k, v in self.inputs.items() if is_missing(v))

    # ── 조회 도우미
    def item(self, item_id):
        for it in self.items:
            if it.item_id == item_id:
                return it
        raise KeyError(item_id)

    def contribution(self, item_id):
        return self.item(item_id).contribution

    def status(self, item_id):
        return self.item(item_id).status

    def fired(self):
        """점수를 받은(또는 감점된) 항목 ID — 상한 조정 항목은 상한이 걸린 날에만 포함."""
        return tuple(it.item_id for it in self.items if it.contribution != 0)

    def shown(self):
        """화면 설명 문구를 남긴 항목(점수 반영·상한 조정·보류) — 종전 '설명 줄' 단위와 같다."""
        return tuple(it for it in self.items if it.detail is not None)

    def labels(self):
        """화면에 나타난 항목의 보고서 라벨 집합(감사 도구의 발화 집계 단위)."""
        out = set()
        for it in self.shown():
            if it.status == HELD:
                out.add(HELD_LABELS.get(it.item_id, ITEM_LABELS[it.item_id] + "보류"))
            else:
                out.add(ITEM_LABELS[it.item_id])
        return out

    def by_factor(self):
        out = {}
        for it in self.items:
            out[it.factor] = out.get(it.factor, 0.0) + it.contribution
        return out

    @property
    def params_fingerprint(self):
        """가중치·임계값 스냅샷의 짧은 해시 — 같은 규칙판에서 설정이 달랐는지 가른다."""
        blob = json.dumps({"w": self.weights, "p": self.params}, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]

    def as_dict(self, compact=True):
        """기록용 직렬화. compact 면 미충족(unmet) 항목은 뺀다(파이 DB 쓰기량 절감)."""
        items = []
        for it in self.items:
            if compact and it.status == UNMET:
                continue
            d = {"id": it.item_id, "st": it.status, "c": round(it.contribution, 4)}
            if it.unverified:
                d["unv"] = list(it.unverified)
            items.append(d)
        return {
            "total": self.total, "raw": round(self.raw_total, 4), "pen": round(self.penalty, 4),
            "ver": self.rules_version, "fp": self.params_fingerprint,
            "missing": list(self.missing_inputs), "derived": list(self.derived_inputs),
            "diag": {k: (None if is_missing(v) else round(float(v), 6)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                     for k, v in self.diagnostics.items()},
            "items": items,
        }


# ── 시너지 경계 계측 (지시서 5-4) ─────────────────────────────────────────────
# 'MACD 확산 유지'(macd_hist >= prev_macd_hist × 0.8, 둘 다 양수일 때)의 비율 경계 근처를
#  '경계 근처'로 본다. 폭은 비율 단위(±0.1 → 0.7~0.9배). 관측 전용 상수다 — 점수에 쓰이지 않는다.
BOUNDARY_NEAR_BAND = 0.1


def boundary_event(res, buy_score):
    """채점 결과 하나에서 시너지 경계·결측 관련 관측값을 뽑는다(점수·판단 불변).

    반환 dict:
      near      비율 경계(0.8배) ±BOUNDARY_NEAR_BAND 안인가(히스토그램 둘 다 양수일 때만)
      ratio     macd_hist / prev_macd_hist (위 조건일 때만, 아니면 None)
      expanding 확산 조건의 실제 값
      stake     확산 조건 하나에 걸린 시너지 점수(조건이 뒤집히면 움직일 점수)
      cross     'admitted' = 확산 덕에 BUY_SCORE 를 넘겼다(잃으면 미달)
                'excluded' = 확산 실패로 BUY_SCORE 미달(얻으면 넘는다)
                None       = 경계가 문턱을 가르지 않았다
                ※ 점수 문턱만 본다. 상태 판정의 다른 조건은 호출자가 반사실 상태로 따로 잰다.
      cf_score  cross 일 때 확산 조건을 뒤집은 반사실 점수(아니면 None)
      fail_open 가격 모멘텀이 단기 게이트 입력 없이(fail-open) 가점됐는가
      unknown_n 확인 불가(필수 입력 결측) 항목 수
    """
    dg = res.diagnostics or {}
    exp = bool(dg.get("macd_expanding"))
    stake = float(dg.get("synergy_stake") or 0.0)
    h, ph = dg.get("macd_hist"), dg.get("prev_macd_hist")
    ratio = None
    if not is_missing(h) and not is_missing(ph) and h > 0 and ph > 0:
        ratio = float(h) / float(ph)
    near = ratio is not None and abs(ratio - MACD_EXPAND_RATIO) <= BOUNDARY_NEAR_BAND
    total = res.total
    cross = cf = None
    if stake > 0 and buy_score is not None:
        if exp and total >= buy_score and round(max(res.raw_total - stake, 0.0), 2) < buy_score:
            cross, cf = "admitted", round(max(res.raw_total - stake, 0.0), 2)
        elif not exp and total < buy_score <= round(max(res.raw_total + stake, 0.0), 2):
            # 확산이 '거짓'인 이유가 비율 경계가 아니라 히스토그램 음수라면 경계 효과가 아니다.
            if ratio is not None or (not is_missing(h) and h > 0):
                cross, cf = "excluded", round(max(res.raw_total + stake, 0.0), 2)
    fail_open = False
    unknown_n = 0
    for it in res.items:
        if it.item_id == "momentum.price_momentum" and it.unverified and it.status == MET:
            fail_open = True
        if it.status == UNKNOWN:
            unknown_n += 1
    return {"near": near, "ratio": ratio, "expanding": exp, "stake": stake, "cross": cross,
            "cf_score": cf, "fail_open": fail_open, "unknown_n": unknown_n,
            "total": total, "ver": res.rules_version}
