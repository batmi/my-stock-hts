# jsonio.py
"""JSON 파일 로드/저장 공용 헬퍼.

내부 모듈 의존성이 전혀 없는 최하위 유틸리티로, config/session을 포함한
모든 계층에서 안전하게 import할 수 있다. (기존에 모듈마다 반복되던
open+json.load/dump+예외처리 보일러플레이트를 일원화)
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def load_json(path, default=None):
    """JSON 파일을 안전하게 로드한다.

    파일이 없거나 파싱에 실패하면 default를 반환한다. (실패는 로그로만 기록)
    """
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"JSON 로드 실패 ({path}): {e}")
    return default


def save_json(path, data, indent=4, ensure_ascii=False):
    """JSON 파일을 저장한다(UTF-8). 상위 디렉터리는 자동 생성.

    성공 여부(bool)를 반환하며, 실패는 로그로 기록한다.
    """
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        return True
    except Exception as e:
        logger.error(f"JSON 저장 실패 ({path}): {e}")
        return False
