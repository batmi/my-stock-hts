# mem_diag.py
# -----------------------------------------------------------------------------
# 메모리/스왑 포화 및 OOM(또는 그로 인한 시스템 리붓) 원인 추적용 경량 진단 로거.
#
# 특징
#  - 표준 라이브러리만 사용(의존성 0) → 무거운 import 이전, 기동 극초기부터 동작 가능.
#  - 매 기록마다 flush + os.fsync 로 디스크(SD카드)에 즉시 내려써, 메모리 포화로
#    시스템이 리붓되어도 '직전 마지막 줄'이 보존된다.
#  - 백그라운드 샘플러 스레드가 1초 간격으로 본 프로세스 RSS/Swap, 시스템 가용메모리/
#    스왑, 스레드 수를 기록하고, 주기적으로 시스템 상위 메모리 프로세스도 남긴다.
#  - 단계 마커(log_event)로 기동 구간별(import/사전점검/워밍업/초기화 등) 메모리 변화를 추적.
#
# 로그 위치: <프로젝트 루트>/logs/memory_diag.log
# 활성화: 기본 off. 환경변수 HTS_MEM_DIAG=1 (또는 true/yes/on) 일 때만 동작.
# -----------------------------------------------------------------------------
import os
import time
import threading
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_ROOT, "logs", "memory_diag.log")

_lock = threading.Lock()
_fh = None
_thread = None
_running = False
_started = False
_trace_on = False
_last_dump_rss = 0


def is_enabled():
    return os.environ.get("HTS_MEM_DIAG", "").strip().lower() in ("1", "true", "yes", "on")


def is_trace_enabled():
    # tracemalloc 기반 '할당 위치 추적'. 오버헤드가 크므로 별도 env로 opt-in.
    return os.environ.get("HTS_MEM_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def enable_trace():
    """tracemalloc 추적을 '이 시점부터' 시작한다.
    [중요] 무거운 라이브러리 import 이후, 의심 구간(자동매매 초기화) 직전에 호출해야 한다.
    import 시점에 켜면 pandas/numpy 등의 대량 할당까지 추적해 저사양 CPU에서 극도로 느려진다.
    """
    global _trace_on
    if not _started or _trace_on or not is_trace_enabled():
        return
    try:
        import tracemalloc
        tracemalloc.start(4)  # 4 프레임까지 추적 (오버헤드 절감)
        _trace_on = True
        log_event("trace-enabled")
    except Exception:
        _trace_on = False


def _dump_top_allocations(tag=""):
    """현재 메모리를 가장 많이 점유한 할당 위치(파일:라인) 상위 10개를 기록한다."""
    if not _trace_on:
        return
    try:
        import tracemalloc
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("lineno")[:10]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        _write(f"{ts} | TRACE-TOP ({tag}) | 상위 할당 위치:")
        for i, st in enumerate(stats, 1):
            fr = st.traceback[0]
            _write(f"    #{i} {st.size/1024/1024:.1f}MB  {fr.filename}:{fr.lineno}  (count={st.count})")
    except Exception as e:
        _write(f"  (tracemalloc dump 실패: {e})")


def _read_self_mem():
    """본 프로세스의 RSS/Swap(kB) 반환. (Linux: /proc/self/status)"""
    rss = swap = 0
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    swap = int(line.split()[1])
    except Exception:
        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux: kB, macOS: bytes
            rss = ru if ru < 10_000_000 else ru // 1024
        except Exception:
            pass
    return rss, swap


def _read_sys_mem():
    """시스템 메모리 정보(kB) dict 반환. (Linux: /proc/meminfo)"""
    info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    if key in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree", "Buffers", "Cached"):
                        try:
                            info[key] = int(parts[1].split()[0])
                        except Exception:
                            pass
    except Exception:
        pass
    return info


def _proc_name(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").strip()
        if raw:
            return raw.decode("utf-8", "replace")[:60]
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip()[:60]
    except Exception:
        return f"pid{pid}"


def _top_processes(n=8):
    """시스템 전체에서 RSS 상위 n개 프로세스 (Linux 전용)."""
    procs = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/statm", "r") as f:
                    # statm: size resident shared ... (단위: page)
                    resident_pages = int(f.read().split()[1])
                rss_kb = resident_pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
                procs.append((rss_kb, pid))
            except Exception:
                continue
    except Exception:
        return []
    procs.sort(reverse=True)
    out = []
    for rss_kb, pid in procs[:n]:
        out.append(f"{_proc_name(pid)}={rss_kb/1024:.1f}MB")
    return out


def _mb(kb):
    return f"{kb/1024:.1f}MB"


def _write(line):
    global _fh
    with _lock:
        try:
            if _fh is None:
                os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
                _fh = open(_LOG_PATH, "a", buffering=1)  # line-buffered
            _fh.write(line + "\n")
            _fh.flush()
            os.fsync(_fh.fileno())  # SD카드에 즉시 기록 → 리붓에도 보존
        except Exception:
            pass


def log_event(stage=""):
    """기동 단계 마커 + 현재 메모리 스냅샷을 즉시 기록."""
    if not _started:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rss, swap = _read_self_mem()
    sysm = _read_sys_mem()
    avail = sysm.get("MemAvailable", 0)
    sfree = sysm.get("SwapFree", 0)
    nthreads = threading.active_count()
    _write(
        f"{ts} | STAGE={stage} | self_rss={_mb(rss)} self_swap={_mb(swap)} | "
        f"sys_avail={_mb(avail)} swap_free={_mb(sfree)} | threads={nthreads}"
    )


def _sampler(interval, top_every):
    global _last_dump_rss
    tick = 0
    while _running:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            rss, swap = _read_self_mem()
            sysm = _read_sys_mem()
            avail = sysm.get("MemAvailable", 0)
            sfree = sysm.get("SwapFree", 0)
            stotal = sysm.get("SwapTotal", 0)
            nthreads = threading.active_count()
            _write(
                f"{ts} | TICK | self_rss={_mb(rss)} self_swap={_mb(swap)} | "
                f"sys_avail={_mb(avail)} swap_used={_mb(stotal - sfree)}/{_mb(stotal)} | threads={nthreads}"
            )
            if top_every > 0 and tick % top_every == 0:
                tops = _top_processes(8)
                if tops:
                    _write(f"{ts} | TOP | " + " | ".join(tops))
            # [할당 추적] RSS가 직전 덤프 대비 30MB 이상 급증하면 상위 할당 위치를 기록
            if _trace_on and (rss - _last_dump_rss) >= 30 * 1024:
                _dump_top_allocations(tag=f"rss={_mb(rss)}")
                _last_dump_rss = rss
            tick += 1
        except Exception:
            pass
        time.sleep(interval)


def start(interval=1.0, top_every=5):
    """진단 로거 시작. HTS_MEM_DIAG 미설정 시 아무 동작도 하지 않는다."""
    global _thread, _running, _started, _last_dump_rss
    if not is_enabled() or _started:
        return False
    _started = True
    _running = True
    _last_dump_rss = 0

    sysm = _read_sys_mem()
    hdr = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write("")
    _write("=" * 90)
    _write(
        f"{hdr} | START mem_diag | pid={os.getpid()} | cpu={os.cpu_count()} | "
        f"MemTotal={_mb(sysm.get('MemTotal', 0))} SwapTotal={_mb(sysm.get('SwapTotal', 0))} | "
        f"interval={interval}s"
    )
    _write("=" * 90)

    _thread = threading.Thread(target=_sampler, args=(interval, top_every), daemon=True, name="MemDiag")
    _thread.start()
    return True


def stop():
    global _running
    _running = False
    log_event("STOP")
