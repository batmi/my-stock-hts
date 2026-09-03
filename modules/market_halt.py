"""시장 정지(서킷브레이커/VI) 감지 및 텔레그램 알림 모듈.

추정(지수 등락률)이 아니라 거래소가 내려주는 '실제 상태 플래그'를 사용한다.
 - 서킷브레이커(CB): KIS 시세 응답의 temp_stop_yn(임시정지여부). 시장 전체가 멈추므로
   대표 유동주 바스켓이 '동시에' 정지하면 시장 CB로 판정(개별 종목 정지 오탐 방지).
 - VI(변동성완화장치): KIS는 vi_cls_code(VI적용구분코드), 토스는 get_warnings로
   종목별 REST 폴링한다(WS 슬롯은 현재가에 우선 배정하므로 VI에는 쓰지 않는다).
   종목 단위라 보유+관심종목으로 감시 범위를 한정한다.
   VI 알림은 REST 부하가 있어 옵션(MARKET_HALT_VI_USE)으로 두며 기본값은 OFF다.
 - 사이드카: 프로그램매매 호가만 정지하고 일반 거래는 지속되어 REST 실제 플래그가
   없으므로 지원하지 않는다(추정 제외 방침).

모드별 지원: KIS(실전/모의) = CB + VI(옵션), 토스 = VI(옵션) 전용. VI는 모두 REST 폴링.
"""
import logging
import time
import math
from datetime import datetime

import config
import api
from core import utils

logger = logging.getLogger(__name__)

# CB 감지용 대표 유동주 바스켓 (시장별). 동시 정지 시 시장 CB로 판정.
_CB_BASKET = {
    "KOSPI": [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차")],
    "KOSDAQ": [("247540", "에코프로비엠"), ("196170", "알테오젠")],
}
# KIS 국내 지수 코드 (보조 표기용 등락률)
_INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}


def _is_kr_domestic_code(code):
    """국내 종목코드(6자리, 숫자 시작, 영숫자) 여부."""
    return bool(code) and len(code) == 6 and code[0].isdigit() and code.isalnum()


def _kis_vi_active(vi_code):
    """KIS vi_cls_code가 VI 발동 상태인지 판정.
    값 스펙이 환경에 따라 다를 수 있어 보수적으로 '정상'을 제외한 값을 발동으로 본다."""
    if vi_code is None:
        return False
    s = str(vi_code).strip().upper()
    return s not in ("", "0", "00", "N", "NONE")


def _toss_warning_is_vi(w):
    """토스 get_warnings 항목이 VI인지 판정."""
    try:
        wt = str(w.get("warningType", "")).upper()
        return "VI" in wt or "VOLATIL" in wt
    except Exception:
        return False


class MarketHaltMonitor:
    """서킷브레이커/VI 감지 싱글톤. 스케줄러가 주기적으로 check()를 호출한다."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.cb_active = {"KOSPI": False, "KOSDAQ": False}
        self.vi_active = set()      # 현재 VI 발동 중인 코드 집합
        self.vi_names = {}          # 코드 -> 종목명 (해제 알림용)
        self.last_cb_check = 0.0
        self.last_vi_check = 0.0
        self._warned_no_vi_field = False

    # ---- 진입점 ----
    def check(self):
        """스케줄러 주기 호출. 내부에서 장중/주기 게이트를 적용한다.

        CB(서킷브레이커)와 VI는 서로 독립적인 스위치로 각각 ON/OFF한다.
         - MARKET_HALT_ALERT_USE: 서킷브레이커(CB) — 시장 전체 정지. KIS 전용, 대표종목 바스켓 REST 폴링.
         - MARKET_HALT_VI_USE: VI — 보유+관심 '종목별' REST 폴링(기본 OFF).
        """
        cb_on = getattr(config, "MARKET_HALT_ALERT_USE", True)
        vi_on = getattr(config, "MARKET_HALT_VI_USE", False)
        if not (cb_on or vi_on):
            return
        if not self._is_kr_market_hours():
            return
        try:
            is_toss = getattr(config.session, "is_toss", False)
            now = time.time()

            # CB(서킷브레이커): 시장 전체 정지 감지. KIS 전용, 대표 유동주 바스켓 REST 폴링.
            if cb_on and not is_toss and (now - self.last_cb_check) >= getattr(config, "MARKET_HALT_CB_INTERVAL", 20):
                self.last_cb_check = now
                self._check_cb_kis()

            # VI: 보유+관심 종목별 REST 폴링(기본 OFF).
            if vi_on and (now - self.last_vi_check) >= getattr(config, "MARKET_HALT_VI_INTERVAL", 30):
                self.last_vi_check = now
                current = self._check_vi_toss() if is_toss else self._check_vi_kis()
                self._diff_vi_alerts(current)
        except Exception as e:
            logger.error(f"[MarketHalt] 점검 오류: {e}")

    # ---- 공통 ----
    def _is_kr_market_hours(self):
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.strftime("%H%M")
        if not ("0900" <= hhmm <= "1530"):
            return False
        try:
            if api.is_holiday_today():
                return False
        except Exception:
            pass
        return True

    def _domestic_targets(self):
        """VI 감시 대상(보유+관심종목) dict(code->name). 국내 종목만."""
        targets = {}
        # 관심종목 (국내 주식/ETF)
        sd = getattr(config.session, "stock_data", None) or {}
        for key in ("stocks_kr", "etfs_kr"):
            for it in sd.get(key, []) or []:
                c = it.get("code")
                if _is_kr_domestic_code(c):
                    targets[c] = it.get("name", c)
        # 보유종목 (시스템 트레이딩 계좌)
        #  [Fix 2026-09-04] 종전에는 계좌 컨텍스트 없이 잔고를 물었다. 이 코드는
        #   스케줄러 스레드에서 도는데 context.trade_context 는 threading.local 이라
        #   상속되지 않아 **수동 계좌** 잔고가 돌아온다(core/utils.inherit_account_context
        #   주석 참조). 실전(mode 2)에서 자동매매 보유 종목이 통째로 VI 감시에서 빠졌다.
        try:
            cano, acnt = utils.system_trading_account()
            with utils.AccountContext(cano):
                holdings, _ = api.get_domestic_balance(cano, acnt)
            for h in holdings or []:
                c = h.get("pdno")
                if _is_kr_domestic_code(c) and int(h.get("hldg_qty", 0) or 0) > 0:
                    targets[c] = h.get("prdt_name", c)
        except Exception as e:
            logger.debug(f"[MarketHalt] 보유종목 조회 실패: {e}")

        cap = getattr(config, "MARKET_HALT_VI_MAX_CODES", 40)
        if len(targets) > cap:
            # 상한 초과 시 일부만 감시 (Pi 부하 방어)
            targets = dict(list(targets.items())[:cap])
        return targets

    # ---- 서킷브레이커(CB) : KIS ----
    def _check_cb_kis(self):
        for market, basket in _CB_BASKET.items():
            halted = 0
            checked = 0
            for code, _name in basket:
                try:
                    res = api.get_current_price_data(code, is_overseas=False)
                    if res and res.get("rt_cd") == "0":
                        out = res.get("output", {}) or {}
                        checked += 1
                        if str(out.get("temp_stop_yn", "N")).upper() == "Y":
                            halted += 1
                except Exception:
                    pass

            if checked == 0:
                continue
            # 시장 CB 판정: 최소 2종목 이상 '동시' 정지 (개별 정지 오탐 방지)
            need = max(2, math.ceil(checked / 2))
            is_cb = halted >= need

            if is_cb and not self.cb_active[market]:
                self.cb_active[market] = True
                self._alert_cb(market, True)
            elif not is_cb and self.cb_active[market]:
                self.cb_active[market] = False
                self._alert_cb(market, False)

    def _index_rate(self, market):
        code = _INDEX_CODE.get(market)
        if not code:
            return None
        try:
            res = api.get_domestic_index_price(code)
            if res and res.get("rt_cd") == "0":
                out = res.get("output", {}) or {}
                curr = float(out.get("bstp_nmix_prpr", 0) or 0)
                prev = float(out.get("bstp_nmix_prdy_clpr", 0) or 0)
                if prev > 0:
                    return (curr - prev) / prev * 100
        except Exception:
            pass
        return None

    def _alert_cb(self, market, active):
        name = "코스피" if market == "KOSPI" else "코스닥"
        rate = self._index_rate(market)
        rate_str = f" (지수 {rate:+.2f}%)" if rate is not None else ""
        if active:
            #  [Fix 2026-09-04] 종전 문구는 "자동매매 시스템도 매매가 보류됩니다"였다.
            #   그런 장치는 없다 — cb_active 를 읽는 곳이 매매 경로에 하나도 없고,
            #   CB 중에도 감시·주문은 그대로 돈다. 알림이 하지 않는 일을 알리면 사람이
            #   손대야 할 순간에 손을 놓는다. 사실만 적고 판단은 사람에게 남긴다.
            #   (CB 중 매매 보류를 실제로 넣을지는 별도 결정 사항이다 — 재개 직후의
            #    급변동에서 손절이 함께 멈추는 대가가 있어 측정 없이 넣지 않는다.)
            msg = (f"🛑 [서킷브레이커 발동] {name} 시장{rate_str}\n"
                   f"전 종목 거래가 일시 정지된 것으로 감지되었습니다.\n"
                   f"⚠️ 자동매매는 계속 가동됩니다 — 필요하면 직접 중지하세요.")
        else:
            msg = (f"✅ [서킷브레이커 해제] {name} 시장{rate_str}\n"
                   f"거래가 재개된 것으로 감지되었습니다.")
        api.send_telegram_message(msg)
        logger.info(f"[MarketHalt] CB {name} {'발동' if active else '해제'}")

    # ---- VI : KIS ----
    def _check_vi_kis(self):
        current = {}
        any_field = False
        for code, name in self._domestic_targets().items():
            try:
                res = api.get_current_price_data(code, is_overseas=False)
                if not res or res.get("rt_cd") != "0":
                    continue
                out = res.get("output", {}) or {}
                if "vi_cls_code" in out:
                    any_field = True
                if _kis_vi_active(out.get("vi_cls_code")):
                    current[code] = name
            except Exception:
                pass
        if not any_field and not self._warned_no_vi_field:
            self._warned_no_vi_field = True
            logger.warning("[MarketHalt] KIS 시세 응답에 vi_cls_code 필드가 없어 VI 감지를 건너뜁니다.")
        return current

    # ---- VI : 토스 ----
    def _check_vi_toss(self):
        current = {}
        try:
            from brokers import toss_api
        except Exception as e:
            logger.debug(f"[MarketHalt] toss_api 임포트 실패: {e}")
            return current
        for code, name in self._domestic_targets().items():
            try:
                warnings = toss_api.get_warnings(code) or []
                if any(_toss_warning_is_vi(w) for w in warnings):
                    current[code] = name
            except Exception:
                pass
        return current

    def _diff_vi_alerts(self, current):
        """현재 VI 집합과 직전 상태를 비교해 신규 발동/해제 알림을 전송."""
        cur_codes = set(current.keys())
        for c in cur_codes - self.vi_active:
            self._alert_vi(c, current.get(c, c), True)
        for c in self.vi_active - cur_codes:
            self._alert_vi(c, self.vi_names.get(c, c), False)
        self.vi_names.update(current)
        self.vi_active = cur_codes

    def _alert_vi(self, code, name, active):
        if active:
            msg = (f"⚡ [VI 발동] {name}({code})\n"
                   f"변동성완화장치(VI)가 발동되어 약 2분간 단일가 매매로 전환됩니다.")
        else:
            msg = (f"🔄 [VI 해제] {name}({code})\n"
                   f"변동성완화장치(VI)가 해제되어 정상 거래가 재개되었습니다.")
        api.send_telegram_message(msg)
        logger.info(f"[MarketHalt] VI {name}({code}) {'발동' if active else '해제'}")
