"""AutoTrader 의 시장·리스크 — 지수 상태(시장 필터)·판단 보류 알림·리스크 배수(국면·휩소율·드로다운·장중 래칫)·
동적 손절 캡의 지수 변동성 배율.

[왜 나눴나 · 2026-10-24 C1 2단계] 이 메서드들은 매 주기 매매 판단보다 먼저 돌아 '이번 주기에 시장을 어떻게 볼
 것인가'를 트레이더 상태(market_index_status·risk_scale·risk_scale_by_market·vol_regime_info)에 적는다. 매수·
 매도 코드는 그 상태를 읽기만 한다. 한 파일에 섞여 있으면 리스크 규칙 하나를 고칠 때 5,000줄짜리 매매 파일을
 함께 읽어야 하고, 리스크 변경이 매매 파일의 변경으로 보인다.

[어떻게 나눴나] 1단계(reporting.TraderReportingMixin)와 같은 규약이다. 메서드 본문은 한 글자도 바꾸지 않고
 옮겼고, AutoTrader 가 이 믹스인을 상속한다 — self._update_risk_scale() · patch.object(AutoTrader, ...) 가 전과
 같이 동작한다. 모듈 전역 이름(config·analysis·datetime …)은 trader.py 와 **같은 객체**를 가리키게 import 했다
 (tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도 trader 와 같은 이름을 쓴다. 동등성 기준은
 주기 재생 기록 두 시나리오(tests/test_cycle_replay.py — base·market_stress)다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.market_risk.save_daily_risk_floor). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import api
import config
from modules import analysis
from modules import db_manager
from modules.auto_trade.common import (_get_trade_account, load_daily_risk_floor, save_daily_risk_floor,
                                       trade_account_key)

#  트레이더 로그와 같은 이름 — 시장·리스크 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class TraderMarketRiskMixin:
    """AutoTrader 의 시장·리스크 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.log·self.initial_asset …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader). 값 없는 선언이라 실행 시에는
    #  AutoTrader 의 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태는 빠져 있다.
    initial_asset: Any
    market_index_status: dict
    market_status_notified: dict
    log: Callable[..., None]
    effective_baseline: Callable[..., Any]

    def _update_market_indices_status(self, notify=True):
        """KOSPI, KOSDAQ 지수 상태 업데이트 및 알림

        [Fix / 추세추종 원칙] "대체 무슨 일이 벌어지고 있는지 모르겠다면, 아무것도 하지 마라."
        기존에는 지수 데이터 조회 실패 시 is_healthy=True로 두어 '판단 불가'가 곧 '매수 허용'이
        되는 fail-open 구조였다. 시장 방향을 모르는 상태에서 신규 진입을 허용하는 것은
        추세추종의 전제(시장 방향을 파악해 포지션을 구축한다) 자체를 무너뜨리므로,
        매수 게이트는 fail-closed(보류)로 전환한다. 상태에 unknown=True 플래그를 실어
        '약세로 판정된 것'과 '판정 자체가 불가한 것'을 화면·로그에서 구분한다.
        (매도·손절 경로는 이 상태를 참조하지 않으므로 데이터 장애와 무관하게 계속 동작한다.)
        """
        # [수정] analysis 모듈의 공통 함수 사용을 위해 리스트로 변경
        target_indices = ["KOSPI", "KOSDAQ"]

        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
        band_pct = getattr(config, 'MARKET_FILTER_BAND', 1.0)

        for market_name in target_indices:
            try:
                # [수정] analysis 모듈의 공통 함수 사용 (Fallback 포함)
                df = analysis.get_domestic_index_data(market_name)
                #  [SSOT 2026-10-24] 판정은 engine.market_filter_status 하나다(결측 검사·이탈 확인 밴드·출처
                #   기록의 근거 주석도 그쪽에 있다). 개별 종목 진단·텔레그램 종목 분석이 같은 함수로
                #   '이 시장의 신규 매수가 지금 막혀 있는가'를 보인다. 알림·로그·변동성 배율은 여기 몫이다.
                stat = _pkg().market_filter_status(market_name, df=df)
                if stat["unknown"]:
                    self.log(f"{market_name} 지수 데이터 부족/조회 실패 → 시장 방향 판단 불가로 신규 매수를 보류합니다. (매도·손절은 정상 동작)")
                    self.market_index_status[market_name] = stat
                    self._notify_market_unknown(market_name, notify)
                    continue

                det = stat["filter"]
                is_healthy = stat["is_healthy"]
                self.market_index_status[market_name] = stat
                #  [2026-10-24] 동적 손절 캡의 변동성 배율은 여기서 갱신하지 않는다 — _update_vol_regime 주석.

                # 상태 변경 알림
                if not notify:
                    continue

                # 밴드가 켜져 있으면 '이평선 아래'가 아니라 '이평선 -밴드% 이탈'이 실제 트리거이므로
                #  문구에 밴드를 함께 실어 화면·알림과 판정식이 어긋나 보이지 않게 한다.
                band_txt = f" -{band_pct:g}%" if band_pct else ""
                #  '어떤 수치로 그렇게 판정했는지'를 알림에도 싣는다 — 받는 사람이 차트를
                #  열지 않고도 얼마나 벌어졌는지, 어디까지 올라와야 풀리는지 알 수 있어야 한다.
                basis = "".join(f"\n{line}" for line in self._format_market_filter_basis(det))
                notified = self.market_status_notified.get(market_name, False)
                if not is_healthy and not notified:
                    api.send_telegram_message(f"📉 [시장 감지] {market_name} 지수가 {ma_period}일 이평선{band_txt} 아래로 하락했습니다.{basis}\n해당 시장 종목의 신규 매수를 일시 중단합니다.")
                    self.market_status_notified[market_name] = True
                elif is_healthy and notified:
                    band_up = f" +{band_pct:g}%" if band_pct else ""
                    api.send_telegram_message(f"📈 [시장 회복] {market_name} 지수가 {ma_period}일 이평선{band_up}을 회복했습니다.{basis}\n매수를 재개합니다.")
                    self.market_status_notified[market_name] = False
            except Exception as e:
                self.log(f"{market_name} 지수 조회 실패: {e} → 시장 방향 판단 불가로 신규 매수를 보류합니다. (매도·손절은 정상 동작)")
                #  예외 경로라 df 를 신뢰할 수 없다 — 출처는 비워 둔다(모른다를 모른다로 남긴다).
                self.market_index_status[market_name] = {"is_healthy": False, "unknown": True, "current": 0,
                                                         "source": None}
                self._notify_market_unknown(market_name, notify)

    @staticmethod
    def _format_market_filter_basis(det):
        """시장 필터 판정 **근거**를 텔레그램 한두 줄로 만든다 (표시 전용).

        판정은 indicators.get_market_filter_detail 하나가 하고 여기는 그 dict 를 읽기만 한다 —
        근거를 여기서 다시 계산하면 알림이 판정과 갈라진다. 판정이 성립하지 않으면
        (ready=False) 숫자를 말하지 않는다: 모르는 것을 아는 척하지 않는다.
        """
        if not isinstance(det, dict) or not det.get('ready'):
            return []
        lines = []
        c, ma, gap = det.get('close'), det.get('ma'), det.get('gap_pct')
        if c is not None and ma is not None and gap is not None:
            tail = ""
            #  판단에 쓴 봉의 일자·출처 — 묵은 지수로 막혀 있는 경우를 알아챌 수 있어야 한다.
            digits = "".join(ch for ch in str(det.get('asof') or "") if ch.isdigit())[:8]
            if len(digits) == 8:
                tail += f" {digits[4:6]}/{digits[6:8]}"
            src = {"TVDATAFEED": "TV"}.get(det.get('source'), det.get('source'))
            if src:
                tail += f" {src}"
            #  소수 한 자리로 맞춘다 — 같은 메시지 안의 차단선·해제선(indicators 사유)과 같은 자릿수여야
            #  눈으로 바로 비교된다.
            lines.append(f"└ 지수 {c:,.1f} / SMA{det.get('ma_period')} {ma:,.1f} ({gap:+.2f}%){tail}")
        if det.get('reason'):
            streak = det.get('streak') or 0
            lines.append(f"└ {det['reason']}" + (f" ({streak}봉째)" if streak else ""))
        return lines

    def _notify_market_unknown(self, market_name, notify=True):
        """지수 판단 불가(데이터 장애)로 매수를 보류할 때 1회만 알린다.

        '약세 판정'과 원인이 다르므로 문구를 분리하되, 스로틀 플래그는
        market_status_notified를 공유해 회복 시 '매수 재개' 알림이 정상적으로 나가게 한다.
        """
        if not notify:
            return
        if self.market_status_notified.get(market_name, False):
            return
        #  전달을 확인한 뒤에 래치를 건다. 종전에는 전송 여부와 무관하게 걸려, 실패하면
        #  '신규 매수 보류' 상태를 운영자가 끝까지 모른 채 지나갔다(래치는 회복 때만 풀린다).
        if _pkg().alert_delivered(
                f"⚠️ [판단 보류] {market_name} 지수 데이터를 확인할 수 없습니다.\n"
                f"시장 방향을 알 수 없으므로 해당 시장 종목의 신규 매수를 보류합니다.\n"
                f"(보유 종목의 손절·트레일링 스탑 감시는 계속됩니다)"):
            self.market_status_notified[market_name] = True

    @staticmethod
    def _whipsaw_risk_scale(whipsaw, params=None):
        """휩소율(0~1) → 리스크 한도 배수. LO 이하 1.0, HI 이상 MIN_SCALE, 사이는 선형 보간.

        휩소율이 높다 = 최근 추세 전환들이 확인 기준(5%)을 채우지 못하고 되돌려졌다
        = 톱니장이다. 추세추종 시스템이 가장 잃기 쉬운 구간이므로 진입 크기를 줄인다."""
        params = params if params is not None else (getattr(config, 'RISK_SCALING_PARAMS', {}) or {})
        try:
            lo = float(params.get("WHIPSAW_LO", 0.40))
            hi = float(params.get("WHIPSAW_HI", 0.75))
            min_scale = float(params.get("WHIPSAW_MIN_SCALE", 0.6))
        except (TypeError, ValueError):
            lo, hi, min_scale = 0.40, 0.75, 0.6
        if whipsaw is None or hi <= lo or not (0 < min_scale < 1.0):
            return 1.0
        if whipsaw <= lo:
            return 1.0
        if whipsaw >= hi:
            return min_scale
        return 1.0 - (whipsaw - lo) / (hi - lo) * (1.0 - min_scale)

    def _update_vol_regime(self):
        """[동적 손절 캡] 지수 변동성 배율을 갱신한다 — 시장 필터 설정과 무관하게 매 주기(값은 하루 한 번 바뀐다).

        이 배율이 engine.effective_atr_stop_cap 을 통해 ATR 손절 캡을 국면에 맞춰 넓힌다.
        [기준 지수는 KOSPI 하나] 코스닥 종목도 KOSPI 를 쓴다 — 캡은 '시장 전체가 지금 얼마나 거친가'를
         재는 장치이고, 이 설정을 정한 검증이 KOSPI 단일 기준으로 수행됐다(backtest.prepare_vol_regime 과 같은 규약).
        [2026-10-24 · 도입 이후 한 번도 작동하지 않던 자리] 종전에는 _update_market_indices_status 가 판단용
         지수 창(약 260봉)으로 배율을 쟀다. 배율의 워밍업은 311봉이라 값이 늘 1.0 이었고, 그 함수는 시장 필터를
         켰을 때만 돌았다. 지금은 analysis.get_live_vol_regime 이 백테스트와 같은 식을 긴 확정 종가 이력에 적용한다.
        이력을 못 받으면 고정 캡(배율 1.0)으로 두고 하루 한 번 알린다 — 백테스트 쪽 실패 규약과 같다.
        """
        ss = config.SELL_STRATEGY
        if not ss.get("MAX_ATR_STOP_LOSS_RATE", -15.0) or not ss.get("ATR_CAP_DYNAMIC", True):
            return              # 캡 미사용·고정 캡 설정이면 배율을 읽지 않는다
        self._vol_regime_tried = True
        info = analysis.get_live_vol_regime()
        prev = getattr(self, 'vol_regime_info', None)
        self.vol_regime_info = info
        if not info:
            _pkg().set_vol_regime_ratio(1.0)
            today = datetime.now().strftime("%Y-%m-%d")
            if getattr(self, '_vol_regime_warned_on', None) != today:
                self._vol_regime_warned_on = today
                msg = ("[동적 손절 캡] 지수 장기 이력을 받지 못해 ATR 손절 캡을 고정값"
                       f"({_pkg().effective_atr_stop_cap():.1f}%)으로 둡니다 — 다음 주기에 다시 시도합니다.")
                self.log(msg)
                logger.warning(msg)
            return
        _pkg().set_vol_regime_ratio(info["ratio"])
        if not prev or prev.get("asof") != info["asof"] or abs(prev.get("ratio", 0.0) - info["ratio"]) > 1e-9:
            asof = info["asof"]
            self.log(f"[동적 손절 캡] 지수 변동성 배율 x{info['ratio']:.2f} → ATR 손절 캡 "
                     f"{_pkg().effective_atr_stop_cap():.1f}% (기준 {asof[4:6]}/{asof[6:8]} 확정 종가, "
                     f"{info['source']} {info['bars']:,}봉)")

    def _update_risk_scale(self):
        """[리스크 스케일링] 시장 국면·휩소율·계좌 드로다운에 따른 신규 진입 리스크 한도 배수 갱신

        [추세추종 2원칙] "자본대비 리스크에 한도를 둬야 한다" — 추세가 먹히지 않는 구간과
        손실 구간에서는 신규 진입 리스크 한도를 줄여 드로다운을 통제한다(터틀식).
        결과는 RiskManager가 종목당 리스크(SYSTEM_RISK_PER_TRADE)와
        히트 캡(SYSTEM_MAX_PORTFOLIO_RISK)에 곱해 사용한다. 청산 로직에는 관여하지 않는다.
        (국면 배수 × 휩소율 배수) × 드로다운 배수가 곱으로 결합된다.
        그 결과는 같은 날 안에서 조이기만 한다 — 푸는 것은 다음 날 첫 주기다(_hold_intraday_floor).

        [시장별 분리 2026-07-27] 국면·휩소율은 KOSPI/KOSDAQ이 서로 다른 시장이므로 각각 산출해
        self.risk_scale_by_market에 담고, 종목 사이징에는 **그 종목이 속한 시장의 배수**를 쓴다.
        (종전에는 두 시장 중 나쁜 쪽 하나를 계좌 전체에 적용해, 코스닥이 톱니장이면 코스피
         종목까지 축소되고 로그에도 'KOSDAQ'만 찍혀 오인을 샀다.)
        계좌 드로다운은 시장과 무관한 계좌 상태이므로 두 시장 배수에 공통으로 곱한다.
        반면 self.risk_scale(단일 값)은 **두 시장 중 열위 쪽**을 유지한다 — 이 값이 쓰이는
        히트 캡은 계좌 전체의 총 오픈 리스크를 묶는 장치라 시장별로 나눌 수 없고,
        보수적인 쪽을 택하는 것이 맞기 때문이다.

        [적용 지점 2026-07-27 — 리스크층 → 기초 비중으로 이동]
        종전에는 이 배수를 allocate_budget의 2)리스크층에만 곱했는데, 그 층은 최종액을 결정하는
        일이 없어(3)변동성 타겟팅이 상시 구속) **배수가 약 0.45 미만으로 내려가기 전까지 배분액이
        1원도 변하지 않았다**. 단일 트리거(PendDown 0.6 / 휩소율 0.6 / DD-5% 0.75)로는 도달하지
        못해 사실상 무력했다(백테스트에서 '스케일링 OFF'와 현행이 거래 803건까지 동일).
        이를 1)기초 비중에 적용하도록 옮겨, 3)의 상한(기초×변동성배수)까지 함께 내려가게 했다.

        [실측 2026-07-27 — 시드 500만/1,000만 · 30종목 무작위 50회 짝비교]
          MDD 개선 46/50·45/50 (중앙 +2.8%p·+3.2%p), PF 개선 41/50·44/50 (중앙 +0.27·+0.34).
          대가는 3년 수익 중앙 -16.9%p·-24.1%p, 유휴현금 +13%p.
          타이밍 가치 검증: 같은 평균 배수(0.694)를 상수로 준 대조군은 수익이 절반(146.5→71.0%),
          PF도 낮았다(2.83→2.20). 셔플 대조군도 동일 → 국면·휩소율 판단이 실제로 기여한다.
          ※ 변동성 캡에 직접 곱하는 방식은 3년 수익 -26%로 열위여서 채택하지 않았다."""
        params = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
        scale = 1.0
        reasons = []
        # 시장별 맵은 항상 두 시장을 채운다 — 국면·휩소율을 모두 꺼도 드로다운 배수가 실릴 곳이 필요하다.
        market_scales = {"KOSPI": 1.0, "KOSDAQ": 1.0}
        market_reasons = {"KOSPI": "", "KOSDAQ": ""}
        try:
            # 1) 시장 국면 + 휩소율: 시장별로 각각 산출한다(코스피/코스닥은 별개 시장).
            #    종목 사이징엔 해당 종목의 시장 배수를, 계좌 단위 히트 캡엔 열위 시장 배수를 쓴다.
            use_regime = params.get("USE_REGIME_RISK_SCALING", True)
            use_whipsaw = params.get("USE_WHIPSAW_RISK_SCALING", True)
            if use_regime or use_whipsaw:
                best_scale, best_reason = 1.0, None
                #  국면 배수는 '드로다운을 곱하기 전' 값으로 따로 보관한다. 아래 판정 불가
                #   경로가 직전 배수를 그대로 쓰는데, 저장본(risk_scale_by_market)에는 이미
                #   드로다운이 곱해져 있어 그것을 재사용하면 드로다운이 두 번 곱해진다.
                prev_regime_scales = getattr(self, 'risk_scale_regime_by_market', None) or {}
                regime_scales = {}
                for m_type in ("KOSPI", "KOSDAQ"):
                    info = analysis.get_market_regime_detail(m_type)
                    m_scale, m_parts = 1.0, []

                    if info.get('unknown'):
                        #  [Fix 2026-09-08] 국면을 **못 읽은** 것이다. 종전에는 이 상태가
                        #   '횡보'와 같은 값이라 축소할 이유가 없는 것으로 읽혔고, 배수가
                        #   x1.00 으로 되돌아갔다 — 지수가 끊긴 바로 그 순간에 신규 진입
                        #   한도가 최대로 열렸다(실측 x0.51 → x1.00, 로그는 '정상 복원').
                        #   시장 필터를 끈 운용에서는 이 배수가 유일한 시장 인식 브레이크다.
                        #   이 메서드는 계산 자체가 실패하면 이미 '기존값 유지'로 빠진다 —
                        #   판정 불가도 같은 규약으로 둔다: 모르면 완화하지 않는다.
                        m_scale = float(prev_regime_scales.get(m_type, 1.0) or 1.0)
                        if m_scale < 1.0:
                            m_parts.append(f"국면 판정 불가 — 직전 x{m_scale:.2f} 유지")
                        regime_scales[m_type] = m_scale
                        market_scales[m_type] = m_scale
                        market_reasons[m_type] = " ".join(m_parts)
                        if m_scale < best_scale:
                            best_scale, best_reason = m_scale, f"{m_type} " + " ".join(m_parts)
                        continue

                    # 1-a) 국면 배수 — 축소 대상은 '하락 미확정'(추세 붕괴 초기)이 핵심
                    if use_regime:
                        key = {"PendDown": ("PENDING_DOWN_RISK_SCALE", 0.6),
                               "Bear": ("BEAR_RISK_SCALE", 1.0)}.get(info['regime'])
                        if key:
                            try:
                                rs = float(params.get(key[0], key[1]))
                            except (TypeError, ValueError):
                                rs = key[1]
                            if 0 < rs < 1.0:
                                m_scale *= rs
                                #  이 자리는 unknown 을 이미 위에서 걸러낸 뒤라 라벨이 같지만,
                                #   국면 글자를 만드는 길을 하나로 두어야 가드가 의미를 갖는다.
                                m_parts.append(f"{analysis.describe_regime(info)[1]} x{rs:g}")

                    # 1-b) 휩소율 배수 — 톱니장일수록 연속적으로 축소
                    if use_whipsaw and info.get('whipsaw_ratio') is not None:
                        ws_scale = self._whipsaw_risk_scale(info['whipsaw_ratio'], params)
                        if ws_scale < 1.0:
                            m_scale *= ws_scale
                            m_parts.append(f"휩소율 {info['whipsaw_ratio']*100:.0f}% x{ws_scale:.2f}")

                    regime_scales[m_type] = m_scale
                    market_scales[m_type] = m_scale
                    market_reasons[m_type] = " ".join(m_parts)
                    if m_scale < best_scale:
                        best_scale, best_reason = m_scale, f"{m_type} " + " ".join(m_parts)

                self.risk_scale_regime_by_market = regime_scales
                if best_reason:
                    scale *= best_scale
                    reasons.append(best_reason)

            # 2) 계좌 드로다운: 자산 고점(HWM) 대비 하락률에 따라 단계적 감속
            if params.get("USE_DRAWDOWN_RISK_SCALING", True):
                dd = self._get_account_drawdown_pct(params)
                try:
                    lv1, sc1 = float(params.get("DD_LEVEL_1", 5.0)), float(params.get("DD_SCALE_1", 0.75))
                    lv2, sc2 = float(params.get("DD_LEVEL_2", 10.0)), float(params.get("DD_SCALE_2", 0.5))
                except (TypeError, ValueError):
                    lv1, sc1, lv2, sc2 = 5.0, 0.75, 10.0, 0.5
                # 드로다운은 계좌 상태(시장 무관)이므로 두 시장 배수에 공통으로 곱한다.
                dd_scale, dd_reason = None, None
                if lv2 > 0 and dd >= lv2 and 0 < sc2 < 1.0:
                    dd_scale, dd_reason = sc2, f"드로다운 {dd:.1f}% x{sc2:g}"
                elif lv1 > 0 and dd >= lv1 and 0 < sc1 < 1.0:
                    dd_scale, dd_reason = sc1, f"드로다운 {dd:.1f}% x{sc1:g}"
                if dd_scale is not None:
                    scale *= dd_scale
                    reasons.append(dd_reason)
                    # [경보] 깊은 드로다운은 그 자체로 알려야 할 사건이다 — 진짜라면 재앙이고,
                    #  가짜라면(자산 스냅샷 오류) 룩백 내내 조용히 리스크 한도를 묶는다.
                    #  2026-08-23 가상투자에서 유령 고점 한 행이 드로다운을 50%로 만들어
                    #  히트 캡을 8.5%→6.8%로 조였고, 증액이 206주기 차단되는 동안 이 사실은
                    #  리스크 스케일링 로그 한 줄에만 묻혀 있었다. 하루 1회만 알린다.
                    if dd >= lv2:
                        today_key = datetime.now().strftime("%Y-%m-%d")
                        if getattr(self, '_dd_alert_date', None) != today_key:
                            self._dd_alert_date = today_key
                            equity = getattr(self, 'current_total_asset', 0) or self.initial_asset
                            msg = (f"⚠️ [계좌 드로다운 {dd:.1f}%]\n"
                                   f"자산 고점 대비 {dd:.1f}% 하락으로 판정해 신규 진입·증액 "
                                   f"리스크 한도를 x{dd_scale:g}로 줄였습니다.\n"
                                   f"(현재 평가자산 {equity:,.0f}원)\n\n"
                                   f"입출금은 정지 중에 있었던 것까지 자동 반영되므로 "
                                   f"조치할 것은 없습니다. 다만 손실이 실제와 다르다면 "
                                   f"자산 스냅샷(daily_asset_history)에 잘못된 고점이 남아 "
                                   f"있을 수 있습니다 — 그대로 두면 "
                                   f"{int(params.get('DD_LOOKBACK_DAYS', 90))}일간 한도가 묶입니다.")
                            self.log(f"[리스크 스케일링] 계좌 드로다운 {dd:.1f}% — 한도 x{dd_scale:g} 축소")
                            #  하루 한 번만 나가는 경보다. 본문이 "잘못된 고점이 남아 있으면
                            #  90일간 한도가 묶인다"고 사람의 확인을 요구하므로, 놓치면 그
                            #  90일이 그대로 흐른다. 전달을 확인한 뒤에 스로틀을 찍는다.
                            if not _pkg().alert_delivered(msg, urgent=True):
                                self._dd_alert_date = None
                    for m_type in market_scales:
                        market_scales[m_type] *= dd_scale
                        market_reasons[m_type] = " ".join(
                            p for p in (market_reasons.get(m_type, ""), dd_reason) if p)
        except Exception as e:
            logger.debug(f"[리스크 스케일링] 배수 계산 실패 (기존값 유지): {e}")
            return

        # 3) 같은 날 안에서는 조이기만 한다 — 푸는 것은 다음 날 첫 주기(_hold_intraday_floor 주석).
        scale, reason, market_scales, market_reasons = self._hold_intraday_floor(
            params, scale, ", ".join(reasons), market_scales, market_reasons)

        prev = getattr(self, 'risk_scale', 1.0)
        self.risk_scale = scale
        self.risk_scale_reason = reason
        self.risk_scale_by_market = market_scales
        self.risk_scale_reason_by_market = market_reasons
        if abs(scale - prev) > 1e-9:
            if scale < 1.0:
                rpt = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0)
                cap = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)
                per_market = ", ".join(
                    f"{m} x{market_scales[m]:.2f}(종목당 {rpt * market_scales[m]:.1f}%)"
                    for m in ("KOSPI", "KOSDAQ") if m in market_scales)
                self.log(f"[리스크 스케일링] 신규 진입 리스크 한도 축소 — {per_market or f'x{scale:.2f}'} "
                         f"| 히트 캡 {cap * scale:.1f}%(계좌 전체, 열위 시장 x{scale:.2f} 기준) "
                         f"({self.risk_scale_reason}) (청산 로직 영향 없음)")
            else:
                self.log("[리스크 스케일링] 리스크 한도 정상 복원 (x1.00)")

    @staticmethod
    def _risk_floor_day():
        """장중 래칫의 '하루' — 일일 시작 자산과 같은 달력일. 날짜가 바뀐 뒤 첫 주기에 새로 잰다."""
        return datetime.now().strftime("%Y-%m-%d")

    def _hold_intraday_floor(self, params, scale, reason, market_scales, market_reasons):
        """[리스크 스케일링 · 장중 래칫 2026-10-24] 같은 날 안에서는 배수를 **조이기만** 한다.

        그날 첫 주기에 잰 값에서 시작한다. 장중에 더 나빠지면(배수가 작아지면) 그 주기에 바로 따르고,
        좋아져도 그날은 유지한다. 푸는 것은 날짜가 바뀐 뒤 첫 주기다. 그래서 돌려주는 값은 언제나
        이번 주기 계산값 이하다 — 종전보다 느슨해지는 경우는 없다.

        [왜] 배수를 주기(약 1분)마다 새로 재면, 드로다운이 5%·10% 문턱 근처에 있거나 지수가 '하락 미확정'
         경계에 있을 때 1~2분 간격으로 오르내린다. 파이 가상투자(08-21~10-23)에서 배수 변경 로그가
         하루 최대 21줄(09-08)이었다. 09-14 에는 히트 캡(x0.49 → 4.9%)이 45분간 막고 있던 삼성E&A 진입이,
         배수가 x0.90 으로 풀린 6분 사이에 16주로 들어갔다(09-29 시간청산 -5.3%). 이 배수를 검증한
         감사(audit_market_axes·audit_drawdown_axis)는 일봉 종가로 하루 한 값을 썼다 — 분 단위로
         풀렸다 조였다 하는 것은 검증된 동작이 아니다.
        [하루 한 번 고정이 아닌 이유] 장중 급락에는 그날 바로 반응해야 한다. 시장 필터를 끈 운용에서는
         이 배수가 유일한 시장 브레이크다.
        [재기동] 당일 최저값을 일일 상태 파일(common.save_daily_risk_floor)에 남긴다 — 장중에 재기동해도
         풀리지 않는다. 못 읽거나 못 쓰면 이번 주기 계산값을 쓴다(종전 동작이고, 그보다 느슨해지지 않는다).
        [설정 변경] RISK_SCALING_PARAMS 가 바뀌면 그 자리에서 새로 잰다 — 축을 끄거나 문턱을 바꾸면
         곧바로 반영돼야 한다.
        """
        day = self._risk_floor_day()
        sig = repr(sorted((str(k), repr(v)) for k, v in (params or {}).items()))
        try:
            account_key = trade_account_key()
        except Exception:
            account_key = ""
        floor = getattr(self, '_risk_floor', None)
        if not floor or floor.get("date") != day:
            floor = load_daily_risk_floor(account_key, day)     # 장중 재기동 — 오늘 남긴 최저값을 이어 쓴다
        if not floor or floor.get("params") != sig:
            #  그날 첫 주기(또는 설정 변경) — 이번 계산값에서 시작한다.
            self._risk_floor = {"date": day, "params": sig, "scale": scale, "reason": reason,
                                "by_market": dict(market_scales), "reason_by_market": dict(market_reasons)}
            self._save_risk_floor(account_key, day)
            return scale, reason, market_scales, market_reasons

        tightened, held = False, []

        def pick(label, raw_v, raw_r, floor_v, floor_r):
            """(적용값, 적용 사유, 새 최저값, 그 사유) — 계산값이 최저 이하면 계산값, 아니면 최저를 유지."""
            nonlocal tightened
            try:
                floor_v = float(floor_v)
            except (TypeError, ValueError):
                floor_v = None
            if floor_v is None or not (0 < floor_v <= 1.0) or raw_v < floor_v - 1e-9:
                tightened = True
                return raw_v, raw_r, raw_v, raw_r
            if raw_v <= floor_v + 1e-9:
                return raw_v, raw_r, raw_v, raw_r       # 같은 값 — 사유는 지금 것으로
            if label:
                held.append(f"{label} 계산 x{raw_v:.2f} → 유지 x{floor_v:.2f}")
            return floor_v, f"[장중 유지] {floor_r}".rstrip(), floor_v, floor_r

        a_scale, a_reason, f_scale, f_reason = pick(None, scale, reason, floor.get("scale"), floor.get("reason", ""))
        fb, fr = floor.get("by_market") or {}, floor.get("reason_by_market") or {}
        a_ms, a_mr, f_ms, f_mr = {}, {}, {}, {}
        for m in market_scales:
            a_ms[m], a_mr[m], f_ms[m], f_mr[m] = pick(
                m, market_scales[m], market_reasons.get(m, ""), fb.get(m), fr.get(m, ""))
        self._risk_floor = {"date": day, "params": sig, "scale": f_scale, "reason": f_reason,
                            "by_market": f_ms, "reason_by_market": f_mr}
        if tightened:
            self._save_risk_floor(account_key, day)
        if held and getattr(self, '_risk_floor_held_noted', None) != day:
            self._risk_floor_held_noted = day
            self.log(f"[리스크 스케일링] 축소 사유가 장중에 풀렸지만 오늘은 유지합니다 — {', '.join(held)} "
                     f"(장중에는 조이기만 하고, 푸는 것은 다음 날 첫 주기다 · 하루 1회 안내)")
        return a_scale, a_reason, a_ms, a_mr

    def _save_risk_floor(self, account_key, day):
        """당일 최저 배수를 파일에 남긴다. 실패하면 하루 한 번 경고한다(이 세션의 래칫은 그대로 지킨다)."""
        try:
            ok = save_daily_risk_floor(account_key, day, self._risk_floor)
        except Exception as e:
            logger.debug(f"[리스크 스케일링] 당일 최저 배수 저장 예외: {e}")
            ok = False
        if not ok and getattr(self, '_risk_floor_save_warned', None) != day:
            self._risk_floor_save_warned = day
            logger.warning("[리스크 스케일링] 오늘의 최저 배수를 파일에 남기지 못했습니다 — 이 세션은 그대로 "
                           "지키지만, 재기동하면 그 시점 계산값에서 다시 시작합니다.")

    def _get_account_drawdown_pct(self, params=None):
        """계좌 드로다운(%) — 최근 DD_LOOKBACK_DAYS 일간 자산 고점(HWM) 대비 현재 평가자산 하락률

        HWM은 daily_asset_history(일일 시작자산 스냅샷)의 룩백 구간 최대값과 당일 시작자산 중
        큰 값을 사용한다. 룩백 제한은 오래된 고점·입출금으로 인한 왜곡을 완화하기 위함이다.
        DB 조회는 하루 1회만 수행하고 캐싱한다."""
        params = params or getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
        equity = getattr(self, 'current_total_asset', 0) or self.initial_asset
        if equity <= 0:
            return 0.0

        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, '_hwm_cache_date', None) != today:
            #  [Fix 2026-09-06] **조회 실패를 하루치 캐시에 굳히지 않는다.**
            #   종전에는 조회 실패도 '이력 없음'도 똑같이 None 이었고 `or 0.0` 이 그것을
            #   0 으로 접었다. 그러면 hwm 은 오늘 기준선까지만 올라간다 — 90일 고점이 사라져
            #   **드로다운이 실제보다 작게 계산된다**. 그러면 DD_SCALE 이 안 걸려 리스크
            #   스케일링(방어)이 그만큼 풀린다. 게다가 종전에는 실패해도 _hwm_cache_date 를
            #   오늘로 박아, 그날 첫 조회 한 번만 실패해도 **하루 종일** 그 상태로 남았다.
            #   못 구했으면 캐시하지 않는다 — 다음 주기에 다시 잰다.
            hwm = 0.0
            hwm_ok = False
            try:
                lookback = int(params.get("DD_LOOKBACK_DAYS", 90))
                if lookback <= 0:
                    lookback = 90
                start_date = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
                cano, acnt = _get_trade_account()
                account_key = f"{cano}-{acnt}"
                #  None = **이력 없음**(신규 계좌)이다. 그것은 결론이므로 캐시한다.
                #  조회 **실패**는 예외로 올라온다(get_max_daily_asset 주석) — 아래에서 받는다.
                raw_hwm = db_manager.db.get_max_daily_asset(start_date, account_key)
                hwm = float(raw_hwm or 0.0)
                hwm_ok = True
            except Exception as e:
                logger.warning(
                    f"[리스크] 자산 고점(HWM)을 조회하지 못했습니다 — 이번 주기 드로다운은 "
                    f"오늘 기준선까지만 봅니다(캐시하지 않고 다음 주기 재시도): {e}")
            if hwm_ok:
                self._hwm_cache = hwm
                self._hwm_cache_date = today
            #  실패했으면 직전에 알던 고점을 지우지 않는다. 0 으로 덮으면 드로다운이
            #  더 작아져(=방어가 더 풀려) 실패가 이득처럼 작동한다.

        # [Fix 2026-09-01] 바닥값은 원본 시작 자산이 아니라 **오늘 입출금까지 반영한**
        #  기준선이다. 원본을 쓰면 오늘 나간 출금이 그대로 고점으로 남아, 정상적인 출금
        #  한 번이 그날 내내 가짜 드로다운(1,000만에서 300만 출금 = 30%)을 만들고 경보까지
        #  울린다. DB 이력 쪽은 net_transfer 로 이미 환산되는데 이 한 줄만 빠져 있었다.
        hwm = max(getattr(self, '_hwm_cache', 0.0),
                  float(self.effective_baseline() or self.initial_asset or 0))
        if hwm <= 0:
            return 0.0
        return max(0.0, (hwm - equity) / hwm * 100.0)
