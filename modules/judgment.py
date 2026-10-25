# modules/judgment.py
"""매매 판정 핵심 — 채점(score_breakdown·calculate_score)·종목 상태 분류(classify_stock_state)·지수 국면 분류
(classify_regime_from_df)·52주 위치(_w52_band).

[C2 · 2026-10-25] modules/analysis.py 에서 옮겼다(본문 그대로). analysis.py 는 화면·일괄 분석·시세 조회가 섞인 6,600줄
파일이고, 엔진·청산·백테스트·포트폴리오가 이 판정 함수들을 거기서 가져다 쓴다. 판정만 따로 읽고 시험할 수 있게 떼어 냈다.
· analysis 가 같은 이름을 다시 import 한다 — analysis.calculate_score 처럼 부르고 patch 하는 기존 코드는 그대로 돈다.
· 이 모듈은 DataFrame·지표·설정만 본다. 시세·지수를 직접 조회하는 판정(get_market_regime·get_index_momentum·
  check_smart_money_turnaround·get_live_vol_regime)과 화면용 색·표시(price_trend_color·describe_regime)는 analysis 에
  남겼다. 최상단에서 api·rich·modules.analysis 를 import 하지 않는다(tests/test_judgment_split.py).
· 채점의 patch 지점은 analysis.calculate_score 하나다. 엔진·백테스트·예약 주문 감시·감사 도구(audit_score_items·
  audit_score_swap·audit_synergy_boundary)·테스트가 그 이름을 바꿔친다. 그래서 여기서 채점을 다시 부르는 자리
  (classify_stock_state 의 score 없는 대체 채점)도 _analysis() 로 그 이름을 거친다 — 이 모듈의 calculate_score 를 바로
  부르면 바꿔치기가 그 길에만 닿지 않는다(실측: 테스트 다섯이 그 길로 바꿔친 점수를 받는다).
"""
import json
import threading
from datetime import datetime

import numpy as np
import pandas as pd

import config
from core import indicators
from modules import score_model


def _analysis():
    """채점 patch 지점(analysis.calculate_score)이 있는 모듈. 서로 import 하므로 부를 때 가져온다."""
    from modules import analysis
    return analysis


def calculate_score(price=None, ema20=None, ema60=None, ema120=None, sar=None, rsi=None, adx=None, cci=None, obv_trend=None, macd=None, macd_signal=None, weights=None, smart_money=False, plus_di=None, minus_di=None, df=None, ind=None, ema_5=None, macd_hist=None, prev_macd_hist=None, prev_cci=None, vol_spike=False, vol_trend=False, w52_pos=None, mom_ret=None, mom_ret_1m=None, mom_ret_3m=None, trend_persist=None):
    """퀀트 멀티팩터 스코어링 모델 (10점 만점) — (총점, 설명 문자열 목록) 호환 어댑터.

    채점 본체는 score_breakdown 이다. 이 함수는 기존 호출자와 감사 도구의 monkeypatch 지점
    (analysis.calculate_score)을 유지하려고 남긴 얇은 어댑터이며, 반환값은 리팩터링 전과
    비트 단위로 같다(tests/test_score_breakdown.py). 항목별 수치가 필요하면 문자열을
    해석하지 말고 score_breakdown 을 부를 것.
    """
    res = score_breakdown(price=price, ema20=ema20, ema60=ema60, ema120=ema120, sar=sar, rsi=rsi, adx=adx, cci=cci,
                          obv_trend=obv_trend, macd=macd, macd_signal=macd_signal, weights=weights,
                          smart_money=smart_money, plus_di=plus_di, minus_di=minus_di, df=df, ind=ind, ema_5=ema_5,
                          macd_hist=macd_hist, prev_macd_hist=prev_macd_hist, prev_cci=prev_cci,
                          vol_spike=vol_spike, vol_trend=vol_trend, w52_pos=w52_pos, mom_ret=mom_ret,
                          mom_ret_1m=mom_ret_1m, mom_ret_3m=mom_ret_3m, trend_persist=trend_persist)
    _LAST_BREAKDOWN.res = res
    return res.total, res.details


# [관측] 어댑터가 방금 만든 구조화 결과를 **같은 스레드에서** 꺼내는 통로. 엔진은 기존처럼
#  calculate_score 를 부르고(테스트·감사 도구의 monkeypatch 지점 유지), 계측이 필요할 때만
#  last_breakdown_for(details) 로 그 호출의 항목 결과를 얻는다 — 채점을 두 번 하지 않는다.
#  details 리스트의 **동일성**(is)으로 '바로 그 호출'인지 확인하므로, calculate_score 가 목으로
#  바뀌었거나 다른 호출이 끼어든 경우에는 None 을 돌려준다(계측만 빠지고 판단은 그대로).
_LAST_BREAKDOWN = threading.local()


def last_breakdown_for(details):
    res = getattr(_LAST_BREAKDOWN, "res", None)
    if res is not None and details is not None and res.details is details:
        return res
    return None


def _resolve_weights(weights):
    if weights is None: weights = config.SCORING_WEIGHTS
    # [안전장치] 개별 룰의 가중치는 DB에 JSON 문자열로 저장된다. 보강 단계가 실패하면
    #  문자열이 그대로 들어와 아래 weights.get()이 AttributeError를 낸다. 점수 계산은
    #  매수·매도 판정 양쪽의 심장이라, 여기서 죽으면 그 종목이 판정에서 통째로 빠진다.
    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except Exception:
            weights = config.SCORING_WEIGHTS
    if not isinstance(weights, dict): weights = config.SCORING_WEIGHTS
    return weights


def score_breakdown(price=None, ema20=None, ema60=None, ema120=None, sar=None, rsi=None, adx=None, cci=None, obv_trend=None, macd=None, macd_signal=None, weights=None, smart_money=False, plus_di=None, minus_di=None, df=None, ind=None, ema_5=None, macd_hist=None, prev_macd_hist=None, prev_cci=None, vol_spike=False, vol_trend=False, w52_pos=None, mom_ret=None, mom_ret_1m=None, mom_ret_3m=None, trend_persist=None):
    """퀀트 멀티팩터 스코어링 모델 (10점 만점) — 항목별 구조화 결과(score_model.ScoreResult).

    입력 해석·산술 순서는 리팩터링 전 calculate_score 와 같다. 항목의 상태·배점·기여·
    입력값과 규칙판·임계값 스냅샷을 함께 돌려준다(score_model 독스트링 참조).
    """
    weights = _resolve_weights(weights)

    # r_* 는 '각 팩터의 세부 항목 기본배점 합(=설계상 만점)' 대비 사용자 가중치의 스케일 배수다.
    # 분모는 세부항목 기본배점 합(고정값)이며 분자만 가중치로 바뀐다.
    #   예) TREND 세부항목 기본합=4.0. 가중치 4.0이면 r_trend=1.0 → 추세 팩터가 4.0점 만점.
    # 4개 팩터 기본배점: 추세4.0 + 모멘텀2.5 + 강도1.5 + 시너지2.0 = 10.0 (총점 10점)
    r_trend = weights.get("TREND", 4.0) / 4.0
    r_mom = weights.get("MOMENTUM", 2.5) / 2.5
    r_str = weights.get("STRENGTH", 1.5) / 1.5
    r_syn = weights.get("SYNERGY", 2.0) / 2.0

    # [관측] 호출자가 넘기지 않아 아래에서 df 로부터 산출한 입력 — 실매매(df 경로)와
    #  백테스트(사전계산 인자 경로)가 같은 판단에서 어느 값을 썼는지 가르는 근거다.
    derived = []

    # [Fix] df가 전달되지 않는 백테스팅 환경을 위한 변수 초기화
    vol_spike_flag = vol_spike
    vol_trend_flag = vol_trend

    if df is not None and ind is not None:
        if not df.empty: price = float(df.iloc[-1]['close'])
        ema20 = ind.get('ema_20')
        ema60 = ind.get('ema_60')
        ema120 = ind.get('ema_120')
        sar = ind.get('psar')
        rsi = ind.get('rsi')
        adx = ind.get('adx')
        cci = ind.get('cci')
        obv_trend = ind.get('obv_trend')
        macd = ind.get('macd')
        macd_signal = ind.get('macd_signal')
        if plus_di is None: plus_di = ind.get('plus_di')
        if minus_di is None: minus_di = ind.get('minus_di')
        
        # [추가] 인자로 넘어오지 않은 세부 지표들을 ind 딕셔너리에서 직접 추출하여 일관성(SSOT) 확보
        if ema_5 is None: ema_5 = ind.get('ema_5')
        if prev_cci is None: prev_cci = ind.get('prev_cci')
        if macd_hist is None: macd_hist = ind.get('macd_hist')
        if prev_macd_hist is None: prev_macd_hist = ind.get('prev_macd_hist')

    import numpy as np
    
    if df is not None and not df.empty:
        # [Early] 선행 지표 동적 계산
        if ema_5 is None:
            ema_5 = df['close'].ewm(span=config.INDICATOR_PARAMS.get('EMA_SHORT', 5), adjust=False).mean().iloc[-1]
            derived.append('ema_5')
            
        if macd is not None and macd_signal is not None and (macd_hist is None or prev_macd_hist is None):
            fast = config.INDICATOR_PARAMS.get('MACD_FAST', 12)
            slow = config.INDICATOR_PARAMS.get('MACD_SLOW', 26)
            sig = config.INDICATOR_PARAMS.get('MACD_SIGNAL', 9)
            macd_series = df['close'].ewm(span=fast, adjust=False).mean() - df['close'].ewm(span=slow, adjust=False).mean()
            signal_series = macd_series.ewm(span=sig, adjust=False).mean()
            hist_series = macd_series - signal_series
            if len(hist_series) > 0: macd_hist = hist_series.iloc[-1]
            if len(hist_series) > 1: prev_macd_hist = hist_series.iloc[-2]
            derived.append('macd_hist')

        if plus_di is None or minus_di is None:
            try:
                high_diff = df['high'].diff()
                low_diff = df['low'].diff()
                pos_dm = np.where((high_diff > 0) & (high_diff > -low_diff), high_diff, 0.0)
                neg_dm = np.where((low_diff < 0) & (-low_diff > high_diff), -low_diff, 0.0)
                tr1 = df['high'] - df['low']
                tr2 = (df['high'] - df['close'].shift()).abs()
                tr3 = (df['low'] - df['close'].shift()).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                adx_period = config.INDICATOR_PARAMS.get('ADX_PERIOD', 14)
                atr = tr.ewm(alpha=1/adx_period, adjust=False).mean()
                atr_val = atr.iloc[-1]
                if atr_val == 0 or np.isnan(atr_val): atr_val = 1.0 # 0 나누기 방어
                plus_di = 100 * pd.Series(pos_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
                minus_di = 100 * pd.Series(neg_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
                derived.append('dmi')
            except Exception: pass

        if prev_cci is None and len(df) > 20:
            try:
                window = config.INDICATOR_PARAMS.get('CCI_WINDOW', 20)
                # [최적화] 동일 수식의 벡터화 버전에 위임 (rolling.apply 대비 100배 이상 빠름, 결과 동일)
                cci_series = indicators.get_cci_full_series(df, window)
                if len(cci_series) > 1: prev_cci = cci_series.iloc[-2]
                derived.append('prev_cci')
            except Exception: pass

        vol_ma_period = config.INDICATOR_PARAMS.get('VOLUME_MA_PERIOD', 20)
        if len(df) >= vol_ma_period:
            vol_ma20 = df['volume'].rolling(window=vol_ma_period).mean().iloc[-1]
            vol_ma5 = df['volume'].rolling(window=5).mean().iloc[-1]
            derived.append('volume')

            if vol_ma5 > vol_ma20:
                vol_trend_flag = True

            if not vol_spike_flag:
                vol_ratio = config.INDICATOR_PARAMS.get('VOLUME_SPIKE_RATIO', 2.0)
                vol = df['volume'].iloc[-1]
                opn = df['open'].iloc[-1]
                if price is not None and vol_ma20 > 0 and vol >= (vol_ma20 * vol_ratio) and price > opn:
                    vol_spike_flag = True

        # [추세추종] 가격 모멘텀 팩터 입력값 동적 계산 (백테스트는 사전계산 값을 인자로 전달)
        try:
            if w52_pos is None and price is not None and 'high' in df.columns and 'low' in df.columns:
                # 화면(표·상세·지수)과 동일한 365일 창. 백테스트는 w52_pos를 인자로 넘기므로
                # 여기 오지 않고, 와도 창이 비어 _w52_band가 기존 tail(250)으로 폴백한다.
                h52, l52 = _w52_band(df)
                if h52 > l52:
                    w52_pos = (price - l52) / (h52 - l52) * 100
                    derived.append('w52_pos')
            if mom_ret is None and price is not None:
                mom_lb = config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
                if len(df) > mom_lb:
                    past_close = float(df['close'].iloc[-(mom_lb + 1)])
                    if past_close > 0:
                        mom_ret = (price / past_close - 1) * 100
                        derived.append('mom_ret')
            # [추세추종] 다중 기간 모멘텀(1·3개월) — 6개월 가격 모멘텀 가점의 정합 게이트 입력
            if mom_ret_1m is None and price is not None:
                lb = config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK_1M', 21)
                if len(df) > lb:
                    past_close = float(df['close'].iloc[-(lb + 1)])
                    if past_close > 0:
                        mom_ret_1m = (price / past_close - 1) * 100
                        derived.append('mom_ret_1m')
            if mom_ret_3m is None and price is not None:
                lb = config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK_3M', 63)
                if len(df) > lb:
                    past_close = float(df['close'].iloc[-(lb + 1)])
                    if past_close > 0:
                        mom_ret_3m = (price / past_close - 1) * 100
                        derived.append('mom_ret_3m')
            # [추세추종] 추세 지속 이력 — 최근 N일 중 종가가 60일선 위였던 비율(%).
            #   오래 유지된 추세가 계속될 확률이 높다는 지속성 원칙의 직접 측정치.
            if trend_persist is None:
                persist_lb = config.INDICATOR_PARAMS.get('TREND_PERSIST_LOOKBACK', 120)
                if len(df) >= persist_lb:
                    ema60_series = df['close'].ewm(span=60, adjust=False).mean()
                    above = df['close'].tail(persist_lb).to_numpy(dtype=float) > ema60_series.tail(persist_lb).to_numpy(dtype=float)
                    trend_persist = float(above.mean() * 100)
                    derived.append('trend_persist')
        except Exception:
            pass

    score_rsi_mid = config.INDICATOR_PARAMS.get('SCORE_RSI_MID', 50)
    score_rsi_strong = config.INDICATOR_PARAMS.get('SCORE_RSI_STRONG', 60)
    score_rsi_overheat = config.INDICATOR_PARAMS.get('SCORE_RSI_OVERHEAT', 80)
    score_rsi_rebound = config.INDICATOR_PARAMS.get('SCORE_RSI_REBOUND', 40)
    cci_lower = config.INDICATOR_PARAMS.get('CCI_LOWER', -100)
    cci_strong = config.INDICATOR_PARAMS.get('SCORE_CCI_STRONG', 0)
    mom_w52_near = config.INDICATOR_PARAMS.get('MOMENTUM_W52_NEAR', 80)
    adx_min = config.INDICATOR_PARAMS.get('SCORE_ADX_MIN', 20)
    persist_min = config.INDICATOR_PARAMS.get('TREND_PERSIST_MIN', 70)
    persist_lb_param = config.INDICATOR_PARAMS.get('TREND_PERSIST_LOOKBACK', 120)
    params = {
        "SCORE_RSI_MID": score_rsi_mid, "SCORE_RSI_STRONG": score_rsi_strong,
        "SCORE_RSI_OVERHEAT": score_rsi_overheat, "SCORE_RSI_REBOUND": score_rsi_rebound,
        "CCI_LOWER": cci_lower, "SCORE_CCI_STRONG": cci_strong, "MOMENTUM_W52_NEAR": mom_w52_near,
        "SCORE_ADX_MIN": adx_min, "TREND_PERSIST_MIN": persist_min,
        "TREND_PERSIST_LOOKBACK": persist_lb_param, "MACD_EXPAND_RATIO": score_model.MACD_EXPAND_RATIO,
        "MA_CLUSTER_CAP": score_model.MA_CLUSTER_CAP_BASE,
    }
    applied_weights = {"TREND": weights.get("TREND", 4.0), "MOMENTUM": weights.get("MOMENTUM", 2.5),
                       "STRENGTH": weights.get("STRENGTH", 1.5), "SYNERGY": weights.get("SYNERGY", 2.0)}

    miss = score_model.is_missing
    inputs_all = {
        "price": price, "ema20": ema20, "ema60": ema60, "ema120": ema120, "ema_5": ema_5, "sar": sar,
        "rsi": rsi, "adx": adx, "cci": cci, "prev_cci": prev_cci, "macd": macd, "macd_signal": macd_signal,
        "macd_hist": macd_hist, "prev_macd_hist": prev_macd_hist, "plus_di": plus_di, "minus_di": minus_di,
        "w52_pos": w52_pos, "mom_ret": mom_ret, "mom_ret_1m": mom_ret_1m, "mom_ret_3m": mom_ret_3m,
        "trend_persist": trend_persist, "obv_trend": obv_trend,
    }
    r_axes = {"TREND": r_trend, "MOMENTUM": r_mom, "STRENGTH": r_str, "SYNERGY": r_syn}

    # [성능] 백테스트·감사는 이 함수를 수십만 번 부른다. 채점 중에는 항목을 튜플로만 쌓고,
    #  ScoreItem 객체(상태 판정·입력 사본 포함)는 결과의 .items 를 처음 읽을 때 만든다
    #  (score_model.ScoreResult). 총점·설명만 쓰는 calculate_score 경로는 그 비용을 내지 않는다.
    items = []

    def _item(item_id, cond, contribution=None, detail=None, status=None, unverified=()):
        """항목 하나를 기록한다. cond 는 조건 충족 여부, status 를 주면 그대로 쓴다."""
        items.append((item_id, cond, contribution, detail, status, unverified))

    def _result(total, raw_total, details, penalty, ma_cluster, diagnostics):
        return score_model.ScoreResult(
            total=total, raw_total=raw_total, details=details, penalty=penalty, ma_cluster=ma_cluster,
            weights=applied_weights, params=params, derived_inputs=tuple(derived), diagnostics=diagnostics,
            raw_items=items, inputs=inputs_all, axes=r_axes)

    if price is None:
        return _result(0.0, 0.0, [], 0.0,
                       {"raw": 0.0, "cap": round(score_model.MA_CLUSTER_CAP_BASE * r_trend, 2), "applied": False}, {})

    score = 0

    # 1. Trend Factor (4.0점) — MA 군집(상한 2.0) + 추세 지속 이력 0.5 + MACD 0선 0.5 + MACD GC/확산 0.5 + SAR 0.5
    # [개선 #2] 이동평균선(EMA) 기반 신호들은 상호 상관(collinearity)이 매우 높아
    #          정배열 상승장에서 동시 충족되며 추세추종으로 점수가 편향됨.
    #          → MA 포지션 점수 합계를 상한으로 제한하고, 나머지는
    #            상대적으로 독립적인 확인 신호(MACD 0선/추세확산, SAR)로 채워
    #            'MA 군집 단독'으로는 TREND 만점을 받지 못하도록 재구성.
    # [추세추종] MA 상한 2.5 → 2.0: 절감분 0.5는 '추세 지속 이력'(아래) 가점으로 이관.
    #          현재 상태(정배열)만 보는 MA 군집과 달리 '얼마나 오래 유지됐는가'를 재는
    #          독립 신호로, 갓 골든크로스한 미검증 추세와 장기 지속 추세를 점수로 구분한다.
    ma_trend_score = 0.0

    def _ma(item_id, cond, label):
        nonlocal ma_trend_score
        if cond:
            s = round(score_model.ITEM_SPECS[item_id][1] * r_trend, 2); ma_trend_score += s
            _item(item_id, True, contribution=s, detail=f"{label} (+{s:.2f})")
        else:
            _item(item_id, False)

    _ma("trend.ema20_above", ema20 is not None and price > ema20, "EMA: 현재가 > 20일선")
    _ma("trend.ema_aligned", ema20 is not None and ema60 is not None and ema120 is not None and ema20 > ema60 and ema60 > ema120, "EMA: 20/60/120 정배열")
    _ma("trend.ema5_over_20", ema20 is not None and ema_5 is not None and ema_5 > ema20, "EMA: 5일선 > 20일선")
    # 60일선 돌파[초기] / 단기 급등 — 같은 슬롯(0.5)을 나눠 쓰는 elif 관계(조건상 상호 배타)
    _brk = ema20 is not None and ema60 is not None and ema20 <= ema60 and price > ema60
    _ma("trend.ma60_breakout_early", _brk, "EMA: 60일선 돌파 [초기]")
    _srg = (not _brk and ema20 is not None and ema60 is not None and ema_5 is not None
            and price > ema_5 and ema_5 > ema20 and ema20 > ema60)
    _ma("trend.short_surge", _srg, "EMA: 단기 급등 추세")
    _ma("trend.ema120_support", ema120 is not None and price > ema120, "EMA: 장기 지지(현재가>120일선)")

    # [개선 #2] MA 포지션 점수 상한 적용 (상관 신호의 과대 가점 방지)
    #  상한이 깎은 양은 trend.ma_cluster_cap 한 항목의 음수 기여로 싣는다 — 개별 EMA 항목의
    #  기여는 상한 전 값 그대로라 '단순 합'과 '실제 반영값'의 차이가 결과에 보인다.
    ma_cap = round(score_model.MA_CLUSTER_CAP_BASE * r_trend, 2)
    ma_raw = ma_trend_score
    ma_capped = ma_trend_score > ma_cap
    if ma_capped:
        ma_trend_score = ma_cap
        _item("trend.ma_cluster_cap", True, contribution=ma_cap - ma_raw, detail=f"[상한] EMA 군집 신호 상한 적용 ({ma_cap:.2f})")
    else:
        _item("trend.ma_cluster_cap", False, status=score_model.UNMET, contribution=0.0)
    score += ma_trend_score

    # [추세추종] 추세 지속 이력: 최근 TREND_PERSIST_LOOKBACK일 중 종가가 60일선 위였던
    #   비율이 기준(TREND_PERSIST_MIN%) 이상일 때 가점. 순간 상태가 아닌 '유지된 기간'을
    #   재는 지속성 신호로, MA 상한 축소분(0.5)을 이관받았다. (NaN은 비교식에서 자동 False)
    if trend_persist is not None and trend_persist >= persist_min:
        s = round(0.5 * r_trend, 2); score += s
        _item("trend.persist", True, contribution=s, detail=f"추세 지속: 최근 {persist_lb_param}일 중 {trend_persist:.0f}% 60일선 위 (+{s:.2f})")
    else:
        _item("trend.persist", False)

    # [개선 #2] MACD 0선 위(추세 확립): MA 포지션과 독립적인 추세 확인 신호
    if macd is not None and macd > 0:
        s = round(0.5 * r_trend, 2); score += s
        _item("trend.macd_above_zero", True, contribution=s, detail=f"MACD: 0선 위 (추세 확립) (+{s:.2f})")
    else:
        _item("trend.macd_above_zero", False)

    # [수정] 단순 MACD > Signal 상태 유지가 아닌, 신규 골든크로스 또는 0선 위 확산 추세일 때만 점수 부여 (인플레이션 방지)
    _gc = macd_hist is not None and prev_macd_hist is not None and macd_hist > 0 and prev_macd_hist <= 0
    _exp_cond = (macd_hist is not None and prev_macd_hist is not None and macd is not None and macd > 0
                 and macd_hist > prev_macd_hist and macd_hist > 0)
    if _gc:
        s = round(0.5 * r_trend, 2); score += s
        _item("trend.macd_new_gc", True, contribution=s, detail=f"MACD: 신규 골든크로스 (+{s:.2f})")
        _item("trend.macd_expanding", False, status=(score_model.SHADOWED if _exp_cond else None), contribution=0.0)
    elif _exp_cond:
        _item("trend.macd_new_gc", False)
        s = round(0.5 * r_trend, 2); score += s
        _item("trend.macd_expanding", True, contribution=s, detail=f"MACD: 상승 추세 확산 (+{s:.2f})")
    else:
        _item("trend.macd_new_gc", False)
        _item("trend.macd_expanding", False)

    if sar is not None and price > sar:
        s = round(0.5 * r_trend, 2); score += s
        _item("trend.sar", True, contribution=s, detail=f"SAR: 상승 추세 (+{s:.2f})")
    else:
        _item("trend.sar", False)
    
    # 2. Momentum Factor (2.5점) — RSI 1.0 + CCI 0.5 + DMI 0.5 + 가격 모멘텀 0.5
    # [추세추종] 반등 성격 가점(RSI 상승 여력, CCI 과매도 탈출)은 추세 구조(주가>60일선) 위에서만 인정.
    #   USE_MEAN_REVERSION 기본 OFF 기조와 정합: '상승 추세 내 눌림목 회복'만 가점하고,
    #   추세 없는 약세 종목의 단순 기술적 반등이 매수 점수를 밀어올리는 것을 차단.
    in_uptrend_structure = ema60 is not None and price > ema60

    rsi_strong = rsi_ext = rsi_pull = False
    if rsi is not None:
        # [Fix] RSI 상단 제한(75) 해제로 과열 구간에서도 기본 강세 점수는 유지 (스코어 클리프 방지)
        if rsi >= score_rsi_mid:
            rsi_strong = True
            s = round(0.5 * r_mom, 2); score += s
            _item("momentum.rsi_strong", True, contribution=s, detail=f"RSI: 강세 구간 (+{s:.2f})")
            # [개선 #6] 과열 구간(>=80)에서는 추가 '모멘텀 확장' 가점을 동결하여
            #          이미 과열된 종목에 고점매수 신호가 강화되는 것을 방지.
            if score_rsi_strong <= rsi < score_rsi_overheat:
                rsi_ext = True
                s = round(0.5 * r_mom, 2); score += s
                _item("momentum.rsi_extension", True, contribution=s, detail=f"RSI: 모멘텀 확장 (+{s:.2f})")
        elif score_rsi_rebound <= rsi < score_rsi_mid and in_uptrend_structure:
            rsi_pull = True
            s = round(0.5 * r_mom, 2); score += s
            _item("momentum.rsi_pullback", True, contribution=s, detail=f"RSI: 상승 여력 구간(추세 내 눌림) (+{s:.2f})")
    if not rsi_strong:
        _item("momentum.rsi_strong", False)
    if not rsi_ext:
        _item("momentum.rsi_extension", False)
    if not rsi_pull:
        _item("momentum.rsi_pullback", False)

    cci_up = cci is not None and cci > cci_strong
    cci_esc_cond = (cci is not None and in_uptrend_structure and prev_cci is not None
                    and prev_cci <= cci_lower and cci > cci_lower)
    if cci is not None:
        # [개선] 'CCI 모멘텀 심화'는 'CCI 상승 추세'·RSI 강세와 상관이 높아 정보 가치가 낮으므로 폐지하고,
        #        그 0.5점을 아래 '가격 모멘텀' 항목으로 이관. CCI는 상승/과매도탈출 중 하나만 인정(최대 0.5).
        if cci_up:
            s = round(0.5 * r_mom, 2); score += s
            _item("momentum.cci_up", True, contribution=s, detail=f"CCI: 상승 추세 (+{s:.2f})")
        elif cci_esc_cond:
            s = round(0.5 * r_mom, 2); score += s
            _item("momentum.cci_escape", True, contribution=s, detail=f"CCI: 과매도권 탈출(추세 내) (+{s:.2f})")
    if not cci_up:
        _item("momentum.cci_up", False)
    if not (cci_esc_cond and not cci_up):
        _item("momentum.cci_escape", False, status=(score_model.SHADOWED if (cci_esc_cond and cci_up) else None), contribution=0.0)

    if plus_di is not None and minus_di is not None and plus_di > minus_di:
        s = round(0.5 * r_mom, 2); score += s
        _item("momentum.dmi", True, contribution=s, detail=f"DMI: +DI > -DI 크로스 (+{s:.2f})")
    else:
        _item("momentum.dmi", False)

    # [추세추종] 가격 모멘텀 (절대 모멘텀 + 52주 신고가 근접) — '강한 종목을 매수하라' 핵심 팩터.
    #   6개월(MOMENTUM_LOOKBACK) 수익률이 양수이고 52주 위치가 기준(MOMENTUM_W52_NEAR) 이상인
    #   주도주에만 가점하여, 지표만 좋은 바닥권 종목과 신고가 랠리 종목을 점수로 구분한다.
    # [추세추종] 다중 기간 모멘텀 정합: 1·3개월 수익률이 명시적으로 음수면 6개월 가점을 보류.
    #   6개월 수치만 좋고 최근 1~3개월이 꺾인 '식어가는 추세'의 고점 진입을 걸러낸다.
    #   단기 수익률을 알 수 없으면(None/NaN) 게이트를 적용하지 않는다(fail-open, 기존 호출자 호환).
    #   [관측] 그 경우 unverified 에 확인하지 못한 입력을 남긴다 — 점수는 바꾸지 않는다.
    mom_align_ok = True
    if mom_ret_1m is not None and mom_ret_1m <= 0: mom_align_ok = False
    if mom_ret_3m is not None and mom_ret_3m <= 0: mom_align_ok = False

    if mom_ret is not None and mom_ret > 0 and w52_pos is not None and w52_pos >= mom_w52_near:
        if mom_align_ok:
            unv = tuple(k for k in score_model.OPTIONAL_GATE_INPUTS if miss(inputs_all[k]))
            s = round(0.5 * r_mom, 2); score += s
            _item("momentum.price_momentum", True, contribution=s, unverified=unv, detail=f"가격 모멘텀: 6개월 +{mom_ret:.1f}% & 52주 위치 {w52_pos:.0f}% (+{s:.2f})")
        else:
            _item("momentum.price_momentum", False, status=score_model.HELD, contribution=0.0, detail="가격 모멘텀 보류: 단기(1·3개월) 모멘텀 이탈 (식어가는 추세)")
    else:
        _item("momentum.price_momentum", False)

    # 3. Strength & Volume Factor (1.5점)
    if adx is not None and adx >= adx_min:
        s = round(0.5 * r_str, 2); score += s
        _item("strength.adx", True, contribution=s, detail=f"ADX: 추세 형성 (+{s:.2f})")
    else:
        _item("strength.adx", False)
        
    if vol_spike_flag or vol_trend_flag:
        s = round(0.5 * r_str, 2); score += s
        if vol_spike_flag:
            _item("strength.vol_spike", True, contribution=s, detail=f"VOL: 거래량 폭증/양봉 (+{s:.2f})")
            _item("strength.vol_trend", False, status=(score_model.SHADOWED if vol_trend_flag else None), contribution=0.0)
        else:
            _item("strength.vol_spike", False)
            _item("strength.vol_trend", True, contribution=s, detail=f"VOL: 거래량 추세 상승 (+{s:.2f})")
    else:
        _item("strength.vol_spike", False)
        _item("strength.vol_trend", False)
        
    if obv_trend or smart_money:
        s = round(0.5 * r_str, 2); score += s
        _item("strength.flow", True, contribution=s, detail=f"수급: OBV/SM 개선 (+{s:.2f})")
    else:
        _item("strength.flow", False, status=(score_model.UNKNOWN if obv_trend is None and not smart_money else None))

    # 4. Synergy Bonus (2.0점)
    # [수정] 시너지 보너스를 확산(macd_hist > prev_macd_hist) 조건 단독에서 완화. MACD가 시그널 위에 있고(macd_hist > 0) 심한 축소가 아닐 때 유지하여 단기 노이즈로 인한 2.0점 증발 방어
    #  [경계 계측] 두 시너지 항목이 이 조건을 공유하므로 경계(macd_hist == prev*0.8)에서 2점이
    #   함께 움직인다. 여유값(macd_hist - prev*0.8)을 diagnostics 에 남겨 경계 근처 판단을 센다.
    is_macd_expanding = False
    macd_expand_margin = None
    if macd_hist is not None and prev_macd_hist is not None:
        is_macd_expanding = (macd_hist > 0 and macd_hist >= prev_macd_hist * score_model.MACD_EXPAND_RATIO)
        try:
            macd_expand_margin = float(macd_hist - prev_macd_hist * score_model.MACD_EXPAND_RATIO)
        except Exception:
            macd_expand_margin = None

    # [개선] 시너지 구조 게이트: 장기 역배열(주가<120일선) '이고' MACD가 0선 아래인 '지하실'에서
    #   히스토그램만 골든크로스한 '데드캣 바운스'가 시너지 2.0점을 쓸어담는 것을 차단.
    #   ADX는 방향을 모르고(폭락장도 높음), is_macd_expanding은 0선 아래 크로스도 True가 되므로
    #   단독으로는 역추세 급반등을 걸러내지 못한다. macd>0(추세 확립) 또는 주가>120일선(장기 구조
    #   회복) 중 하나라도 충족할 때만 시너지를 인정한다. (단독 Trend 가점의 macd>0 게이트와 정합)
    synergy_structure_ok = not (macd is not None and macd <= 0 and (ema120 is None or price <= ema120))

    #  [경계 계측] 확산 조건 하나에 걸려 있는 시너지 점수(stake) — 나머지 조건은 충족한 항목의
    #   배점 합이다. 확산이 참이면 '이 조건이 뒤집히면 잃을 점수', 거짓이면 '뒤집히면 얻을 점수'.
    #   조건식을 다른 곳에서 다시 쓰지 않도록 채점 코드 안에서 함께 센다(관측 전용, 점수 불변).
    _syn_unit = round(1.0 * r_syn, 2)
    _ts_other = bool(ema60 is not None and price > ema60 and (adx is not None and adx >= adx_min) and synergy_structure_ok)
    _mt_other = bool((rsi is not None and rsi >= score_rsi_strong) and obv_trend and synergy_structure_ok)
    synergy_stake = _syn_unit * (int(_ts_other) + int(_mt_other))

    if ema60 is not None and price > ema60 and is_macd_expanding and (adx is not None and adx >= adx_min) and synergy_structure_ok:
        s = round(1.0 * r_syn, 2)
        score += s
        _item("synergy.trend_start", True, contribution=s, detail=f"추세 시작: 주가>60일선+MACD확산+ADX 20↑ (+{s:.2f})")
    else:
        _item("synergy.trend_start", False)

    # Momentum Thrust
    if is_macd_expanding and (rsi is not None and rsi >= score_rsi_strong) and obv_trend and synergy_structure_ok:
        s = round(1.0 * r_syn, 2)
        score += s
        _item("synergy.momentum_thrust", True, contribution=s, detail=f"모멘텀 폭발: MACD확산+RSI 60↑+OBV (+{s:.2f})")
    else:
        _item("synergy.momentum_thrust", False)

    # 5. 추세 악화 감점 (Deterioration Penalty)
    # [개선 #1] 기존 스코어는 '가산 전용'이라 추세가 꺾여도 후행 지표(EMA 정배열 등)가
    #          점수를 떠받쳐 매도 신호(SELL_SCORE 미달)가 지연되는 구조적 약점이 있음.
    #          명확한 하락 반전 신호에 대해 감점을 부여하여 점수가 악화를 적시 반영하도록 보정.
    penalty = 0.0
    if macd is not None and macd_signal is not None and macd < macd_signal:
        p = round(0.5 * r_trend, 2); penalty -= p
        _item("penalty.macd_dead_cross", True, contribution=-p, detail=f"감점: MACD 데드크로스 (-{p:.2f})")
    else:
        _item("penalty.macd_dead_cross", False)
    if macd_hist is not None and prev_macd_hist is not None and macd_hist < 0 and macd_hist < prev_macd_hist:
        p = round(0.5 * r_mom, 2); penalty -= p
        _item("penalty.macd_accel_down", True, contribution=-p, detail=f"감점: MACD 하락 가속(0선 이하 확대) (-{p:.2f})")
    else:
        _item("penalty.macd_accel_down", False)
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        p = round(0.5 * r_str, 2); penalty -= p
        _item("penalty.minus_di", True, contribution=-p, detail=f"감점: -DI 우위(매도세 강화) (-{p:.2f})")
    else:
        _item("penalty.minus_di", False)

    score += penalty
    raw_total = score
    if score < 0:
        score = 0.0

    # 화면 설명은 항목 순서 그대로(=리팩터링 전 문자열 순서). 단, EMA 군집 상한 문구는
    #  리팩터링 전처럼 EMA 항목들 바로 뒤에 온다 — 항목 기록 순서가 이미 그렇다.
    #  같은 슬롯의 대체 항목은 '충족한 쪽'이 먼저 기록되도록 위에서 순서를 맞췄다.
    details = [t[3] for t in items if t[3] is not None]

    return _result(round(score, 2), raw_total, details, penalty,
                   {"raw": ma_raw, "cap": ma_cap, "applied": ma_capped},
                   {"macd_expand_margin": macd_expand_margin, "macd_expanding": is_macd_expanding,
                    "synergy_structure_ok": synergy_structure_ok, "synergy_stake": synergy_stake,
                    "macd_hist": macd_hist, "prev_macd_hist": prev_macd_hist})


def classify_regime_from_df(df, params=None):
    """[국면 판정 핵심] 이중 EMA 교차 + 추종 확인(follow-through) 규칙.

    에드 세이코타 방식: 빠른 EMA(9일, β=0.25)와 느린 EMA(41일, β=0.05)가 교차하면
    방향 전환으로 보되, 교차 이후 지수가 확인 기준(기본 5%)만큼 진행해야 '확정 추세'로
    인정한다. 미달 상태에서 다시 교차하면 그 구간은 휩소(실패한 추세)로 집계한다.

    Returns:
        dict: {
          'regime': 'Bull'|'PendUp'|'PendDown'|'Bear'|'Sideways',
          'moved_pct': 교차 이후 현재까지 진행률(%),
          'whipsaw_ratio': 직전 N개 완료 교차 중 확인 기준 미달 비율(0~1, 산출 불가 시 None),
          'segments': 집계에 사용한 완료 교차 구간 수,
          'unknown': **판정 자체를 못 했는가**(데이터 부족/조회 실패). True 면 나머지 값은
                     자리를 채운 기본값일 뿐 시장에 대해 아무것도 말하지 않는다.
        }
        데이터가 부족하면 regime='Sideways' + unknown=True 를 돌려준다.

    [왜 unknown 이 따로 있는가 · 2026-09-08] 종전에는 '지수를 못 읽었다'와 '실제로 횡보다'가
     **글자 하나 다르지 않은 같은 딕셔너리**였다. 그 값을 읽는 리스크 스케일링은 횡보를
     '축소할 이유 없음'으로 보므로, 지수 조회가 끊긴 순간 진입 리스크 한도가 조용히
     x1.00(최대)으로 되돌아갔다 — 실측: 하락 미확정+휩소 85%에서 x0.51 이던 배수가 다음
     주기에 x1.00, 로그는 '리스크 한도 정상 복원'. 회복된 것이 아니라 눈을 감은 것이다.

    지수 화면(market.py)과 자동매매(analysis.get_market_regime)가 같은 판정을 쓰도록
    데이터프레임만 받는 순수 함수로 분리했다.
    """
    params = params or getattr(config, 'MARKET_REGIME_PARAMS', {}) or {}
    slow = int(params.get("REGIME_EMA_SLOW", 41))

    blank = {'regime': "Sideways", 'moved_pct': 0.0, 'whipsaw_ratio': None, 'segments': 0,
             'unknown': True}
    if df is None or df.empty or 'close' not in df.columns or len(df) < slow:
        return blank

    close = pd.to_numeric(df['close'], errors='coerce').dropna()
    if len(close) < slow:
        return blank

    # 판정식은 indicators.get_regime_series 하나만 둔다 — 시장 필터(국면 동시 확인)도 같은 함수를
    # 쓰므로, 여기서 로직을 따로 들고 있으면 두 판정이 소리 없이 갈라진다.
    ser = indicators.get_regime_series(close, params)
    whipsaw = ser['whipsaw'][-1]
    return {'regime': ser['regime'][-1], 'moved_pct': float(ser['moved_pct'][-1]),
            'whipsaw_ratio': None if np.isnan(whipsaw) else float(whipsaw),
            'segments': int(ser['segments'][-1]), 'unknown': False}


def classify_stock_state(price=None, ema20=None, ema60=None, ema120=None, sar=None, rsi=None, prev_rsi=None, adx=None, cci=None, obv_trend=None, macd=None, macd_signal=None, thresholds=None, w52_pos=None, smart_money=False, plus_di=None, minus_di=None, df=None, ind=None, ema_5=None, macd_hist=None, prev_macd_hist=None, prev_cci=None, vol_spike=False, vol_trend=False, is_yangbong=False, mom_ret=None, mom_ret_1m=None, mom_ret_3m=None, trend_persist=None, score=None):
    # score: 같은 판단에서 호출자가 이미 calculate_score 로 구한 점수(0 하한 적용, 반올림 전).
    #  주면 내부에서 다시 채점하지 않는다 — 상태 분류와 표시·게이트 점수가 같은 계산 결과를
    #  쓰게 하고(5-5 입력 일관성), 판단 1회당 채점 2회를 1회로 줄인다. 호출자는 이 함수에
    #  넘기는 것과 같은 입력으로 채점해야 한다(engine.analyze_buy·backtest.calculate_daily_status).
    if df is not None and ind is not None:
        if not df.empty: price = float(df.iloc[-1]['close'])
        ema20 = ind.get('ema_20')
        ema60 = ind.get('ema_60')
        ema120 = ind.get('ema_120')
        sar = ind.get('psar')
        rsi = ind.get('rsi')
        adx = ind.get('adx')
        cci = ind.get('cci')
        obv_trend = ind.get('obv_trend')
        macd = ind.get('macd')
        macd_signal = ind.get('macd_signal')
        if plus_di is None: plus_di = ind.get('plus_di')
        if minus_di is None: minus_di = ind.get('minus_di')

        # [추가] 인자로 넘어오지 않은 세부 지표들을 ind 딕셔너리에서 직접 추출
        if ema_5 is None: ema_5 = ind.get('ema_5')
        if prev_cci is None: prev_cci = ind.get('prev_cci')
        if macd_hist is None: macd_hist = ind.get('macd_hist')
        if prev_macd_hist is None: prev_macd_hist = ind.get('prev_macd_hist')

        if plus_di is None or minus_di is None:
            import numpy as np
            try:
                high_diff = df['high'].diff()
                low_diff = df['low'].diff()
                pos_dm = np.where((high_diff > 0) & (high_diff > -low_diff), high_diff, 0.0)
                neg_dm = np.where((low_diff < 0) & (-low_diff > high_diff), -low_diff, 0.0)
                tr1 = df['high'] - df['low']
                tr2 = (df['high'] - df['close'].shift()).abs()
                tr3 = (df['low'] - df['close'].shift()).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                adx_period = config.INDICATOR_PARAMS.get('ADX_PERIOD', 14)
                atr = tr.ewm(alpha=1/adx_period, adjust=False).mean()
                atr_val = atr.iloc[-1]
                if atr_val == 0 or np.isnan(atr_val): atr_val = 1.0 # 0 나누기 방어
                plus_di = 100 * pd.Series(pos_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
                minus_di = 100 * pd.Series(neg_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
            except Exception: pass
            
    if price is None or ema60 is None or sar is None or rsi is None:
        return "-", "[dim]", "데이터 부족"
    
    # [수정] 1순위 절대 방어 필터를 가장 위로 끌어올림 (역추세 매수보다 우선 적용하여 떨어지는 칼날 완벽 방어)
    is_panic_regime = False
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        if adx is not None and adx >= 45: # ADX 45 이상의 초강력 하락장
            is_panic_regime = True
            # [추세추종] ADX는 방향 무관 지표라 강한 상승추세 직후의 급락 눌림에서도 45 이상이
            #  유지된 채 -DI가 역전될 수 있음. 추세 구조(60일선)가 살아있는 동안은 광폭 손절
            #  (ATR 손절/샹들리에 TS)에 청산을 맡기고, 구조 훼손을 동반한 경우에만 즉시 '매도'로 분류
            #  (승자 포지션의 반추세성 조기 전량청산 방지. 매수 게이트는 -DI 우위 hard_caution이 계속 차단)
            if price < ema60:
                return "매도", "[blue]", "초강력 투매 패닉 구간 (ADX 과열 및 -DI 우위)"

    # [추가] 2. 낙폭과대(역추세) 반등 조건 확인
    #  패닉 구간(is_panic_regime)에서는 60일선 위라도 역매수를 봉쇄해 '떨어지는 칼날 방어'를 유지한다
    #  (구조 게이트 도입으로 패닉 체크가 return하지 않게 되면서 MR 경로가 열리는 회귀 방지)
    use_mr = thresholds.get("USE_MEAN_REVERSION", config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False)) if thresholds else config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False)
    if use_mr and not is_panic_regime and ema20 is not None and prev_rsi is not None and rsi is not None:
        mr_rsi = thresholds.get("MR_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = thresholds.get("MR_DISPARITY_MAX", config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        
        disparity = (price / ema20) * 100
        
        is_yangbong_flag = is_yangbong
        if df is not None and not df.empty:
            is_yangbong_flag = df.iloc[-1]['close'] > df.iloc[-1]['open']

        # [개선 #3] 떨어지는 칼날(데드캣) 방어 강화 — 하락 가속도 둔화 확인.
        #          기존 조건(RSI 반등+양봉+수급)만으로는 ADX<45 의 추세적 하락 중에도
        #          역매수가 발동될 수 있어 방어가 얇음. df가 있을 때, 당일 저가가
        #          직전 5거래일 저점을 추가로 경신(=신저가 갱신, 낙하 지속)하면 진입 보류.
        mr_decel_ok = True
        if df is not None and not df.empty and len(df) >= 6:
            try:
                prior_low = df['low'].iloc[-6:-1].min()
                today_low = df['low'].iloc[-1]
                if today_low < prior_low:
                    mr_decel_ok = False
            except Exception:
                pass

        # [수정] RSI 반등 + 양봉 + 수급 확인(OBV 상승 또는 스마트머니 유입) + 하락 둔화를 모두 만족해야 역매수 발동
        # 조건 미충족 시 단순 기술적 반등(데드캣)으로 간주하여 진입 보류
        if rsi <= mr_rsi and rsi > prev_rsi and disparity <= mr_disp and is_yangbong_flag and (obv_trend or smart_money) and mr_decel_ok:
            return "역매수", "[magenta]", "낙폭과대 (역매수 반등 신호 + 양봉 + 수급 확인 + 하락 둔화)"

    # [수정] 가중치 적용 점수 계산 (thresholds에 weights가 포함되어 있을 수 있음)
    weights = thresholds.get("WEIGHTS") if thresholds else None
    if score is None:
        score, _ = _analysis().calculate_score(
            price=price, ema20=ema20, ema60=ema60, ema120=ema120, sar=sar, rsi=rsi, adx=adx, cci=cci,
            obv_trend=obv_trend, macd=macd, macd_signal=macd_signal, weights=weights, smart_money=smart_money,
            plus_di=plus_di, minus_di=minus_di, df=df, ind=ind, ema_5=ema_5, macd_hist=macd_hist,
            prev_macd_hist=prev_macd_hist, prev_cci=prev_cci, vol_spike=vol_spike, vol_trend=vol_trend,
            w52_pos=w52_pos, mom_ret=mom_ret,  # [추세추종] 가격 모멘텀 팩터 입력 전달 (df 없이 호출되는 백테스트 경로 패리티)
            mom_ret_1m=mom_ret_1m, mom_ret_3m=mom_ret_3m, trend_persist=trend_persist  # [추세추종] 다중 기간 정합·추세 지속 이력 동일 전달
        )

    # [수정] config.py의 설정값을 사용하여 상태 판정
    if thresholds:
        buy_score = thresholds.get("BUY_SCORE", config.ANALYSIS_THRESHOLDS["BUY_SCORE"])
        rise_score = thresholds.get("RISE_SCORE", config.ANALYSIS_THRESHOLDS["RISE_SCORE"])
        buy_rsi_max = thresholds.get("BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"])
        interest_min = thresholds.get("INTEREST_SIGNAL_MIN", config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3))
        interest_ma60_near = thresholds.get("INTEREST_MA60_NEAR", config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97))

        use_super = thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True))
        super_score = thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0))
        super_w52 = thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0))
        super_rsi = thresholds.get("SUPER_BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 80.0))
    else:
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        
        interest_min = config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3)
        interest_ma60_near = config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97)

        use_super = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
        super_score = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0)
        super_w52 = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
        super_rsi = config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 80.0)

    reasons = []
    is_severe_danger = False
    
    # [수정] 위험 조건 완화: 장기(120)와 중기(60) 이평선을 모두 이탈해야 '위험'으로 간주 (변동성 감소)
    if ema120 is not None and price < ema120 and price < ema60: 
        is_severe_danger = True
        reasons.append("이평선 완전 이탈(60&120)")
    elif rsi <= (config.INDICATOR_PARAMS["RSI_LOWER"] - 10): 
        is_severe_danger = True # 위험 기준은 하한선보다 더 낮게 설정 (예: 20)
        reasons.append(f"RSI 초과매도({rsi:.1f})")
        
    # [수정] ADX 과열 중 RSI 하락은 '위험'보다는 '주의'로 이동
    if is_severe_danger: return "매도", "[blue]", ", ".join(reasons)
    
    is_caution = False
    # [추가] 위험형 주의(hard): '관심(태동)'으로도 분류 불가한 명백한 하락/과열 신호.
    #        약세형(soft: 이평선 한쪽 이탈)과 분리하여, 60일선 또는 120일선 중 한쪽 아래여도
    #        추세 전환 초기 신호가 있으면 '관심'으로 건질 수 있도록 한다.
    #  [주의] 60·120일선을 '동시에' 이탈한 종목은 위 is_severe_danger에서 이미 '매도'로 확정
    #        되어 여기까지 오지 않는다(추세추종 원칙상 의도된 차단 — 역배열 데드캣 반등을
    #        관심으로도 올리지 않는다). 따라서 이 아래 약세형 경로가 실제로 커버하는 것은
    #        '한쪽만 이탈'(또는 120일선 데이터 부족으로 위험 판정이 불가한) 경우다.
    hard_caution = False
    # [수정] 주의 조건: 60일선 이탈 또는 120일선 이탈 → 약세형(soft, 관심 후보 가능)
    if price < ema60:
        is_caution = True
        reasons.append("60일선 이탈")
    if ema120 is not None and price < ema120:
        is_caution = True
        reasons.append("120일선 이탈")
    if sar is not None and sar > price:
        is_caution = True; hard_caution = True
        reasons.append("SAR 매도신호")
    if rsi >= (config.INDICATOR_PARAMS["RSI_UPPER"] + 10):
        is_caution = True; hard_caution = True
        reasons.append(f"RSI 과열({rsi:.1f})")
    elif rsi <= config.INDICATOR_PARAMS["RSI_LOWER"]:
        is_caution = True; hard_caution = True
        reasons.append(f"RSI 침체({rsi:.1f})")
    elif adx is not None and prev_rsi is not None and adx >= 40 and rsi < prev_rsi:
        is_caution = True; hard_caution = True
        reasons.append(f"ADX과열({adx:.1f})+RSI하락")

    if macd is not None and macd_signal is not None and macd < macd_signal:
        is_caution = True; hard_caution = True
        reasons.append("MACD 데드크로스")
    # [추가] DMI 매도세 우위 필터
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        is_caution = True; hard_caution = True
        reasons.append("-DI 우위(매도세 강함)")

    # 2순위: 얼리 스테이지 및 기본 매수 조건 (위험 필터를 모두 통과해야 함)
    is_super = use_super and score >= super_score and w52_pos is not None and w52_pos >= super_w52
    actual_buy_rsi_max = super_rsi if is_super else buy_rsi_max

    if score >= buy_score and rsi < actual_buy_rsi_max:
        # [핵심 방어 로직] 하락 반전 신호(고점 꺾임)를 방어하기 위한 강력 필터
        # 다음 조건 중 하나라도 해당되면 아무리 점수가 높아도 "매수"가 아닌 "주의"로 강등
        down_trend_flags = []
        if sar is not None and sar > price: down_trend_flags.append("SAR 매도신호")
        if macd is not None and macd_signal is not None and macd < macd_signal: down_trend_flags.append("MACD 데드크로스")
        if plus_di is not None and minus_di is not None and minus_di > plus_di: down_trend_flags.append("-DI 우위")
        
        if down_trend_flags:
            is_caution = True; hard_caution = True
        else:
            if is_super: return "강매수", "[magenta]", "매수 조건 충족 (슈퍼 모멘텀 적용)"
            else: return "매수", "[red]", "매수 조건 충족 (얼리 스테이지 반등 포함)"

    # [상승/대기] 주의(약세+위험) 신호가 전혀 없고 점수가 양호 → 추세 정렬이 완성된 강한 상태.
    #   두 하위 케이스를 구분하되 색상·매수 로직상 취급은 '상승'과 동급(sibling)으로 둔다
    #   (아래 return 지점에서 이 함수를 소비하는 멤버십 목록들이 '상승'과 함께 '대기'도 포함해야 함):
    #   - 대기: 점수는 매수 기준(buy_score) 충족이나 단기 RSI 과열(actual_buy_rsi_max 이상,
    #           단 과열 주의선 RSI_UPPER+10 미만)로 진입만 보류 → 눌림목 매수 대기(매수 직전).
    #   - 상승: 점수가 rise~buy 사이(아직 폭발적 강도 미달) → 점수 축적 대기.
    if not is_caution and score >= rise_score:
        if score >= buy_score:
            return "대기", "[orange3]", f"매수 직전 (점수 충족, RSI 과열 눌림 대기 · RSI {rsi:.0f}≥{actual_buy_rsi_max:.0f})"
        return "상승", "[orange3]", "상승 추세 (점수 양호, 점수 축적 대기)"

    # [관심(태동)] 추세 정렬은 미완성('상승' 미달)이나, 위험형 신호가 없고
    #   추세 전환 초기 신호가 INTEREST_SIGNAL_MIN개 이상 포착된 종목.
    #   60일선 또는 120일선 중 한쪽 아래(약세형 주의)여도 초기 반등 여력을 빠르게 포착하기 위함.
    #   (양쪽 동시 이탈은 위 is_severe_danger에서 '매도'로 확정되어 여기 도달하지 않는다)
    #   수동 스윙 매매 모니터링 대상으로 표시한다. 자동매매 진입 상태가 아니다.
    if not hard_caution and interest_min > 0:
        interest_signals = []
        if ema_5 is not None and ema20 is not None and ema_5 > ema20:
            interest_signals.append("단기 골든크로스(5>20)")
        if macd_hist is not None and prev_macd_hist is not None and macd_hist > prev_macd_hist:
            interest_signals.append("MACD 히스토그램 개선")
        elif macd is not None and macd_signal is not None and macd > macd_signal:
            interest_signals.append("MACD 골든크로스")
        if plus_di is not None and minus_di is not None and plus_di > minus_di:
            interest_signals.append("+DI 우위")
        if rsi is not None and prev_rsi is not None and rsi >= config.INDICATOR_PARAMS.get("RSI_MID", 50) and rsi > prev_rsi:
            interest_signals.append("RSI 50선 상향")
        if cci is not None and prev_cci is not None and cci > prev_cci and cci > -100:
            interest_signals.append("CCI 개선")
        if vol_trend or vol_spike or obv_trend or smart_money:
            interest_signals.append("수급 유입")
        if price is not None and ema60 is not None and price >= ema60 * interest_ma60_near:
            interest_signals.append("60일선 근접/돌파")

        if len(interest_signals) >= interest_min:
            return "관심", "[green]", "추세 전환 초기 신호 " + str(len(interest_signals)) + "개 (" + ", ".join(interest_signals) + ")"

    if is_caution: return "주의", "[yellow]", ", ".join(reasons)
    return "관망", "[white]", "방향성 탐색 구간"


_W52_MIN_BARS = 200   # 이보다 짧으면 창이 52주를 못 채운 것 → 차트 대신 벤더(API/스캐너) 값을 쓴다


def _w52_high_low(chart_df):
    """일봉에서 '최근 365일'(=52주) 구간의 (고가, 저가)를 구한다. 산출 불가 시 (None, None).

    tail(250)을 쓰면 안 된다 — 250거래일은 실측상 373일치(2025-07-14~2026-07-22)라 52주보다
    8일 넓고, 그 경계 밖 극값이 밴드를 통째로 왜곡한다(TIGER 조선TOP10: 창 밖 18,855가 잡혀
    20.2%, 실제 52주 기준은 11.0%). 날짜로 잘라 벤더의 52주 정의와 창을 맞춘다.

    봉 수가 _W52_MIN_BARS 미만이면 신규상장이거나 차트 수신이 잘린 경우다. 이때 좁아진 밴드를
    그대로 쓰면 52주 위치가 부풀려지므로 호출부가 벤더 값으로 폴백하도록 None을 준다.
    """
    # [SSOT 2026-09-04] 구현은 core.indicators 로 내렸다 — 매수·매도 판정과 api 계층까지
    #  같은 창을 보게 하려면 최하위에 있어야 한다. 여기는 이름만 남긴다.
    return indicators.w52_high_low(chart_df, now=datetime.now())


def _w52_band(chart_df):
    """52주 고/저 밴드 (h52, l52). 산출 불가 시 (0.0, 0.0).

    화면별로 창 정의가 달라 같은 종목의 52주 값이 어긋나던 것을 통일하기 위한 단일 진입점이다.
    (표는 365일, 지수·상세·점수 폴백은 tail(250)=실측 373일을 쓰고 있었다. 차이는 경계 밖
     8일에 극값이 걸릴 때만 생기지만, 걸리면 크다 — TIGER 조선TOP10 20.2%→11.0%.
     2026-07-24 보유 10종목 실측: 삼성SDI 46.2%→45.0%, 삼성전자·현대차 각 -0.3%p)

    365일 창을 못 채우는 경우(신규상장·차트 수신 절단)만 보유 봉 전체로 폴백한다.
    폴백 조건은 _w52_high_low가 판정한다(_W52_MIN_BARS 미만).

    주의: 창의 기준점은 '오늘'이다. 과거 시점 df(백테스트)에 쓰면 창이 어긋나지만, 그 경로는
    w52_pos를 사전계산해 인자로 넘기므로 여기에 도달하지 않는다. 도달하더라도 창이 비어
    _w52_high_low가 None을 주고 기존 tail(250) 동작으로 폴백한다.
    """
    return indicators.w52_band(chart_df, now=datetime.now())
