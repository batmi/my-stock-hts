# jsonio.py
"""JSON 파일 로드/저장 공용 헬퍼.

내부 모듈 의존성이 전혀 없는 최하위 유틸리티로, config/session을 포함한
모든 계층에서 안전하게 import할 수 있다. (기존에 모듈마다 반복되던
open+json.load/dump+예외처리 보일러플레이트를 일원화)

[왜 원자적 쓰기인가 · 2026-09-04]
 이 헬퍼를 타는 파일은 전부 '상태'다 — 제한 종목(restricted_stocks.json), 일일 상태
 (daily_state.json: 일일 손실 한도·당일 매수 이력), 수동 보유(manual_positions.json),
 관심종목(stock.json), 설정(dynamic_config.json). 종전 구현은 open(path,'w') 로
 **파일을 먼저 비우고** 썼다. 운영기는 램 1GB 라즈베리파이이고 OOM 킬 이력이 있다
 (modules/heartbeat.py 주석 참조). 쓰는 도중 프로세스가 사라지면 반쪽짜리 JSON 이 남는다.
 같은 디렉터리의 임시 파일에 쓰고 fsync 한 뒤 os.replace 로 갈아 끼우면, 중간에 죽어도
 파일은 '옛 내용' 아니면 '새 내용'이지 그 사이가 없다. 하트비트는 이미 이 방식인데
 정작 매매 상태 파일들이 아니었다.

[왜 손상을 격리하는가]
 종전에는 파싱 실패도 로그 한 줄 남기고 default(빈 딕셔너리/리스트)를 돌려줬다. 방향이
 fail-open 이라 나쁘다 — restricted_stocks.json 이 깨지면 제한 목록이 조용히 비고,
 자동매매가 수동 보유 종목을 자기 것으로 본다. 게다가 그 뒤 저장이 한 번만 일어나면
 빈 상태가 파일에 굳어 **복구할 원본조차 사라진다**.
 그래서 내용이 깨진 파일은 지우지 않고 `<파일>.corrupt.<타임스탬프>` 로 치워 둔다.
 (a) 원본이 보존되고, (b) 다음 저장이 그것을 덮지 못하며, (c) 로그·화면에 남는다.

[왜 fail-closed 가 아닌가]
 '제한 목록을 못 읽으면 전 종목을 제한으로 본다'는 반대편 fail-closed 는 더 위험하다.
 제한 종목은 자동매매가 손대지 않는 종목이라, 전부 제한이 되면 **보유 포지션의 손절과
 트레일링 청산까지 멈춘다**. 읽기 실패의 대가로 포지션을 무방비로 두는 것이므로,
 여기서는 '빈 상태로 계속하되 시끄럽게 알리고 원본을 지킨다'를 택한다.
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

#  내용이 깨진 파일(파싱 불가)만 격리한다. 권한·IO 오류는 파일 자체가 멀쩡할 수 있어
#  건드리지 않는다 — 일시적 오류에 정상 파일을 치우면 그게 곧 데이터 손실이다.
_CORRUPT_ERRORS = (json.JSONDecodeError, UnicodeDecodeError, ValueError)


def _notify_corrupt(path, kept, err):
    """손상 사실을 로그와 화면 양쪽에 남긴다.

    config 를 최상단에서 import 하지 않는다 — 이 모듈은 config 보다 아래층이다
    (config 가 이것을 쓴다). 호출 시점에 늦게 물어보고, 없으면 로그로만 끝낸다.
    """
    where = f" (원본 보존: {os.path.basename(kept)})" if kept else " (원본 보존 실패)"
    logger.error(f"JSON 손상 감지 ({path}): {err}{where}")
    try:
        import config
        config.console.print(
            f"[bold red]⚠️ 상태 파일이 손상되었습니다: {os.path.basename(path)}[/bold red]\n"
            f"[dim]   비어 있는 상태로 계속 진행합니다{where}. 내용을 확인하고 되돌리세요.[/dim]")
    except Exception:      # noqa: BLE001 - 화면 통지는 부가 기능이다
        pass


def _quarantine(path):
    """손상된 파일을 지우지 않고 옆으로 치운다. 보존 경로(또는 None)를 돌려준다."""
    dest = f"{path}.corrupt.{time.strftime('%Y%m%d_%H%M%S')}"
    try:
        os.replace(path, dest)
        return dest
    except Exception as e:      # noqa: BLE001
        logger.error(f"JSON 손상본 보존 실패 ({path}): {e}")
        return None


def load_json(path, default=None):
    """JSON 파일을 안전하게 로드한다.

    파일이 없으면 조용히 default 를 돌려준다(아직 만들어지지 않은 상태 파일은 정상이다).
    내용이 깨져 있으면 원본을 `.corrupt.<타임스탬프>` 로 치우고, 로그·화면에 남긴 뒤
    default 를 돌려준다 — '없음'과 '깨짐'을 구분하지 못하던 자리를 갈랐다.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except _CORRUPT_ERRORS as e:
        _notify_corrupt(path, _quarantine(path), e)
    except Exception as e:      # noqa: BLE001 - 권한·IO 오류: 파일은 건드리지 않는다
        logger.error(f"JSON 로드 실패 ({path}): {e}")
    return default


def save_json(path, data, indent=4, ensure_ascii=False):
    """JSON 파일을 원자적으로 저장한다(UTF-8). 상위 디렉터리는 자동 생성.

    같은 디렉터리의 임시 파일에 쓰고 fsync 한 뒤 os.replace 로 갈아 끼운다
    (다른 디렉터리의 임시 파일은 파일시스템이 다르면 rename 이 원자적이지 않다).
    임시 파일 이름에 PID 를 넣어, 같은 경로를 두 프로세스가 저장해도 서로의 반쪽을
    집어 가지 않게 한다. 성공 여부(bool)를 돌려주며, 실패는 로그로 기록한다.
    """
    parent = os.path.dirname(path)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"JSON 저장 실패 ({path}): {e}")
        #  반쪽짜리 임시 파일을 남기지 않는다 — 다음 저장이 덮어쓰긴 하지만,
        #  실패가 쌓이면 디렉터리에 쓰레기가 남는다.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:      # noqa: BLE001
            pass
        return False
