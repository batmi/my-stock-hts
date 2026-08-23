"""프로세스 생존 신호(하트비트) — '죽으면 알린다'를 프로세스 밖에서 성립시킨다.

[왜 필요한가]
종전 감시는 전부 **프로세스 안**에 있었다(scheduler._check_heartbeat 는 자동매매
*스레드*가 죽었는지만 본다). 그래서 프로세스 자체가 사라지면 — 라즈베리파이 OOM 킬,
SD 카드 오류, 전원 순단 — 알릴 주체가 함께 죽는다. 포지션을 든 채로 손절·트레일링
감시가 멈추는데 텔레그램은 조용하고, 사람이 화면을 볼 때까지 무방비가 된다.
'이상하면 보낸다'는 구조는 침묵과 정상이 구분되지 않는다는 뜻이다.

[뒤집은 구조]
살아 있는 프로세스가 주기적으로 이 파일에 도장을 찍고(beat), **다음 도장을 언제까지
찍겠다(deadline)** 는 약속을 함께 적는다. 밖에서 도는 감시자(tools/hts_watchdog.py,
cron)는 그 약속이 지나갔는지만 본다. 감시자는 장 시간도, 설정도, 계좌도 알 필요가 없다
— 시각 비교 하나뿐이다. 그래서 config 를 import 하지 않고(라즈베리파이에서 cron 이
몇 분마다 무거운 import 를 반복하지 않게), 이 모듈도 config 에 의존하지 않는다.

[하지 않는 것 — 의도된 설계]
**되살리지 않는다.** 자동 재기동은 넣지 않는다(2026-08-23 운용자 결정). 죽은 원인을
모르는 채 다시 띄우면 같은 원인으로 다시 죽거나, 더 나쁘게는 반쯤 살아 주문을 낸다.
이 모듈이 하는 일은 '죽었다는 사실을 사람에게 알리는 것' 하나뿐이고, 되살릴지 말지는
사람이 상황을 보고 정한다.

[정상 종료와 사고사의 구분]
메뉴에서 종료하거나 SIGTERM 을 받으면 stopped() 로 '내가 스스로 내려간다'를 남긴다.
감시자는 그 표식이 있으면 침묵한다. 반대로 SIGKILL(OOM)·전원 차단·처리되지 않은
예외로 끝난 경우에는 마지막 도장이 그대로 남아 약속 시각을 넘기고, 그때 알림이 간다.
그래서 stopped() 는 '의도한 종료 경로'에서만 부른다 — atexit 로 걸면 크래시 종료까지
정상 종료로 덮어써서 정작 알려야 할 죽음을 숨긴다.
"""
import json
import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT_PATH = os.path.join(BASE_DIR, "logs", "heartbeat.json")
#  감시자가 '이미 알렸다'를 기억하는 자리. 살아 있는 프로세스가 아니라 감시자가 쓴다
#  (하트비트 파일에 같이 적으면 죽은 프로세스의 기록을 감시자가 덮어쓰게 된다).
ALERT_STATE_PATH = os.path.join(BASE_DIR, "logs", "heartbeat_alert.json")

#  약속 시각 = 지금 + 도장 주기 × 이 배수 + 여유. 한 번쯤 늦는 것(GC·디스크 지연·
#  파이의 순간 부하)으로는 울리지 않고, 정말 멈췄을 때만 넘어가도록 잡은 값이다.
DEADLINE_MULTIPLIER = 3
DEADLINE_SLACK_SEC = 60


def _atomic_write(path, payload):
    """같은 디렉토리에 임시 파일로 쓰고 rename — 감시자가 반쪽짜리 JSON 을 읽지 않게."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def beat(interval_sec=60, running=None, mode=None, instance=None, holdings=None, path=None):
    """살아 있다는 도장을 찍는다. 실패해도 절대 호출부를 깨뜨리지 않는다.

    interval_sec: 다음 도장까지의 예정 간격(초). 약속 시각이 여기서 나온다.
    running/mode/instance/holdings: 알림 본문에 쓸 상황 정보. 값을 만들어 내지 않고
      호출부가 넘겨준 것만 적는다(이 모듈이 config·session 을 모르게 두기 위해서다).
    """
    path = path or HEARTBEAT_PATH
    now = time.time()
    try:
        _atomic_write(path, {
            "state": "alive",
            "ts": now,
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "deadline": now + interval_sec * DEADLINE_MULTIPLIER + DEADLINE_SLACK_SEC,
            "interval_sec": interval_sec,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "mode": mode,
            "instance": instance,
            "auto_running": running,
            "holdings": holdings,
        })
    except Exception as e:
        logger.debug(f"[Heartbeat] 기록 실패(무시): {e}")


def stopped(reason="정상 종료", path=None):
    """의도한 종료임을 남긴다 — 감시자는 이 표식을 보면 알리지 않는다."""
    path = path or HEARTBEAT_PATH
    now = time.time()
    try:
        _atomic_write(path, {
            "state": "stopped",
            "ts": now,
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "reason": reason,
        })
    except Exception as e:
        logger.debug(f"[Heartbeat] 종료 표식 기록 실패(무시): {e}")


def read(path=None):
    path = path or HEARTBEAT_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"[Heartbeat] 읽기 실패: {e}")
        return None


def evaluate(now=None, path=None):
    """감시자의 판정. 네 가지 상태만 낸다.

    unknown — 하트비트 파일이 없거나 읽을 수 없다. **알리지 않는다.**
        한 번도 뜬 적 없는 기기에서 감시자를 켜면 매번 울릴 텐데, 그건 신호가 아니라
        소음이다. 도장을 한 번이라도 찍은 뒤부터가 감시 대상이다.
    stopped — 사람이/시스템이 의도적으로 내렸다. 알리지 않는다.
    ok      — 약속 시각 안이다.
    dead    — 약속 시각을 넘겼다. 알린다.
    """
    now = now if now is not None else time.time()
    data = read(path)
    if not data:
        return {"state": "unknown", "data": None, "age": None,
                "detail": "하트비트 기록이 없습니다(아직 한 번도 기동하지 않았거나 로그가 지워짐)."}

    if data.get("state") == "stopped":
        return {"state": "stopped", "data": data, "age": now - float(data.get("ts", now)),
                "detail": f"정상 종료 표식({data.get('reason', '')})."}

    ts = float(data.get("ts", 0) or 0)
    deadline = float(data.get("deadline", 0) or 0)
    age = now - ts
    if now <= deadline:
        return {"state": "ok", "data": data, "age": age,
                "detail": f"{int(age)}초 전 신호."}
    return {"state": "dead", "data": data, "age": age,
            "detail": f"마지막 신호 {data.get('iso', '?')} 이후 {int(age // 60)}분 {int(age % 60)}초 무응답 "
                      f"(약속 시각 {int(now - deadline)}초 초과)."}


# ---------------------------------------------------------------------------
# 감시자(외부 프로세스)용 — 알림만 한다
# ---------------------------------------------------------------------------

def _send_telegram(text):
    """텔레그램 직접 발신.

    api.send_telegram_message 를 쓰지 않는다 — 그 경로는 config·session·스레드풀을
    끌고 오는데, 감시자는 몇 분마다 도는 cron 프로세스이고 운영기는 램 1GB 다.
    자격증명은 살아 있는 프로세스와 같은 곳(~/.htsrc → 환경변수)에서 읽는다.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정"
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, ""
    except Exception as e:
        return False, str(e)


def _load_alert_state():
    try:
        with open(ALERT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alert_state(state):
    try:
        _atomic_write(ALERT_STATE_PATH, state)
    except Exception as e:
        logger.debug(f"[Heartbeat] 알림 상태 기록 실패(무시): {e}")


def check_and_notify(now=None, path=None, notify=True):
    """감시자 본체. 죽었으면 한 번 알리고, 되살아났으면 한 번 알린다.

    같은 사망 건으로 반복해서 울리지 않는다 — cron 이 5분마다 도는데 매번 보내면
    알림 자체가 소음이 되어 정작 다음 사고를 놓친다. 마지막으로 알린 하트비트의
    타임스탬프를 기억해 두고, 그 건에 대해서는 다시 보내지 않는다.

    복구 알림을 함께 보내는 이유: 죽음만 알리면 사람이 '아직도 죽어 있나'를 계속
    직접 확인해야 한다. 다시 도장이 찍히기 시작하면 그 사실도 한 번 알린다.

    반환: (상태 문자열, 알림 전송 여부)
    """
    result = evaluate(now=now, path=path)
    state = result["state"]
    alert = _load_alert_state()
    data = result.get("data") or {}
    sent = False

    if state == "dead":
        if alert.get("notified_ts") == data.get("ts"):
            return state, False           # 이미 알린 사망 건
        if notify:
            label = data.get("instance") or "MyStock HTS"
            lines = [
                "🔴 [시스템 중단 감지] 프로세스가 응답하지 않습니다",
                f"인스턴스: {label} (pid {data.get('pid')}, {data.get('host')})",
                f"마지막 신호: {data.get('iso', '?')}",
                result["detail"],
            ]
            if data.get("mode"):
                lines.append(f"모드: {data['mode']}")
            if data.get("auto_running"):
                lines.append("⚠️ 자동매매가 가동 중이었습니다 — 손절·트레일링 감시가 멈춰 있습니다.")
            if data.get("holdings"):
                lines.append(f"보유 종목 {data['holdings']}개 — 포지션이 무방비 상태입니다.")
            lines.append("")
            lines.append("자동으로 재기동하지 않습니다. 서버에 접속해 상태를 확인하세요.")
            ok, err = _send_telegram("\n".join(lines))
            sent = ok
            if not ok:
                logger.warning(f"[Heartbeat] 사망 알림 전송 실패: {err}")
        # 전송에 실패해도 기록은 남긴다 — 실패를 재시도로 덮으면 텔레그램이 살아난
        #  순간 밀린 알림이 한꺼번에 쏟아진다. 실패는 로그로 드러낸다.
        _save_alert_state({"notified_ts": data.get("ts"), "notified_at": time.time(),
                           "delivered": sent})
        return state, sent

    if state == "ok" and alert.get("notified_ts") and alert.get("notified_ts") != data.get("ts"):
        if notify:
            label = data.get("instance") or "MyStock HTS"
            ok, err = _send_telegram(
                f"🟢 [시스템 복구] 프로세스가 다시 신호를 보내고 있습니다\n"
                f"인스턴스: {label} (pid {data.get('pid')}, {data.get('host')})\n"
                f"최근 신호: {data.get('iso', '?')}"
            )
            sent = ok
            if not ok:
                logger.warning(f"[Heartbeat] 복구 알림 전송 실패: {err}")
        _save_alert_state({})
        return state, sent

    if state == "stopped":
        _save_alert_state({})

    return state, False
