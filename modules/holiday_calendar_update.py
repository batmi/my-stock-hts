"""holidays 패키지 자동 갱신 — 앱이 스스로 주기 갱신하되, **바뀐 휴장일을 반드시 알린다**.

[왜 이렇게 · 2026-09-20]
 종전에는 (1) run.sh 가 기동 때마다 `pip install --upgrade holidays` 를 돌렸다가, 휴장일 판정
 라이브러리가 사람 모르게 바뀌는 것이 문제라 (2) tools/update_holidays.sh 를 운용자가 cron 에
 걸도록 바꿨다([[dependencies-single-source]]). 그런데 그 cron 은 운용자 입력이고, 잊히면 임시공휴일이
 반영되지 않는다. 운용자 입력은 없애되 '조용한 변경 금지'는 지킨다:

   · 7일마다 한 번(상태 파일 기준) 앱 안에서 갱신을 시도한다 — 기동 직후 백그라운드 + 스케줄러 일일 점검.
   · 갱신 **전후로** 향후 400일의 KR·US 공휴일과 거래소(MIC) 휴장일 집합을 새 인터프리터에서 받아 비교한다.
     (지금 프로세스는 이미 옛 버전을 import 했으므로 프로세스 안에서 비교하면 새 버전이 안 보인다.)
   · 달라진 날짜가 있으면 콘솔·로그·텔레그램으로 **날짜 단위로** 알린다. 없으면 로그 한 줄.
   · 새 달력은 **다음 기동부터** 적용된다(실행 중 모듈을 갈아 끼우지 않는다 — 장중 판정이 바뀌면 안 된다).

[한계] 국내 휴장 판정은 KIS 휴장일 API 가 1차라 라이브러리는 폴백·토스 모드 비-오늘 날짜·해외 거래소
 달력에만 쓰인다(api/market_calendar.py). 그래도 그 자리들은 '휴장인데 연다'로 조용히 틀리는 종류라
 임시공휴일 반영을 자동화할 가치가 있다.

[규약] 실행 중 기능이 tools/ 스크립트를 subprocess 로 띄우지 않는다([[no-subprocess-for-features]]).
 여기서 띄우는 것은 pip 와 '이 모듈의 함수를 새 인터프리터에서 부르는' 얇은 러너뿐이다.
"""
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "holidays_update_state.json"
CHECK_INTERVAL_DAYS = 7
HORIZON_DAYS = 400
PIP_TIMEOUT_SEC = 600          # 라즈베리파이의 느린 네트워크·디스크를 감안
SNAPSHOT_TIMEOUT_SEC = 120
_RUN_LOCK = threading.Lock()   # 기동 직후 스레드와 스케줄러 점검이 겹치지 않게


def _state_file():
    return os.path.join(config.JSON_DIR, STATE_FILE_NAME)


def _load_state():
    try:
        with open(_state_file(), encoding="utf-8") as fp:
            return json.load(fp) or {}
    except Exception:      # noqa: BLE001 - 상태 파일이 없거나 깨졌으면 '한 번도 안 했다'
        return {}


def _save_state(state):
    from core import jsonio
    jsonio.save_json(_state_file(), state)


def is_due(now=None, state=None):
    """마지막 점검으로부터 CHECK_INTERVAL_DAYS 가 지났는가(기록이 없으면 True)."""
    state = _load_state() if state is None else state
    last = state.get("last_check")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    return (now or datetime.now()) - last_dt >= timedelta(days=CHECK_INTERVAL_DAYS)


# ---------------------------------------------------------------------------
# 스냅샷 — 새 인터프리터에서 실행된다(설치된 버전 그대로)
# ---------------------------------------------------------------------------
def calendar_snapshot(horizon_days=HORIZON_DAYS, today=None):
    """{'version': str, 'calendars': {이름: ['YYYY-MM-DD', ...]}} — 향후 horizon_days 의 휴장일.

    KR·US 는 api.market_calendar.get_holiday_name 와 같은 규칙(근로자의 날·연말 폐장일 포함)이 아니라
    **라이브러리 자체**의 답이다 — 비교 대상이 라이브러리 버전 차이이기 때문이다.
    """
    import holidays
    from api import market_calendar
    today = today or datetime.now().date()
    end = today + timedelta(days=horizon_days)
    years = sorted({today.year, end.year})
    cals = {"KR": holidays.KR(years=years), "US": holidays.US(years=years, observed=True)}
    for mic, spec in market_calendar.EXCHANGE_CALENDARS.items():
        try:
            if "financial" in spec:
                cals[mic] = holidays.financial_holidays(spec["financial"], years=years)
            else:
                cals[mic] = holidays.country_holidays(spec["country"], subdiv=spec.get("subdiv"), years=years)
        except Exception:      # noqa: BLE001 - 이 버전에 없는 달력은 빈 집합(그 자체가 차이로 드러난다)
            cals[mic] = {}
    out = {}
    for name, cal in cals.items():
        out[name] = sorted(d.strftime("%Y-%m-%d") for d in cal if today <= d <= end)
    return {"version": getattr(holidays, "__version__", "?"), "calendars": out}


def _print_snapshot():
    """새 인터프리터 러너의 진입점 — 표준출력에 JSON 한 덩어리."""
    print(json.dumps(calendar_snapshot(), ensure_ascii=False))


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snapshot_in_fresh_interpreter():
    code = "from modules.holiday_calendar_update import _print_snapshot; _print_snapshot()"
    res = subprocess.run([sys.executable, "-c", code], cwd=_project_root(), capture_output=True,
                         text=True, timeout=SNAPSHOT_TIMEOUT_SEC)
    if res.returncode != 0:
        raise RuntimeError(f"스냅샷 실패: {(res.stderr or '').strip()[-300:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def _pip_upgrade():
    """pip 로 holidays 를 올린다. (성공 여부, 마지막 출력 줄)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "holidays"]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv:
        cmd.append("--break-system-packages")     # 시스템 파이썬(PEP 668) — run.sh 와 같은 처리
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_SEC)
    tail = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
    return res.returncode == 0, (tail[-1] if tail else "")


def diff_snapshots(before, after):
    """{달력: {'added': [...], 'removed': [...]}} — 달라진 달력만."""
    out = {}
    b, a = before.get("calendars", {}), after.get("calendars", {})
    for name in sorted(set(b) | set(a)):
        added = sorted(set(a.get(name, [])) - set(b.get(name, [])))
        removed = sorted(set(b.get(name, [])) - set(a.get(name, [])))
        if added or removed:
            out[name] = {"added": added, "removed": removed}
    return out


def format_report(before_ver, after_ver, changes):
    lines = [f"📅 [휴장일 달력 갱신] holidays {before_ver} → {after_ver}"]
    if not changes:
        lines.append("향후 400일 휴장일 변화 없음.")
        return "\n".join(lines)
    lines.append("향후 400일 휴장일이 달라졌습니다 — **다음 기동부터** 적용됩니다:")
    for name, d in changes.items():
        if d["added"]:
            lines.append(f"  · {name} 추가: {', '.join(d['added'])}")
        if d["removed"]:
            lines.append(f"  · {name} 제거: {', '.join(d['removed'])}")
    return "\n".join(lines)


def run_if_due(force=False, notify=True, now=None):
    """주기가 됐으면 갱신을 시도하고 결과 dict 를, 아니면 None 을 돌려준다. 예외를 올리지 않는다.

    결과: {'ok': bool, 'before': ver, 'after': ver, 'changes': {...}, 'message': str}
    """
    if not force and not is_due(now):
        return None
    if not _RUN_LOCK.acquire(blocking=False):
        return None                      # 다른 스레드가 이미 돌리는 중
    try:
        return _run(notify=notify, now=now)
    finally:
        _RUN_LOCK.release()


def _run(notify, now):
    state = _load_state()
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    try:
        before = _snapshot_in_fresh_interpreter()
        ok, pip_tail = _pip_upgrade()
        if not ok:
            #  네트워크 단절 등 — 다음 주에 다시 본다. 오늘로 도장은 찍지 않아 내일 다시 시도한다.
            msg = f"[휴장일 달력 갱신] pip 실패 — {pip_tail or '원인 불명'} (현재 {before.get('version')})"
            logger.warning(msg)
            state.update({"last_attempt": stamp, "last_error": pip_tail})
            _save_state(state)
            return {"ok": False, "before": before.get("version"), "after": before.get("version"),
                    "changes": {}, "message": msg}
        after = _snapshot_in_fresh_interpreter()
    except Exception as e:      # noqa: BLE001 - 갱신 실패는 운용을 막지 않는다
        msg = f"[휴장일 달력 갱신] 실패 — {e}"
        logger.warning(msg)
        state.update({"last_attempt": stamp, "last_error": str(e)[:200]})
        _save_state(state)
        return {"ok": False, "before": None, "after": None, "changes": {}, "message": msg}

    changes = diff_snapshots(before, after)
    message = format_report(before.get("version"), after.get("version"), changes)
    state.update({"last_check": stamp, "last_attempt": stamp, "version": after.get("version"),
                  "last_changes": changes, "last_error": None})
    _save_state(state)
    if changes or before.get("version") != after.get("version"):
        logger.warning(message.replace("**", ""))
        if notify:
            try:
                import api
                api.send_telegram_message(message)
            except Exception as e:      # noqa: BLE001
                logger.debug(f"[휴장일 달력 갱신] 텔레그램 알림 실패: {e}")
    else:
        logger.info(message)
    return {"ok": True, "before": before.get("version"), "after": after.get("version"),
            "changes": changes, "message": message}


def start_background_check(delay_sec=90):
    """기동 직후 한 번 — 기동을 늦추지 않도록 잠시 뒤 데몬 스레드에서 run_if_due 를 돈다."""
    def _job():
        try:
            import time
            time.sleep(delay_sec)
            run_if_due()
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[휴장일 달력 갱신] 백그라운드 점검 실패: {e}")
    t = threading.Thread(target=_job, daemon=True, name="HolidaysUpdate")
    t.start()
    return t
