"""상태 JSON 의 내구성 — 원자적 저장과 손상 격리.

[배경] core/jsonio 를 타는 파일은 전부 '상태'다(제한 종목·일일 상태·수동 보유·
관심종목·설정). 종전 save_json 은 open(path,'w') 로 파일을 먼저 비우고 썼고,
load_json 은 파싱 실패를 로그 한 줄로 삼키고 빈 값을 돌려줬다. 운영기는 OOM 킬
이력이 있는 라즈베리파이라 '쓰다 만 파일'이 실제로 생길 수 있고, 그 뒤 저장이 한 번만
일어나면 빈 상태가 굳어 원본조차 사라진다(restricted_stocks.json 이 비면 자동매매가
수동 보유 종목을 자기 것으로 본다).
"""
import json
import os

import pytest

from core import jsonio


@pytest.fixture
def state_dir(tmp_path):
    """상태 파일 전용 디렉터리.

    tmp_path 를 그대로 쓰면 conftest 가 만든 테스트 DB 파일들이 섞여 들어와
    '디렉터리에 무엇이 남았는가'를 볼 수 없다.
    """
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_save_is_atomic_no_truncated_file_on_crash(state_dir, monkeypatch):
    """쓰는 도중 죽어도 원본은 '옛 내용' 그대로다 (반쪽 JSON 이 남지 않는다)."""
    path = str(state_dir / "restricted.json")
    jsonio.save_json(path, {"005930": {"reason": "수동보유"}})

    # 직렬화 도중 프로세스가 죽는 상황 — os.replace 전에 예외를 던진다.
    def _boom(*args, **kwargs):
        raise OSError("디스크 가득 참 / OOM")

    monkeypatch.setattr(jsonio.os, "replace", _boom)
    assert jsonio.save_json(path, {"000660": {"reason": "새 내용"}}) is False

    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"005930": {"reason": "수동보유"}}
    # 실패한 임시 파일을 남기지 않는다
    assert [p.name for p in state_dir.iterdir()] == ["restricted.json"]


def test_corrupt_file_is_quarantined_not_overwritten(state_dir):
    """깨진 파일은 지우지 않고 옆으로 치운다 — 다음 저장이 원본을 덮지 못한다."""
    path = str(state_dir / "daily_state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"buy_count": 3, "reali')      # 쓰다 만 JSON

    assert jsonio.load_json(path, default={}) == {}          # 빈 상태로 계속한다

    kept = [p for p in state_dir.iterdir() if ".corrupt." in p.name]
    assert len(kept) == 1, "손상본이 보존되어야 복구할 수 있다"
    assert kept[0].read_text(encoding="utf-8") == '{"buy_count": 3, "reali'
    assert not os.path.exists(path), "격리했으므로 원래 자리에는 남지 않는다"

    #  격리 뒤 저장이 일어나도 보존본은 그대로다(종전에는 이 저장이 원본을 덮어 끝이었다).
    jsonio.save_json(path, {})
    assert kept[0].read_text(encoding="utf-8") == '{"buy_count": 3, "reali'


def test_missing_file_is_quiet_and_io_error_does_not_quarantine(state_dir, monkeypatch):
    """'없음'은 정상이라 조용하고, 읽기 오류(권한·IO)에는 파일을 건드리지 않는다."""
    missing = str(state_dir / "never_written.json")
    assert jsonio.load_json(missing, default={"seed": 1}) == {"seed": 1}
    assert list(state_dir.iterdir()) == []

    path = str(state_dir / "stock.json")
    jsonio.save_json(path, {"stocks_kr": [{"code": "005930"}]})

    def _denied(*args, **kwargs):
        raise PermissionError("일시적 권한 오류")

    monkeypatch.setattr("builtins.open", _denied)
    assert jsonio.load_json(path, default=None) is None
    monkeypatch.undo()

    #  일시적 오류에 정상 파일을 치우면 그게 곧 데이터 손실이다.
    assert [p.name for p in state_dir.iterdir()] == ["stock.json"]
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"stocks_kr": [{"code": "005930"}]}


def test_roundtrip_preserves_korean_and_structure(state_dir):
    path = str(state_dir / "manual_positions.json")
    rows = [{"code": "005930", "name": "삼성전자", "qty": 10}]
    assert jsonio.save_json(path, rows) is True
    assert jsonio.load_json(path, default=[]) == rows
