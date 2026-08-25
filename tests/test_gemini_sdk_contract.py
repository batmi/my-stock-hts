"""Gemini 신 SDK(google-genai)와의 **계약** 고정.

[왜 따로 두는가 · 2026-08-25] 나머지 AI 테스트는 `theme_analysis._gemini_stream` 을 통째로
 patch 한다. 그건 의도된 이음매지만(SDK 객체 그래프를 흉내 내지 않기 위한 것), 대가로
 **SDK 경계 자체가 한 번도 실행되지 않는다.** 커버리지로 확인한 실제 상태는
 `_gemini_client` 9줄 중 8줄 미실행, `_gemini_stream` 4줄 중 3줄 미실행이었다.

 그 구멍으로 실제 버그가 하나 빠져나갔다. 차트 이미지 분석이 구 SDK 관행대로
 `{"mime_type": ..., "data": ...}` dict 를 contents 에 넣고 있었는데, 신 SDK 는 Part 만
 받는다 — 스위트 3,400건이 전부 초록인 채로 메뉴에서만 18건짜리 검증 오류로 죽었다.

 그래서 여기서는 **_gemini_stream 을 patch 하지 않는다.** 대신 그 아래 한 칸(_gemini_client)
 만 가짜로 바꿔, 요청 객체를 SDK 의 진짜 타입으로 조립하는 코드가 실제로 돌게 한다.
 마지막에는 SDK 가 전송 직전에 만드는 `_GenerateContentParameters` 로 통째로 검증한다 —
 그 검증을 통과하면 그 호출은 적어도 '형식 때문에' 죽지는 않는다.
"""
from unittest.mock import MagicMock, patch

import pytest

import config
from modules import theme_analysis

pytest.importorskip("google.genai")


@pytest.fixture(autouse=True)
def _sdk_ready():
    theme_analysis._ensure_genai()
    saved = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = "TEST_KEY"
    yield
    config.GEMINI_API_KEY = saved
    theme_analysis._GENAI_CLIENT = None
    theme_analysis._GENAI_CLIENT_KEY = None


class _Recorder:
    """generate_content_stream 호출 인자를 그대로 받아 두는 가짜 클라이언트."""

    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content_stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        chunk = MagicMock()
        chunk.text = "계약 확인용 응답"
        chunk.candidates = [MagicMock()]
        return iter([chunk])


def _run(fn, *args, **kwargs):
    """_gemini_client 만 가짜로 바꾸고 나머지 경로는 전부 진짜로 태운다."""
    rec = _Recorder()
    with patch('modules.theme_analysis._gemini_client', return_value=rec):
        out = fn(*args, **kwargs)
    return rec, out


def _validate_request(call):
    """SDK 가 전송 직전에 만드는 요청 객체로 검증한다(네트워크 없음)."""
    from google.genai import types
    return types._GenerateContentParameters(
        model=call["model"], contents=call["contents"], config=call["config"])


# ---------------------------------------------------------------------------
# 텍스트 리포트 — 가장 흔한 경로
# ---------------------------------------------------------------------------
def test_text_report_builds_a_valid_request():
    rec, out = _run(theme_analysis._run_gemini_report, "프롬프트 본문", label="계약")
    assert out == "계약 확인용 응답"
    assert len(rec.calls) == 1
    _validate_request(rec.calls[0])


def test_default_generation_config_is_accepted_by_the_sdk():
    """기본 생성 옵션(temperature·top_p·max_output_tokens)이 신 SDK 타입에 그대로 들어가는가.

    구 SDK 는 dict 를 그대로 받아 줬으므로 키 이름이 틀려도 조용히 무시됐다. 신 SDK 의
    GenerateContentConfig 는 **모르는 키를 거부**한다 — 즉 여기서 죽으면 실사용도 죽는다.
    """
    from google.genai import types
    rec, _ = _run(theme_analysis._run_gemini_report, "프롬프트", label="계약")
    cfg = rec.calls[0]["config"]
    assert isinstance(cfg, types.GenerateContentConfig)
    assert cfg.temperature == 0.2 and cfg.top_p == 0.95 and cfg.max_output_tokens == 8192
    # 자동 함수 호출은 꺼져 있어야 한다(켜져 있으면 호출마다 SDK 경고가 콘솔을 더럽힌다).
    assert cfg.automatic_function_calling.disable is True


def test_caller_supplied_generation_config_overrides_and_validates():
    rec, _ = _run(theme_analysis._run_gemini_report, "프롬프트",
                  label="계약", generation_config={"temperature": 0.0})
    assert rec.calls[0]["config"].temperature == 0.0
    _validate_request(rec.calls[0])


def test_unknown_generation_key_is_rejected_not_ignored():
    """계약이 실제로 살아 있다는 확인 — 오타 난 옵션이 조용히 무시되면 이 테스트는 무의미하다."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        theme_analysis._gemini_stream("프롬프트", config.GEMINI_MODEL, {"max_tokens": 10})


# ---------------------------------------------------------------------------
# 멀티모달 — 실제로 깨졌던 자리
# ---------------------------------------------------------------------------
def test_chart_image_request_is_valid(tmp_path):
    """차트 이미지 분석이 SDK 검증을 통과하는 요청을 만드는가.

    2026-08-25 이전에는 dict 를 넣어 `_GenerateContentParameters` 검증에서 18건 에러로
    죽었다. 아래 _validate_request 가 그 검증과 같은 것이다.
    """
    from google.genai import types
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    rec, out = _run(theme_analysis.analyze_chart_image_with_gemini,
                    str(img), "삼성전자", "005930", "6개월")
    assert out == "계약 확인용 응답"
    contents = rec.calls[0]["contents"]
    assert isinstance(contents[0], str)
    assert isinstance(contents[1], types.Part)
    assert contents[1].inline_data.mime_type == "image/png"
    _validate_request(rec.calls[0])


# ---------------------------------------------------------------------------
# 클라이언트 수명 — 키가 바뀌면 갈아타야 한다
# ---------------------------------------------------------------------------
def test_client_is_reused_and_rebuilt_when_the_key_changes():
    """구 SDK 의 genai.configure() 는 전역이었지만 신 SDK 는 클라이언트가 키를 들고 있다.

    매번 새로 만들면 커넥션 풀을 버리고, 캐시만 하고 키 변경을 놓치면 옛 키로 계속 쏜다.
    """
    theme_analysis._GENAI_CLIENT = None
    theme_analysis._GENAI_CLIENT_KEY = None
    first = theme_analysis._gemini_client()
    assert first is not None
    assert theme_analysis._gemini_client() is first, "같은 키인데 클라이언트를 새로 만든다"

    config.GEMINI_API_KEY = "ANOTHER_KEY"
    assert theme_analysis._gemini_client() is not first, "키가 바뀌었는데 옛 클라이언트를 쓴다"


def test_stream_passes_model_and_contents_through():
    """폴백 모델 재시도가 '어느 모델로 갔는가'를 이 자리에서 정한다."""
    rec = _Recorder()
    with patch('modules.theme_analysis._gemini_client', return_value=rec):
        list(theme_analysis._gemini_stream("본문", "gemini-test-model", {"temperature": 0.1}))
    assert rec.calls[0]["model"] == "gemini-test-model"
    assert rec.calls[0]["contents"] == "본문"
