"""자산 집계 결손(degraded)을 **사람이 보는 화면**도 말해야 한다.

[무엇이 있었나 · 2026-09-08] account.get_asset_status_data 는 네 구간(금일손익·국내잔고·
 해외잔고·예수금)을 각자 try 로 감싸고, 한 구간이 실패해도 예외 없이 **총액을 줄이기만**
 한다. 2026-09-06에 그 사실을 담을 자리(summary_data['degraded'])를 만들고 자동매매가
 그것을 읽어 기준선 갱신을 미루게 했다. 그런데 같은 값을 쓰는 화면 셋 —
 콘솔 자산 현황표(메뉴 4)·텔레그램 /asset·텔레그램 /profit — 은 그 표식을 버리고
 숫자만 찍었다.

[왜 위험한가] 실측(계좌 주식비중 36%, 국내 잔고 조회만 실패):
    정상          총자산 10,000,000  (주식 3,600,000 + 현금 6,400,000)
    국내잔고 실패  총자산  6,400,000  (주식 0 + 현금 6,400,000)
 /profit 은 이 값을 '총 계좌 자산 증감'의 분자로 쓴다 — 조회 한 번 실패가 -36% 손실로
 보고된다. 사람이 그 숫자를 보고 내리는 결정(중단·증액·출금)은 되돌리기 어렵다.
 방어 장치가 있는데 그것을 읽는 눈이 없으면 방어가 아니다.
"""
import inspect
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(fn):
    return inspect.getsource(fn)


def test_집계기가_결손_구간_이름을_들고_있다():
    """아래 화면 가드들의 전제 — 이 값이 사라지면 가드가 빈 것을 검사하게 된다."""
    from modules import account
    src = _src(account.get_asset_status_data)
    assert '"degraded": []' in src
    #  네 구간이 각자 실패를 적는가(한 구간이라도 조용하면 그만큼 총액이 조용히 준다)
    for part in ("금일손익", "국내잔고", "해외잔고", "예수금"):
        assert f"'{part}'" in src or f'"{part}"' in src, f"{part} 구간이 결손을 적지 않는다"


def test_콘솔_자산표가_결손을_말한다():
    from modules import account
    src = _src(account._display_asset_status)
    assert "degraded" in src, "콘솔 자산 현황표가 결손 표식을 아예 보지 않는다"
    #  표를 찍기만 하고 끝나면 안 된다 — 결손일 때 화면에 글자가 나가야 한다.
    assert "집계하지 못한 구간" in src


def test_텔레그램_잔고조회가_결손을_말한다():
    from modules.telegram_bot import TelegramCommander
    src = _src(TelegramCommander._cmd_balance)
    assert "degraded" in src, "/asset 이 결손 표식을 버린다"
    assert "집계하지 못한 구간" in src


def test_텔레그램_profit_이_자산_증감_옆에_결손을_말한다():
    """여기서는 값이 비율의 분자가 된다 — 가장 크게 오독되는 자리다."""
    from modules.telegram_bot import TelegramCommander
    src = _src(TelegramCommander._cmd_profit)
    assert "asset_degraded" in src, "/profit 이 결손 표식을 버린다"
    assert "자산 집계 일부 실패" in src
    #  경고가 자산 증감 줄 뒤에 붙어야 그 숫자와 함께 읽힌다.
    warn_at = src.index("자산 집계 일부 실패")
    line_at = src.index("총 계좌 자산 증감")
    assert line_at < warn_at, "경고가 자산 증감 줄보다 앞이라 어느 숫자 얘기인지 흐려진다"


def test_결손이_없으면_아무_말도_덧붙이지_않는다():
    """온전한 값에 경고가 붙으면 경고가 배경음이 되어 아무도 안 읽는다."""
    from modules import account
    from modules.telegram_bot import TelegramCommander

    targets = [(account._display_asset_status, "집계하지 못한 구간"),
               (TelegramCommander._cmd_balance, "집계하지 못한 구간"),
               (TelegramCommander._cmd_profit, "자산 집계 일부 실패")]
    for fn, marker in targets:
        lines = _src(fn).splitlines()
        idx = next(i for i, ln in enumerate(lines) if marker in ln)
        #  문구 위쪽에서 가장 가까운 if 문이 결손 목록을 보고 있어야 한다.
        guard = next((ln for ln in reversed(lines[:idx]) if ln.strip().startswith("if ")), "")
        assert "_deg" in guard or "degraded" in guard, (
            f"{fn.__qualname__}: 결손 문구가 결손 여부와 무관하게 나간다 (가장 가까운 조건: {guard!r})")
