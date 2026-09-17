"""갤러리 카드 드래그 정렬(브라우저 기억)과 ✕ 삭제(서버 DELETE) — 2026-09-17.

[정렬] 서버는 종전처럼 최신순으로 내보낸다(기본은 시간순). 운용자가 끌어 놓은 순서는 파일명
 배열로 그 브라우저의 localStorage 에 남고, 페이지가 다시 만들어져도 그 순서로 세운다.
 새로 그린 차트(저장 목록에 없음)는 맨 앞에 최신순으로 둔다. 서버는 관여하지 않는다.
[삭제] 인증 없는 서버가 파일을 지우는 유일한 경로다. 그래서 chart/ **바로 아래** *.png 하나만
 받고, 파일명 검사와 realpath 검사 둘 다 통과해야 지운다. WEBCHART_ALLOW_DELETE=0 이면 403 이고
 버튼도 숨긴다.
"""
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import config
from modules import web_dashboard as wd


@pytest.fixture
def chart_dir(tmp_path, monkeypatch):
    d = tmp_path / "chart"
    (d / wd.THUMB_DIRNAME).mkdir(parents=True)
    for n in ("005930_daily.png", "KOSPI_daily.png"):
        (d / n).write_bytes(b"x")
        (d / wd.THUMB_DIRNAME / n).write_bytes(b"t")
    monkeypatch.setattr(config, "CHART_DIR", str(d), raising=False)
    monkeypatch.setattr(config, "WEBCHART_ALLOW_DELETE", True, raising=False)
    monkeypatch.setattr(wd, "resolve_stock_name", lambda code, ov: None)
    return str(d)


@pytest.fixture
def server(chart_dir):
    class H(wd.ChartRequestHandler):
        def __init__(self, *a, **k):
            k["directory"] = chart_dir
            super().__init__(*a, **k)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _delete(port, path):
    try:
        return urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE"), timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------- 정렬(클라이언트)

def test_카드가_드래그_가능하고_파일명_열쇠를_단다(chart_dir):
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert html.count('draggable="true"') == 2
    assert 'data-key="005930_daily.png"' in html and 'data-key="KOSPI_daily.png"' in html
    for needle in ("localStorage", "applySavedOrder", "dragstart", "drop", "resetOrder"):
        assert needle in html, f"정렬 스크립트에 {needle} 가 없다"


def test_서버_기본_순서는_여전히_최신순이다(chart_dir):
    """드래그 정렬은 브라우저 위에 얹는 것이다 — 서버가 내보내는 기본은 시간순이어야 한다."""
    old, new = os.path.join(chart_dir, "005930_daily.png"), os.path.join(chart_dir, "KOSPI_daily.png")
    os.utime(old, (1_000_000, 1_000_000)); os.utime(new, (2_000_000, 2_000_000))
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert html.index('data-key="KOSPI_daily.png"') < html.index('data-key="005930_daily.png"')


def test_새_차트는_저장_순서_앞에_최신순으로_선다(chart_dir):
    """저장 목록에 없는 카드가 뒤로 밀리면 '기본은 시간순'이 깨진다 — 스크립트가 fresh 를 앞에 둔다."""
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert "[...fresh, ...kept]" in html


def test_드롭_직후_클릭이_라이트박스를_열지_않는다(chart_dir):
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert 'onclick="cardClick(' in html and 'onclick="openLightbox(' not in html
    assert "suppressClick" in html


# ---------------------------------------------------------------- 삭제(서버)

def test_정상_삭제는_원본과_썸네일을_지우고_인덱스를_다시_만든다(server, chart_dir):
    wd.update_chart_index(chart_dir)
    assert _delete(server, "/005930_daily.png") == 204
    assert not os.path.exists(os.path.join(chart_dir, "005930_daily.png"))
    assert not os.path.exists(os.path.join(chart_dir, wd.THUMB_DIRNAME, "005930_daily.png"))
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert "005930_daily.png" not in html and "KOSPI_daily.png" in html


@pytest.mark.parametrize("path", [
    "/../outside.png", "/%2e%2e/outside.png", "/thumbs/KOSPI_daily.png",
    "/index.html", "/KOSPI_daily.png/", "/.png", "/",
])
def test_경로_규약_밖은_지우지_않는다(server, chart_dir, tmp_path, path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"s")
    before = set(os.listdir(chart_dir))
    assert _delete(server, path) == 404
    assert outside.exists()
    assert set(os.listdir(chart_dir)) == before


def test_없는_파일은_404(server):
    assert _delete(server, "/nope.png") == 404


def test_삭제를_끄면_403이고_버튼도_숨는다(server, chart_dir, monkeypatch):
    monkeypatch.setattr(config, "WEBCHART_ALLOW_DELETE", False, raising=False)
    assert _delete(server, "/KOSPI_daily.png") == 403
    assert os.path.exists(os.path.join(chart_dir, "KOSPI_daily.png"))
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    assert '<body class="no-delete">' in html


def test_파일명_해석은_이름_하나만_받는다():
    f = wd._delete_target_name
    assert f("/a.png") == "a.png"
    assert f("/%ED%95%9C%EA%B8%80.png") == "한글.png"
    assert f("/a.png?v=1") == "a.png"
    for bad in ("/", "/a", "/a.PNG.txt", "/../a.png", "/x/a.png", "/..", "/a\\b.png"):
        assert f(bad) is None, bad


def test_realpath_가_chart_밖이면_지우지_않는다(chart_dir, tmp_path):
    """파일명 검사를 우회한 심볼릭 링크도 realpath 검사에서 막힌다."""
    outside = tmp_path / "outside.png"; outside.write_bytes(b"s")
    link = os.path.join(chart_dir, "link.png")
    os.symlink(str(outside), link)
    ok, msg = wd.delete_chart_file(chart_dir, "link.png")
    assert (ok, msg) == (False, "not found")
    assert outside.exists()


def test_삭제_확인은_브라우저_confirm_이_아니라_페이지_안_창이다(chart_dir):
    """브라우저 confirm() 은 'http://…/ 페이지의 메시지:' 머리말을 강제로 붙인다(운용자 요청으로 제거)."""
    wd.update_chart_index(chart_dir)
    html = open(os.path.join(chart_dir, "index.html"), encoding="utf-8").read()
    script = html[html.index("<script>"):]
    for native in ("confirm(`", "confirm('", 'confirm("', "alert("):
        assert native not in script, f"브라우저 기본 창을 다시 쓴다: {native}"
    assert 'id="confirm"' in html and "askConfirm(" in script
    assert "escapeHtml(title)" in script, "종목명이 HTML 로 들어가므로 이스케이프해야 한다"
