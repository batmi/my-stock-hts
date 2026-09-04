# modules/manage/scan.py
"""병렬 종목 조회의 **실패를 세어 화면에 밝히는** 공용 수집기.

[왜 있나 · 2026-09-04] 공시(disclosure)·수급/오버행(insider)·배당실적 캘린더(events)
 세 화면이 모두 `ThreadPoolExecutor` + `as_completed` 로 종목을 병렬 조회하면서 워커
 예외를 `except Exception: pass` 로 버렸다. DART 레이트리밋이나 네트워크 순단으로 절반이
 실패해도 표는 멀쩡해 보이고, 운영자는 그것을 **'해당 없음'으로 읽는다**.

 방향이 나쁘다. 오버행(잠재 매도물량)과 자기주식 취득은 '없다'로 읽는 순간 판단이 반대로
 간다 — 같은 파일의 `rem_estimated` 가 이미 '모르면 위험 쪽'을 택하고 있는데, 그보다
 앞단인 조회 실패는 통째로 조용했다. **조회 실패는 데이터 없음이 아니다.**
 (DB 조회 실패를 같은 이유로 드러낸 이력: modules/db_manager.py 상단 주석)

여기서 예외를 다시 던지지는 않는다 — 일부만이라도 보여 주는 편이 낫다. 다만 몇 종목을
못 봤는지는 반드시 말한다.
"""
import logging

import config

logger = logging.getLogger(__name__)


class ScanFailures:
    """조회 실패 종목 수집기. `record()` 로 모으고 `announce()` 로 화면에 밝힌다."""

    #  화면에 이름을 늘어놓을 최대 개수. 넘으면 '외 N개'로 접는다(표 폭 상한 때문).
    NAME_LIMIT = 5

    def __init__(self, what="조회"):
        self.what = what
        self.failed = {}      # code -> 마지막 오류 문자열

    def __len__(self):
        return len(self.failed)

    def __bool__(self):
        return bool(self.failed)

    def record(self, code, err):
        """종목 하나의 조회 실패를 기록한다. 예외는 로그로 남긴다(원인 추적용)."""
        key = str(code or "?")
        self.failed[key] = str(err)
        logger.debug(f"[{self.what}] {key} 조회 실패: {err}")

    def note(self):
        """화면에 찍을 한 줄. 실패가 없으면 None."""
        if not self.failed:
            return None
        names = list(self.failed)
        head = ", ".join(names[:self.NAME_LIMIT])
        more = f" 외 {len(names) - self.NAME_LIMIT}개" if len(names) > self.NAME_LIMIT else ""
        return (f"⚠️ {len(names)}개 종목을 조회하지 못했습니다({head}{more}). "
                f"아래 결과에는 그 종목이 빠져 있습니다 — '해당 없음'이 아닙니다.")

    def announce(self):
        """실패가 있으면 화면에 밝힌다. 밝혔으면 True."""
        line = self.note()
        if not line:
            return False
        config.console.print(f"[yellow]{line}[/yellow]\n")
        return True

    def telegram_note(self):
        """텔레그램 본문에 덧붙일 한 줄(색 태그 없음). 실패가 없으면 None."""
        line = self.note()
        return f"※ {line[2:].strip()}" if line else None
