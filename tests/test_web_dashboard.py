"""차트 갤러리 웹 대시보드 — 파일명 파싱·종목명 캐시·인프로세스 서버.

[왜 고정하나 · 2026-08-29] 이 경로는 차트를 한 장 그릴 때마다 돌고, 종전 구현은
PNG 하나당 외부 HTTP 조회를 한 번씩 냈다(캐시 없음). 텔레그램 /chart 는 폴링
스레드에서 이 경로를 타므로 라즈베리파이에서 봇이 그만큼 멈춘다. 조회 횟수 자체가
회귀 대상이라 테스트로 못박는다.
"""
import os
import threading
import urllib.request

import pytest

import config
from modules import web_dashboard as wd


# ==========================================================
# 1. 파일명 파싱 — 규약을 아는 파일만 해석한다
# ==========================================================
@pytest.mark.parametrize("filename,expected", [
    ("analysis_005930_daily.png", ("005930", "일봉")),
    ("analysis_005930_weekly.png", ("005930", "주봉")),
    ("analysis_AAPL_hourly.png", ("AAPL", "시간봉")),
    # 몬테카를로는 종목당 한 장으로 덮어쓴다.
    ("mc_dist_005930.png", ("005930", "몬테카를로")),
    # 타임스탬프가 붙은 옛 파일도 정리되기 전까지는 읽어야 한다.
    #  (split('_') 로는 code 가 'dist' 가 되던 이름이다)
    ("mc_dist_005930_20260829_143000.png", ("005930", "몬테카를로 08/29 14:30")),
    # 규약 밖 — 해석하지 않는다(외부 조회로 이어지면 안 된다).
    ("randomfile.png", (None, None)),
    ("analysis_005930.png", (None, None)),
    ("mc_dist_005930_bogus.png", (None, None)),
])
def test_parse_chart_filename(filename, expected):
    assert wd.parse_chart_filename(filename) == expected


def test_mc_filename_is_not_mistaken_for_an_overseas_ticker():
    """[회귀] 'dist' 를 해외 티커로 읽어 TradingView 에 질의하던 버그."""
    code, subtitle = wd.parse_chart_filename("mc_dist_005930_20260829_143000.png")
    assert code == "005930"
    assert code.isdigit()          # 국내로 판정되어야 한다
    assert "dist" not in (subtitle or "")


# ==========================================================
# 2. 종목명 — 관심종목 우선, 외부 조회는 코드당 최대 1회
# ==========================================================
@pytest.fixture
def universe(monkeypatch):
    monkeypatch.setattr(config.session, "stock_data", {
        "stocks_kr": [{"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}],
        "etfs_kr": [], "stocks_us": [{"name": "Apple Inc.", "code": "AAPL"}], "etfs_us": [],
    }, raising=False)
    wd.clear_name_cache()
    yield
    wd.clear_name_cache()


def test_universe_names_need_no_network(universe, monkeypatch):
    """관심종목에 있는 종목은 외부를 전혀 조회하지 않는다."""
    import api
    calls = []
    monkeypatch.setattr(api, "get_stock_name_by_code",
                        lambda c, o: calls.append(c) or "SHOULD_NOT_BE_USED")
    assert wd.resolve_stock_name("005930", False) == "삼성전자"
    assert wd.resolve_stock_name("AAPL", True) == "Apple Inc."
    assert calls == []


def test_unknown_code_hits_network_once_then_caches(universe, monkeypatch):
    """관심종목 밖이면 한 번만 조회하고, 그 뒤로는 캐시로 답한다(실패도 캐시)."""
    import api
    calls = []
    monkeypatch.setattr(api, "get_stock_name_by_code",
                        lambda c, o: (calls.append(c), None)[1])
    for _ in range(5):
        assert wd.resolve_stock_name("999999", False) is None
    assert len(calls) == 1, f"외부 조회가 {len(calls)}회 발생했다 — 캐시가 듣지 않는다"


def test_index_generation_makes_no_network_call_per_file(universe, monkeypatch, tmp_path):
    """[회귀] 인덱스 1회 생성에 PNG 개수만큼 외부 요청이 나가던 문제."""
    import api
    calls = []
    monkeypatch.setattr(api, "get_stock_name_by_code",
                        lambda c, o: calls.append(c) or "X")
    for name in ("analysis_005930_daily.png", "analysis_005930_weekly.png",
                 "analysis_AAPL_daily.png", "mc_dist_005930_20260829_143000.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n")

    wd.update_chart_index(str(tmp_path))
    assert calls == [], f"외부 조회 {len(calls)}회 — 관심종목/캐시로 해결됐어야 한다"

    html_txt = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "삼성전자 005930" in html_txt
    assert "몬테카를로" in html_txt


def test_index_escapes_names(universe, monkeypatch, tmp_path):
    """따옴표가 든 종목명이 카드의 onclick 을 깨뜨리지 않는다."""
    monkeypatch.setattr(config.session, "stock_data",
                        {"stocks_kr": [{"name": "A\"B'C <s>", "code": "005930"}]}, raising=False)
    wd.clear_name_cache()
    (tmp_path / "analysis_005930_daily.png").write_bytes(b"\x89PNG\r\n")
    wd.update_chart_index(str(tmp_path))
    html_txt = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<s>" not in html_txt
    assert "&lt;s&gt;" in html_txt


def test_index_renders_empty_gallery(tmp_path):
    """차트가 하나도 없어도 디렉터리 목록 대신 안내가 나온다."""
    wd.update_chart_index(str(tmp_path))
    html_txt = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "아직 생성된 차트가 없습니다" in html_txt


# ==========================================================
# 4. 차트 파일은 종목·기간당 한 장 — 다시 그리면 덮어쓴다
# ==========================================================
def test_monte_carlo_chart_overwrites_per_stock(tmp_path, monkeypatch):
    """[회귀] 몬테카를로가 타임스탬프 이름으로 무한히 쌓이던 문제.

    파일이 늘면 chart/ 가 지우기 전까지 줄지 않고, 갤러리 인덱스는 차트를 그릴 때마다
    다시 쓰이므로 인덱스 생성도 함께 무거워진다.

    [왜 렌더링을 흉내내나] 실제 matplotlib 렌더링을 xdist 워커 안에서 돌리면 macOS 에서
    그 워커가 뒤에 실행하는 무관한 테스트에서 크래시했다(CoreText/폰트 적재). 검증
    대상은 파일 이름 규약과 정리 로직이므로 savefig 만 가짜로 둔다 — 실제 렌더링은
    test_server_lifecycle_end_to_end 가 자식 프로세스에서 밟는다.
    """
    import config as cfg
    from modules import chart

    monkeypatch.setattr(cfg, "CHART_DIR", str(tmp_path))
    _stub_matplotlib(monkeypatch, chart, tmp_path)

    # 옛 규약으로 쌓여 있던 같은 종목 파일 + 건드리면 안 되는 이웃들
    (tmp_path / "mc_dist_005930_20260828_090000.png").write_bytes(b"old1")
    (tmp_path / "mc_dist_005930_20260829_100000.png").write_bytes(b"old2")
    (tmp_path / "mc_dist_000660_20260829_100000.png").write_bytes(b"other")
    (tmp_path / "analysis_005930_daily.png").write_bytes(b"analysis")

    chart.generate_monte_carlo_histogram([1.0, 2.0, -1.0], "삼성전자", "005930", open_file=False)

    names = sorted(f.name for f in tmp_path.glob("*.png"))
    assert "mc_dist_005930.png" in names, f"새 규약 파일이 없다: {names}"
    assert not [n for n in names if n.startswith("mc_dist_005930_")], \
        f"같은 종목의 옛 파일이 남았다: {names}"
    # 다른 종목과 분석 차트는 건드리지 않는다.
    assert "mc_dist_000660_20260829_100000.png" in names
    assert "analysis_005930_daily.png" in names


def test_monte_carlo_rerun_does_not_accumulate(tmp_path, monkeypatch):
    """같은 종목을 여러 번 돌려도 파일은 한 장이다."""
    import config as cfg
    from modules import chart

    monkeypatch.setattr(cfg, "CHART_DIR", str(tmp_path))
    _stub_matplotlib(monkeypatch, chart, tmp_path)
    for _ in range(3):
        chart.generate_monte_carlo_histogram([1.0, 2.0, -1.0], "삼성전자", "005930", open_file=False)

    assert sorted(f.name for f in tmp_path.glob("mc_dist_*.png")) == ["mc_dist_005930.png"]


def _stub_matplotlib(monkeypatch, chart, tmp_path):
    """savefig 만 '파일을 만드는 가짜'로 바꾼다 — 실제 렌더링은 하지 않는다."""
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.savefig.side_effect = lambda path, **kw: open(path, "wb").write(b"PNG")
    fake.get_fignums.return_value = []
    # plt.hist 는 (n, bins, patches) 3-튜플을 언패킹해서 받는다.
    fake.hist.return_value = ([1], [0, 1], [])
    monkeypatch.setattr(chart, "plt", fake)
    monkeypatch.setattr(chart, "np", __import__("numpy"))
    monkeypatch.setattr(chart, "_matplotlib_ready", True)
    monkeypatch.setattr(chart, "setup_korean_font", lambda: None)


# ==========================================================
# 5. 웹서버 — 실소켓 검증은 **자식 프로세스**에서 한다
# ==========================================================
#  [왜 자식 프로세스인가 · 2026-08-29] 실제 리스닝 소켓을 xdist 워커 안에서 띄우면
#   macOS 에서 그 워커가 뒤에 실행하는 무관한 테스트에서 크래시했다
#   (worker 'gwN' crashed → 세션 전체가 중단되어 절반만 돌았다). 제품 쪽 문제는
#   아니지만(아래 항목들은 자식에서 전부 통과한다) 스위트를 통째로 흔들므로 격리한다.
#   덕분에 bind → serve → GET → shutdown 전 구간을 진짜로 밟으면서도 워커는 깨끗하다.
_SERVER_PROBE = r"""
import sys, os, time, threading, urllib.request, tempfile
sys.path.insert(0, PROJECT_ROOT)
import config
from modules import web_dashboard as wd, chart

tmp = tempfile.mkdtemp()
config.CHART_DIR = tmp
config.WEBCHART_ACTIVE = True
wd.update_chart_index(tmp)

# ── ① 기동 → 실제 HTTP 응답 (os.chdir 를 하지 않는다)
cwd_before = os.getcwd()
assert wd.start_web_server(port=0, host="127.0.0.1"), "기동 실패"
port = wd._server.server_address[1]
body = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5).read().decode()
assert "Chart Dashboard" in body, "갤러리가 아니다"
assert os.getcwd() == cwd_before, "os.chdir 가 일어났다"

# ── ② 중복 start 는 무해하고 포트를 바꾸지 않는다
assert wd.start_web_server(port=0, host="127.0.0.1")
assert wd._server.server_address[1] == port, "중복 start 가 서버를 갈아치웠다"

# ── ③ 차트를 그리는 내내 서버가 유지된다 (종전에는 렌더링 전에 죽였다)
for _ in range(3):
    chart.generate_monte_carlo_histogram([1.0, 2.0, -1.0, 0.5] * 10,
                                         "삼성전자", "005930", open_file=False)
    r = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5)
    assert r.status == 200, "차트 생성 중에 서버가 내려갔다"
assert wd.is_web_server_running()

wd.stop_web_server()
assert not wd.is_web_server_running()

# ── ④ start 직후 stop 이 막히지 않는다
#     CPython: shutdown() 은 serve_forever 가 루프에 들어가 있어야 돌아온다.
#     진입 전에 부르면 영영 셋되지 않는 이벤트를 기다린다(실측 재현됨).
worst = 0.0
for _ in range(15):
    assert wd.start_web_server(port=0, host="127.0.0.1")
    began = time.monotonic()
    wd.stop_web_server()
    worst = max(worst, time.monotonic() - began)
assert worst < 2.0, "stop 이 %.1fs 걸렸다 — 종료 경로가 막힐 수 있다" % worst

# ── ⑤ 포트가 이미 잡혀 있으면 예외가 아니라 False (기동은 계속돼야 한다)
import socket
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(1)
busy = sock.getsockname()[1]
try:
    assert wd.start_web_server(port=busy, host="127.0.0.1") is False, "충돌을 알리지 않았다"
    assert not wd.is_web_server_running()
finally:
    sock.close()
    wd.stop_web_server()

# ── ⑥ 서버 스레드가 남지 않는다
alive = [t.name for t in threading.enumerate() if t.name == "webchart-server" and t.is_alive()]
assert not alive, "서버 스레드가 남았다: %s" % alive

# ── ⑦ 렌더 버퍼가 쌓이지 않는다 (1GB 파이의 OOM 원인)
for _ in range(5):
    chart.generate_monte_carlo_histogram([1.0, 2.0, -1.0, 0.5] * 20,
                                         "삼성전자", "005930", open_file=False)
assert not chart.plt.get_fignums(), "닫히지 않은 Figure 가 남았다"

print("PROBE_OK")
"""


def test_server_lifecycle_end_to_end():
    """bind → serve → GET → shutdown 전 구간을 자식 프로세스에서 실제로 밟는다."""
    import pathlib
    import subprocess
    import sys as _sys

    root = str(pathlib.Path(__file__).resolve().parent.parent)
    script = _SERVER_PROBE.replace("PROJECT_ROOT", repr(root))
    proc = subprocess.run([_sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=180, cwd=root)
    assert "PROBE_OK" in proc.stdout, (
        f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout[-3000:]}"
        f"\n--- stderr ---\n{proc.stderr[-3000:]}")


# ==========================================================
# 6. 서버를 띄우지 않고 확인할 수 있는 것들
# ==========================================================
def test_start_reports_failure_without_binding(monkeypatch):
    """기동 실패는 예외가 아니라 False 로 알린다 — 소켓을 잡지 않고 확인한다."""
    def boom(*a, **kw):
        raise OSError(48, "Address already in use")
    monkeypatch.setattr(wd, "ThreadingHTTPServer", boom)
    assert wd.start_web_server(port=1, host="127.0.0.1") is False
    assert not wd.is_web_server_running()


def test_web_server_url_is_reachable_text(monkeypatch):
    """0.0.0.0 바인딩을 그대로 안내하면 붙을 수 없으므로 바꿔 적는다."""
    monkeypatch.setattr(config, "WEBCHART_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "WEBCHART_PORT", 9095)
    assert "0.0.0.0" not in wd.web_server_url()
    assert "9095" in wd.web_server_url()

    monkeypatch.setattr(config, "WEBCHART_HOST", "127.0.0.1")
    assert wd.web_server_url() == "http://127.0.0.1:9095/"


def test_default_port_is_not_browser_blocked():
    """기본 포트가 브라우저 차단 목록에 걸리면 기능 자체가 열리지 않는다."""
    blocked = {1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
               87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
               138, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531,
               532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720,
               1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
               6697, 10080}
    assert int(config.WEBCHART_PORT) not in blocked


def test_chart_generation_has_no_pause_decorator():
    """[회귀] 차트를 그리는 동안 웹서버를 죽이던 데코레이터가 되살아나지 않게 한다.

    회수 대상이던 별도 인터프리터가 사라졌으므로 얻을 것은 없고, 갤러리 접속이 끊기고
    락 없는 스레드 경합이 생기는 손해만 남는다. (서버가 실제로 유지되는지는 위
    test_server_lifecycle_end_to_end ③ 에서 확인한다)
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "modules" / "chart.py").read_text(encoding="utf-8")
    assert "@pause_web_server_while_running" not in src
    assert "def pause_web_server_while_running" not in src


def test_chart_dpi_is_configurable_and_defaults_to_300():
    """파이에서 코드를 고치지 않고 렌더 메모리 봉우리를 낮출 수 있어야 한다."""
    import inspect
    from modules import chart
    assert inspect.signature(chart.generate_visual_chart).parameters['dpi'].default is None
    assert int(config.CHART_DPI) == 300, "기본값은 종전과 같아야 한다(조용히 화질을 낮추지 않는다)"
