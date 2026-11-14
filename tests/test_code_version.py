"""실행 중인 코드의 커밋 표시 (2026-11-14).

운영 기기는 pull 과 재기동이 따로 일어난다. '지금 도는 코드가 어느 커밋인가'가 기동 로그·하트비트·관제 화면에
없어 점검 때마다 간접 증거로 추정했다. 여기서 고정하는 것:
  · .git 을 직접 읽는다(가지 파일·packed-refs·분리된 HEAD·워크트리) — 못 읽으면 None, 지어내지 않는다.
  · 기동 로그 한 줄, 하트비트 파일, 관제 화면(/health)에 같은 값이 나온다.
  · 기동 뒤 저장소가 바뀌면 관제 화면이 '재기동해야 반영된다'고 주의로 올린다.
"""
import json
import logging
import os
from unittest.mock import patch

import config
from core import code_version
from modules import heartbeat
from modules.auto_trade import AutoTrader

A = "a" * 40
B = "b" * 40


def _repo(tmp_path, head, refs=None, packed=None):
    gd = tmp_path / ".git"
    gd.mkdir()
    (gd / "HEAD").write_text(head + "\n", encoding="utf-8")
    for ref, commit in (refs or {}).items():
        p = gd / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(commit + "\n", encoding="utf-8")
    if packed:
        (gd / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted\n" + "".join(
            f"{c} {r}\n" for r, c in packed.items()), encoding="utf-8")
    return str(tmp_path)


def test_가지_파일에서_커밋과_브랜치를_읽는다(tmp_path):
    root = _repo(tmp_path, "ref: refs/heads/main", refs={"refs/heads/main": A})
    assert code_version.read_head(root) == (A, "main")


def test_가지_파일이_없으면_packed_refs_에서_찾는다(tmp_path):
    root = _repo(tmp_path, "ref: refs/heads/main",
                 packed={"refs/heads/main": A, "refs/remotes/origin/main": B})
    assert code_version.read_head(root) == (A, "main")


def test_이름이_겹치는_다른_가지를_집지_않는다(tmp_path):
    """'refs/heads/main' 을 찾는데 'refs/heads/old-main' 줄을 집으면 안 된다."""
    root = _repo(tmp_path, "ref: refs/heads/main", packed={"refs/x/refs/heads/main": B, "refs/heads/main": A})
    assert code_version.read_head(root)[0] == A


def test_분리된_HEAD_는_브랜치가_없다(tmp_path):
    assert code_version.read_head(_repo(tmp_path, A)) == (A, None)


def test_워크트리의_gitdir_파일을_따라간다(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _repo(main, "ref: refs/heads/main", refs={"refs/heads/main": A, "refs/heads/wt": B})
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/wt\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert code_version.read_head(str(wt)) == (B, "wt")


def test_저장소가_아니면_모른다고_답한다(tmp_path):
    assert code_version.read_head(str(tmp_path)) == (None, None)
    with patch.object(code_version, "_STARTED", (None, None)):
        assert code_version.label() == "알 수 없음"
        assert code_version.drift() is None, "모르는 것을 '바뀌었다'고 말하면 안 된다"


def test_표시는_7자와_브랜치다():
    with patch.object(code_version, "_STARTED", (A, "main")):
        assert code_version.label() == "aaaaaaa (main)"
    with patch.object(code_version, "_STARTED", (A, None)):
        assert code_version.label() == "aaaaaaa"


def test_기동_뒤_저장소가_바뀌면_새_커밋을_알려준다():
    with patch.object(code_version, "_STARTED", (A, "main")):
        with patch.object(code_version, "read_head", return_value=(A, "main")):
            assert code_version.drift() is None
        with patch.object(code_version, "read_head", return_value=(B, "main")):
            assert code_version.drift() == "bbbbbbb"
        with patch.object(code_version, "read_head", return_value=(None, None)):
            assert code_version.drift() is None


def test_이_저장소의_커밋을_실제로_읽는다():
    commit, _ = code_version.read_head()
    if not os.path.exists(os.path.join(code_version.ROOT, ".git")):
        assert commit is None
    else:
        assert commit and len(commit) == 40 and set(commit) <= set("0123456789abcdef")


def test_기동_로그에_코드_커밋이_남는다(caplog):
    with patch.object(code_version, "_STARTED", (A, "main")), \
         caplog.at_level(logging.INFO, logger=config.STARTUP_LOGGER_NAME):
        config.log_system_start()
    assert any("코드 aaaaaaa (main)" in r.getMessage() for r in caplog.records)


def test_하트비트_파일에_커밋이_실린다(tmp_path):
    path = str(tmp_path / "hb.json")
    with patch.object(code_version, "_STARTED", (A, "main")):
        assert heartbeat.beat(path=path)
        assert json.load(open(path, encoding="utf-8"))["commit"] == "aaaaaaa"
        heartbeat.stopped(path=path)
        assert json.load(open(path, encoding="utf-8"))["commit"] == "aaaaaaa"
    with patch.object(code_version, "_STARTED", (None, None)):
        heartbeat.beat(path=path)
        assert json.load(open(path, encoding="utf-8"))["commit"] is None


def _health():
    AutoTrader._instance = None
    try:
        return AutoTrader().get_health_message()
    finally:
        AutoTrader._instance = None


def test_관제_화면에_코드가_나오고_바뀌면_재기동을_알린다():
    with patch.object(code_version, "_STARTED", (A, "main")):
        with patch.object(code_version, "read_head", return_value=(A, "main")):
            msg = _health()
        assert "• 코드: aaaaaaa (main)" in msg and "재기동해야" not in msg
        with patch.object(code_version, "read_head", return_value=(B, "main")):
            msg = _health()
        assert "실행 중 aaaaaaa (main) → 저장소 bbbbbbb" in msg and "재기동해야 새 코드가 반영됩니다" in msg
