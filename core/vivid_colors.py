"""ANSI 16색 → 256색 고정 매핑 — 터미널 테마가 색을 바꾸지 못하게 한다.

[왜] rich 마크업의 [red]·[blue] 같은 이름색은 실제 RGB가 아니라 **터미널 팔레트의 슬롯 번호**(SGR 31·34)다.
 cmux(Ghostty 코어)처럼 파스텔 테마를 얹은 터미널은 그 슬롯을 흐린 색으로 그려서, 같은 화면이 기기마다
 다르게 보인다(2026-09-15 실측: 왼쪽 cmux 파스텔 vs 오른쪽 기본 팔레트). 256색(SGR 38;5;n)과 트루컬러는
 테마가 손대지 않으므로 어느 터미널에서나 같은 색이 난다.
[어떻게] rich 가 색을 이스케이프로 바꾸는 단 한 곳(Color.get_ansi_codes)을 감싸, STANDARD(0~15) 색이면
 아래 표의 256색 인덱스로 내보낸다. 마크업·테마·호출부는 그대로다. 트루컬러가 아니라 256색인 이유:
 macOS Terminal.app 등은 트루컬러를 못 그리지만 256색은 사실상 모든 터미널·tmux·ssh 가 같게 그린다.
[끄기] 환경변수 HTS_VIVID_COLORS=0 (터미널 테마 색을 그대로 쓰고 싶을 때). 터미널이 16색만 지원하면
 (rich 판정 color_system == 'standard') 자동으로 건너뛴다.
"""
import os

from rich.color import Color, ColorType

# ANSI 슬롯 → xterm-256 인덱스. 어두운 배경 기준으로 '기본 팔레트'(오른쪽 화면)에 가깝게 골랐다.
#  0 black  1 red   2 green  3 yellow  4 blue   5 magenta  6 cyan   7 white
#  8 grey   9 b.red 10 b.grn 11 b.yel  12 b.blu 13 b.mag   14 b.cyn 15 b.white
VIVID_256 = {
    #  [2026-09-15 조정] 첫 매핑(196·40·220·45·171·203·83·227·207·87)은 "너무 쨍하다"는 피드백으로
    #   한 단계씩 채도를 내렸다. 여전히 테마 파스텔보다는 훨씬 선명하고, 슬롯 간 구분은 그대로다.
    0: 16,    # #000000
    1: 167,   # #d75f5f  red        (196 → 167 → 174 → 167, 174 는 너무 죽어 한 단계 복귀)
    2: 71,    # #5faf5f  green      (40 → 71 → 108 → 71)
    3: 179,   # #d7af5f  yellow     (220 → 179 → 180 → 179)
    4: 68,    # #5f87d7  blue       (was 69  #5f87ff)
    5: 133,   # #af5faf  magenta    (171 → 170 → 133, 아주 약간만)
    6: 74,    # #5fafd7  cyan       (was 45  #00d7ff)
    7: 250,   # #bcbcbc  white      ≈ CONSOLE_TEXT_COLOR(#c0c0c0)
    8: 244,   # #808080  grey
    9: 174,   # #d78787  bright red     (203 → 174 → 181 → 174)
    10: 114,  # #87d787  bright green   (83 → 114 → 151 → 114)
    11: 186,  # #d7d787  bright yellow  (227 → 186 → 187 → 186)
    12: 111,  # #87afff  bright blue    (was 75  #5fafff)
    13: 176,  # #d787d7  bright magenta (207 → 177 → 176)
    14: 116,  # #87d7d7  bright cyan    (was 87  #5fffff)
    15: 231,  # #ffffff  bright white
}

_installed = False
_orig_get_ansi_codes = None


def enabled():
    return os.environ.get("HTS_VIVID_COLORS", "1").strip().lower() not in ("0", "false", "no", "off")


def _terminal_supports_256():
    """rich 가 판정한 색 체계가 256색 이상인가. 콘솔이 아직 없거나 판정 불가면 True(종전처럼 시도)."""
    try:
        import config
        cs = getattr(config.console, "color_system", None)
        return cs in (None, "256", "truecolor", "eight_bit")
    except Exception:      # noqa: BLE001
        return True


def install():
    """Color.get_ansi_codes 를 한 번만 감싼다. 여러 번 불러도 안전하다."""
    global _installed, _orig_get_ansi_codes
    if _installed:
        return
    _orig_get_ansi_codes = Color.get_ansi_codes

    def get_ansi_codes(self, foreground=True):
        if (self.type == ColorType.STANDARD and self.number is not None
                and enabled() and _terminal_supports_256()):
            idx = VIVID_256.get(self.number)
            if idx is not None:
                return ("38" if foreground else "48", "5", str(idx))
        return _orig_get_ansi_codes(self, foreground)

    Color.get_ansi_codes = get_ansi_codes
    _installed = True


def uninstall():
    global _installed
    if _installed and _orig_get_ansi_codes is not None:
        Color.get_ansi_codes = _orig_get_ansi_codes
        _installed = False
