"""첫 주기의 계좌 감시가 자산 집계 결손인 채로 오늘의 기준선을 박지 않는가 (2026-10-25).

[배경] 오늘 시작 자산(initial_asset)은 일일 손실 한도의 분모이자 사이징 기준이고, 한 번 박히면
 그날 되돌리지 않는다. 그래서 자산 집계가 결손(degraded)을 알리면 기동(initialize)과 날짜 변경
 갱신은 기준선을 박지 않는다(2026-09-08 — '모르면 박지 않는다', account.get_asset_status_data 의
 degraded 주석이 같은 계약을 적어 두었다).

[무엇이 틀려 있었나] 기동이 시작 자산을 보류한 날, 첫 주기의 계좌 감시(_monitor_account_status)는
 결손이 그대로인 채 대안 계산값(예수금 + 국내 보유 평가)을 기준선으로 박았다. 대안 계산에는 해외
 주식 평가액이 없다. 해외 보유가 있는 계좌면 기준선이 그만큼 낮게 박히고, 집계가 돌아오면 그 차이가
 '이익'으로 보여 손실 한도가 그만큼 늦게 걸린다. 기동이 보류한 결정을 한 주기 뒤에 뒤집은 것이라
 2026-09-01 의 반토막 검사 누락(같은 함수의 다음 분기 주석)과 같은 모양이다.

보류를 넣으면서 드러난 구멍도 함께 막는다. 시작 자산을 못 박은 주기에도 기준 원금은 먼저 잡고 오프라인 대조를
끝냈다가, 시작 자산이 잡히는 주기에 원금을 그 시점으로 다시 잡았다. 그 사이의 입출금은 장중 감지에도 오프라인
대조(하루 1회)에도 걸리지 않고 사라진다(주기 재생 account_sync 에서 150만 출금이 그렇게 사라졌다). 이제 두 기준은
같은 스냅샷에서 함께 잡고, 그 전의 입출금은 그때 오프라인 대조가 되찾는다. 급감 거부(2026-09-01) 분기도 같다.

같은 자리의 로그는 '이전 자산으로 대체'라고 적었는데, 대체값은 이전 주기 자산이 아니라 대안 계산이다.
"""
from unittest.mock import patch

import pytest

import config
from modules.auto_trade import AutoTrader

CASH = 9_000_000
EVAL = 1_000_000              # 국내 보유 평가
OVERSEAS = 500_000            # 해외 주식 평가 — 대안 계산에는 없다
CLEAN = CASH + EVAL + OVERSEAS
FALLBACK = CASH + EVAL

HOLDINGS = [{'pdno': '005930', 'prdt_name': '삼성전자', 'hldg_qty': '10', 'ord_psbl_qty': '10',
             'pchs_avg_pric': '100000', 'prpr': '100000', 'pchs_amt': '1000000',
             'evlu_amt': str(EVAL), 'evlu_pfls_amt': '0', 'evlu_pfls_rt': '0.00'}]
SUMMARY = [{'dnca_tot_amt': str(CASH), 'prvs_rcdl_excc_amt': str(CASH),
            'scts_evlu_amt': str(EVAL), 'tot_evlu_amt': str(CASH + EVAL)}]
DEPOSIT = {'deposit': CASH, 'd2_deposit': CASH, 'd2_real': CASH,
           'foreign_deposit': 0, 'order_possible': CASH}


@pytest.fixture
def trader(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.initial_asset = 0             # 기동이 시작 자산을 보류한 상태
    t.baseline_principal = 0
    t.buy_halted = False
    t.net_transfer_today = 0
    RECON.clear()
    yield t
    t.is_running = False
    AutoTrader._instance = None


def _asset(degraded=(), sec_eval_zero=False, cash=CASH):
    if cash != CASH:
        return {'tot_asset': cash + EVAL + OVERSEAS, 'sec_eval': EVAL + OVERSEAS, 'order_possible': cash,
                'degraded': []}
    if degraded:
        return {'tot_asset': FALLBACK, 'sec_eval': EVAL, 'order_possible': CASH,
                'degraded': list(degraded)}
    if sec_eval_zero:
        return {'tot_asset': CASH, 'sec_eval': 0, 'order_possible': CASH, 'degraded': []}
    return {'tot_asset': CLEAN, 'sec_eval': EVAL + OVERSEAS, 'order_possible': CASH, 'degraded': []}


RECON = []      # _reconcile_offline_transfer 에 넘어간 원금(호출마다)


def _cycle(trader, asset, saved=0, delivered=True, plausible=True, cash=CASH):
    """한 주기. 기록한 것(로그·경보·기준선 저장)을 돌려준다. 오프라인 대조 호출은 RECON 에 남는다."""
    logs, alerts, saves = [], [], []
    deposit = dict(DEPOSIT, deposit=cash, d2_deposit=cash, d2_real=cash, order_possible=cash)
    with patch('modules.auto_trade.account.get_asset_status_data', return_value=asset), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.save_daily_asset', return_value=True), \
         patch('modules.auto_trade.account_sync.load_daily_initial_asset', return_value=saved), \
         patch('modules.auto_trade.account_sync.is_plausible_baseline', return_value=plausible), \
         patch('modules.auto_trade.account_sync.save_daily_initial_asset',
               side_effect=lambda acc, v, principal=None: (saves.append(v), True)[1]), \
         patch('modules.auto_trade.alert_delivered',
               side_effect=lambda m, urgent=False: (alerts.append(m), delivered)[1]), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch.object(trader, '_reconcile_offline_transfer',
                      side_effect=lambda acc, principal, ok: (RECON.append(principal), 0)[1]), \
         patch.object(trader, '_run_account_circuit_breaker'), \
         patch.object(trader, '_refine_trade_records', side_effect=lambda x: x), \
         patch.object(trader, 'log', side_effect=lambda m, *a, **k: logs.append(str(m))):
        trader._monitor_account_status(HOLDINGS, SUMMARY, deposit)
    return logs, alerts, saves


# ── 1. 결손이면 박지 않고, 풀리면 온전한 값으로 잡는다 ──────────────────────────────

def test_결손이면_첫_주기도_기준선을_박지_않는다(trader):
    _, alerts, saves = _cycle(trader, _asset(degraded=["해외잔고"]))
    assert trader.initial_asset == 0, f"결손인 대안 계산값 {trader.initial_asset:,}원이 기준선이 됐다"
    assert all(v == 0 for v in saves), f"결손 값을 기준선으로 저장했다: {saves}"
    assert len(alerts) == 1 and "[시작 자산 보류]" in alerts[0], alerts
    assert "해외 주식 평가 제외" in alerts[0] and "결손: 해외잔고" in alerts[0], alerts[0]


def test_결손이_풀린_주기에_온전한_값으로_잡는다(trader):
    _cycle(trader, _asset(degraded=["해외잔고"]))
    _, _, saves = _cycle(trader, _asset())
    assert trader.initial_asset == CLEAN, (
        f"기준선이 {trader.initial_asset:,}원 — 해외 평가가 든 {CLEAN:,}원이어야 한다")
    assert CLEAN in saves


def test_주식_평가액_0_수신도_같은_보류다(trader):
    """결손 표식이 없어도 '보유가 있는데 집계의 주식 평가가 0'이면 같은 대안 계산으로 빠진다."""
    _, alerts, _ = _cycle(trader, _asset(sec_eval_zero=True))
    assert trader.initial_asset == 0
    assert alerts and "주식 평가액 0 수신" in alerts[0], alerts


def test_저장된_기준선은_결손이어도_그대로_쓴다(trader):
    """보류는 '새로 재는 값'에만 건다 — 같은 날 앞서 온전하게 잰 기록을 막으면 안 된다."""
    _, alerts, _ = _cycle(trader, _asset(degraded=["해외잔고"]), saved=10_400_000)
    assert trader.initial_asset == 10_400_000
    assert not alerts


def test_이미_기준선이_있으면_결손_주기는_건드리지_않는다(trader):
    """대조군 — 장중 결손 주기는 종전대로 대안 계산으로 보고만 하고 기준선은 그대로다."""
    trader.initial_asset = CLEAN
    _, alerts, saves = _cycle(trader, _asset(degraded=["해외잔고"]))
    assert trader.initial_asset == CLEAN and not alerts and not [v for v in saves if v != CLEAN]


# ── 2. 두 기준(시작 자산·기준 원금)은 같은 스냅샷에서 함께 잡는다 ─────────────────────

PRINCIPAL = CASH + 1_000_000          # 현금 + 국내 매입원가(10주 × 10만) − 실현손익(0)


@pytest.mark.parametrize("why", ["결손", "급감"])
def test_보류한_주기에는_기준_원금도_오프라인_대조도_미룬다(trader, why):
    if why == "결손":
        _cycle(trader, _asset(degraded=["해외잔고"]))
    else:
        _cycle(trader, _asset(), plausible=False)
    assert trader.initial_asset == 0
    assert trader.baseline_principal == 0, "시작 자산 없이 기준 원금만 먼저 잡았다"
    assert RECON == [], "시작 자산을 잡기 전에 오프라인 대조를 끝냈다 — 그 사이 입출금이 사라진다"
    assert trader.net_transfer_today == 0, f"기준 원금 없이 순입출금을 쟀다: {trader.net_transfer_today:,}"


def test_보류_사이의_출금은_시작_자산이_잡히는_주기의_대조로_넘어간다(trader):
    """출금이 보류 주기와 첫 온전한 주기 사이에 일어나도, 그 주기의 오프라인 대조가 그 시점 원금으로 돈다."""
    _cycle(trader, _asset(degraded=["해외잔고"]))
    out = 1_500_000
    _cycle(trader, _asset(cash=CASH - out), cash=CASH - out)
    assert trader.initial_asset == CLEAN - out
    assert RECON == [PRINCIPAL - out], RECON
    assert trader.baseline_principal == PRINCIPAL - out
    assert trader.net_transfer_today == 0       # 그 출금은 오프라인 대조 몫이다 — 장중 순입출금으로 또 세지 않는다


def test_기준을_잡은_뒤의_출금은_종전대로_장중_순입출금이다(trader):
    """대조군 — 같은 날 기준을 잡은 뒤의 출금은 장중 감지가 맡는다."""
    _cycle(trader, _asset())
    out = 1_500_000
    _cycle(trader, _asset(cash=CASH - out), cash=CASH - out)
    assert RECON == [PRINCIPAL] and trader.baseline_principal == PRINCIPAL
    assert trader.net_transfer_today == -out


# ── 3. 경보는 하루 한 번, 닿은 것을 확인한 뒤에 스로틀을 찍는다 ──────────────────────

def test_같은_날_두_번_알리지_않는다(trader):
    _, first, _ = _cycle(trader, _asset(degraded=["해외잔고"]))
    _, second, _ = _cycle(trader, _asset(degraded=["해외잔고"]))
    assert len(first) == 1 and not second, (first, second)


def test_못_닿은_경보는_다음_주기에_다시_보낸다(trader):
    _, first, _ = _cycle(trader, _asset(degraded=["해외잔고"]), delivered=False)
    _, second, _ = _cycle(trader, _asset(degraded=["해외잔고"]))
    assert first and second, "전달이 안 된 경보를 보낸 것으로 쳤다 — 차단기가 꺼진 사실을 모른다"


def test_기동이_이미_알렸으면_다시_보내지_않는다(trader):
    """기동(initialize)의 '[시작 자산 보류]'가 같은 사실을 이미 알렸다 — 첫 주기가 또 보내면 잡음이다."""
    from datetime import datetime
    trader._baseline_defer_date = datetime.now().strftime("%Y-%m-%d")
    _, alerts, _ = _cycle(trader, _asset(degraded=["해외잔고"]))
    assert trader.initial_asset == 0 and not alerts


def test_기동의_보류_경보가_첫_주기_스로틀을_찍는다():
    """위 테스트의 전제 — 기동 쪽이 실제로 그 표식을 남긴다."""
    import inspect
    src = inspect.getsource(AutoTrader.initialize)
    i = src.index("[시작 자산 보류]")
    assert "self._baseline_defer_date =" in src[i:i + 900]


# ── 4. 로그 문구 ───────────────────────────────────────────────────────────────

def test_대체_문구는_대안_계산을_말하고_한_줄이다(trader):
    trader.initial_asset = CLEAN
    logs, _, _ = _cycle(trader, _asset(degraded=["해외잔고"]))
    lines = [m for m in logs if "대안 계산" in m]
    assert len(lines) == 1, f"같은 사건이 여러 줄로 찍힌다: {lines}"
    line = lines[0]
    assert "이전 자산" not in line, line
    assert f"예수금 {CASH:,}원" in line and f"국내 보유 평가 {EVAL:,}원" in line, line
    assert f"= {FALLBACK:,}원" in line and "해외 주식 평가 제외" in line, line
    assert "결손: 해외잔고" in line, line


def test_주식_평가액_0_수신의_대체_문구(trader):
    trader.initial_asset = CLEAN
    logs, _, _ = _cycle(trader, _asset(sec_eval_zero=True))
    lines = [m for m in logs if "대안 계산" in m]
    assert len(lines) == 1 and "주식 평가액 0 수신" in lines[0] and "이전 자산" not in lines[0], lines
