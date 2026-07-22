"""내부자 순증감 요약: 매매 외 사유(신규·재보고 전량 기재 / 회사 일괄 지급) 제외 검증.

DART elestock.json은 변동사유를 제공하지 않아 다음 두 패턴으로 매매 외 보고를 걸러낸다.
  1) 신규·재보고 시 보유 전량이 증감 칸에 그대로 들어옴 → 보유수량 차분으로 복원
  2) 우리사주·스톡그랜트 등 일괄 지급 → 같은 날 다수 보고자가 같은 방향
"""
from unittest.mock import patch

from modules.manage import insider


def _row(code="005930", name="삼성전자", dt="20260601", who="홍길동",
         qty=None, chg=None, no=None, baseline=False):
    return {
        "rcept_no": no or (dt + "000001"), "rcept_dt": dt, "repror": who,
        "ofcps": "상무", "main_shrholdr": "", "qty": qty, "chg": chg,
        "rate": None, "rate_chg": None, "baseline": baseline,
        "code": code, "name": name,
    }


# --------------------------------------------------------------------------
# 1) 보유수량 차분 (_apply_real_chg)
# --------------------------------------------------------------------------
def test_rereport_full_quantity_is_not_counted_as_acquisition():
    """재보고로 보유 전량이 증감에 기재되면 실제 변동분만 잡아야 한다.

    실측: 국민연금공단 한미약품 06-01 '증감 +1,281,813'은 보유 전량 재기재이고
    직전 보고(1,269,470) 대비 실제 변동은 +12,343이다.
    """
    rows = [
        _row(dt="20260522", who="국민연금공단", qty=1269470, chg=-137654),
        _row(dt="20260601", who="국민연금공단", qty=1281813, chg=1281813),
    ]
    insider._apply_real_chg(rows)
    assert rows[0]["real_chg"] == -137654       # 직전 보고 없음 → 보고값 사용
    assert rows[1]["real_chg"] == 12343         # 전량 기재 대신 차분


def test_full_period_net_flips_sign_after_correction():
    """한미약품 90일 순증감은 보고값 합(+2,458,181)이 아니라 차분 합(-88,036)이다."""
    hist = [(("20260402"), 1407124, 107890), ("20260522", 1269470, -137654),
            ("20260601", 1281813, 1281813), ("20260605", 1276747, -5066),
            ("20260610", 1295586, 1295586), ("20260702", 1319088, 23502)]
    rows = [_row(code="128940", name="한미약품", dt=d, who="국민연금공단",
                 qty=q, chg=c, baseline=(d == "20260402")) for d, q, c in hist]
    insider._apply_real_chg(rows)
    naive = sum(r["chg"] for r in rows if not r["baseline"])
    real = sum(r["real_chg"] for r in rows if not r["baseline"])
    assert naive == 2458181                      # 기존(잘못된) 집계
    assert real == -88036                        # 차분 기준 실제 순증감
    assert naive > 0 and real < 0                # 신호가 정반대로 뒤집힌다


def test_first_ever_report_with_full_quantity_is_zeroed():
    """직전 보고가 없고 증감 == 보유 전량이면 매매로 보지 않는다."""
    rows = [_row(dt="20260601", who="국민연금공단", qty=2212945, chg=2212945)]
    insider._apply_real_chg(rows)
    assert rows[0]["real_chg"] == 0


def test_genuine_change_is_preserved():
    """전량 기재가 아닌 일반 보고는 보고값을 그대로 쓴다."""
    rows = [_row(dt="20260601", qty=5000, chg=-1200)]
    insider._apply_real_chg(rows)
    assert rows[0]["real_chg"] == -1200


def test_reporters_are_tracked_independently():
    """보고자가 다르면 서로의 보유수량으로 차분하면 안 된다."""
    rows = [_row(dt="20260601", who="A", qty=1000, chg=1000),
            _row(dt="20260602", who="B", qty=9000, chg=500)]
    insider._apply_real_chg(rows)
    assert rows[0]["real_chg"] == 0              # A의 최초 전량 기재
    assert rows[1]["real_chg"] == 500            # B는 A와 무관


def test_same_reporter_different_stock_is_separate():
    """같은 보고자라도 종목이 다르면 별개 시계열이다."""
    rows = [_row(code="005930", dt="20260601", who="국민연금공단", qty=1000, chg=1000),
            _row(code="000660", dt="20260602", who="국민연금공단", qty=8000, chg=8000)]
    insider._apply_real_chg(rows)
    assert rows[1]["real_chg"] == 0              # 005930의 1,000주로 차분하지 않는다


# --------------------------------------------------------------------------
# 2) 회사 일괄 이벤트 (_bulk_event_keys)
# --------------------------------------------------------------------------
def test_bulk_grant_is_detected():
    """다수 임원이 같은 날 같은 방향으로 보고하면 일괄 지급이다."""
    rows = [_row(dt="20260721", who=f"임원{i}", qty=100 * i, chg=100)
            for i in range(1, 9)]
    insider._apply_real_chg(rows)
    assert ("005930", "20260721") in insider._bulk_event_keys(rows)


def test_mixed_direction_group_is_real_trading():
    """방향이 갈리면 개별 매매다 (실측: SK하이닉스 2026-06-09 3증가/2감소)."""
    chgs = [304, 31, 2205, -419, -1000]
    rows = [_row(code="000660", dt="20260609", who=f"임원{i}", qty=10000, chg=c)
            for i, c in enumerate(chgs)]
    insider._apply_real_chg(rows)
    assert ("000660", "20260609") not in insider._bulk_event_keys(rows)


def test_small_group_is_not_bulk():
    """보고자 수가 임계 미만이면 일괄로 보지 않는다 (과잉 제외 방지)."""
    rows = [_row(dt="20260601", who=f"임원{i}", qty=1000, chg=100)
            for i in range(insider._BULK_MIN_REPORTERS - 1)]
    insider._apply_real_chg(rows)
    assert insider._bulk_event_keys(rows) == set()


def test_bulk_detection_is_per_date():
    """다른 날짜 보고가 합산되어 일괄로 오판되면 안 된다."""
    rows = [_row(dt=f"2026060{i}", who=f"임원{i}", qty=1000, chg=100)
            for i in range(1, 9)]
    insider._apply_real_chg(rows)
    assert insider._bulk_event_keys(rows) == set()


def test_baseline_rows_excluded_from_bulk_detection():
    """기준선 행은 기간 밖 보고라 일괄 판정에 끼면 안 된다."""
    rows = [_row(dt="20260601", who=f"임원{i}", qty=1000, chg=100, baseline=True)
            for i in range(8)]
    insider._apply_real_chg(rows)
    assert insider._bulk_event_keys(rows) == set()


# --------------------------------------------------------------------------
# 3) 렌더링 (요약표 · 상세표)
# --------------------------------------------------------------------------
def _render(fn, rows, **kw):
    insider._apply_real_chg(rows)
    printed = []
    with patch.object(insider.config.console, "print", side_effect=lambda *a, **k: printed.append(a)):
        fn(rows, **kw)
    return printed


def test_summary_has_last_report_date_column():
    """요약표에 '최근 보고일'이 있어야 신호의 신선도를 판단할 수 있다."""
    rows = [_row(dt="20260601", qty=1000, chg=-100),
            _row(dt="20260715", qty=900, chg=-100, no="20260715000001")]
    printed = _render(insider._render_summary, rows)
    table = next(a[0] for a in printed if hasattr(a[0], "columns"))
    headers = [c.header for c in table.columns]
    assert "최근 보고일" in headers
    cells = [c._cells for c in table.columns]
    assert any("2026-07-15" in str(x) for x in cells[headers.index("최근 보고일")])


def test_summary_excludes_bulk_events():
    """일괄 지급은 요약 순증감에서 빠져야 한다."""
    bulk = [_row(dt="20260721", who=f"임원{i}", qty=1000, chg=100, no=f"2026072100000{i}")
            for i in range(8)]
    trade = [_row(dt="20260710", who="대표이사", qty=5000, chg=-3000)]
    printed = _render(insider._render_summary, bulk + trade)
    table = next(a[0] for a in printed if hasattr(a[0], "columns"))
    headers = [c.header for c in table.columns]
    net = str(table.columns[headers.index("순증감(주)")]._cells[0])
    assert "-3,000" in net                       # 일괄 +800이 섞이지 않았다
    assert "순처분" in str(table.columns[headers.index("신호")]._cells[0])


def test_detail_table_drops_bulk_rows():
    """상세표에서도 일괄 지급을 빼야 최신순 목록이 덮이지 않는다."""
    bulk = [_row(dt="20260721", who=f"임원{i}", qty=1000, chg=100, no=f"2026072100000{i}")
            for i in range(8)]
    trade = [_row(dt="20260710", who="대표이사", qty=5000, chg=-3000)]
    printed = _render(insider._render_insiders, bulk + trade)
    table = next(a[0] for a in printed if hasattr(a[0], "columns"))
    headers = [c.header for c in table.columns]
    reporters = [str(x) for x in table.columns[headers.index("보고자")]._cells]
    assert reporters == ["대표이사"]


def test_summary_skipped_when_only_bulk_rows():
    """전부 일괄 지급이면 요약표를 그리지 않는다(빈 표 방지)."""
    rows = [_row(dt="20260721", who=f"임원{i}", qty=1000, chg=100, no=f"2026072100000{i}")
            for i in range(8)]
    printed = _render(insider._render_summary, rows)
    assert not any(hasattr(a[0], "columns") for a in printed if a)
