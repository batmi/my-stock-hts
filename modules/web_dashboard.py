import glob
import html
import logging
import os
import re
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import config

logger = logging.getLogger(__name__)

# ==========================================================
# [웹서버] 차트 갤러리 — 같은 프로세스의 데몬 스레드로 돈다
# ==========================================================
#  [왜 subprocess 가 아닌가 · 2026-08-29]
#   ① 규약 — 실행 중 기능이 tools/ 스크립트를 subprocess 로 띄우면 안 된다. 로직은
#      modules/ 에 두고 CLI(tools/web_server.py)가 그것을 쓰는 얇은 껍데기여야 한다.
#   ② 메모리 — 파이썬 인터프리터를 하나 더 띄우면 1GB 라즈베리파이에서 RSS 15~30MB 를
#      더 먹는다. 스레드로 돌리면 서버 객체는 수 KB 다. 종전에 '차트 렌더링 중 웹서버를
#      죽여 메모리를 회수'하던 장치는 그 두 번째 인터프리터를 겨냥한 것이었는데, 회수
#      대상이 사라졌으므로 함께 걷어낸다(그 장치는 갤러리를 보는 중에 접속이 끊기는
#      부작용이 있었고, 스레드 간 락도 없었다).
#   ③ 종료 — daemon 스레드라 os._exit 로 끊어도 남지 않는다. terminate/kill 이 필요 없다.
#
#  [os.chdir 를 쓰지 않는다] SimpleHTTPRequestHandler 의 directory= 인자로 서빙 경로를
#   준다. 같은 프로세스에서 chdir 하면 상대경로를 쓰는 다른 모듈이 전부 흔들린다.
_server = None
_server_thread = None
_server_lock = threading.RLock()
_stopping = False          # stop_web_server 가 의도적으로 내리는 중인가
_restarts = 0              # 예기치 않은 종료 뒤 되살린 횟수(세션 누적)

# 서버 루프가 스스로 죽었을 때 되살리는 횟수 상한.
#  [왜 필요한가 · 2026-08-29] 실운영(라즈베리파이)에서 웹서버가 종료되는 일이 있었다.
#  원인은 300 DPI 차트 렌더링의 메모리 폭주로 리눅스 OOM Killer 가 **별도 프로세스였던**
#  웹서버를 죽인 것이었고, 당시엔 bash `while true` 래퍼로 되살렸다.
#  이제 웹서버는 같은 프로세스의 스레드라 OS 가 이것만 따로 죽일 수는 없다. 하지만
#  소켓 오류 등으로 루프가 빠져나올 수는 있고, 그때 조용히 사라지면 증상이 똑같다.
#  들키지 않는 죽음을 없애기 위해 경고를 남기고 되살린다. 무한 재시도는 하지 않는다 —
#  포트를 뺏겼거나 구조적 문제라면 로그만 시끄러워진다.
MAX_SERVER_RESTARTS = 5


class ChartRequestHandler(SimpleHTTPRequestHandler):
    # 브라우저가 연결(Keep-Alive)만 맺어두고 요청을 보내지 않을 때 소켓을 회수한다.
    timeout = 5

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('directory', config.CHART_DIR)
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # 기본 구현은 stderr 로 직접 찍어 rich 화면을 깨뜨린다. 로거로 돌린다.
        logger.debug("[webchart] " + (fmt % args))


def _serve_forever(httpd, started):
    """서버 루프. 예외를 삼켜 처리되지 않은 스레드 예외를 만들지 않는다.

    stop 이 소켓을 먼저 닫으면 여기서 OSError 가 나는데, 그건 정상 종료 경로다.
    의도한 종료가 아니면 **경고를 남기고 되살린다** — 조용히 사라지면 사용자에게는
    '웹서버가 또 죽었다'로만 보이고 원인이 어디에도 남지 않는다.
    """
    global _restarts
    crashed = None
    try:
        started.set()
        # poll_interval 은 shutdown() 요청을 알아채는 주기다 — 종료 지연의 상한이 된다.
        #  0.5s 면 종료 때마다 0.5s 를 기다린다. 0.2s 로 줄여도 깨어나는 횟수는 초당 5회라
        #  라즈베리파이에서도 무시할 수 있다.
        httpd.serve_forever(poll_interval=0.2)
    except Exception as e:
        crashed = e

    with _server_lock:
        # 의도적 종료(stop_web_server)면 여기서 끝이다.
        if _stopping or _server is not httpd:
            if crashed:
                logger.debug(f"[webchart] 서버 루프 종료(정상 경로): {crashed}")
            return
        if _restarts >= MAX_SERVER_RESTARTS:
            logger.error(f"[webchart] 서버가 {_restarts}회 되살아난 뒤 또 멈췄습니다 — 재시작을 중단합니다: {crashed}")
            return
        _restarts += 1

    logger.warning(f"[webchart] 서버 루프가 예기치 않게 멈춰 되살립니다 "
                   f"({_restarts}/{MAX_SERVER_RESTARTS}): {crashed}")
    _restart_after_crash(httpd)


def _restart_after_crash(dead):
    """죽은 서버를 정리하고 같은 주소로 다시 띄운다."""
    global _server, _server_thread
    with _server_lock:
        if _server is not dead:
            return
        addr = dead.server_address
        _server = _server_thread = None
    try:
        dead.server_close()
    except Exception:
        pass
    start_web_server(port=addr[1], host=addr[0])


def _safe_shutdown(httpd):
    try:
        httpd.shutdown()
    except Exception as e:
        logger.debug(f"[webchart] shutdown 실패: {e}")


def start_web_server(port=None, host=None):
    """차트 갤러리 웹서버를 백그라운드 스레드로 띄운다. 이미 떠 있으면 아무것도 하지 않는다.

    성공 여부를 돌려준다 — 포트 충돌(EADDRINUSE)이면 False 이고, 호출부는 안내를 바꾼다.
    """
    global _server, _server_thread
    with _server_lock:
        if _server is not None:
            return True

        port = int(config.WEBCHART_PORT if port is None else port)
        host = config.WEBCHART_HOST if host is None else host
        try:
            httpd = ThreadingHTTPServer((host, port), ChartRequestHandler)
        except OSError as e:
            config.console.print(
                f"\n[red]차트 웹서버 구동 실패 ({host}:{port}) — {e}[/red]\n"
                f"[dim]   다른 포트를 쓰려면 WEBCHART_PORT 환경변수를 지정하세요.[/dim]")
            return False
        except Exception as e:
            config.console.print(f"\n[red]차트 웹서버 구동 실패: {e}[/red]")
            return False

        httpd.daemon_threads = True
        started = threading.Event()
        thread = threading.Thread(target=_serve_forever, args=(httpd, started),
                                  name="webchart-server", daemon=True)
        thread.start()
        # [중요] serve_forever 가 실제로 루프에 들어갈 때까지 기다린다.
        #  CPython 문서: "shutdown() must be called while serve_forever() is running in a
        #  different thread otherwise it will deadlock." 루프 진입 전에 stop 이 들어오면
        #  shutdown() 이 영영 셋되지 않는 이벤트를 기다린다 — 실측으로 재현된다
        #  (start 직후 stop 을 반복하면 수십 회 안에 걸린다). 종료 경로(atexit)가 멈추면
        #  프로그램이 안 죽으므로 여기서 막는다.
        started.wait(timeout=5)
        _server, _server_thread = httpd, thread
        logger.info(f"[webchart] 차트 웹서버 시작: http://{host}:{port}/ ({config.CHART_DIR})")
        return True


def stop_web_server():
    """웹서버를 멈춘다. 핸들러 스레드 안에서 부르면 안 된다(shutdown 이 자기 자신을 기다린다).

    **어떤 경우에도 막히지 않는다.** shutdown() 은 serve_forever 가 루프에 들어가 있어야만
    돌아오므로(위 start 주석 참조), 별도 스레드에서 부르고 시간을 묶는다. 그 뒤 소켓을
    닫으면 아직 루프에 못 들어간 serve_forever 도 곧바로 빠져나오며 대기가 풀린다.
    종료 경로(atexit·os._exit 직전)가 여기서 멈추면 프로그램이 죽지 않는다.
    """
    global _server, _server_thread, _stopping
    with _server_lock:
        if _server is None:
            return
        srv, thread = _server, _server_thread
        _server = _server_thread = None
        _stopping = True

    stopper = threading.Thread(target=_safe_shutdown, args=(srv,),
                               name="webchart-stop", daemon=True)
    stopper.start()
    stopper.join(timeout=3)

    try:
        srv.server_close()
    except Exception as e:
        logger.debug(f"[webchart] server_close 실패: {e}")
    if thread is not None:
        thread.join(timeout=3)

    with _server_lock:
        _stopping = False


def is_web_server_running():
    with _server_lock:
        return _server is not None


def web_server_url():
    """안내 문구용 접속 주소. 0.0.0.0 바인딩은 그대로 보여주면 붙을 수 없으므로 바꿔 적는다."""
    host = config.WEBCHART_HOST
    if host in ("0.0.0.0", "::", ""):
        host = "<이 서버의 IP>"
    return f"http://{host}:{config.WEBCHART_PORT}/"


# ==========================================================
# [종목명] 갤러리 카드 제목 — 코드당 1회만 외부를 조회한다
# ==========================================================
#  [왜 캐시가 필요한가 · 2026-08-29] api.get_stock_name_by_code 는 캐시가 없어
#   국내는 네이버 금융에 HTTP GET, 해외는 TradingView Screener 질의를 매번 날린다.
#   인덱스는 차트를 한 장 그릴 때마다 다시 쓰이므로, 캐시가 없으면 chart/ 안의
#   **PNG 개수만큼** 외부 요청이 나간다. 텔레그램 /chart 는 폴링 스레드에서 이 경로를
#   타므로 봇이 그 시간만큼 멈춘다(라즈베리파이).
#  순서: ① 관심종목(stock.json) — 네트워크 0회 → ② 외부 조회 1회 → ③ 결과를 코드에 못박음.
#   실패(None)도 캐시한다. 못 찾는 코드에 매번 3초 타임아웃을 태울 이유가 없다.
_NAME_CACHE = {}
_NAME_CACHE_LOCK = threading.Lock()


def _name_from_universe(code):
    """관심종목 목록에서 종목명을 찾는다(네트워크 없음). 없으면 None."""
    try:
        data = getattr(config.session, 'stock_data', None) or {}
        for key in ("stocks_kr", "etfs_kr", "stocks_us", "etfs_us"):
            for item in data.get(key) or []:
                if item.get('code') == code:
                    return item.get('name') or None
    except Exception as e:
        logger.debug(f"[webchart] 관심종목 조회 실패: {e}")
    return None


def resolve_stock_name(code, is_overseas):
    """종목명을 돌려준다. 프로세스 수명 동안 코드당 외부 조회는 최대 1회."""
    key = (code, bool(is_overseas))
    with _NAME_CACHE_LOCK:
        if key in _NAME_CACHE:
            return _NAME_CACHE[key]

    name = _name_from_universe(code)
    if not name:
        try:
            import api
            name = api.get_stock_name_by_code(code, is_overseas)
        except Exception as e:
            logger.debug(f"[webchart] 종목명 조회 실패({code}): {e}")
            name = None
        # 조회 실패 시 이 함수는 코드 자체를 돌려주기도 한다 — 이름이 아니므로 버린다.
        if name == code:
            name = None

    with _NAME_CACHE_LOCK:
        _NAME_CACHE[key] = name
    return name


def clear_name_cache():
    """관심종목이 바뀌었을 때 등 캐시를 비운다(테스트·설정 변경 경로)."""
    with _NAME_CACHE_LOCK:
        _NAME_CACHE.clear()


# ==========================================================
# [파일명] 차트 PNG 이름 → (종목코드, 부제)
# ==========================================================
#  [왜 split('_') 로는 안 되나 · 2026-08-29] 몬테카를로 결과는 한때
#   `mc_dist_{code}_{YYYYMMDD}_{HHMMSS}.png` 였고, split('_') 하면 parts[1] 이 'dist' 가
#   된다. 'dist'.isdigit() 가 False → 해외로 판정 → **문자열 'dist' 로 TradingView 질의**가
#   파일 수만큼 나가고, 제목에는 종목코드가 기간인 양 찍혔다.
#   이름 규약을 아는 파일만 해석하고, 모르는 파일은 조회 없이 파일명 그대로 보여준다.
#
#  [규약] 차트는 **종목·기간당 한 장**이고 다시 그리면 덮어쓴다.
#   · analysis_{code}_{period}.png — 분석 차트
#   · mc_dist_{code}.png           — 몬테카를로 분포
#   타임스탬프가 붙은 옛 몬테카를로 파일도 계속 읽는다 — 정리되기 전까지 갤러리에서
#   깨져 보이면 안 되기 때문이다(새로 그릴 때 chart._purge_legacy_mc_charts 가 지운다).
_ANALYSIS_RE = re.compile(r'^analysis_(?P<code>[A-Za-z0-9]+)_(?P<period>[A-Za-z]+)$')
_MC_RE = re.compile(r'^mc_dist_(?P<code>[A-Za-z0-9]+)$')
_MC_LEGACY_RE = re.compile(r'^mc_dist_(?P<code>[A-Za-z0-9]+)_(?P<stamp>\d{8}_\d{6})$')

_PERIOD_LABEL = {'daily': '일봉', 'weekly': '주봉', 'monthly': '월봉',
                 'hourly': '시간봉', 'intraday': '분봉'}


def parse_chart_filename(filename):
    """`(code, subtitle)`. 규약을 모르는 파일이면 `(None, None)`."""
    stem = filename[:-4] if filename.lower().endswith('.png') else filename

    m = _ANALYSIS_RE.match(stem)
    if m:
        period = m.group('period')
        return m.group('code'), _PERIOD_LABEL.get(period.lower(), period.upper())

    m = _MC_RE.match(stem)
    if m:
        return m.group('code'), "몬테카를로"

    m = _MC_LEGACY_RE.match(stem)
    if m:
        stamp = m.group('stamp')
        return m.group('code'), f"몬테카를로 {stamp[4:6]}/{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"

    return None, None


# ==========================================================
# [썸네일] 갤러리 첫 화면은 원본 PNG 를 내려받지 않는다
# ==========================================================
#  [왜 필요한가 · 2026-09-03] 분석 차트는 20x28인치를 300DPI 로 그려 6000x8400px,
#   장당 2.5MB 다. 갤러리 카드는 그것을 180x130px 로 줄여 보여줄 뿐인데, 종전에는
#   원본을 그대로 <img src> 로 걸었다. 차트가 30장이면 첫 접속에 75MB 를 받는다.
#   그래서 차트를 그리는 김에(= 원본을 다시 디코드하지 않고, 살아 있는 Figure 에서)
#   낮은 DPI 로 한 장 더 저장해 두고, 카드는 그 썸네일을 건다. 라이트박스를 열면
#   그때 원본을 받는다.
#  [왜 PNG 를 다시 읽어 줄이지 않나] 6000x8400 RGBA 를 PIL 로 열면 그것만 200MB 다.
#   1GB 라즈베리파이에서 OOM Killer 를 부르는 크기라, 디코드 경로는 쓰지 않는다.
#   그래서 **썸네일이 없으면 원본을 건다** — 종전 동작 그대로다(다시 그리면 생긴다).
THUMB_DIRNAME = 'thumbs'
THUMB_WIDTH_PX = 400          # 카드 표시 폭(180px)의 2배 남짓 — 고DPI 화면까지 감당


def thumbnail_path(png_path):
    """원본 차트 PNG 경로 → 썸네일 PNG 경로."""
    directory, filename = os.path.split(png_path)
    return os.path.join(directory, THUMB_DIRNAME, filename)


def _thumb_src(chart_dir, filename, mtime):
    """카드에 걸 이미지의 (상대경로, 원본여부). 썸네일이 낡았으면 원본을 쓴다."""
    thumb = os.path.join(chart_dir, THUMB_DIRNAME, filename)
    try:
        if os.path.getmtime(thumb) >= mtime:
            return f"{THUMB_DIRNAME}/{filename}"
    except OSError:
        pass
    return filename


def _purge_orphan_thumbs(chart_dir, live_names):
    """원본이 사라진 썸네일을 지운다. 실패해도 인덱스 생성은 계속한다."""
    thumb_dir = os.path.join(chart_dir, THUMB_DIRNAME)
    try:
        names = os.listdir(thumb_dir)
    except OSError:
        return
    for name in names:
        if name in live_names:
            continue
        try:
            os.remove(os.path.join(thumb_dir, name))
        except OSError as e:
            logger.debug(f"[webchart] 고아 썸네일 삭제 실패({name}): {e}")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chart Dashboard</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E%F0%9F%93%88%3C/text%3E%3C/svg%3E">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --border: rgba(255, 255, 255, 0.1);
        }
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(56, 189, 248, 0.05), transparent 25%),
                radial-gradient(circle at 85% 30%, rgba(139, 92, 246, 0.05), transparent 25%);
            color: var(--text-main);
            margin: 0;
            padding: 2rem;
            min-height: 100vh;
        }
        header {
            text-align: center;
            margin-bottom: 3rem;
            animation: fadeInDown 0.8s ease;
        }
        h1 {
            font-size: 2.5rem;
            font-weight: 600;
            margin: 0;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p.subtitle {
            color: var(--text-muted);
            margin-top: 0.5rem;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 1.5rem;
            max-width: 1400px;
            margin: 0 auto;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            backdrop-filter: blur(10px);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            cursor: pointer;
            animation: fadeIn 1s ease both;
        }
        .card:hover {
            transform: translateY(-8px) scale(1.02);
            box-shadow: 0 15px 30px rgba(0, 0, 0, 0.3);
            border-color: rgba(56, 189, 248, 0.3);
        }
        .card img {
            width: 100%;
            height: 130px;
            object-fit: cover;
            border-bottom: 1px solid var(--border);
            transition: filter 0.3s;
        }
        .card:hover img {
            filter: brightness(1.1);
        }
        .card-info {
            padding: 0.8rem;
        }
        .card-title {
            font-weight: 600;
            font-size: 0.95rem;
            margin: 0 0 0.4rem 0;
            color: var(--text-main);
            word-break: break-all;
        }
        .card-meta {
            font-size: 0.75rem;
            color: var(--text-muted);
            display: flex;
            justify-content: space-between;
        }
        
        /* Lightbox Modal */
        #lightbox {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(15, 23, 42, 0.95);
            backdrop-filter: blur(8px);
            z-index: 1000;
            opacity: 0;
            transition: opacity 0.3s ease;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }
        #lightbox.active {
            display: flex;
            opacity: 1;
        }
        #lightbox.active.zoomed {
            display: block;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 20px 0;
        }
        #lightbox img {
            max-width: 95%;
            max-height: 95vh;
            border-radius: 8px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            transform: scale(0.95);
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            cursor: zoom-in;
            display: block;
            margin: 0 auto;
        }
        #lightbox.active img {
            transform: scale(1);
        }
        #lightbox.active.zoomed img {
            max-width: none;
            max-height: none;
            width: 100%;
            height: auto;
            border-radius: 0;
            cursor: zoom-out;
        }
        .close-btn {
            position: absolute;
            top: 20px; right: 30px;
            font-size: 2.5rem;
            color: white;
            cursor: pointer;
            transition: color 0.2s;
            z-index: 1001;
        }
        .close-btn:hover { color: var(--accent); }

        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: scale(0.95); }
            to { opacity: 1; transform: scale(1); }
        }
    </style>
</head>
<body>
    <header>
        <h1>Chart Dashboard</h1>
    </header>
    <div class="gallery">
        <!-- INJECT_CARDS -->
    </div>

    <div id="lightbox" onclick="closeLightbox()">
        <span class="close-btn" onclick="closeLightbox()">&times;</span>
        <img id="lightbox-img" src="" alt="Full Chart" onclick="toggleZoom(event)">
    </div>

    <script>
        function toggleZoom(event) {
            event.stopPropagation();
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.toggle('zoomed');
        }
        function openLightbox(src) {
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.remove('zoomed');
            document.getElementById('lightbox-img').src = src;
            lightbox.classList.add('active');
            document.body.style.overflow = 'hidden';
        }
        function closeLightbox() {
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.remove('active');
            lightbox.classList.remove('zoomed');
            setTimeout(() => { document.getElementById('lightbox-img').src = ''; }, 300);
            document.body.style.overflow = 'auto';
        }
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeLightbox();
        });
    </script>
</body>
</html>
"""

def update_chart_index(chart_dir):
    """차트 디렉토리의 png 를 읽어 index.html 갤러리를 생성한다."""
    png_files = glob.glob(os.path.join(chart_dir, '*.png'))

    # 수정 시간 기준으로 최신순 정렬
    png_files.sort(key=os.path.getmtime, reverse=True)

    cards_html = ""
    for idx, f in enumerate(png_files):
        filename = os.path.basename(f)
        mtime = os.path.getmtime(f)
        date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')

        code, subtitle = parse_chart_filename(filename)
        if code:
            # 6자리 숫자가 아니면 해외 티커로 본다(국내 코드 규약).
            stock_name = resolve_stock_name(code, not code.isdigit())
            head = f"{stock_name} {code}" if stock_name else code
            alt_text = f"{head} ({subtitle})"
            title_html = (f"{html.escape(head)}<br>"
                          f"<span style='font-size: 0.85em; color: var(--text-muted);'>"
                          f"({html.escape(subtitle)})</span>")
        else:
            # 규약 밖 파일 — 외부 조회 없이 파일명 그대로 보여준다.
            alt_text = filename
            title_html = html.escape(filename)

        anim_delay = (idx % 10) * 0.1
        # 파일명·종목명은 전부 이스케이프한다(따옴표 하나에 카드가 깨지지 않도록).
        src = f"{html.escape(filename, quote=True)}?v={int(mtime)}"
        # 카드에는 썸네일을, 라이트박스에는 원본을 건다.
        thumb_src = (f"{html.escape(_thumb_src(chart_dir, filename, mtime), quote=True)}"
                     f"?v={int(mtime)}")

        cards_html += f'''
        <div class="card" style="animation-delay: {anim_delay}s" onclick="openLightbox(&quot;{src}&quot;)">
            <img src="{thumb_src}" alt="{html.escape(alt_text, quote=True)}"
                 loading="lazy" decoding="async">
            <div class="card-info">
                <h3 class="card-title">{title_html}</h3>
                <div class="card-meta">
                    <span>{date_str}</span>
                </div>
            </div>
        </div>
        '''

    if not cards_html:
        cards_html = ('<p style="grid-column: 1 / -1; text-align: center; color: var(--text-muted);">'
                      '아직 생성된 차트가 없습니다. 메뉴에서 차트를 만들면 여기에 쌓입니다.</p>')

    _purge_orphan_thumbs(chart_dir, {os.path.basename(f) for f in png_files})

    html_content = HTML_TEMPLATE.replace('<!-- INJECT_CARDS -->', cards_html)

    index_path = os.path.join(chart_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as fp:
        fp.write(html_content)


if __name__ == "__main__":
    # 독립적으로 실행할 때를 대비한 처리
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    chart_dir = os.path.join(base_dir, 'chart')
    update_chart_index(chart_dir)
    print(f"Chart index generated at: {os.path.join(chart_dir, 'index.html')}")
