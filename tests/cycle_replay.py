"""주기 재생 하네스 — AutoTrader 를 실제 기동 경로(start → _run_loop)로 몇 주기 돌려 바깥으로 보이는 결과를 기록한다.

[왜 · 2026-10-24] AutoTrader(7,659줄)를 책임별로 나누는 작업(C1)의 동등성 기준이다. 나누기 전(기준 커밋)에
이 하네스로 남긴 기록과 나눈 뒤의 기록이 같으면, 바깥에서 보이는 동작이 바뀌지 않았다는 뜻이다.
메서드 단위 테스트는 한 메서드의 계약만 보지만, 나누는 작업이 깨뜨리기 쉬운 것은 메서드 **사이**의
흐름(누가 어떤 상태를 언제 쓰고 읽는가)이라 주 루프를 통째로 돌린다.

[무엇을 가짜로 두나] 바깥 세계만 바꾼다. 판단·주문·기록 코드는 실제 코드가 돈다.
  · 설정: 클래스 기본값(GlobalSettings()) — 기기의 운용 설정 파일에 기대지 않는다
  · 계좌·주문: 가상투자 브로커(paper_broker) — 실제 운용 모드 1 과 같은 경로
  · 시세·차트·지수: 이 파일의 합성 시장(시드 고정)
  · 시계: 고정 시작 시각 + time.sleep 만큼 앞으로 간다(datetime·time 을 프로젝트 모듈에서 바꾼다).
    시각은 운용 기기처럼 로컬 = KST 로 읽힌다 — 비교 테스트는 새 프로세스를 TZ=Asia/Seoul 로 띄운다
  · 스레드: AutoTrader 스레드는 띄우지 않고 _run_loop 를 이 스레드에서 돌린다. 스레드 풀은 순차 실행
    (실행 순서를 고정해 로그·알림 순서까지 결정적으로 만든다)
  · 알림: 텔레그램 발송을 가로채 기록한다. 경보(alert_delivered — 전달을 확인하고 스로틀을 거는 알림)도 따로
    기록한다 — 토큰이 없는 테스트 환경에서는 진짜 함수가 보내지 않고 True 만 돌려줘 기록에 남지 않았다
    (판단 보류·계좌 드로다운 경보가 그렇다). 대역도 같은 True 를 돌려준다.
[무엇을 기록하나] 주문·체결(가상 원장), 거래 기록 표, 알림, 트레이더 로그, 화면 출력(상태·보고·건강),
 그리고 트레이더 상태 요약. 결정적이지 않은 값(임시 경로·메모리 사용량)은 가린다.
[시나리오 · SCENARIOS] 시나리오마다 기준 기록 파일이 따로 있다.
  · base — 매매 경로(피라미딩·손절·시간청산·추세이탈·트레일링·슬롯 가득·신규 매수). 지수는 판정이 서지 않는
    단조 상승이라 시장·리스크 경로는 거의 지나지 않는다.
  · market_stress — 같은 종목·보유에 시장·리스크 압박을 얹는다(C1 2단계 '시장·리스크' 분리의 기준, 2026-10-24).
    KOSPI 하락 미확정·휩소율 → 장중 반등(장중 래칫 유지) → 조회 실패(판단 보류), KOSDAQ 이평선 이탈(필터 차단)
    → 회복 → 조회 중 예외, 자산 고점 대비 10% 넘는 계좌 드로다운(2단계 감속·경보), 지수 장기 이력 수신 실패
    (동적 손절 캡 고정·경고).
  · account_sync — base 시장에 계좌 사건을 얹는다(C1 3단계 '계좌 동기화' 분리의 기준, 2026-10-25).
    꺼져 있던 사이의 출금 30만(첫 주기 원금 대조로 되찾음) → 1주기 뒤 장중 출금 150만(그 주기 일일 손실 한도로
    방어 모드 → 다음 주기 순입출금 반영으로 스스로 해제 → 3주기 연속 확인으로 입출금 알림), 기동 시 외부 체결
    동기화 결과(외부 매수 제한·부분 복원 안내), 기동~첫 주기의 자산 집계 일부 실패(시작 자산 보류 → 첫 주기에
    대안 계산으로 기준선을 잡는다).
"""
import concurrent.futures
import datetime as _dt
import json
import os
import re
import sys
import threading
import time as _real_time
from contextlib import ExitStack
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import api
import config
from modules import analysis, db_manager, paper_broker
import brokers.realtime  # noqa: F401 - 재생 중에 처음 import 되면 진짜 시계를 쓰므로 미리 올려 둔다

KST = ZoneInfo("Asia/Seoul")
START = _dt.datetime(2026, 10, 21, 10, 0, 0)     # 수요일 정규장, 개장 30분 진입 보류가 끝난 뒤
SEED_CAPITAL = 10_000_000
CYCLES = 4
_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN = os.path.join(_FIXTURES, "cycle_replay_golden.json")
SCENARIOS = {"base": GOLDEN, "market_stress": os.path.join(_FIXTURES, "cycle_replay_golden_market.json"),
             "account_sync": os.path.join(_FIXTURES, "cycle_replay_golden_account.json")}


# ── 시계 ───────────────────────────────────────────────────────────────────────────────────
class Clock:
    def __init__(self, start):
        self.now = start

    def advance(self, seconds):
        self.now = self.now + _dt.timedelta(seconds=float(seconds))

    def epoch(self):
        return self.now.replace(tzinfo=KST).timestamp()


CLOCK = Clock(START)


class _FakeMeta(type):
    """isinstance(진짜 datetime, 가짜 datetime) 이 참이 되게 한다 — 바꾼 모듈의 isinstance 검사가 깨지지 않게."""
    __real__ = object

    def __instancecheck__(cls, obj):
        return isinstance(obj, cls.__real__)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls.__real__)


class FakeDatetime(_dt.datetime, metaclass=_FakeMeta):
    __real__ = _dt.datetime

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return CLOCK.now
        return CLOCK.now.replace(tzinfo=KST).astimezone(tz)

    @classmethod
    def today(cls):
        return CLOCK.now

    @classmethod
    def utcnow(cls):
        return CLOCK.now.replace(tzinfo=KST).astimezone(_dt.timezone.utc).replace(tzinfo=None)


class FakeDate(_dt.date, metaclass=_FakeMeta):
    __real__ = _dt.date

    @classmethod
    def today(cls):
        return CLOCK.now.date()


class FakeTime:
    """time 모듈 대역 — 시계를 읽고, sleep 은 시계를 앞으로 민다(실제로 자지 않는다)."""

    def __init__(self, on_sleep=None):
        self._on_sleep = on_sleep

    def time(self):
        return CLOCK.epoch()

    def monotonic(self):
        return CLOCK.epoch()

    def perf_counter(self):
        return CLOCK.epoch()

    def sleep(self, seconds):
        CLOCK.advance(max(float(seconds), 0.0))
        if self._on_sleep:
            self._on_sleep()

    def __getattr__(self, name):
        return getattr(_real_time, name)


# ── 순차 실행 스레드 풀 ─────────────────────────────────────────────────────────────────────
class SerialExecutor(concurrent.futures.Executor):
    """submit 즉시 이 스레드에서 실행한다 — 결과 순서가 제출 순서로 고정된다."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:      # noqa: BLE001 - 실제 풀과 같이 Future 에 담는다
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1, buffersize=None):
        return iter([fn(*a) for a in zip(*iterables)])

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def _as_completed_in_order(fs, timeout=None):
    return iter(list(fs))


class _NoStartThread(threading.Thread):
    """AutoTrader 가 띄우는 스레드 — 만들되 띄우지 않는다(주 루프는 하네스가 직접 돈다)."""

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


# ── 합성 시장 ──────────────────────────────────────────────────────────────────────────────
#  kind: up(강매수 후보) · side(관망) · down(매도 상태). seed 는 그 kind 의 상태가 나오는 것을 확인한 값이다.
#  path 는 주기마다의 현재가(전일 종가 대비). 기동 직후 슬롯 5/4 → 손절·시간청산·추세이탈로 빈 슬롯에 신규 매수.
UNIVERSE = [
    {"code": "100010", "name": "재생상승A", "exchange": "KOSPI", "kind": "up", "seed": 1, "base": 20000.0,
     "path": [0.000, 0.004, 0.008, 0.012]},          # 시드 보유 +13% → 피라미딩
    {"code": "100020", "name": "재생상승B", "exchange": "KOSDAQ", "kind": "up", "seed": 2, "base": 15000.0,
     "path": [0.000, -0.030, -0.080, -0.120]},       # 조건 충족인데 슬롯이 차 주문 미전송 → 이어서 급락
    {"code": "100030", "name": "재생상승C", "exchange": "KOSPI", "kind": "up", "seed": 5, "base": 30000.0,
     "path": [0.000, 0.002, 0.004, 0.006]},          # 신규 매수 후 보유
    {"code": "100040", "name": "재생되밀림", "exchange": "KOSPI", "kind": "up", "seed": 1, "base": 25000.0,
     "path": [-0.020, -0.050, -0.090, -0.110]},      # 시드 보유 +25% 에서 고점 대비 되밀림 → 트레일링 스탑
    {"code": "100050", "name": "재생하락", "exchange": "KOSDAQ", "kind": "down", "seed": 1, "base": 12000.0,
     "path": [0.000, -0.005, -0.010, -0.015]},       # 시드 보유 -20% → ATR 손절
    {"code": "100060", "name": "재생정체", "exchange": "KOSDAQ", "kind": "side", "seed": 3, "base": 18000.0,
     "path": [0.000, 0.000, 0.000, 0.000]},          # 시드 보유 20영업일, 본전 아래 → 시간청산
    {"code": "100070", "name": "재생이탈", "exchange": "KOSPI", "kind": "side", "seed": 4, "base": 25000.0,
     "path": [0.000, -0.030, -0.080, -0.100]},       # 시드 보유, 점수 하락 + 60일선 이탈 → 추세이탈 매도
]
#  기동 전에 들고 있던 포지션(가상 원장·거래 기록에 심는다). rel = 평단 / 전일 종가.
SEEDED_HOLDINGS = [
    {"code": "100010", "qty": 30, "rel": 1 / 1.13, "days_ago": 6},
    {"code": "100040", "qty": 20, "rel": 0.80, "days_ago": 8},
    {"code": "100050", "qty": 40, "rel": 1.25, "days_ago": 12},
    {"code": "100060", "qty": 40, "rel": 1.005, "days_ago": 20},
    {"code": "100070", "qty": 20, "rel": 0.735, "days_ago": 8},
]
#  [market_stress] 지수 — 주기마다의 오늘 봉(전일 종가 대비). None = 그 주기에 지수 조회 실패(빈 표),
#   "error" = 조회 중 예외.
#   값은 실제 판정 함수(analysis.classify_regime_from_df·indicators.get_market_filter_detail)로 찾았다:
#   KOSPI  하락 미확정·휩소율 50% · 필터 통과 → 3주기 +1.5% 반등에 상승 미확정 → 4주기 조회 실패
#   KOSDAQ 하락 미확정·휩소율 75% · 이평선 -2.6% 이탈(차단) → 3주기 +4% 에 +1.7% 회복(해제) → 4주기 예외
STRESS_INDEX = {
    "KOSPI": {"seed": 1, "amp": 0.04, "period": 50, "trend": 0.0008, "tail": [-0.004] * 6,
              "path": [0.000, -0.002, 0.015, None]},
    "KOSDAQ": {"seed": 3, "amp": 0.03, "period": 30, "trend": 0.0004, "tail": [-0.008] * 8,
               "path": [-0.004, -0.004, 0.040, "error"]},
}
#  [market_stress] 기동 전 자산 이력(일일 시작 자산) — 15영업일 전 고점 1,190만, 어제 1,070만.
#   기동 자산(약 1,060만)이 고점 대비 10% 넘게 빠져 있어 드로다운 2단계(감속·경보)가 걸린다.
STRESS_ASSET_HISTORY = [11_000_000 + 60_000 * i for i in range(16)] + [11_900_000 - 120_000 * i for i in range(1, 11)]
#  [account_sync] 계좌 사건.
#   · 어제 시작 자산 1,090만과 그때의 기준 원금(오늘 원금 + 30만)을 남겨 둔다 — 꺼져 있던 사이 30만이 빠졌다.
#   · 1주기가 끝난 뒤 가상 계좌에서 150만을 출금한다(장중 입출금).
#   · 기동 시 외부 체결 동기화는 가상투자에서 꺼진다(holdings_backfill.supports_broker_history) — 증권사 체결 이력을
#     대조한 결과 요약을 대역으로 준다. 복원·제한 등록 자체는 holdings_backfill 테스트가 본다.
ACCOUNT_OFFLINE_WITHDRAWAL = 300_000
ACCOUNT_YESTERDAY_ASSET = 10_900_000
ACCOUNT_INTRADAY = {1: -1_500_000}          # 주기 번호(그 주기가 시작되기 직전) → 가상 입출금액
ACCOUNT_BACKFILL = {"written": 2, "skipped": 0, "restricted": ["재생외부(100090)"],
                    "partial": [("100060", "재생정체", 10)], "error": None}
ACCOUNT_DEGRADED_CYCLES = {0}               # 이 주기(0 = 기동 포함)의 자산 집계는 해외잔고 구간이 실패한다


def _daily(kind, seed, base, n=260, end=None):
    rs = np.random.RandomState(seed)
    if kind == "up":
        drift = np.r_[np.full(n - 15, 0.004), np.full(10, -0.006), np.full(5, 0.012)]
    elif kind == "side":
        drift = np.zeros(n)
    else:
        drift = np.full(n, -0.003)
    noise = rs.normal(0, 0.012, n)
    close = base * np.cumprod(1 + drift + noise)
    vol = (rs.uniform(0.8, 1.2, n) * 200000).astype(int)
    vol[-5:] = (vol[-5:] * np.linspace(1.5, 2.5, 5)).astype(int)
    dates = pd.bdate_range(end=end or (START.date() - _dt.timedelta(days=1)), periods=n)
    op = close / (1 + rs.normal(0, 0.004, n))
    hi = np.maximum(op, close) * (1 + rs.uniform(0, 0.01, n))
    lo = np.minimum(op, close) * (1 - rs.uniform(0, 0.01, n))
    return pd.DataFrame({"date": dates.strftime("%Y%m%d"), "open": op.round(), "high": hi.round(),
                         "low": lo.round(), "close": close.round(), "volume": vol})


def _stress_index(market_name, cycle):
    """[market_stress] 지수 일봉 — 확정 299봉(진동 + 끝의 하락) + 오늘 봉. 경로 칸이 None 이면 빈 표(조회 실패)."""
    spec = STRESS_INDEX[market_name]
    today = spec["path"][min(cycle, len(spec["path"]) - 1)]
    if today is None:
        return pd.DataFrame()
    if today == "error":
        raise ConnectionError(f"재생: {market_name} 지수 조회 실패")
    n = 299
    rs = np.random.RandomState(spec["seed"])
    t = np.arange(n)
    r = (spec["trend"] + spec["amp"] * 2 * np.pi / spec["period"] * np.cos(2 * np.pi * t / spec["period"])
         + rs.normal(0, 0.004, n))
    r[-len(spec["tail"]):] += np.array(spec["tail"])
    close = 2500.0 * np.cumprod(1 + r)
    close = np.r_[close, close[-1] * (1 + today)]
    dates = list(pd.bdate_range(end=START.date() - _dt.timedelta(days=1), periods=n).strftime("%Y%m%d"))
    dates.append(START.strftime("%Y%m%d"))
    df = pd.DataFrame({"date": dates, "open": close, "high": close * 1.003, "low": close * 0.997,
                       "close": close, "volume": np.full(n + 1, 1_000_000)})
    df.attrs["source"] = "REPLAY"
    return df


def _tick_floor(price):
    """호가 단위로 내린다(코스피·코스닥 공통 2023 개편 표)."""
    p = float(price)
    for lim, unit in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if p < lim:
            return int(p // unit * unit)
    return int(p // 1000 * 1000)


class Market:
    def __init__(self, universe, scenario="base"):
        self.items = {u["code"]: u for u in universe}
        self.daily = {u["code"]: _daily(u["kind"], u["seed"], u["base"]) for u in universe}
        self.cycle = 0          # 하네스가 주기마다 올린다(가격 경로의 칸)
        self.scenario = scenario

    def prev_close(self, code):
        return float(self.daily[code].iloc[-1]["close"])

    def price(self, code):
        u = self.items.get(code)
        if u is None:
            return 0.0
        k = min(self.cycle, len(u["path"]) - 1)
        return float(_tick_floor(self.prev_close(code) * (1 + u["path"][k])))

    def chart(self, code):
        if code not in self.daily:
            return pd.DataFrame()
        df = self.daily[code].copy()
        u = self.items[code]
        seen = [self.prev_close(code) * (1 + r) for r in u["path"][:min(self.cycle, len(u["path"]) - 1) + 1]]
        cur = self.price(code)
        today = {"date": CLOCK.now.strftime("%Y%m%d"), "open": float(_tick_floor(seen[0])),
                 "high": float(_tick_floor(max(seen + [cur]))), "low": float(_tick_floor(min(seen + [cur]))),
                 "close": cur, "volume": int(df["volume"].iloc[-5:].mean())}
        return pd.concat([df, pd.DataFrame([today])], ignore_index=True)

    def index(self, market_name):
        if self.scenario == "market_stress":
            return _stress_index(market_name, self.cycle)
        n = 200
        closes = 2500.0 * np.cumprod(np.full(n, 1.0015))
        dates = pd.bdate_range(end=START.date(), periods=n).strftime("%Y%m%d")
        df = pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.003, "low": closes * 0.997,
                           "close": closes, "volume": np.full(n, 1_000_000)})
        df.attrs["source"] = "REPLAY"
        return df

    @staticmethod
    def kospi_long(settled_dd):
        """동적 손절 캡의 지수 변동성 배율 입력 — 15년 KOSPI 확정 종가(끝 60일만 변동성을 키워 배율이 1.0 을 벗어난다)."""
        n = 3700
        rs = np.random.RandomState(7)
        sd = np.r_[np.full(n - 60, 0.010), np.full(60, 0.018)]
        close = 1000.0 * np.cumprod(1 + rs.normal(0.0003, sd))
        return pd.Series(close), settled_dd, "REPLAY"


# ── 실행 ───────────────────────────────────────────────────────────────────────────────────
_MASKS = (
    (re.compile(r"/(?:private/)?(?:var|tmp)/[^\s'\"]*"), "<TMP>"),
    (re.compile(r"[\d,]+(?:\.\d+)?\s*MB"), "<MB>"),          # 메모리·디스크 여유는 실행 기기 값이다
    (re.compile(r"[\d,]+(?:\.\d+)?\s*GB"), "<GB>"),
    (re.compile(r"pid[=: ]*\d+"), "pid=<PID>"),
)


def _mask(s):
    s = str(s)
    for rx, rep in _MASKS:
        s = rx.sub(rep, s)
    return s.rstrip()


def _project_modules():
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name in ("config", "main") or name.split(".")[0] in ("modules", "api", "core", "brokers"):
            yield mod


def run(tmp_path, cycles=CYCLES, scenario="base"):
    """하네스를 한 번 돌리고 기록(dict)을 돌려준다. tmp_path 는 가상 원장 DB 자리."""
    if scenario not in SCENARIOS:
        raise ValueError(f"모르는 시나리오: {scenario}")
    CLOCK.now = START
    os.makedirs(str(tmp_path), exist_ok=True)
    market = Market(UNIVERSE, scenario)
    telegrams = []
    trader_box = {}

    def _on_sleep():
        t = trader_box.get("t")
        if t is not None and len(t.cycle_secs_history) >= cycles:
            t.is_running = False

    fake_time = FakeTime(on_sleep=_on_sleep)

    def _send_telegram(msg, *a, **k):
        telegrams.append(_mask(msg))
        return True

    alerts = []

    def _alert_delivered(message, urgent=False):
        alerts.append({"urgent": bool(urgent), "message": _mask(message)})
        return True             # 토큰 없는 진짜 함수와 같은 값(알림 수단 없음 = 전달로 본다)

    with ExitStack() as st:
        P = lambda *a, **k: st.enter_context(patch.object(*a, **k))     # noqa: E731

        # 설정 — 클래스 기본값으로 고정한다(다른 설정 패치보다 먼저: 필드 대입은 지금의 settings 로 간다).
        #  이 기기의 운용 설정 파일(json/dynamic_config.json)은 config import 때 이미 읽혀 있어, 그대로 두면 기록이
        #  기기마다 달라진다 — 실측: 맥북 파일이 RSI 상한 70 을 70.0 으로 줘 화면 글자가 갈렸고, 매매일지 연동이 켜져 있었다.
        pinned = config.GlobalSettings()
        pinned.ENABLE_TELEGRAM = False          # conftest 격리와 같은 값(발송은 아래에서 가로채 기록한다)
        P(config, "settings", pinned)

        # 시계 — 프로젝트 모듈이 import 해 둔 datetime/date/time 을 바꾼다
        for mod in _project_modules():
            if getattr(mod, "datetime", None) is _dt.datetime:
                P(mod, "datetime", FakeDatetime)
            if getattr(mod, "date", None) is _dt.date:
                P(mod, "date", FakeDate)
            if getattr(mod, "time", None) is _real_time:
                P(mod, "time", fake_time)
        P(concurrent.futures, "ThreadPoolExecutor", SerialExecutor)
        P(concurrent.futures, "as_completed", _as_completed_in_order)
        P(concurrent.futures, "wait", lambda fs, timeout=None, return_when=None:
          concurrent.futures._base.DoneAndNotDoneFutures(set(fs), set()))
        from modules.auto_trade import trader as _trader_mod
        P(_trader_mod.threading, "Thread", _NoStartThread)

        # 가상투자 계좌
        db_path = os.path.join(str(tmp_path), "replay_paper.db")
        P(config, "PAPER_DB_FILE_PATH", db_path, create=True)
        P(config, "PAPER_SEED_CAPITAL", SEED_CAPITAL, create=True)
        sess = config.session
        for k, v in (("is_paper", True), ("is_toss", False), ("cano", "PAPER"), ("acnt_prdt_cd", ""),
                     ("auto_cano", "PAPER"), ("auto_acnt_prdt_cd", ""), ("auto_app_key", "replay-key"),
                     ("auto_app_secret", "replay-secret"), ("hts_id", "")):
            P(sess, k, v, create=True)
        stock_data = {"stocks_kr": [{"code": u["code"], "name": u["name"], "exchange": u["exchange"]}
                                    for u in UNIVERSE], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
        P(sess, "stock_data", stock_data, create=True)
        original_db = db_manager.db.db_path
        db_manager.db.switch_path(db_path)
        st.callback(lambda: (db_manager.db.close_all_connections(), db_manager.db.switch_path(original_db)))
        paper_broker.init_tables()
        paper_broker.reset(SEED_CAPITAL)
        paper_broker.reset_price_cache()
        P(paper_broker, "_odno_seq", 0)
        _seed_holdings(market)
        if scenario == "market_stress":
            _seed_asset_history()
        if scenario == "account_sync":
            _seed_principal_snapshot()

        # 합성 시장
        P(paper_broker, "_current_price", lambda code, fallback=0.0: market.price(code) or fallback)
        P(paper_broker, "_krx_settled_close", lambda code: 0.0)   # 정규장 중 — 확정 종가 평가 없음
        P(api, "get_chart_data", lambda code, *a, **k: market.chart(code))
        P(api, "get_current_price", lambda code, *a, **k: market.price(code))
        P(api, "get_current_price_data", lambda code, *a, **k: _price_payload(market, code))
        P(api, "prefetch_multiple_current_prices", lambda *a, **k: None)
        P(api, "get_realtime_vol_strength", lambda *a, **k: 135.0)
        P(api, "get_ask_bid_ratio", lambda *a, **k: 1.6)
        P(api, "get_investor_trend", lambda *a, **k: [])
        P(api, "check_server_health", lambda *a, **k: True)
        P(api, "is_holiday_today", lambda *a, **k: False)
        P(api, "is_nxt_tradeable", lambda *a, **k: False)
        P(api, "send_telegram_message", _send_telegram)
        from modules import auto_trade as _pkg_mod
        P(_pkg_mod, "alert_delivered", _alert_delivered)     # 트레이더는 _pkg().alert_delivered 로 부른다
        P(analysis, "get_domestic_index_data", lambda market_name, *a, **k: market.index(market_name))
        P(analysis, "_live_vol_history", Market.kospi_long if scenario != "market_stress"
          else (lambda settled_dd: (None, None, None)))      # market_stress: 장기 이력을 못 받는다
        #  일일 손실 한도 경보는 프로세스 로그 파일(mystock.log)의 꼬리를 붙인다 — 실행 기기의 파일이라 고정한다.
        from modules.auto_trade import engine as _engine_mod
        for mod in (_engine_mod, _trader_mod):
            P(mod, "get_mystock_log_tail", lambda n=20: "<로그 꼬리>\n")
        if scenario == "account_sync":
            from modules import account as _account_mod, holdings_backfill
            P(holdings_backfill, "sync_account", lambda *a, **k: json.loads(json.dumps(ACCOUNT_BACKFILL)))
            P(_account_mod, "fetch_period_realized", lambda *a, **k: {"realized": 0, "cost": 0})   # 장부 대조: 일치
            _asset_status = _account_mod.get_asset_status_data

            def _asset_status_degraded(*a, **k):
                data = _asset_status(*a, **k)
                if data is not None and market.cycle in ACCOUNT_DEGRADED_CYCLES:
                    data = dict(data, degraded=list(data.get("degraded") or []) + ["해외잔고"])
                return data
            P(_account_mod, "get_asset_status_data", _asset_status_degraded)
        analysis.reset_live_vol_regime()
        st.callback(analysis.reset_live_vol_regime)
        #  국면 판정은 프로세스 전역 캐시(TTL 60초)와 판정 불가 경고 표시를 든다 — 앞 실행의 값을 물려받지 않게.
        for cache in (analysis._MARKET_REGIME_CACHE, analysis._REGIME_UNKNOWN_WARNED):
            cache.clear()
            st.callback(cache.clear)

        AutoTrader = _trader_mod.AutoTrader     # 패키지가 재수출하는 것과 같은 클래스
        AutoTrader._instance = None
        t = AutoTrader()
        trader_box["t"] = t
        t.start(interactive=False)
        trace: dict = {"start_logs": [_mask(x) for x in t.logs]}
        if t.is_running:
            t.thread = threading.current_thread()
            _cycle_hook(t, market)
            t._run_loop()
        t.is_running = True                 # 화면은 '실행 중' 상태로 그린다(운용자가 보는 모양)
        trace.update(_collect(t, telegrams, scenario))
        trace["alerts"] = list(alerts)
        #  종료 경로 — 실제 정지(stop)로 잠금을 풀고 정지 알림을 남긴다(다음 재생이 '중복 실행'에 걸리지 않게)
        n_logs, n_tg, n_al = len(t.logs), len(telegrams), len(alerts)
        t.thread = _NoStartThread()         # 루프 스레드가 끝난 상태(실제 정지와 같은 경로)
        t.stop(use_status=False)
        trace["stop"] = {"logs": [_mask(x) for x in t.logs[n_logs:]], "telegram": telegrams[n_tg:],
                         "alerts": alerts[n_al:]}
        t.is_running = False
        AutoTrader._instance = None
    return trace


def _cycle_hook(t, market):
    """주기마다 가격 경로를 한 칸 넘긴다 — _record_cycle_duration 이 주기 끝 표식이다.
    account_sync 는 그 자리에서 다음 주기 전에 일어날 가상 입출금도 넣는다."""
    orig = t._record_cycle_duration

    def _wrapped(*a, **k):
        r = orig(*a, **k)
        market.cycle += 1
        if market.scenario == "account_sync" and market.cycle in ACCOUNT_INTRADAY:
            ok, msg = paper_broker.adjust_seed(ACCOUNT_INTRADAY[market.cycle])
            assert ok, msg
        return r
    t._record_cycle_duration = _wrapped


def _price_payload(market, code):
    p = market.price(code)
    prev = market.prev_close(code) if code in market.daily else p
    rate = (p / prev - 1) * 100 if prev else 0.0
    return {"rt_cd": "0", "output": {"stck_prpr": str(int(p)), "prdy_ctrt": f"{rate:.2f}",
                                      "stck_sdpr": str(int(prev)), "acml_vol": "500000",
                                      "stck_hgpr": str(int(p)), "stck_lwpr": str(int(p))}}


def _seed_holdings(market):
    """기동 전 보유분을 가상 원장과 거래 기록에 심는다(매수일은 days_ago 영업일 전)."""
    for h in SEEDED_HOLDINGS:
        when = pd.bdate_range(end=START, periods=h["days_ago"] + 1)[0].strftime("%Y-%m-%d 10:00:00")
        name = market.items[h["code"]]["name"]
        avg = float(_tick_floor(market.prev_close(h["code"]) * h["rel"]))
        odno = f"SEED{h['code']}"
        with paper_broker._lock:
            assert paper_broker._book_fill("buy", h["code"], name, h["qty"], avg, odno, when) is None
        db_manager.db.insert_trade("buy(AUTO)", h["code"], name, h["qty"], str(avg), odno,
                                   reason="재생 시드", order_status="체결", custom_time=when,
                                   buy_price=avg)


def _seed_asset_history():
    """[market_stress] 기동 전 영업일들의 일일 시작 자산(자산 고점 → 드로다운)."""
    days = pd.bdate_range(end=START.date() - _dt.timedelta(days=1), periods=len(STRESS_ASSET_HISTORY))
    key = paper_broker._account_key()
    for d, v in zip(days, STRESS_ASSET_HISTORY):
        assert db_manager.db.save_daily_asset(d.strftime("%Y-%m-%d"), key, v)


def _seed_principal_snapshot():
    """[account_sync] 어제의 시작 자산과 기준 원금 — 오늘 원금보다 ACCOUNT_OFFLINE_WITHDRAWAL 만큼 많다.

    원금 = 현금 + 매입원가 − 실현손익. 기동 전 보유는 매수만 있어 실현손익이 0 이다.
    """
    principal = paper_broker.get_cash() + sum(p["qty"] * p["avg_price"] for p in paper_broker.get_positions())
    day = pd.bdate_range(end=START.date() - _dt.timedelta(days=1), periods=1)[0].strftime("%Y-%m-%d")
    assert db_manager.db.save_daily_asset(day, paper_broker._account_key(), ACCOUNT_YESTERDAY_ASSET,
                                          principal=int(round(principal)) + ACCOUNT_OFFLINE_WITHDRAWAL)


def _collect(t, telegrams, scenario="base"):
    out = {"logs": [_mask(x) for x in t.logs], "telegram": list(telegrams)}
    out["fills"] = [{k: r.get(k) for k in ("time", "type", "code", "qty", "price", "amount", "fee", "profit_amt")}
                    for r in paper_broker.get_fills()]
    out["positions"] = paper_broker.get_positions()
    out["cash"] = round(paper_broker.get_cash(), 2)
    out["trades"] = [{k: r.get(k) for k in ("time", "type", "code", "qty", "price", "order_status", "reason",
                                            "profit_amt", "stop_loss_rate")}
                     for r in reversed(db_manager.db.get_trades() or [])]
    out["state"] = {k: _jsonable(getattr(t, k, None)) for k in (
        "buy_halted", "buy_halt_reason", "risk_scale", "risk_scale_by_market", "initial_asset",
        "baseline_principal", "current_total_asset", "consecutive_errors", "last_holdings_count",
        "portfolio_heat_amt", "skipped_by_market_filter_count")}
    if scenario == "market_stress":
        out["state"].update({k: _jsonable(getattr(t, k, None)) for k in (
            "risk_scale_reason", "risk_scale_reason_by_market", "risk_scale_regime_by_market",
            "market_status_notified")})
        floor = dict(getattr(t, "_risk_floor", None) or {})
        floor.pop("params", None)       # 설정 서명(repr) — 길고, 설정 기본값이 바뀔 때마다 흔들린다
        out["state"]["risk_floor"] = _jsonable(floor)
    if scenario == "account_sync":
        out["state"].update({k: _jsonable(getattr(t, k, None)) for k in (
            "net_transfer_today", "buy_halt_kind", "_net_transfer_saved", "_pending_transfer_amt",
            "_pending_transfer_count", "_offline_reconcile_date")})
        out["asset_history"] = [list(r) for r in db_manager.db.execute_query(
            "SELECT date, asset, net_transfer, principal FROM daily_asset_history WHERE account = ? ORDER BY date",
            (paper_broker._account_key(),), fetch="all") or []]
    out["screens"] = _screens(t)
    return out


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except TypeError:
        return repr(v)


def _screens(t):
    """화면·보고 출력 — 같은 상태에서 같은 글자가 나와야 한다."""
    screens = {}
    console = config.console
    for name, fn in (("print_status", lambda: t.print_status()),
                     ("print_health", lambda: t.print_health()),
                     ("print_report", lambda: t.print_report()),
                     ("log_current_holdings", lambda: t.log_current_holdings())):
        saved_width = console._width
        console.width = 135
        n_logs = len(t.logs)
        try:
            with console.capture() as cap:
                try:
                    with patch("rich.prompt.Prompt.ask", return_value=""), \
                            patch("core.utils.pause", lambda *a, **k: None):
                        fn()
                except Exception as e:      # noqa: BLE001 - 화면이 죽는 것도 기록 대상이다
                    console.print(f"<{type(e).__name__}: {e}>")
        finally:
            console._width = saved_width
        screens[name] = [_mask(x) for x in cap.get().splitlines()] + [_mask(x) for x in t.logs[n_logs:]]
    for name, fn in (("get_status_message", t.get_status_message), ("get_health_message", t.get_health_message),
                     ("get_performance_report", t.get_performance_report)):
        try:
            screens[name] = [_mask(x) for x in str(fn()).splitlines()]
        except Exception as e:          # noqa: BLE001
            screens[name] = [f"<{type(e).__name__}: {e}>"]
    return screens


def env_signature():
    """기준 기록을 만든 환경 — 비교가 실패했을 때 의존성 판 차이인지 가려 보려고 기록에 함께 남긴다."""
    from importlib.metadata import version
    return {"pandas": pd.__version__, "numpy": ".".join(np.__version__.split(".")[:2]), "rich": version("rich")}


def dump(trace):
    return json.dumps(trace, ensure_ascii=False, indent=1, sort_keys=True, default=str)
