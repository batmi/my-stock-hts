"""README 파일 트리는 실제 저장소와 같아야 하고, 한·영 README 는 같은 트리를 가져야 한다.

[왜 · 2026-09-20] 문서 표류 감사에서 트리에 `modules/krx_openapi.py`(국내 일봉 정본)·
`modules/manage/scan.py`·`core/vivid_colors.py` 가 빠져 있었고, 본문 세 곳은 09-18 에 폐기된
"pykrx 1순위" 순서를 여전히 쓰고 있었다. 트리는 기계적으로 대조할 수 있으니 여기서 지킨다
(본문 서술의 표류는 잡지 못한다 — 그건 감사가 맡는다). README 는 한·영 동시 반영이 규약이라
([[readme-bilingual-sync]]) 두 파일의 트리 항목 집합이 같아야 한다.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READMES = ("README.md", "README.en.md")
# 트리에 '디렉토리 한 줄'로만 적고 하위 파일을 열거하지 않는 곳 — 열거를 강제하지 않는다.
UNLISTED_DIRS = {"api/quotes", "tests", "tools", "data", "db", "json", "logs", "chart"}
# 트리 대조 대상 소스 디렉토리(실제 .py 가 빠지면 잡는다).
SOURCE_DIRS = ("modules", "api", "core", "brokers")


def _tree_entries(readme):
    """트리 그림의 각 줄을 저장소 상대 경로로 푼다(들여쓰기 4칸 = 깊이 1)."""
    entries = []
    path = []
    for line in open(os.path.join(ROOT, readme), encoding="utf-8").read().splitlines():
        m = re.match(r"^([│ ]*)[├└]── ([^\s#]+)", line)
        if not m:
            continue
        depth = len(m.group(1)) // 4
        name = m.group(2).rstrip("/")
        path = path[:depth] + [name]
        entries.append("/".join(path))
    return entries


@pytest.mark.parametrize("readme", READMES)
def test_every_tree_entry_exists(readme):
    entries = _tree_entries(readme)
    assert len(entries) > 50, "트리를 못 읽었다 — 그림 형식이 바뀌었으면 정규식을 고쳐라"
    ghosts = [e for e in entries
              if "{" not in e and "*" not in e and not os.path.exists(os.path.join(ROOT, e))]
    assert not ghosts, f"{readme} 트리에 없는 파일이 적혀 있다: {ghosts}"


@pytest.mark.parametrize("readme", READMES)
def test_every_source_module_is_in_the_tree(readme):
    listed = set(_tree_entries(readme))
    missing = []
    for base in SOURCE_DIRS:
        for dirpath, _, files in os.walk(os.path.join(ROOT, base)):
            rel_dir = os.path.relpath(dirpath, ROOT)
            if "__pycache__" in rel_dir or rel_dir in UNLISTED_DIRS:
                continue
            for fn in files:
                if not fn.endswith(".py") or fn == "__init__.py":
                    continue
                rel = f"{rel_dir}/{fn}"
                if rel not in listed:
                    missing.append(rel)
    assert not missing, f"{readme} 트리에 빠진 모듈 — 한 줄 설명과 함께 넣어라(한·영 둘 다): {missing}"


def test_korean_and_english_trees_list_the_same_paths():
    ko, en = (set(_tree_entries(r)) for r in READMES)
    assert ko == en, f"한·영 README 트리가 다르다 — 한글에만: {sorted(ko - en)} / 영문에만: {sorted(en - ko)}"


def test_the_parser_actually_reads_the_tree():
    entries = _tree_entries("README.md")
    assert "modules/krx_openapi.py" in entries
    assert "core/vivid_colors.py" in entries
