#!/usr/bin/env python3
"""프로세스 사망 감시자 — 죽었으면 텔레그램으로 알린다. 되살리지는 않는다.

[왜 밖에 있나]
프로그램 안의 감시는 프로그램이 살아 있을 때만 동작한다. 라즈베리파이 OOM 킬처럼
프로세스가 통째로 사라지는 실패에서는 알릴 주체가 함께 죽어 아무 일도 일어나지 않는다.
그래서 이 스크립트는 **본체와 별개의 프로세스**로, cron 이 주기적으로 띄운다.

[무엇을 하나]
logs/heartbeat.json 에 적힌 '다음 도장 약속 시각'이 지났는지만 본다. 지났으면 텔레그램
알림 한 번. 그게 전부다 — **자동 재기동은 하지 않는다**(2026-08-23 운용자 결정).
판정 로직은 modules/heartbeat.py 에 있고 이 파일은 그 얇은 사용자일 뿐이다.

[설치 — cron]
    crontab -e
    */5 * * * * . $HOME/.htsrc; /home/pi/my-stock-hts/tools/hts_watchdog.py >> /home/pi/my-stock-hts/logs/watchdog.log 2>&1

  `. $HOME/.htsrc` 가 앞에 붙는 이유: cron 은 로그인 셸 환경을 물려받지 않아
  TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 가 비어 있고, 그러면 감시자가 조용히 아무것도
  못 보낸다(run.sh 가 같은 이유로 ~/.htsrc 를 직접 읽는다).

  5분 주기면 최악의 경우 사망 후 약 5분 + 약속 여유(기본 4분) 안에 알림이 간다.
  더 촘촘히 두어도 되지만, 파이에서는 이 프로세스도 램을 쓴다는 점을 감안할 것.

[사용법]
    tools/hts_watchdog.py            # 판정 + 필요 시 알림 (cron 용)
    tools/hts_watchdog.py --status   # 판정만 출력, 알림 없음 (사람이 확인할 때)

[종료 코드]  0 = 정상/판정 불가,  1 = 사망 감지
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import heartbeat   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="MyStock HTS 프로세스 사망 감시 (알림 전용)")
    ap.add_argument("--status", action="store_true",
                    help="판정 결과만 출력하고 알림은 보내지 않는다")
    args = ap.parse_args()

    result = heartbeat.evaluate()
    data = result.get("data") or {}
    stamp = data.get("iso", "-")

    if args.status:
        print(f"[{result['state']}] {result['detail']}")
        if data:
            print(f"  마지막 기록: {stamp} · pid {data.get('pid')} · {data.get('host')} "
                  f"· 모드 {data.get('mode')} · 자동매매 {data.get('auto_running')}")
        return 1 if result["state"] == "dead" else 0

    state, sent = heartbeat.check_and_notify()
    # cron 로그에 남는 한 줄. 평소에는 ok 만 쌓이고, 사고 때 그 자리가 비어 있는 것
    #  자체가 단서가 된다(감시자까지 못 떴다는 뜻이므로).
    print(f"[{state}] {result['detail']}" + ("  → 텔레그램 알림 전송" if sent else ""))
    return 1 if state == "dead" else 0


if __name__ == "__main__":
    sys.exit(main())
