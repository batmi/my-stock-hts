"""ANSI 이름색을 256색으로 고정해 터미널 테마가 색을 바꾸지 못하게 한다(core/vivid_colors)."""
import io
from unittest.mock import patch

from rich.console import Console

import config  # noqa: F401 - import 시 vivid_colors.install() 이 붙는다
from core import vivid_colors


def _render(markup, color_system="256"):
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system=color_system, width=80).print(markup, end="")
    return buf.getvalue()


def test_named_colors_become_256_codes():
    out = _render("[red]r[/] [bold blue]b[/] [green]g[/] [bright_red]br[/]")
    assert "\x1b[38;5;167m" in out          # red
    assert "\x1b[1;38;5;68m" in out         # bold blue — 조합 스타일도 같은 자리를 지난다
    assert "\x1b[38;5;71m" in out           # green
    assert "\x1b[38;5;174m" in out          # bright_red
    assert "\x1b[31m" not in out and "\x1b[34m" not in out, "팔레트 슬롯 코드가 남으면 테마가 색을 바꾼다"


def test_background_and_hex_untouched():
    out = _render("[on red]x[/] [#ff0000]h[/]")
    assert "\x1b[48;5;167m" in out          # 배경도 같은 표
    assert "\x1b[38;5;196m" in out          # hex 는 rich 가 256 으로 내리고 우리는 손대지 않는다


def test_env_switch_restores_palette_codes(monkeypatch):
    monkeypatch.setenv("HTS_VIVID_COLORS", "0")
    out = _render("[red]r[/]")
    assert "\x1b[31m" in out


def test_skips_when_terminal_is_16_color_only():
    with patch.object(vivid_colors, "_terminal_supports_256", lambda: False):
        out = _render("[red]r[/]")
    assert "\x1b[31m" in out


def test_all_16_slots_mapped():
    assert sorted(vivid_colors.VIVID_256) == list(range(16))
    assert all(0 <= v <= 255 for v in vivid_colors.VIVID_256.values())
